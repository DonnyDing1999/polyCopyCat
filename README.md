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
polycopycat watch 0x地址A 0x地址B --interval 10
```

- 地址用 Polymarket 个人主页 URL 里的那个 0x 地址（proxy wallet）
- 首次轮询只建立基线，不会把历史刷成"新成交"；加 `--backfill 5` 可先回放最近 5 条
- 加 `--json` 输出 JSON lines，方便接下游程序；`--include-maker` 连挂单侧成交一起看
- 走代理或本地测试时用 `--base-url`（或环境变量 `POLYCOPYCAT_DATA_API_URL`）指到别的入口

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
- [ ] 实时性升级：CLOB WebSocket 订阅
- [ ] 跟单下单：按固定金额或比例复制，接 CLOB 下单
- [ ] 风控：单笔上限、总敞口上限、滑点保护、市场黑名单
- [ ] 下单与成交结果通知

## 风险提示

预测市场波动大，跟单不保证盈利，别人赚钱的策略照抄也可能亏。代码仅供学习研究，实盘资金自负盈亏。另外 Polymarket 对部分地区有访问限制，使用前自行确认合规。
