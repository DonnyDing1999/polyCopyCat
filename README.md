# polyCopyCat

一个 Polymarket 跟单系统：盯住你选定的交易员地址，他们开仓、平仓时，按你设定的规则自动跟单。

完整工作流：**`scout` 找人 → `run`（纸面）验证 → `report` 复盘 → 确认后再上实盘**。

> 套利扫描工具（站内互补对 + Kalshi 跨所）已拆分至独立仓库 [polyArb](https://github.com/DonnyDing1999/polyArb)。

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
pip install "polycopycat[os-certs]"  # 公司网络 TLS 中间人：改用系统信任库
```

### 公司网络 / TLS 中间人（CERTIFICATE_VERIFY_FAILED）

公司网络常做 TLS 中间人：代理把 Polymarket 的证书换成自己签的，签发它的公司根 CA 装在系统钥匙串里（所以 Safari/Chrome 能正常打开），但 Python 的 requests/websocket 默认走 certifi 自带的 Mozilla 根、看不到公司根，于是 `SSL: CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`，实时流和轮询全连不上。

修复：装 `truststore`，让 Python 改用**操作系统信任库**（就是浏览器用的那套），公司根自然就认了：

```bash
pip install truststore --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

装上后 CLI 启动会自动启用（requests 和 websocket 一起生效），无需改配置或设环境变量。想临时禁用设 `POLYCOPYCAT_NO_TRUSTSTORE=1`。最省事的替代是换一个不拦 TLS 的网络（手机热点、家里 wifi），默认证书就够用。

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
| `run --config <文件>` | 启动跟单引擎（先自检 Data API/CLOB 可达性，纸面/实盘由配置决定） |
| `report` | 查看账本：信号统计、持仓、盈亏、最近订单 |
| `us <子命令>` | Polymarket US（美国合规站）行情与匹配：markets / book / bbo / match |

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

- **排除**（宁严勿松）：样本不足、成交额太小、多日不活跃、回放亏损、胜率过低、频率过高（疑似机器人）、**大样本下胜率高得离谱（疑似结构性套利）**、**持仓浮亏占成本过高（疑似囤死仓/只认盈不认亏）**、**跨场馆套利单腿（胜率异常高 + 几乎全在贴近1.0平仓、几乎不割肉——输腿在别的账户/场外博彩，本钱包只见幸存赢腿）**、**慢速做市/流动性提供（同一 token 反复双向循环成交额占比高 + 双向点差薄——吃价差而非看方向；快进快出规则只抓分钟级快翻，这条抓小时级慢做市）**，以及最关键的——**快进快出占比过高（疑似做市/套利）**。这类地址账面常年"盈利"、胜率漂亮，但赚的是价差返佣，方向毫无参考价值，跟单基本必亏
- **打分**（只在合格者间排序）：盈利 40 + 胜率 25 + 市场广度 15 + 活跃度 10 + 单笔规模 10，公式刻意简单透明
- 窗口外建的仓（只见卖不见买）盈亏未知，不掺进胜率——只对能自证的部分下结论

### run / report —— 跟单引擎

```bash
polycopycat run --config copycat.json            # mode 由配置决定，默认 paper
polycopycat run --config copycat.json --paper    # 保险丝：强制纸面
polycopycat report --config copycat.json         # 或 report --ledger data/copycat.sqlite3
polycopycat report --config copycat.json --by-target   # 按目标拆分：谁的跟单真赚钱
polycopycat report --config copycat.json --mark        # 拉实时盘口给持仓做市值重估，显示浮盈亏
```

引擎管道（每个环节的结论都落账本，可追溯）：

```
信号（watch/stream 双通道去重）→ 逐笔过滤 → 聚合/轧差（窗口，默认 2s）→ 仓位计算 → 风控闸门 → 执行器 → sqlite 账本 → 通知
                                                                        └ 对账线程：目标持仓镜像 / 实盘持仓同步 / 纸面自动结算 / 可赎回提醒
```

## 配置参考（copycat.json）

完整示例见 [config.example.json](config.example.json)。所有金额单位 USDC，价格量纲 0~1。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `paper` | `paper` 纸面模拟 / `live` 实盘 |
| `ledger_path` | `data/copycat.sqlite3` | 账本位置（`data/` 已在 .gitignore） |
| `reconcile_interval_s` | 300 | 对账周期：刷新目标镜像、实盘同步持仓、纸面结算检查 |
| `auto_settle_resolved` | true | 纸面：市场结算后按 1.00/0.00 自动入账清仓 |
| `data_api_url` / `ws_url` / `clob_url` | 官方入口 | 走代理或本地测试时覆盖 |
| **targets[]** |  | 跟单目标，至少一个 |
| `.address` | 必填 | 目标 proxy wallet 地址 |
| `.ratio` / `.max_per_trade_usdc` | 继承 sizing | 按目标覆盖比例/单笔上限 |
| `.paused` | false | 暂停跟单（仍监控、仍维护镜像） |
| **sizing** |  | 买入金额怎么算 |
| `.mode` | `proportional` | `proportional`：目标金额×ratio；`fixed`：固定 fixed_usdc |
| `.ratio` / `.fixed_usdc` / `.max_per_trade_usdc` | 0.1 / 20 / 100 | 比例、固定额、单笔上限 |
| `.depth_aware` | false | 买入时按盘口深度封顶（书浅自动缩量，避免滑点破顶）|
| `.max_follow_multiple` | 1.0 | 深度放大上限：书够深时基准量最多顶到几倍（1=只封顶不放大）|
| **filters** |  | 信号级过滤 |
| `.min_target_notional_usdc` | 10 | 尘埃单阈值：目标成交金额低于此不跟 |
| `.max_signal_age_s` | 30 | 信号超龄不跟（价格早走了）；短线/日内市场的上限 |
| `.long_horizon_age_s` | 120 | 长线市场（距结束 ≥ long_horizon_days）放宽后的时效上限；null 关闭分级 |
| `.long_horizon_days` | 7 | 判定长线的最小剩余天数（市场结束时间取自 CLOB 元数据） |
| `.follow_sells` | true | 是否跟随卖出 |
| `.skip_title_patterns` | [] | 市场标题命中任一子串（不分大小写）即不跟，用于屏蔽短期指数/日内快盘（如 `["up or down", "opens up"]`）|
| **risk** |  | 上限设为 null 表示不启用该项 |
| `.max_market_exposure_usdc` | 200 | 单市场持仓成本上限 |
| `.max_total_exposure_usdc` | 1000 | 总持仓成本上限 |
| `.daily_max_loss_usdc` | 100 | 当日已实现亏损熔断（只拦开新仓，减仓放行） |
| `.market_blacklist` | [] | condition id 或 slug |
| `.kill_switch_file` | `STOP` | 该文件存在时全面停止开新仓 |
| **execution** |  |  |
| `.slippage_cap` | 0.02 | 限价 = 目标成交价 ± 此值，FAK 绝不追价 |
| `.retry_no_fill_s` | 3 | 限价内无对手盘时隔几秒重试一次（盘口是流动的）；null 不重试 |
| **aggregate** |  | 信号聚合与轧差 |
| `.window_s` | 2.0 | 聚合窗口秒数（也是跟单延迟上限）；0 或 null 逐笔跟单 |
| `.idle_flush_s` | 0.5 | 窗口内静默多久提前收批（碎片连发是毫秒级，静默=不会再来）；0 等满窗口 |
| `.net_across_targets` | true | 同窗口内多目标对同一 token 反向时只执行净头寸 |
| **health** |  | 在跟目标健康巡检 + 候选发现 |
| `.check_interval_s` | 21600 | 巡检周期（默认 6 小时）；0 或 null 关闭 |
| `.auto_pause` | true | 目标命中排除规则自动暂停。考核口径与招聘不同：**活仓浮亏**（当前被套）+ **窗口净盈亏**（死仓只追溯回放窗口内买入后归零的，老死仓不算，恢复可达）|
| `.auto_resume` | true | 被巡检暂停的目标恢复合格后自动复跟（手动暂停的不碰）|
| `.discover_interval_s` | 86400 | 候选发现周期：扫全站活跃 top N 找可跟新面孔；0 或 null 关闭 |
| `.discover_candidates` | 100 | 每轮评估多少个活跃地址（合格新面孔写 discover-latest.json + 通知）|
| `.auto_recruit` | false | 合格新面孔自动加入跟单（**仅纸面模式**；招募档案 recruited.json 重启自动并回）|
| `.recruit_ratio` / `.recruit_max_per_trade_usdc` | 0.05 / 25 | 招募目标的保守档位（比常规目标小）|
| `.recruit_max_targets` | 15 | 目标总数上限（配置 + 招募），防无限膨胀 |
| **watch** |  | 信号源 |
| `.stream` | true | 实时推送 + 轮询兜底 |
| `.poll_interval` | null | null = 自动（stream 模式 60s，纯轮询 10s） |
| `.backfill` | 0 | 启动时回放最近 N 条 |
| **notify** |  | 不配 Telegram / Discord 就只打日志 |
| `.telegram_bot_token_env` | "" | 存 bot token 的**环境变量名**（不是 token 本身） |
| `.telegram_chat_id` | "" | 接收通知的 chat |
| `.discord_webhook_url_env` | "" | 存 Discord 频道 webhook URL 的**环境变量名**（见「Discord 通知/部署」）|
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
| `POLYCOPYCAT_US_URL` | 覆盖 Polymarket US gateway 入口 |
| `POLYCOPYCAT_PRIVATE_KEY` | 实盘私钥（变量名可在 `live.private_key_env` 改；绝不落盘、绝不进日志） |
| 自定义名 | Telegram bot token，变量名由 `notify.telegram_bot_token_env` 指定 |
| 自定义名 | Discord 频道 webhook URL，变量名由 `notify.discord_webhook_url_env` 指定 |

## 数据来源

| 接口 | 默认入口 | 用在哪 | 状态 |
| --- | --- | --- | --- |
| Data API | `data-api.polymarket.com` | 成交带、持仓（trades/watch/scout/镜像/对账） | 公开，无需鉴权 |
| 实时数据流 | `wss://ws-live-data.polymarket.com` | 实时成交推送（官网活动流同款） | 公开；协议按公开资料实现 |
| CLOB | `clob.polymarket.com` | 市场元数据、订单簿（只读）；实盘下单（L1/L2 鉴权） | 官方 |
| 排行榜 | `lb-api.polymarket.com` | scout 候选来源 | **非正式文档**，不可用时自动跳过 |
| Polymarket US gateway | `gateway.polymarket.us` | `us` 子命令（美国站行情、市场匹配） | 公开，无需鉴权；有反爬拦截，见下节 |

## 时延：轮询 vs 实时推送

| 模式 | 新成交被发现的时延 | 原理 |
| --- | --- | --- |
| 轮询 | 平均约「轮询间隔的一半 + Data API 索引延迟」，`--interval 10` 时约 5~12 秒 | 定期拉 `GET /trades` 增量比对 |
| `--stream` | 通常 1 秒内 | 订阅实时数据流，成交即推；轮询降级为兜底对账 |

轮询想更快可以把 `--interval` 压到 2 秒左右，代价是请求量（每地址每轮 1 个请求），太激进可能触发限流。

实时流只能全站订阅、客户端过滤：订阅消息带 `filters` 字段做服务端过滤**实测不可用**（2026-07 验证过 camelCase/snake_case、字符串/对象四种形态——一旦携带，服务端静默不推任何消息，实时通道整个失效）。不要再试。

## 跟单规则与已知边界

**买入**：跟随金额 = min(目标金额 × ratio, 单笔上限)（或固定金额模式）；限价 = 目标成交价 + slippage_cap，按市场 tick 取整，FAK 只吃限价内的盘口。计划量低于市场最小下单量（一般 5 份）会跳过并记入账本——**跟小单风格的目标时把 ratio 调高（如 1.0）或改用 fixed 模式**，否则大部分信号会被这条规则丢掉；`report` 里 `skipped` 占比过高就是这个信号。

**深度感知与放大**（`sizing.depth_aware`）：跟单放大不了目标的**收益率**，只能在同一条边上按更大的量吃同一口利润，而能吃多大由盘口深度决定——超过限价内盘口能承接的量，多下的部分只会把价格打坏、反噬自己。开启后买入定量会拉一次实时订单簿：先按 `max_follow_multiple` 放大基准金额（想吃更大的本），再用限价内的盘口容量封顶（吃不下的不下），仍受单笔上限约束。`max_follow_multiple=1` 时只封顶不放大（纯安全上限，推荐默认）；调到 2~3 则在书深时加大绝对金额（同样的价差、更大的本），书浅时自动缩回能成交的量。执行日志/通知里会标出「深度放大 N×（吃盘口容量 $X 的 Y%）」，据此判断某个市场到底有没有放大空间。

**卖出**：目标卖掉他持仓的 x%，就卖掉自己持仓的 x%。目标持仓镜像 = 启动/定期 `/positions` 快照 + 每笔成交实时累加（无论该笔跟没跟都更新），merge/redeem 等不出现在成交流里的变化由对账兜住；镜像没记录时按全平保守离场；余量将低于最小下单量时直接全平，避免死仓。

**动态时效**：时效闸按市场期限分级。距结束 ≥7 天的长线市场（世界杯冠军、年底价位这类）放宽到 `long_horizon_age_s`（默认 120s）——这类市场一笔成交后价格几小时都动不了多少，两分钟内跟进依然新鲜；日内/短线维持 `max_signal_age_s`（默认 30s）严格上限。实现上逐笔粗筛先按放宽上限放行，拿到 CLOB 元数据（含市场结束时间）后再按该市场的精确上限复核，超龄成员从并单组里剔除、剩余照常执行。市场没给结束时间时按短线保守处理。

**聚合与轧差**：目标一次建仓常拆成几秒内的多笔碎片成交。引擎收到首笔信号后等一个窗口（`aggregate.window_s`，默认 2s），把窗口内同目标、同 token、同方向的成交合成一笔（数量求和、价格按 VWAP）再走后续管道——金额阈值按合并后口径判，碎片单不再被尘埃规则逐笔拦掉，单笔上限也按合并后的一笔计。同窗口内多个目标对同一 token 方向相反时只执行净头寸（`net_across_targets`），被抵消的信号记为 `netted`。窗口就是跟单延迟的下限，跟高频目标时调小或设 0 关闭。

**纸面模式**（默认）：执行器拉实时订单簿逐档模拟 FAK 成交，滑点按真实盘口算，账本记的就是「如果真跟会怎样」。注意纸面结果略偏乐观（没有排队竞争、你的单不推动价格）：纸面亏的实盘一定亏，纸面小赚的未必赚。市场结算后对账线程会按结算价 1.00/0.00 自动入账清仓（`auto_settle_resolved`），持仓和盈亏不需要手工维护。

**实盘边界**：

- 下单响应不含逐档成交明细，持仓以定期对账为准，当日亏损熔断在实盘是近似值——务必同时设好敞口上限
- 市场结算后引擎提醒「可赎回」，但不自动发链上 redeem 交易（那是一笔要 gas 的链上操作），去 Polymarket 页面点一下；纸面模式则已自动入账
- EOA 首次交易需要先对交易所合约做 USDC/CTF 授权并备少量 POL；资金是 Polygon 上的 USDC.e

## 账本（sqlite）

三张表，`report` 读的就是它们：

- **signals**：每笔目标成交一行，`trade_key` 唯一约束提供重启幂等；`source` 记录信号通道（stream 实时 / poll 轮询 / backfill 回放），用于诊断哪条路真正促成成交；`status` 记录归宿：`executed` / `filtered`（没过过滤）/ `skipped`（不值得下）/ `risk_blocked`（风控拦截）/ `no_fill`（限价内无对手盘）/ `netted`（轧差抵消）/ `error`。并单执行的一组信号共享一张订单，detail 里注明挂在哪个信号下
- **orders**：每次执行一行：限价、请求量、成交量、均价、滑点、该单已实现盈亏；纸面自动结算记为 `REDEEM` / `settled`（signal_id=0，非信号驱动）
- **positions**：当前持仓（份额、均价、累计已实现盈亏）；纸面由成交直接记账，实盘由对账快照覆盖
- **events**：巡检/招募动作档案（health_pause / health_resume / recruit），复盘「谁被停过几次、为什么」的依据；report 的「池子状态」「信号通道与过滤」小节读的就是它和 signals
- **state**：引擎状态键值（巡检暂停名单、巡检/发现计时）——重启后接着算而不是从零，被停的目标不会因为部署重启临时复跟

**执行质量**（report 自动展示）：账本里每笔成交都记了「信号→成交延迟」和「跟入价 vs 目标价」，report 聚合成延迟中位/均值/最大、平均价差、**延迟+滑点合计成本**（多付了多少钱）与全额成交率。它回答的是另一半问题：不是"目标赚不赚"，而是"**我跟他的方式**有没有把他的 edge 磨没"——合计成本逼近毛利时，跟得再准也白跟。

**`report --by-target`**：跟多个目标时，按目标拆分「已实现盈亏 / 累计买入 / 执行占信号比（跟单率）/ 各状态信号数」，用来回答"这几个人里谁的纸面跟单真赚钱、谁的动作根本跟不上"。卖出跟随平仓的盈亏经 orders→signals 干净归到目标；市场结算入账的盈亏在持仓层（一个 token 可能多目标共建），单列成"未按目标归属"一桶，可归因 + 未归属 = 账本总盈亏。

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

## Polymarket US（美国合规站）

Polymarket US 和主站是两个独立平台：跑在 QCEX（CFTC 持牌交易所）上，账户要 KYC，订单簿中心化，也没有公开的按用户成交数据，所以在美国站上"盯人跟单"做不到。目前接入的是无需鉴权的 gateway 只读行情，用途是对照两站盘口，以及把主站信号对应到美国站市场：

```bash
polycopycat us markets nfl                    # 搜索市场；不带关键词则列出活跃市场
polycopycat us book <slug> --depth 5          # 订单簿（卖侧在上，买侧在下）
polycopycat us bbo <slug>                     # 最优买卖价、价差、未平仓量
polycopycat us match "Bitcoin above $100k" --outcome Yes --quote   # 主站市场 → US 候选
```

`us match` 把主站市场的标题（或 slug）拿到美国站搜索，按词面相似度排序：词集重合 70% + 数字重合 20% + 结果名 10%。数字单独加权，因为价位和日期往往是两个市场之间唯一的区别（"BTC above $100k" 和 "BTC above $150k" 词面几乎一样）。分数只用来排序，两站的结算口径可能不同，下单前先人工确认是不是同一个问题。

已知边界：

- gateway 有反爬拦截，脚本直连可能拿到 403。被拦时用 `--us-url` 或 `POLYCOPYCAT_US_URL` 指到自己的代理
- 交易和实时推送在 `api.polymarket.us`，需要 Ed25519 API key（在 polymarket.us/developer 申请），连行情 WebSocket 也要凭证。美国站实盘执行器等拿到 key 后接官方 SDK `polymarket-us`（见 Roadmap）
- 价格量纲与主站一致（0~1 美元）；金额字段是 `{"value": "0.55", "currency": "USD"}` 形式的对象，订单簿卖侧键名是 `offers`，客户端都已归一成和主站一致的形状

## Discord 通知 / 部署

想在 Discord 上看纸面 dry run 的实时动态，认清一点：**paper dry run 是一个 7×24 常驻引擎**（挂着实时 WebSocket、跑信号循环、连 CLOB、写账本），不能塞进 Cloudflare Worker 那种「有请求才醒、30 秒即杀」的无状态函数里。正确形态是：**引擎跑在一台常开、且能访问 `clob.polymarket.com` 的机器上**（云 VPS 最稳；公司网络封 CLOB，跑不了），Discord 只是它的输出口。

接入方式是**频道 webhook 推送**（无需建 bot、无需 token、无需注册斜杠命令）：引擎每笔跟单成交、风控拦截、市场结算都 POST 到你的频道。

1. Discord 里：目标频道 → 编辑频道 → 整合 → Webhook → 新建 → 复制 URL（形如 `https://discord.com/api/webhooks/<id>/<token>`）。
2. 跑引擎的机器上把 URL 放进环境变量（**别写进配置文件**，它是半机密）：`export POLYCOPYCAT_DISCORD_WEBHOOK=<刚复制的URL>`。
3. 配置里只写**变量名**：`"notify": { "discord_webhook_url_env": "POLYCOPYCAT_DISCORD_WEBHOOK" }`。
4. `run` 起来后，跟单动态就实时进频道了。webhook URL 半机密（拿到即可往频道发消息），所以走环境变量、绝不落盘。

想要 Discord 里主动查询（如 `/report` 斜杠命令）而不只是被动接收，那是另一套（Cloudflare Worker + 斜杠命令，见项目根目录 GUIDE.md 的方式 A）——但它读不到 VPS 上的账本，得等引擎先在 VPS 上跑起来、再把账本或报告暴露出来，属于后续工作。

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

```python
# Polymarket US：行情与市场匹配
from polycopycat.us import UsApiClient, match_us_markets

us = UsApiClient()
print(us.get_bbo("某个市场slug").best_bid)
for m in match_us_markets(us, "Bitcoin above $100k", top=3):
    print(m.score, m.market.slug, m.market.title)
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
├── _http.py          # 带重试的 HTTP GET（429/5xx 指数退避）
├── engine/           # 跟单引擎
│   ├── engine.py     #   主体：信号队列 → 过滤 → 计算 → 风控 → 执行 → 记账；对账线程
│   ├── config.py     #   copycat.json 解析与校验
│   ├── clob.py       #   CLOB 只读：市场元数据（缓存）、订单簿
│   ├── signals.py    #   Signal / OrderIntent / 信号过滤
│   ├── sizing.py     #   跟单金额、限价（tick 取整、最小量、深度封顶/放大）
│   ├── depth.py      #   订单簿深度分析：限价内可吃容量、深度感知定量
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
├── us/               # Polymarket US（美国合规站）
│   ├── api.py        #   gateway 只读行情：markets / book / bbo / settlement / search
│   └── match.py      #   主站市场 → US 市场的词面匹配打分
tests/                # 258 个单测，全部离线（HTTP/WS 均为注入的假实现）
config.example.json   # 引擎配置示例
.claude/skills/verify # 端到端验证手册：本地 mock 全套接口驱动真实 CLI
.github/workflows/    # 真实接口 CI：scout / smoke
.scout-request 等     # 改这些请求文件即触发对应 CI，结果由 CI 提交回
scout-results/ 等     # CI 回写的评估 / 扫描 / 冒烟结果（json + txt）
```

## 开发与测试

```bash
pip install -e ".[dev]"
pytest                # 258 个单测，无网络依赖，约 1s
```

端到端验证不打真实 API：用本地 mock（Data API + CLOB + WebSocket 都是标准库/websockets 起的假服务）驱动真实 CLI 子进程，逐场景断言输出与账本。具体流程和各命令的验证点写在 `.claude/skills/verify/SKILL.md`。实盘下单路径无法离线验证，上线前先小额实测。

真实接口行为差异靠两个 GitHub Actions 工作流兜底（结果由 CI 提交回分支）：

- `scout`：改 `.scout-request` 触发，内容透传给 `polycopycat scout`
- `smoke`：改 `.smoke-request` 触发，一次跑通 trades / `watch --stream`（RTDS 协议实测）/ CLOB 元数据与订单簿 / 纸面引擎全链路

**真实环境验证状态（2026-07 冒烟 4/4 PASS）**：Data API（成交/持仓/全站流）、
RTDS 实时推送（90 秒 16 条实时成交、240 秒零断线）、CLOB（元数据/订单簿/批量、
negRisk 市场）、排行榜均已实测通过；纸面引擎在真实行情下完成过
买入跟单与跟随卖出。**唯一未实测路径是实盘签名下单**（需要私钥与真实资金），
上线时用最小金额验证第一单。Polymarket US gateway 对无浏览器指纹的直连有反爬
拦截（403），`us` 子命令的解析逻辑按官方 SDK 契约实现并全部离线覆盖，
真实入口通了之后再补冒烟。

版本号在 `pyproject.toml` 与 `polycopycat/__init__.py`（当前 0.28.0），每交付一个里程碑 minor +1。

## 状态 / Roadmap

- [x] 读取目标地址的成交记录 + 轮询监控（`trades` / `watch`）
- [x] 实时成交推送（WebSocket）+ 轮询兜底对账（`watch --stream`）
- [x] 跟单引擎 M0：纸面模拟（真实盘口算滑点）、sqlite 账本、`run` / `report`
- [x] 跟单引擎 M1：实盘 FAK 下单（py-clob-client）、Telegram 通知
- [x] 跟单引擎 M2：卖出跟随（持仓镜像）、定期对账、可赎回提醒
- [x] scout：候选地址发现与战绩回放评分（排除做市/亏损/低样本地址）
- [x] 套利扫描（站内互补对 + Kalshi 跨所）→ 已拆分至独立仓库 [polyArb](https://github.com/DonnyDing1999/polyArb)
- [x] Polymarket US：gateway 只读行情 + 主站市场匹配（`us` 子命令）
- [x] 跟单引擎 M3：信号聚合（分批建仓并单）、多目标轧差、纸面自动结算入账
- [x] `report --by-target`：按目标拆分盈亏与信号归属，评估多目标里谁值得真金白银跟
- [x] 深度感知跟单（`sizing.depth_aware`）：按盘口深度封顶买入量、书深时可放大，避免滑点破顶
- [x] 执行质量分析（report）+ 目标健康巡检（自动暂停/复跟）+ 账本每日备份（deploy/backup.sh）
- [x] 候选发现（周期扫全站活跃 top N 找可跟新面孔）+ 链路提速（聚合静默提前收批、WS 死链 15s 发现）
- [x] 动态跟单池：合格新面孔自动招募进纸面跟单（recruited.json 持久化），变质自动暂停、恢复自动复跟
- [x] 可观测性：信号通道打标（stream/poll/backfill）、过滤原因分布、巡检/招募事件档案与池子状态报表
- [x] report --mark 实时市值重估（浮盈亏 + 纸面总盈亏）+ 对账回填缺失的市场元数据（title/conditionId）
- [x] 巡检/发现状态持久化（暂停名单+计时进账本 state 表，重启不清不重置）
- [ ] Polymarket US 实盘执行器：官方 `polymarket-us` SDK 下单，把主站信号镜像到美国站（需 API key）
- [ ] 实盘链上自动 redeem（web3 直发 redeemPositions，需 gas 管理；纸面已自动入账）

## 风险提示

预测市场波动大，跟单不保证盈利，别人赚钱的策略照抄也可能亏。代码仅供学习研究，实盘资金自负盈亏。另外 Polymarket 对部分地区有访问限制，使用前自行确认合规。
