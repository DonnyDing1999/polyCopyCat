"""命令行入口：读取 / 监控其他地址在 Polymarket 的下单。

用法示例::

    polycopycat trades 0x地址 --limit 20
    polycopycat watch 0x地址A 0x地址B --interval 10 --backfill 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade
from .watcher import TradeWatcher


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
    if as_json:
        print(json.dumps(trade.to_dict(), ensure_ascii=False), flush=True)
    else:
        print(_format_trade(trade), flush=True)


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
    watcher = TradeWatcher(
        client,
        args.addresses,
        on_trade=lambda trade: _emit(trade, args.json),
        poll_interval=args.interval,
        backfill=args.backfill,
    )
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        print("已停止监控。", file=sys.stderr)
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
        "--interval", type=float, default=10.0,
        help="轮询间隔秒数（默认 10）",
    )
    p_watch.add_argument(
        "--backfill", type=int, default=0,
        help="启动时先回放每个地址最近 N 条历史成交（默认 0，只看新的）",
    )
    p_watch.set_defaults(func=cmd_watch)
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
