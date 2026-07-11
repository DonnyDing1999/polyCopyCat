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

## 坑

- 子进程环境里设 `NO_PROXY=127.0.0.1,localhost`，否则请求可能被代理劫走。
- 直连官方 API 在本沙箱必然失败（CONNECT 403），那是环境限制不是代码问题。
