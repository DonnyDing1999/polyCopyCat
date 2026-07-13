---
name: verify
description: 端到端验证 polyCopyCat 的改动——真实跑 CLI，别只跑单测
---

# 验证 polyCopyCat

## 安装 / 运行

```bash
pip install -e ".[dev]"        # 装包 + pytest
polycopycat --help             # CLI 入口（等价 python -m polycopycat）
```

## 端到端驱动

官方 Data API（data-api.polymarket.com）在受限沙箱里常被网络策略拦截。
验证时起一个本地 mock（http.server 提供 `GET /trades`，返回 Data API
形状的 JSON 数组，按 timestamp 新→旧），然后用 `--base-url` 指过去：

```bash
polycopycat trades 0x<40位hex> --limit 2 --base-url http://127.0.0.1:<port>
polycopycat watch 0x<40位hex> --interval 0.5 --backfill 1 --base-url http://127.0.0.1:<port>
```

值得驱动的流：

- trades 一次性读取（人类格式 + `--json` 两种输出）
- watch：首轮只建基线不刷屏 → 往 mock 里注入新成交 → 只多打一行 →
  SIGINT 干净退出（stderr 打「已停止监控。」，退出码 0）
- mock 先回 2 次 503 再成功 → 客户端自动重试；一直 503 → 退出码 1
- 非法地址 / 打错 flag → argparse 报错，退出码 2
- `watch --stream`：`pip install websockets` 起本地 mock WS
  （收 subscribe、回应 "ping"、往客户端推
  `{"topic":"activity","type":"trades","payload":{…裸trade…}}`），
  CLI 加 `--ws-url ws://127.0.0.1:<port>`。验证点：推送→输出的时延
  （应为毫秒级）、`transport.abort()` 掐线后 ~1s 重连并触发对账轮询
  补漏（配大 `--interval` 才能证明是对账而不是常规轮询捞到的）、
  同一笔成交跨通道只输出一次
- `run --config`（跟单引擎，纸面）：mock 再加 CLOB 两个端点
  `/markets/{cid}`（tick/最小量/negRisk/accepting）和 `/book`
  （bids/asks 档位），`/positions` 按 user 过滤返回目标持仓。
  配置 data_api_url/clob_url/ws_url 全指向 mock。验证点：推目标
  BUY → 纸面按盘口逐档成交且滑点正确；目标 SELL x% → 跟随卖出
  自己持仓的 x%（注意镜像=启动快照+之后的成交累加）；尘埃单被过滤；
  超敞口被风控拦截；touch 停机文件即全停；SIGINT 后 `report
  --ledger` 对得上每一笔。注意：账本删掉重建时要连 -wal/-shm
  一起删，否则 sqlite 报 disk I/O error
- 实盘防线在 CLI 层验证：未确认风险 / 缺私钥 → 干净报错退出 1；
  `--paper` 能把 live 配置强制转纸面。真实下单路径无法离线验证
- `scout`：mock `/trades`（带 user=单地址成交带、不带 user=全站流）、
  `/positions`、`/leaderboard`，构造几个人设（多市场止盈鲸鱼 /
  20 秒快进快出做市机器人 / 割肉亏损户 / 3 笔小样本）。验证点：
  鲸鱼合格且排第一、做市者哪怕账面盈利+高胜率也要被快进快出占比
  排除、`--targets-snippet` 只含合格地址、排行榜挂掉能优雅降级
- `us`（Polymarket US gateway）：mock `/v1/markets`、`/v1/search`
  （events 里嵌 markets）、`/v1/markets/{slug}/book`（注意卖侧键名
  是 offers、价格是 `{"value":"0.52"}` 对象）、`/v1/markets/{slug}/bbo`，
  CLI 加 `--us-url http://127.0.0.1:<port>`。验证点：markets 默认送
  active=true&closed=false；book 卖侧从高到低在上、买侧从高到低在下；
  bbo 价差算对；match 数字权重让 $100k 排在 $150k 前、`--quote --json`
  时每行带 bbo；mock 回 403 → 退出码 1 报「请求失败」（真实 gateway
  有反爬，脚本直连 403 属环境限制）

## 坑

- 子进程环境里设 `NO_PROXY=127.0.0.1,localhost`，否则请求可能被代理劫走。
- 直连官方 API 在本沙箱必然失败（CONNECT 403），那是环境限制不是代码问题。
