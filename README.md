# polyCopyCat

一个 Polymarket 跟单系统：盯住你选定的交易员地址，他们开仓、平仓时，按你设定的规则自动跟单。

完整工作流：**`scout` 找人 → `run`（纸面）验证 → `report` 复盘 → 确认后再上实盘**。

## 想法

Polymarket 的成交都在链上（Polygon），任何地址的持仓和历史成交都是公开的。找到几个长期赚钱的地址，跟着他们下单，就是这个项目做的事：

- 发现值得跟的地址（战绩回放 + 做市/套利地址识别）
- 监控目标地址成交（轮询 + WebSocket 实时推送双通道）
- 按固定金额或比例复制买入，按持仓比例跟随卖出
- 风控：单笔/单市场/总敞口上限、当日亏损熔断、滑点保护、黑名单、一键停机
- 纸面模式先验证（真实盘口模拟成交），实盘走官方 CLOB 客户端

## 安装

需要 Python ≥ 3.10。

```bash
pip install -e .            # 基础：监控 + scout + 纸面跟单
pip install -e ".[dev]"     # 开发：加 pytest
pip install "polycopycat[live]"  # 实盘：加 py-clob-client
```

## 三步上手

```bash
# 1. 找人：评估活跃地址，生成配置片段
polycopycat scout --targets-snippet

# 2. 纸面跟单：把片段并入配置，跑起来观察一两周
cp config.example.json copycat.json   # 填 targets、调限额
polycopycat run --config copycat.json

# 3. 复盘：随时看持仓 / 盈亏 / 每一笔决策
polycopycat report --config copycat.json
```

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `trades <地址>` | 一次性读取某地址最近成交（新→旧） |
| `watch <地址...>` | 持续监控多个地址的新成交（轮询或 `--stream` 实时） |
| `scout [地址...]` | 寻找值得跟单的地址：回放战绩、排除做市/亏损地址 |
| `arb-scan` | 扫描互补对套利机会（只读研究工具，不下单） |
| `xarb-scan` | Kalshi × Polymarket 跨所套利：提名配对 / 算价差（只读） |
| `run --config <文件>` | 启动跟单引擎（纸面/实盘由配置决定） |
| `report` | 查看账本：信号统计、持仓、盈亏、最近订单 |

所有子命令都支持 `--json`（JSON lines 输出接下游）、`--base-url`（替换 Data API 入口）和 `-v` 调试日志（放在子命令前后均可）；`--version` 看版本。地址一律用 Polymarket 个人主页 URL 里的 0x 地址（proxy wallet）。

### trades / watch —— 读取与监控

```bash
polycopycat trades 0x目标地址 --limit 20          # --include-maker 连挂单侧一起看
polycopycat watch 0x地址A 0x地址B --interval 10   # 轮询模式
polycopycat watch 0x地址A --stream --backfill 5   # 实时推送 + 启动回放最近 5 条
```

- 首次轮询只建立基线，不会把历史刷成"新成交"
- `--stream` 下轮询自动降级为兜底对账（默认 60s）；断线重连后立刻补一轮，与实时通道共用一套去重，不会重复上报

### scout —— 找跟单对象

```bash
polycopycat scout                          # 默认从全站最近成交挖活跃地址
polycopycat scout --from-leaderboard       # 叠加官方排行榜作为候选来源
polycopycat scout 0x地址A 0x地址B          # 直接评估指定地址
polycopycat scout --targets-snippet --top 5
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--candidates` | 40 | 最多评估多少个候选 |
| `--pages` | 1 | 每个地址回放几页成交带（每页 500 笔） |
| `--min-trades` / `--min-notional` | 20 / 2000 | 样本与成交额下限 |
| `--min-win-rate` | 0.5 | 胜率下限（主要看胜率时调高） |
| `--window` | 30d | 排行榜统计窗口（1d/7d/30d/all） |
| `--top` | 5 | `--targets-snippet` 输出前几名 |

评估方法：用与账本一致的均价法**回放**每个候选的公开成交带，得出已实现盈亏、胜率、持仓时长、市场广度，然后**先排除后打分**：

- **排除**（宁严勿松）：样本不足、成交额太小、多日不活跃、回放亏损、胜率过低、频率过高（疑似机器人）、**大样本下胜率高得离谱（疑似结构性套利）**、**持仓浮亏占成本过高（疑似囤死仓/只认盈不认亏）**，以及最关键的——**快进快出占比过高（疑似做市/套利）**。这类地址账面常年"盈利"、胜率漂亮，但赚的是价差返佣，方向毫无参考价值，跟单基本必亏
- **打分**（只在合格者间排序）：盈利 40 + 胜率 25 + 市场广度 15 + 活跃度 10 + 单笔规模 10，公式刻意简单透明
- 窗口外建的仓（只见卖不见买）盈亏未知，不掺进胜率——只对能自证的部分下结论

### arb-scan —— 套利扫描（只读）

二元市场的 Yes+No 恰有一边赎回 $1，两边价格之和偏离 $1 就是套利空间——排行榜上那些"胜率 100%"的地址赚的就是这个。`arb-scan` 扫一帧快照，量化现在还剩多少这种机会：

```bash
polycopycat arb-scan --max-markets 500 --min-edge 0.005 --min-profit 0.5
```

- **买对冲**：`ask(Yes)+ask(No) < $1`，两边买入锁定利润
- **铸造卖出**：`bid(Yes)+bid(No) > $1`，铸一对卖掉（需链上 split，仅提示）
- 市场目录来自 Gamma API（按 24h 成交量取前 N 个活跃市场）

诚实定位：这是速度游戏，肉眼可见的价差通常几百毫秒内被专业机器人吃掉；本工具只读不下单，用于研究"值不值得做执行版"，不承诺你能抢到。

**实测结论（2026-07，CI 真实扫描）**：前 100 与前 1000 活跃市场两轮扫描、利润门槛压到 $0.1，机会数均为 **0**——互补对套利在人肉时间尺度上已被专业机器人吃尽，执行版没有价值。本工具保留用作市场效率探针。

### xarb-scan —— Kalshi × Polymarket 跨所套利（只读）

同一个现实事件在两个所都有市场时，「Poly 买 Yes + Kalshi 买 No」构成完全对冲，两腿成本（含 Kalshi 手续费 `0.07×P×(1−P)`）之和 < $1 即锁定利润。跨所价差因开户/KYC/资金分踞的摩擦**无法被机器人瞬间抹平**，比站内套利更可能存在。

```bash
polycopycat xarb-scan --suggest                 # 自动提名两所相似市场（只提名不采信）
polycopycat xarb-scan --pairs xarb-pairs.json   # 对人工确认过的配对算精确价差
```

**核心风险不是价格是配对**：两边"看起来一样"的市场，结算条款可能有细微差异（数据源/截止时间/措辞），配错对不是套利而是双边敞口，可能两腿全亏。所以自动匹配只输出候选（标题相似度 + 数字 token 严格一致 + 截止时间接近），**必须人工核对两边结算规则文本**后写进配对文件（格式见 `xarb-pairs.example.json`）才参与价差计算。执行侧还有单腿风险、两所资金划转与各自的合规要求（Kalshi/Polymarket 的地区与 KYC 规则），自担。

### run / report —— 跟单引擎

```bash
polycopycat run --config copycat.json            # mode 由配置决定，默认 paper
polycopycat run --config copycat.json --paper    # 保险丝：强制纸面
polycopycat report --config copycat.json         # 或 report --ledger data/copycat.sqlite3
```

引擎管道（每个环节的结论都落账本，可追溯）：

```
信号（watch/stream 双通道去重）→ 过滤 → 仓位计算 → 风控闸门 → 执行器 → sqlite 账本 → 通知
                                                                └ 对账线程：目标持仓镜像 / 实盘持仓同步 / 可赎回提醒
```

## 配置参考（copycat.json）

完整示例见 [config.example.json](config.example.json)。所有金额单位 USDC，价格量纲 0~1。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `paper` | `paper` 纸面模拟 / `live` 实盘 |
| `ledger_path` | `data/copycat.sqlite3` | 账本位置（`data/` 已在 .gitignore） |
| `reconcile_interval_s` | 300 | 对账周期：刷新目标镜像、实盘同步持仓 |
| `data_api_url` / `ws_url` / `clob_url` | 官方入口 | 走代理或本地测试时覆盖 |
| **targets[]** |  | 跟单目标，至少一个 |
| `.address` | 必填 | 目标 proxy wallet 地址 |
| `.ratio` / `.max_per_trade_usdc` | 继承 sizing | 按目标覆盖比例/单笔上限 |
| `.paused` | false | 暂停跟单（仍监控、仍维护镜像） |
| **sizing** |  | 买入金额怎么算 |
| `.mode` | `proportional` | `proportional`：目标金额×ratio；`fixed`：固定 fixed_usdc |
| `.ratio` / `.fixed_usdc` / `.max_per_trade_usdc` | 0.1 / 20 / 100 | 比例、固定额、单笔上限 |
| **filters** |  | 信号级过滤 |
| `.min_target_notional_usdc` | 10 | 尘埃单阈值：目标成交金额低于此不跟 |
| `.max_signal_age_s` | 30 | 信号超龄不跟（价格早走了） |
| `.follow_sells` | true | 是否跟随卖出 |
| **risk** |  | 上限设为 null 表示不启用该项 |
| `.max_market_exposure_usdc` | 200 | 单市场持仓成本上限 |
| `.max_total_exposure_usdc` | 1000 | 总持仓成本上限 |
| `.daily_max_loss_usdc` | 100 | 当日已实现亏损熔断（只拦开新仓，减仓放行） |
| `.market_blacklist` | [] | condition id 或 slug |
| `.kill_switch_file` | `STOP` | 该文件存在时全面停止开新仓 |
| **execution** |  |  |
| `.slippage_cap` | 0.02 | 限价 = 目标成交价 ± 此值，FAK 绝不追价 |
| **watch** |  | 信号源 |
| `.stream` | true | 实时推送 + 轮询兜底 |
| `.poll_interval` | null | null = 自动（stream 模式 60s，纯轮询 10s） |
| `.backfill` | 0 | 启动时回放最近 N 条 |
| **notify** |  | 不配 Telegram 就只打日志 |
| `.telegram_bot_token_env` | "" | 存 bot token 的**环境变量名**（不是 token 本身） |
| `.telegram_chat_id` | "" | 接收通知的 chat |
| **live** |  | 实盘参数，paper 模式忽略 |
| `.private_key_env` | `POLYCOPYCAT_PRIVATE_KEY` | 私钥所在环境变量名 |
| `.funder` | "" | 资金地址（proxy wallet）；EOA 直连留空 |
| `.signature_type` | 2 | 0=EOA，1=邮箱钱包代理，2=浏览器钱包代理 |
| `.i_understand_live_trading_risk` | false | 必须显式改 true 才允许实盘启动 |

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `POLYCOPYCAT_DATA_API_URL` | 覆盖 Data API 入口 |
| `POLYCOPYCAT_WS_URL` | 覆盖实时推送入口 |
| `POLYCOPYCAT_CLOB_URL` | 覆盖 CLOB 入口 |
| `POLYCOPYCAT_LB_URL` | 覆盖排行榜入口 |
| `POLYCOPYCAT_GAMMA_URL` | 覆盖 Gamma 市场目录入口（arb-scan 用） |
| `POLYCOPYCAT_KALSHI_URL` | 覆盖 Kalshi 接口入口（xarb-scan 用） |
| `POLYCOPYCAT_PRIVATE_KEY` | 实盘私钥（变量名可在 `live.private_key_env` 改；绝不落盘、绝不进日志） |
| 自定义名 | Telegram bot token，变量名由 `notify.telegram_bot_token_env` 指定 |

## 数据来源

| 接口 | 默认入口 | 用在哪 | 状态 |
| --- | --- | --- | --- |
| Data API | `data-api.polymarket.com` | 成交带、持仓（trades/watch/scout/镜像/对账） | 公开，无需鉴权 |
| 实时数据流 | `wss://ws-live-data.polymarket.com` | 实时成交推送（官网活动流同款） | 公开；协议按公开资料实现 |
| CLOB | `clob.polymarket.com` | 市场元数据、订单簿（只读）；实盘下单（L1/L2 鉴权） | 官方 |
| 排行榜 | `lb-api.polymarket.com` | scout 候选来源 | **非正式文档**，不可用时自动跳过 |
| Gamma | `gamma-api.polymarket.com` | arb-scan 市场目录（活跃市场按成交量过滤） | 官方目录接口 |
| Kalshi | `api.elections.kalshi.com/trade-api/v2` | xarb-scan 跨所行情（市场列表/订单簿，公开只读） | 官方，无需鉴权 |

## 时延：轮询 vs 实时推送

| 模式 | 新成交被发现的时延 | 原理 |
| --- | --- | --- |
| 轮询 | 平均约「轮询间隔的一半 + Data API 索引延迟」，`--interval 10` 时约 5~12 秒 | 定期拉 `GET /trades` 增量比对 |
| `--stream` | 通常 1 秒内 | 订阅实时数据流，成交即推；轮询降级为兜底对账 |

轮询想更快可以把 `--interval` 压到 2 秒左右，代价是请求量（每地址每轮 1 个请求），太激进可能触发限流。

## 跟单规则与已知边界

**买入**：跟随金额 = min(目标金额 × ratio, 单笔上限)（或固定金额模式）；限价 = 目标成交价 + slippage_cap，按市场 tick 取整，FAK 只吃限价内的盘口。计划量低于市场最小下单量（一般 5 份）会跳过并记入账本——**跟小单风格的目标时把 ratio 调高（如 1.0）或改用 fixed 模式**，否则大部分信号会被这条规则丢掉；`report` 里 `skipped` 占比过高就是这个信号。

**卖出**：目标卖掉他持仓的 x%，就卖掉自己持仓的 x%。目标持仓镜像 = 启动/定期 `/positions` 快照 + 每笔成交实时累加（无论该笔跟没跟都更新），merge/redeem 等不出现在成交流里的变化由对账兜住；镜像没记录时按全平保守离场；余量将低于最小下单量时直接全平，避免死仓。

**纸面模式**（默认）：执行器拉实时订单簿逐档模拟 FAK 成交，滑点按真实盘口算，账本记的就是「如果真跟会怎样」。注意纸面结果略偏乐观（没有排队竞争、你的单不推动价格）：纸面亏的实盘一定亏，纸面小赚的未必赚。

**实盘边界**：

- 下单响应不含逐档成交明细，持仓以定期对账为准，当日亏损熔断在实盘是近似值——务必同时设好敞口上限
- 市场结算后引擎提醒「可赎回」，但不自动发链上 redeem 交易，去 Polymarket 页面点一下
- 短窗口内的分批建仓暂不聚合，每笔独立跟单
- EOA 首次交易需要先对交易所合约做 USDC/CTF 授权并备少量 POL；资金是 Polygon 上的 USDC.e

## 账本（sqlite）

三张表，`report` 读的就是它们：

- **signals**：每笔目标成交一行，`trade_key` 唯一约束提供重启幂等；`status` 记录归宿：`executed` / `filtered`（没过过滤）/ `skipped`（不值得下）/ `risk_blocked`（风控拦截）/ `no_fill`（限价内无对手盘）/ `error`
- **orders**：每次执行一行：限价、请求量、成交量、均价、滑点、该单已实现盈亏
- **positions**：当前持仓（份额、均价、累计已实现盈亏）；纸面由成交直接记账，实盘由对账快照覆盖

`report` 输出示例：

```
信号统计: executed=2, filtered=1, risk_blocked=2
已实现盈亏: 累计 $-0.52，今日 $-0.52

## 当前持仓（1 个）
       26.22 份 @ 0.514  成本 $   13.47  已实现 $   -0.52  [Yes] Will X happen by June 30?

## 最近订单（最多 20 条）
  #2 paper SELL 21.85@≤0.480 → filled 成交 21.85@0.490 滑点 +0.010 pnl -0.52  [Yes] ...
  #1 paper BUY 48.07@≤0.520 → filled 成交 48.07@0.514 滑点 +0.014 pnl +0.00  [Yes] ...
```

## 在代码里使用

```python
from polycopycat import DataApiClient, TradeStream, TradeWatcher

client = DataApiClient()

# 读成交 / 持仓
for t in client.get_trades("0x目标地址", limit=10):
    print(t.time_utc, t.side, t.size, t.price, t.title)
positions = client.get_positions("0x目标地址")

# 监控：轮询 + 实时推送，共用一套去重；跟单逻辑挂 on_trade
watcher = TradeWatcher(client, ["0x目标地址"], on_trade=print, poll_interval=60)
stream = TradeStream(["0x目标地址"], on_trade=watcher.ingest, on_gap=watcher.request_poll)
stream.start()
watcher.run_forever()
```

```python
# scout 编程接口
from polycopycat.scout import ScoutConfig, scout_addresses

verdicts = scout_addresses(client, ["0x地址A", "0x地址B"], config=ScoutConfig())
for v in verdicts:
    print(v.address, v.eligible, v.score, v.reasons)
```

引擎的组装方式见 `polycopycat/cli.py` 的 `cmd_run`（CopyEngine + 执行器 + 账本 + 通知的接线就是全部）。

## 项目结构

```
polycopycat/
├── cli.py            # 命令行入口：trades / watch / scout / run / report
├── data_api.py       # Data API 客户端（成交、持仓；公开只读）
├── models.py         # Trade / Position 数据模型（宽容解析）
├── watcher.py        # 轮询监控：增量去重、基线、ingest/request_poll
├── stream.py         # 实时成交推送：订阅、保活、指数退避重连、on_gap
├── _http.py          # 带重试的 HTTP GET/POST（429/5xx 指数退避）
├── arb.py            # 互补对套利扫描器（Gamma 目录 + 批量订单簿，只读）
├── kalshi.py         # Kalshi 只读客户端（市场列表/订单簿，美分→美元）
├── xarb.py           # 跨所套利：候选配对提名 + 确认配对价差（含费）
├── engine/           # 跟单引擎
│   ├── engine.py     #   主体：信号队列 → 过滤 → 计算 → 风控 → 执行 → 记账；对账线程
│   ├── config.py     #   copycat.json 解析与校验
│   ├── clob.py       #   CLOB 只读：市场元数据（缓存）、订单簿
│   ├── signals.py    #   Signal / OrderIntent / 信号过滤
│   ├── sizing.py     #   跟单金额、限价（tick 取整、最小量）
│   ├── risk.py       #   风控闸门：敞口、熔断、黑名单、停机开关
│   ├── executor.py   #   纸面执行器：订单簿逐档模拟 FAK
│   ├── live.py       #   实盘执行器：py-clob-client 下 FAK 限价单
│   ├── mirror.py     #   目标持仓镜像
│   ├── ledger.py     #   sqlite 账本：信号/订单/持仓/盈亏
│   └── notify.py     #   日志 / Telegram 通知
├── scout/            # 找可跟单地址
│   ├── metrics.py    #   成交带回放 → 战绩指标
│   ├── score.py      #   排除规则 + 打分
│   └── runner.py     #   候选来源（全站流/排行榜）与评估编排
tests/                # 165 个单测，全部离线（HTTP/WS 均为注入的假实现）
config.example.json   # 引擎配置示例
.claude/skills/verify # 端到端验证手册：本地 mock 全套接口驱动真实 CLI
.github/workflows/    # 三个真实接口 CI：scout / arb-scan / smoke
.scout-request 等     # 改这些请求文件即触发对应 CI，结果由 CI 提交回
scout-results/ 等     # CI 回写的评估 / 扫描 / 冒烟结果（json + txt）
```

## 开发与测试

```bash
pip install -e ".[dev]"
pytest                # 124 个单测，无网络依赖，<1s
```

端到端验证不打真实 API：用本地 mock（Data API + CLOB + WebSocket 都是标准库/websockets 起的假服务）驱动真实 CLI 子进程，逐场景断言输出与账本。具体流程和各命令的验证点写在 `.claude/skills/verify/SKILL.md`。实盘下单路径无法离线验证，上线前先小额实测。

真实接口行为差异靠三个 GitHub Actions 工作流兜底（结果由 CI 提交回分支）：

- `scout`：改 `.scout-request` 触发，内容透传给 `polycopycat scout`
- `arb-scan`：改 `.arb-request` 触发，真实市场套利扫描
- `smoke`：改 `.smoke-request` 触发，一次跑通 trades / `watch --stream`（RTDS 协议实测）/ CLOB 元数据与订单簿 / 纸面引擎全链路
- `xarb-scan`：改 `.xarb-request` 触发，Kalshi × Polymarket 跨所扫描

**真实环境验证状态（2026-07 冒烟 4/4 PASS）**：Data API（成交/持仓/全站流）、
RTDS 实时推送（90 秒 16 条实时成交、240 秒零断线）、CLOB（元数据/订单簿/批量、
negRisk 市场）、Gamma 目录、排行榜均已实测通过；纸面引擎在真实行情下完成过
买入跟单与跟随卖出。**唯一未实测路径是实盘签名下单**（需要私钥与真实资金），
上线时用最小金额验证第一单。

版本号在 `pyproject.toml` 与 `polycopycat/__init__.py`（当前 0.8.1），每交付一个里程碑 minor +1。

## 状态 / Roadmap

- [x] 读取目标地址的成交记录 + 轮询监控（`trades` / `watch`）
- [x] 实时成交推送（WebSocket）+ 轮询兜底对账（`watch --stream`）
- [x] 跟单引擎 M0：纸面模拟（真实盘口算滑点）、sqlite 账本、`run` / `report`
- [x] 跟单引擎 M1：实盘 FAK 下单（py-clob-client）、Telegram 通知
- [x] 跟单引擎 M2：卖出跟随（持仓镜像）、定期对账、可赎回提醒
- [x] scout：候选地址发现与战绩回放评分（排除做市/亏损/低样本地址）
- [x] arb-scan：互补对套利扫描（只读快照，买对冲/铸造两类机会；实测 0 机会）
- [x] xarb-scan：Kalshi × Polymarket 跨所套利（候选配对提名 + 确认配对含费价差）
- [ ] 信号聚合（分批建仓合并跟单）、自动 redeem、多目标信号轧差、跨所执行版

## 风险提示

预测市场波动大，跟单不保证盈利，别人赚钱的策略照抄也可能亏。代码仅供学习研究，实盘资金自负盈亏。另外 Polymarket 对部分地区有访问限制，使用前自行确认合规。
