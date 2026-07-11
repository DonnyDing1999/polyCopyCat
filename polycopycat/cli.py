"""命令行入口：读取 / 监控其他地址在 Polymarket 的下单，以及跟单引擎。

用法示例::

    polycopycat trades 0x地址 --limit 20
    polycopycat watch 0x地址A 0x地址B --interval 10 --backfill 5
    polycopycat watch 0x地址A --stream        # 实时推送，秒内跟到新下单
    polycopycat run --config copycat.json     # 跟单引擎（纸面/实盘由配置决定）
    polycopycat report --config copycat.json  # 查看持仓与盈亏
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading

from . import __version__
from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade
from .watcher import TradeWatcher

# 实时推送线程和轮询主线程都会打印成交，避免行间交错
_EMIT_LOCK = threading.Lock()


def _address(value: str) -> str:
    try:
        return normalize_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _short(text: str) -> str:
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def _format_trade(trade: Trade) -> str:
    notional = f"${trade.notional:,.2f}"
    line = (
        f"{trade.time_utc}  {_short(trade.proxy_wallet)}  "
        f"{trade.side:<4} {trade.size:>10.2f} @ {trade.price:.3f}  "
        f"{notional:>10}  [{trade.outcome}] {trade.title}"
    )
    if trade.transaction_hash:
        line += f"  tx {_short(trade.transaction_hash)}"
    return line


def _emit(trade: Trade, as_json: bool) -> None:
    line = (
        json.dumps(trade.to_dict(), ensure_ascii=False)
        if as_json
        else _format_trade(trade)
    )
    with _EMIT_LOCK:
        print(line, flush=True)


def cmd_trades(args: argparse.Namespace) -> int:
    client = DataApiClient(base_url=args.base_url)
    trades = client.get_trades(
        args.address, limit=args.limit, taker_only=not args.include_maker
    )
    for trade in trades:
        _emit(trade, args.json)
    if not trades:
        print("（该地址暂无成交记录）", file=sys.stderr)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    client = DataApiClient(base_url=args.base_url)
    # 纯轮询模式勤快点；实时模式下轮询只是兜底对账，可以放慢
    interval = args.interval if args.interval is not None else (60.0 if args.stream else 10.0)
    watcher = TradeWatcher(
        client,
        args.addresses,
        on_trade=lambda trade: _emit(trade, args.json),
        poll_interval=interval,
        backfill=args.backfill,
    )
    stream = None
    if args.stream:
        from .stream import TradeStream

        stream = TradeStream(
            watcher.addresses,
            on_trade=watcher.ingest,       # 与轮询共用一套去重，不会重复上报
            on_gap=watcher.request_poll,   # 断线重连后立即对账补漏
            ws_url=args.ws_url,
        )
        stream.start()
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        print("已停止监控。", file=sys.stderr)
    finally:
        if stream is not None:
            stream.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .engine.clob import ClobReadClient
    from .engine.config import ConfigError, load_config
    from .engine.engine import CopyEngine
    from .engine.executor import PaperExecutor
    from .engine.ledger import Ledger
    from .engine.notify import build_notifier
    from .stream import TradeStream

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    if args.paper:
        config.mode = "paper"

    data_client = DataApiClient(base_url=args.base_url or config.data_api_url)
    clob = ClobReadClient(base_url=args.clob_url or config.clob_url)
    ledger = Ledger(config.ledger_path)
    notifier = build_notifier(config.notify)
    if config.mode == "live":
        from .engine.live import LiveExecutor

        try:
            executor = LiveExecutor(config, host=args.clob_url or config.clob_url)
        except RuntimeError as exc:
            print(f"无法启动实盘模式：{exc}", file=sys.stderr)
            ledger.close()
            return 1
        print(
            "⚠️  实盘模式：会用真实资金在 Polymarket 下单，风控上限见配置。",
            file=sys.stderr,
        )
    else:
        executor = PaperExecutor(clob)

    engine = CopyEngine(
        config, clob=clob, ledger=ledger, executor=executor, notifier=notifier
    )
    engine.start()

    addresses = [t.address for t in config.targets]
    interval = config.watch.poll_interval
    if interval is None:
        interval = 60.0 if config.watch.stream else 10.0
    watcher = TradeWatcher(
        data_client, addresses,
        on_trade=engine.submit,
        poll_interval=interval,
        backfill=config.watch.backfill,
    )
    stream = None
    if config.watch.stream:
        stream = TradeStream(
            addresses,
            on_trade=watcher.ingest,
            on_gap=watcher.request_poll,
            ws_url=args.ws_url or config.ws_url,
        )
        stream.start()
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        print("正在停止跟单引擎……", file=sys.stderr)
    finally:
        if stream is not None:
            stream.stop()
        engine.stop()
        ledger.close()
    print("已停止。", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .engine.config import ConfigError, load_config
    from .engine.ledger import Ledger
    from .engine.risk import day_start_ts

    ledger_path = args.ledger
    if ledger_path is None:
        try:
            ledger_path = load_config(args.config).ledger_path
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 1
    ledger = Ledger(ledger_path)
    try:
        positions = ledger.positions()
        counts = ledger.signal_counts()
        total_pnl = ledger.realized_pnl_total()
        today_pnl = ledger.realized_pnl_since(day_start_ts())

        print(f"# 账本 {ledger_path}")
        print(
            f"信号统计: " + (
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "（暂无）"
            )
        )
        print(f"已实现盈亏: 累计 ${total_pnl:+.2f}，今日 ${today_pnl:+.2f}")
        print(f"\n## 当前持仓（{len(positions)} 个）")
        if not positions:
            print("（空仓）")
        for p in positions:
            print(
                f"  {p.size:>10.2f} 份 @ {p.avg_cost:.3f}  成本 ${p.cost:>8.2f}  "
                f"已实现 ${p.realized_pnl:+8.2f}  [{p.outcome}] {p.title}"
            )
        print(f"\n## 最近订单（最多 {args.limit} 条）")
        rows = ledger.recent_orders(args.limit)
        if not rows:
            print("（暂无订单）")
        for r in rows:
            print(
                f"  #{r['id']} {r['mode']} {r['side']} {r['req_size']:.2f}@≤{r['limit_price']:.3f}"
                f" → {r['status']} 成交 {r['filled_size']:.2f}@{r['avg_price']:.3f}"
                f" 滑点 {r['slippage']:+.3f} pnl {r['realized_pnl']:+.2f}"
                f"  [{r['outcome']}] {r['title']}"
            )
    finally:
        ledger.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true",
        help="按 JSON lines 输出，方便接下游程序",
    )
    common.add_argument(
        "--base-url", default=None,
        help="Data API 地址（默认官方接口，也可用环境变量 "
             "POLYCOPYCAT_DATA_API_URL 覆盖，便于走代理或本地测试）",
    )

    parser = argparse.ArgumentParser(
        prog="polycopycat",
        description="读取 / 监控其他地址在 Polymarket 上的下单（成交记录）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_trades = sub.add_parser(
        "trades", parents=[common],
        help="一次性读取某地址最近的成交（新→旧）",
    )
    p_trades.add_argument(
        "address", type=_address,
        help="目标地址，用 Polymarket 个人主页 URL 里的 0x 地址（proxy wallet）",
    )
    p_trades.add_argument(
        "--limit", type=int, default=20,
        help="最多读取多少条（默认 20，单页上限 500）",
    )
    p_trades.add_argument(
        "--include-maker", action="store_true",
        help="包含挂单侧成交（默认只看主动成交 takerOnly）",
    )
    p_trades.set_defaults(func=cmd_trades)

    p_watch = sub.add_parser(
        "watch", parents=[common],
        help="持续监控一个或多个地址的新成交",
    )
    p_watch.add_argument(
        "addresses", nargs="+", type=_address, metavar="address",
        help="要监控的地址，可以给多个",
    )
    p_watch.add_argument(
        "--interval", type=float, default=None,
        help="轮询间隔秒数（默认：纯轮询模式 10；--stream 模式下轮询只是兜底对账，默认 60）",
    )
    p_watch.add_argument(
        "--backfill", type=int, default=0,
        help="启动时先回放每个地址最近 N 条历史成交（默认 0，只看新的）",
    )
    p_watch.add_argument(
        "--stream", action="store_true",
        help="启用实时推送（WebSocket）：新成交秒内到达，轮询自动降级为兜底对账",
    )
    p_watch.add_argument(
        "--ws-url", default=None,
        help="实时推送地址（默认官方 wss://ws-live-data.polymarket.com，"
             "也可用环境变量 POLYCOPYCAT_WS_URL 覆盖）",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_run = sub.add_parser(
        "run", parents=[common],
        help="启动跟单引擎（纸面模拟或实盘，由配置文件决定）",
    )
    p_run.add_argument(
        "--config", required=True,
        help="引擎配置文件路径（可从 config.example.json 复制修改）",
    )
    p_run.add_argument(
        "--paper", action="store_true",
        help="强制纸面模式（覆盖配置里的 mode，实盘前的保险丝）",
    )
    p_run.add_argument(
        "--ws-url", default=None,
        help="实时推送地址（默认官方，也可用环境变量 POLYCOPYCAT_WS_URL 覆盖）",
    )
    p_run.add_argument(
        "--clob-url", default=None,
        help="CLOB 地址（默认官方 clob.polymarket.com，"
             "也可用环境变量 POLYCOPYCAT_CLOB_URL 覆盖）",
    )
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser(
        "report", parents=[common],
        help="查看跟单账本：持仓、盈亏、最近订单",
    )
    p_report.add_argument("--config", default="copycat.json", help="引擎配置文件路径")
    p_report.add_argument("--ledger", default=None, help="直接指定账本 sqlite 路径（优先于 --config）")
    p_report.add_argument("--limit", type=int, default=20, help="最近订单条数（默认 20）")
    p_report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except DataApiError as exc:
        print(f"请求 Polymarket Data API 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
