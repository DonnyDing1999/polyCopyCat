# polyCopyCat

一个 Polymarket 跟单系统：盯住你选定的交易员地址，他们在 Polymarket 上开仓、平仓时，按你设定的规则自动跟单。

## 想法

Polymarket 的成交都在链上（Polygon），任何地址的持仓和历史成交都是公开的。找到几个长期赚钱的地址，跟着他们下单，就是这个项目要做的事：

- 监控目标地址的成交（Polymarket Data API / CLOB WebSocket）
- 发现新开仓或平仓后，按固定金额或按比例复制下单
- 风控：单笔金额上限、总敞口上限、滑点保护、市场黑名单
- 下单和成交结果的通知

## 快速开始

第一块已经就位：**读取其他地址的下单（成交记录）**，数据来自 Polymarket 公开的 Data API，无需 API key。

```bash
pip install -e .
```

一次性读取某地址最近的成交（新→旧）：

```bash
polycopycat trades 0x目标地址 --limit 20
```

持续监控一个或多个地址的新成交：

```bash
polycopycat watch 0x地址A 0x地址B --interval 10   # 轮询模式
polycopycat watch 0x地址A --stream                # 实时推送模式（推荐，秒内跟到）
```

- 地址用 Polymarket 个人主页 URL 里的那个 0x 地址（proxy wallet）
- 首次轮询只建立基线，不会把历史刷成"新成交"；加 `--backfill 5` 可先回放最近 5 条
- 加 `--json` 输出 JSON lines，方便接下游程序；`--include-maker` 连挂单侧成交一起看
- 走代理或本地测试时用 `--base-url` / `--ws-url`（或环境变量
  `POLYCOPYCAT_DATA_API_URL` / `POLYCOPYCAT_WS_URL`）指到别的入口

## 时延：轮询 vs 实时推送

跟单对时延敏感，两种通道的差别：

| 模式 | 新成交被发现的时延 | 原理 |
| --- | --- | --- |
| 轮询（默认） | 平均约「轮询间隔的一半 + Data API 索引延迟」，`--interval 10` 时大约 5~12 秒 | 定期拉 `GET /trades` 增量比对 |
| `--stream` 实时 | 通常 1 秒内 | 订阅 Polymarket 实时数据流（官网活动流同款 WebSocket），成交即推 |

- 轮询想更快可以把 `--interval` 压到 2 秒左右，代价是请求量变大（每地址每轮 1 个请求），太激进可能触发限流
- `--stream` 模式下轮询自动降级为兜底对账（默认 60 秒一轮）：WebSocket 断线重连后会立刻触发一次对账轮询，把断线期间漏掉的成交补回来，且与实时通道共用一套去重、不会重复上报

## 找跟单对象：scout

跟谁比怎么跟更重要。`scout` 从公开数据里筛选值得跟的地址：

```bash
polycopycat scout                          # 默认从全站最近成交挖活跃地址评估
polycopycat scout --from-leaderboard       # 叠加官方排行榜作为候选来源
polycopycat scout 0x地址A 0x地址B          # 直接评估指定地址
polycopycat scout --targets-snippet        # 顺手生成 copycat.json 的 targets 段
```

评估方法：拉每个候选的公开成交带（≤500 笔/页，`--pages` 可加深），
用与账本一致的均价法**回放**出已实现盈亏、胜率、持仓时长、市场广度，
再按规则先排除后打分：

- **排除**（宁严勿松）：样本不足、成交额太小、多日不活跃、回放亏损、
  胜率过低，以及最关键的——**快进快出占比过高（疑似做市/套利）**。
  这类地址账面常年"盈利"、胜率漂亮，但赚的是价差返佣，方向毫无
  参考价值，跟单基本必亏
- **打分**（只在合格者间排序）：盈利 40 + 胜率 25 + 市场广度 15 +
  活跃度 10 + 单笔规模 10，公式刻意简单透明
- 窗口外建的仓（只见卖不见买）盈亏未知，不掺进胜率——只对能自证的
  部分下结论

注意：排行榜接口非正式文档（不可用时自动跳过，可用 `--leaderboard-url` /
`POLYCOPYCAT_LB_URL` 覆盖）；回放窗口有限，历史盈利不代表未来，
选出来的地址先用纸面模式跑一段再说。

## 跟单引擎

监控只是信号源，`polycopycat run` 才是跟单本体：

```
信号（watch/stream）→ 过滤 → 仓位计算 → 风控闸门 → 执行器 → sqlite 账本 → 通知
```

```bash
cp config.example.json copycat.json   # 改成你的目标地址和限额
polycopycat run --config copycat.json # 默认 paper 纸面模式
polycopycat report --config copycat.json  # 随时看持仓 / 盈亏 / 最近订单
```

### 先跑纸面（强烈建议）

`mode: "paper"` 不动真钱：执行器拉实时订单簿模拟 FAK 成交，滑点按真实盘口计算，
账本里记的就是「如果真跟会怎样」。跑一两周 `report` 一看便知：跟这个地址到底
赚不赚、滑点吃掉多少。`--paper` 参数可以强制任何配置以纸面运行（实盘前的保险丝）。

### 跟单规则（配置里都能调）

- **买入**：跟随金额 = min(目标金额 × ratio, 单笔上限)，或固定金额模式；
  限价 = 目标成交价 + slippage_cap，FAK 只吃限价内的盘口，绝不追价
- **卖出**：目标卖掉他持仓的 x%，就卖掉自己持仓的 x%（持仓镜像实时累加 +
  定期 `/positions` 对账，merge/redeem 造成的变化也能兜住）；余量不足最小
  下单量时全平，避免死仓
- **过滤**：尘埃单（金额阈值）、过期信号（默认 30s）、可按目标暂停
- **风控**：单市场敞口、总敞口、当日亏损熔断（只拦开新仓，减仓放行）、
  市场黑名单、`STOP` 文件一键停机

### 实盘

```bash
pip install "polycopycat[live]"
export POLYCOPYCAT_PRIVATE_KEY=0x...   # 建议专用小额热钱包
```

配置里 `mode: "live"`，并把 `live.i_understand_live_trading_risk` 显式改成
`true`（不改会拒绝启动）。浏览器钱包用户：`signature_type: 2` + `funder`
填 Polymarket 个人主页的 proxy wallet 地址；邮箱钱包用 1；EOA 直连用 0。
首次用 EOA 交易需要先对交易所合约做 USDC/CTF 授权（见 Polymarket 文档）。

实盘已知边界（代码里也有注释）：

- 下单响应不含逐档成交明细，持仓以定期对账（Data API `/positions`）为准，
  因此当日亏损熔断在实盘是近似值——务必同时设好敞口上限
- 市场结算后引擎会提醒「可赎回」，但不会自动发链上 redeem 交易（涉及资金
  操作且难以离线验证，宁缺勿滥），去 Polymarket 页面点一下即可
- 短窗口内的分批建仓暂不聚合，每笔独立跟单

在代码里使用（跟单逻辑之后就挂在 `on_trade` 回调上）：

```python
from polycopycat import DataApiClient, TradeWatcher

client = DataApiClient()
for t in client.get_trades("0x目标地址", limit=10):
    print(t.time_utc, t.side, t.size, t.price, t.title)

watcher = TradeWatcher(client, ["0x目标地址"], on_trade=print, poll_interval=10)
watcher.run_forever()
```

开发与测试：

```bash
pip install -e ".[dev]"
pytest
```

## 状态 / Roadmap

- [x] 读取目标地址的成交记录 + 轮询监控（`polycopycat trades` / `polycopycat watch`）
- [x] 实时性升级：实时成交推送（WebSocket）+ 轮询兜底对账（`watch --stream`）
- [x] 跟单引擎 M0：纸面模拟（真实盘口算滑点）、sqlite 账本、`run` / `report`
- [x] 跟单引擎 M1：实盘 FAK 下单（py-clob-client）、Telegram 通知
- [x] 跟单引擎 M2：卖出跟随（持仓镜像）、定期对账、可赎回提醒
- [x] scout：候选地址发现与战绩回放评分（排除做市/亏损/低样本地址）
- [ ] 信号聚合（分批建仓合并跟单）、自动 redeem、多目标信号轧差

## 风险提示

预测市场波动大，跟单不保证盈利，别人赚钱的策略照抄也可能亏。代码仅供学习研究，实盘资金自负盈亏。另外 Polymarket 对部分地区有访问限制，使用前自行确认合规。
