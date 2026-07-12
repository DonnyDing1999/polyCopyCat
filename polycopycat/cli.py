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
import time

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


def cmd_scout(args: argparse.Namespace) -> int:
    from .scout import (
        ScoutConfig,
        ScoutError,
        candidates_from_leaderboard,
        candidates_from_recent_trades,
        scout_addresses,
        targets_snippet,
    )

    client = DataApiClient(base_url=args.base_url)
    candidates: list[str] = list(args.addresses)
    if args.from_leaderboard:
        try:
            found = candidates_from_leaderboard(
                args.leaderboard_url, window=args.window, limit=args.candidates
            )
            print(f"排行榜候选 {len(found)} 个（窗口 {args.window}）", file=sys.stderr)
            candidates.extend(found)
        except ScoutError as exc:
            print(f"排行榜不可用，跳过该来源：{exc}", file=sys.stderr)
    if args.from_firehose or not candidates:
        if not args.from_firehose:
            print("未指定候选来源，默认从全站最近成交里挖活跃地址", file=sys.stderr)
        found = candidates_from_recent_trades(client, top=args.candidates)
        print(f"全站成交流候选 {len(found)} 个", file=sys.stderr)
        candidates.extend(found)

    unique: dict[str, None] = {}
    for address in candidates:
        unique.setdefault(address, None)
    candidates = list(unique)[: args.candidates]
    if not candidates:
        print("没有可评估的候选地址", file=sys.stderr)
        return 1

    config = ScoutConfig(
        min_trades=args.min_trades,
        min_notional_usdc=args.min_notional,
        min_win_rate=args.min_win_rate,
    )
    print(
        f"开始评估 {len(candidates)} 个地址（每个拉最近 {args.pages} 页成交带 + 当前持仓）……",
        file=sys.stderr,
    )
    verdicts = scout_addresses(
        client, candidates, config=config, pages=args.pages,
        progress=lambda a, i, n: print(f"  [{i}/{n}] {_short(a)}", file=sys.stderr),
    )

    if args.json:
        for v in verdicts:
            print(json.dumps(v.to_dict(), ensure_ascii=False), flush=True)
    else:
        for rank, v in enumerate(verdicts, 1):
            s = v.stats
            if v.eligible and s is not None:
                win = f"{s.win_rate:.0%} ({s.wins}/{s.matched_sells})" if s.win_rate is not None else "未知(纯持有)"
                idle_h = max(0.0, (time.time() - s.last_ts) / 3600) if s.last_ts else float("inf")
                print(
                    f"{rank:>3}. {_short(v.address)}  分 {v.score:>5.1f}  合格  "
                    f"回放盈亏 ${s.realized_pnl:>+10,.2f}  胜率 {win}  "
                    f"市场 {s.n_markets}  笔均 ${s.avg_trade_usdc:,.0f}  "
                    f"持仓成本 ${v.exposure_usdc:,.0f}  最近活跃 {idle_h:.1f}h 前"
                )
            else:
                print(f"{rank:>3}. {_short(v.address)}  排除  {'；'.join(v.reasons)}")
    eligible_n = sum(1 for v in verdicts if v.eligible)
    print(
        f"\n合格 {eligible_n} / {len(verdicts)}。提醒：回放窗口有限（每页≤500笔），"
        "历史盈利不代表未来，正式跟单前先用纸面模式验证。",
        file=sys.stderr,
    )
    if args.targets_snippet:
        if eligible_n:
            print("\n# 可直接并入 copycat.json 的 targets 段（自行调整 ratio/限额）：")
            print(targets_snippet(verdicts, top=args.top))
        else:
            print("没有合格地址，不生成 targets 片段", file=sys.stderr)
    return 0


def cmd_arb_scan(args: argparse.Namespace) -> int:
    from ._http import HttpError
    from .arb import ArbScanner
    from .engine.clob import ClobReadClient

    clob = ClobReadClient(base_url=args.clob_url)
    scanner = ArbScanner(clob, gamma_url=args.gamma_url)
    print(
        f"扫描前 {args.max_markets} 个活跃市场（按 24h 成交量），"
        f"最小边际 {args.min_edge}，最小可锁定利润 ${args.min_profit}……",
        file=sys.stderr,
    )
    try:
        opportunities = scanner.scan(
            max_markets=args.max_markets,
            min_edge=args.min_edge,
            min_profit=args.min_profit,
        )
    except HttpError as exc:
        print(f"扫描失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        for opp in opportunities:
            print(json.dumps(opp.to_dict(), ensure_ascii=False), flush=True)
    else:
        if not opportunities:
            print("本轮快照没有达到阈值的套利机会（正常：这类机会存活时间极短）")
        for i, opp in enumerate(opportunities, 1):
            kind = "买对冲" if opp.kind == "buy_pair" else "铸造卖出(需链上split)"
            flag = " [negRisk]" if opp.neg_risk else ""
            print(
                f"{i:>3}. {kind}  边际 ${opp.edge_per_pair:.3f}/对 × {opp.max_pairs:,.0f} 对"
                f" ≈ ${opp.profit_usdc:,.2f}  Yes {opp.price_yes:.3f} + No {opp.price_no:.3f}"
                f"{flag}  {opp.question}"
            )
    print(
        f"\n共 {len(opportunities)} 个机会（顶档深度口径）。提醒：这是一帧快照，"
        "等你手动下单时大概率已被吃掉；执行版有单腿成交风险，需另行实现。",
        file=sys.stderr,
    )
    return 0


def cmd_xarb_scan(args: argparse.Namespace) -> int:
    from ._http import HttpError
    from .engine.clob import ClobReadClient
    from .kalshi import KalshiClient
    from .xarb import PairsError, XarbScanner, load_pairs

    clob = ClobReadClient(base_url=args.clob_url)
    kalshi = KalshiClient(base_url=args.kalshi_url)
    scanner = XarbScanner(clob, kalshi, gamma_url=args.gamma_url)
    suggest_mode = args.suggest or not args.pairs
    try:
        if args.pairs and args.loop:
            # 比赛中本地循环监控：目录缓存一次，每轮只拉两所订单簿
            pairs = load_pairs(args.pairs)
            poly_cache = scanner.poly_market_index()
            print(
                f"循环监控 {len(pairs)} 条配对，每 {args.loop:.0f}s 一轮，"
                "只打印达到阈值的机会（Ctrl-C 停止）……",
                file=sys.stderr,
            )
            try:
                while True:
                    started = time.monotonic()
                    opportunities = scanner.scan_pairs(
                        pairs, min_edge=args.min_edge, min_profit=args.min_profit,
                        poly_markets=poly_cache,
                    )
                    stamp = time.strftime("%H:%M:%S")
                    if opportunities:
                        for opp in opportunities:
                            line = (
                                json.dumps(opp.to_dict(), ensure_ascii=False)
                                if args.json else
                                f"[{stamp}] 🎯 {opp.combo}  边际 ${opp.edge_per_pair:.3f}/对 × "
                                f"{opp.max_pairs:,.0f} ≈ ${opp.profit_usdc:,.2f}  "
                                f"{opp.poly_question} ↔ {opp.kalshi_ticker}"
                            )
                            print(line, flush=True)
                    else:
                        print(f"[{stamp}] 无达标价差", file=sys.stderr, flush=True)
                    elapsed = time.monotonic() - started
                    time.sleep(max(0.0, args.loop - elapsed))
            except KeyboardInterrupt:
                print("已停止监控。", file=sys.stderr)
            return 0

        if args.pairs:
            pairs = load_pairs(args.pairs)
            print(f"对 {len(pairs)} 条已确认配对算价差（Kalshi 手续费已计入）……", file=sys.stderr)
            diagnostics: list = []
            opportunities = scanner.scan_pairs(
                pairs, min_edge=args.min_edge, min_profit=args.min_profit,
                diagnostics_out=diagnostics,
            )
            if args.json:
                for opp in opportunities:
                    print(json.dumps(opp.to_dict(), ensure_ascii=False), flush=True)
                for diag in diagnostics:
                    print(json.dumps({"diagnostic": True, **diag}, ensure_ascii=False), flush=True)
            else:
                if not opportunities:
                    print("已确认配对中当前没有达到阈值的跨所价差")
                for i, opp in enumerate(opportunities, 1):
                    print(
                        f"{i:>3}. {opp.combo}  边际 ${opp.edge_per_pair:.3f}/对 × "
                        f"{opp.max_pairs:,.0f} ≈ ${opp.profit_usdc:,.2f}  "
                        f"Poly {opp.poly_price:.3f} + Kalshi {opp.kalshi_price:.3f} "
                        f"(费 {opp.kalshi_fee:.3f})  {opp.poly_question} ↔ {opp.kalshi_ticker}"
                    )
                if diagnostics:
                    print("\n—— 各配对当前最优组合（负边际 = 无套利，差多少一目了然）——")
                    diagnostics.sort(
                        key=lambda d: -(d["edge_per_pair"] if d["edge_per_pair"] is not None else -9)
                    )
                    for d in diagnostics:
                        if d["edge_per_pair"] is None:
                            print(f"  [无报价] {d['poly_question']} ↔ {d['kalshi_ticker']}  {d.get('detail','')}")
                            continue
                        print(
                            f"  边际 {d['edge_per_pair']:+.3f}（两腿成本 {d['sum_cost']:.3f}，"
                            f"Poly {d['poly_price']:.3f} + Kalshi {d['kalshi_price']:.3f} + 费 {d['kalshi_fee']:.3f}，"
                            f"深度 {d['depth']:,.0f}）  {d['poly_question']} ↔ {d['kalshi_ticker']}"
                        )
        if suggest_mode:
            if not args.pairs:
                print("未指定 --pairs，进入候选配对建议模式", file=sys.stderr)
            # 用低地板拉全量打分，展示时再按 --min-score 分档，
            # 这样零达标时也能看到"最接近的候选"作校准参考
            scored = scanner.suggest_pairs(
                max_poly=args.max_markets, max_kalshi=args.max_kalshi,
                min_score=0.15, top=max(args.top, 10),
                kalshi_series=args.kalshi_series, query=args.query,
                event_probe=args.event_probe,
            )
            qualified = [s for s in scored if s["score"] >= args.min_score][: args.top]
            near_misses = [s for s in scored if s["score"] < args.min_score][:10]

            def _print_suggestion(i, s, mark=""):
                gap = (
                    f"截止差 {s['close_gap_days']} 天"
                    if s["close_gap_days"] is not None else "截止差未知"
                )
                kind = "结构化" if s.get("match_type") == "structured" else "文本"
                print(f"{i:>3}. {mark}{kind}匹配 {s['score']:.2f}（{gap}）")
                print(f"     Poly:   {s['poly_question']}  [{s['poly_condition_id']}]")
                print(f"     Kalshi: {s['kalshi_title']}  [{s['kalshi_ticker']}]")

            if args.json:
                for s in qualified:
                    print(json.dumps(s, ensure_ascii=False), flush=True)
            else:
                if not qualified:
                    print(f"没有相似度 ≥ {args.min_score} 的候选配对")
                for i, s in enumerate(qualified, 1):
                    _print_suggestion(i, s)
                if not qualified and near_misses:
                    print(f"\n—— 未达标的最接近候选（仅供校准阈值参考）——")
                    for i, s in enumerate(near_misses, 1):
                        _print_suggestion(i, s, mark="[未达标] ")
            print(
                "\n⚠️ 候选只是文本相似：两边结算条款（数据源/截止时间/措辞）必须人工"
                "核对等价后，才能写进配对文件用于价差扫描——配错对不是套利，是双边敞口。",
                file=sys.stderr,
            )
    except (HttpError, PairsError) as exc:
        print(f"跨所扫描失败：{exc}", file=sys.stderr)
        return 1
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
    own_address = None
    if config.mode == "live":
        from .engine.live import LiveExecutor, own_trading_address

        try:
            executor = LiveExecutor(config, host=args.clob_url or config.clob_url)
        except RuntimeError as exc:
            print(f"无法启动实盘模式：{exc}", file=sys.stderr)
            ledger.close()
            return 1
        own_address = own_trading_address(config)
        print(
            "⚠️  实盘模式：会用真实资金在 Polymarket 下单，风控上限见配置。",
            file=sys.stderr,
        )
    else:
        executor = PaperExecutor(clob)

    engine = CopyEngine(
        config, clob=clob, ledger=ledger, executor=executor, notifier=notifier,
        data_client=data_client, own_address=own_address,
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
        "-v", "--verbose", action="store_true",
        help="输出调试日志（放在子命令前后均可）",
    )
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

    p_scout = sub.add_parser(
        "scout", parents=[common],
        help="寻找值得跟单的地址：回放公开成交带评估战绩，排除做市/亏损地址",
    )
    p_scout.add_argument(
        "addresses", nargs="*", type=_address, metavar="address",
        help="直接指定要评估的候选地址（可与来源开关混用）",
    )
    p_scout.add_argument(
        "--from-firehose", action="store_true",
        help="从全站最近成交里挖活跃地址作为候选（不给任何来源时的默认）",
    )
    p_scout.add_argument(
        "--from-leaderboard", action="store_true",
        help="从官方排行榜取候选（接口非正式文档，不可用时自动跳过）",
    )
    p_scout.add_argument(
        "--leaderboard-url", default=None,
        help="排行榜接口地址（默认 lb-api.polymarket.com，"
             "可用环境变量 POLYCOPYCAT_LB_URL 覆盖）",
    )
    p_scout.add_argument(
        "--window", default="30d", choices=["1d", "7d", "30d", "all"],
        help="排行榜统计窗口（默认 30d）",
    )
    p_scout.add_argument("--candidates", type=int, default=40,
                         help="最多评估多少个候选（默认 40）")
    p_scout.add_argument("--pages", type=int, default=1,
                         help="每个地址回放几页成交带（每页 500 笔，默认 1）")
    p_scout.add_argument("--top", type=int, default=5,
                         help="--targets-snippet 输出前几名（默认 5）")
    p_scout.add_argument("--min-trades", type=int, default=20,
                         help="样本下限：窗口内最少成交笔数（默认 20）")
    p_scout.add_argument("--min-notional", type=float, default=2000.0,
                         help="窗口内总成交额下限 USDC（默认 2000）")
    p_scout.add_argument("--min-win-rate", type=float, default=0.5,
                         help="胜率下限 0~1（默认 0.5；主要看胜率时可调高）")
    p_scout.add_argument(
        "--targets-snippet", action="store_true",
        help="额外输出可直接并入 copycat.json 的 targets 配置段",
    )
    p_scout.set_defaults(func=cmd_scout)

    p_arb = sub.add_parser(
        "arb-scan", parents=[common],
        help="扫描互补对套利机会：ask(Yes)+ask(No)<$1 等（只读研究工具，不下单）",
    )
    p_arb.add_argument("--max-markets", type=int, default=500,
                       help="扫描前多少个活跃市场，按 24h 成交量降序（默认 500）")
    p_arb.add_argument("--min-edge", type=float, default=0.005,
                       help="每对最小价差，价格量纲（默认 0.005 = 半分钱）")
    p_arb.add_argument("--min-profit", type=float, default=0.5,
                       help="顶档深度下最小可锁定利润 USDC（默认 0.5）")
    p_arb.add_argument("--gamma-url", default=None,
                       help="市场目录接口（默认 gamma-api.polymarket.com，"
                            "可用环境变量 POLYCOPYCAT_GAMMA_URL 覆盖）")
    p_arb.add_argument("--clob-url", default=None,
                       help="CLOB 地址（默认官方，环境变量 POLYCOPYCAT_CLOB_URL）")
    p_arb.set_defaults(func=cmd_arb_scan)

    p_xarb = sub.add_parser(
        "xarb-scan", parents=[common],
        help="Kalshi × Polymarket 跨所套利：提名候选配对 / 对已确认配对算价差（只读）",
    )
    p_xarb.add_argument("--pairs", default=None,
                        help="人工确认过的配对文件（见 xarb-pairs.example.json）；不给则进入建议模式")
    p_xarb.add_argument("--suggest", action="store_true",
                        help="输出候选配对建议（可与 --pairs 同时用）")
    p_xarb.add_argument("--max-markets", type=int, default=300,
                        help="建议模式下 Polymarket 候选池大小（默认 300）")
    p_xarb.add_argument("--max-kalshi", type=int, default=2000,
                        help="建议模式下 Kalshi 候选池大小（默认 2000）")
    p_xarb.add_argument("--kalshi-series", default=None,
                        help="只在指定 Kalshi 系列内找配对（如 KXBTCD），縮小池子提高信噪比")
    p_xarb.add_argument("--query", nargs="+", default=None, metavar="词",
                        help="建议模式：只配对含任一关键词的市场/事件（如 --query france cup）")
    p_xarb.add_argument("--event-probe", type=int, default=40,
                        help="建议模式：粗配后拉取多少个 Kalshi 事件做精配（默认 40）")
    p_xarb.add_argument("--loop", type=float, default=None, metavar="秒",
                        help="配对模式：本地循环监控间隔秒数（比赛中用，CI 太慢；只打印达标机会）")
    p_xarb.add_argument("--min-score", type=float, default=0.5,
                        help="建议模式的标题相似度阈值 0~1（默认 0.5）")
    p_xarb.add_argument("--top", type=int, default=20, help="建议条数上限（默认 20）")
    p_xarb.add_argument("--min-edge", type=float, default=0.01,
                        help="配对扫描：每对最小已扣费边际（默认 0.01）")
    p_xarb.add_argument("--min-profit", type=float, default=1.0,
                        help="配对扫描：顶档深度下最小可锁定利润 USDC（默认 1）")
    p_xarb.add_argument("--kalshi-url", default=None,
                        help="Kalshi 接口（默认 api.elections.kalshi.com/trade-api/v2，"
                             "环境变量 POLYCOPYCAT_KALSHI_URL）")
    p_xarb.add_argument("--gamma-url", default=None,
                        help="Gamma 市场目录（环境变量 POLYCOPYCAT_GAMMA_URL）")
    p_xarb.add_argument("--clob-url", default=None,
                        help="CLOB 地址（环境变量 POLYCOPYCAT_CLOB_URL）")
    p_xarb.set_defaults(func=cmd_xarb_scan)

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
