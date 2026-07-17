"""命令行入口：读取 / 监控其他地址在 Polymarket 的下单，以及跟单引擎。

用法示例::

    polycopycat trades 0x地址 --limit 20
    polycopycat watch 0x地址A 0x地址B --interval 10 --backfill 5
    polycopycat watch 0x地址A --stream        # 实时推送，秒内跟到新下单
    polycopycat run --config copycat.json     # 跟单引擎（纸面/实盘由配置决定）
    polycopycat report --config copycat.json  # 查看持仓与盈亏
    polycopycat us match "btc above 100k" --quote  # Polymarket US（美国站）行情对照
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from . import __version__
from ._http import HttpError
from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade
from .watcher import TradeWatcher

logger = logging.getLogger(__name__)

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


def _preflight_lines(
    data_ok: bool, data_msg: str, clob_ok: bool, clob_msg: str
) -> list[str]:
    """把自检结果格式化成给用户看的行（纯函数，便于测试）。"""
    lines = [
        "启动自检：探测依赖接口……",
        f"  Data API: {'✓ ' if data_ok else '✗ '}{data_msg}",
        f"  CLOB:     {'✓ ' if clob_ok else '✗ '}{clob_msg}",
    ]
    if not clob_ok:
        lines.append(
            "⚠️  CLOB 不可达 → 引擎拿不到市场元数据/订单簿，任何信号都无法执行"
            "（会记为 error）。"
        )
        lines.append(
            "    多为公司网关/地区策略封了 clob.polymarket.com；"
            "换云服务器或不拦截的网络（手机热点）再跑。"
        )
    if not data_ok:
        lines.append("⚠️  Data API 不可达 → 拿不到成交/持仓，监控与跟单都起不来。")
    if data_ok and clob_ok:
        lines.append("  依赖接口就绪。实时流状态见后续日志「实时成交流已连接」。")
    return lines


def run_preflight(data_client, clob) -> tuple[bool, bool]:
    """探测 Data API 与 CLOB，打印自检结果，返回 (data_ok, clob_ok)。"""
    data_ok, data_msg = data_client.ping()
    clob_ok, clob_msg = clob.ping()
    for line in _preflight_lines(data_ok, data_msg, clob_ok, clob_msg):
        print(line, file=sys.stderr)
    return data_ok, clob_ok


def cmd_run(args: argparse.Namespace) -> int:
    from .engine.clob import ClobReadClient
    from .engine.config import ConfigError, load_config
    from .engine.engine import CopyEngine, merge_recruited_targets
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
    if not args.skip_preflight:
        run_preflight(data_client, clob)
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

    restored = merge_recruited_targets(config)
    if restored:
        print(f"已并回 {len(restored)} 个历史自动招募目标", file=sys.stderr)

    def _follow_new_target(address: str) -> None:
        # 招募发生在发现线程里，此时 watcher/stream 早已建好（首轮发现距启动 ≥1h）
        watcher.add_address(address)
        if stream is not None:
            stream.add_address(address)

    engine = CopyEngine(
        config, clob=clob, ledger=ledger, executor=executor, notifier=notifier,
        data_client=data_client, own_address=own_address,
        on_new_target=_follow_new_target,
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

    report_config = None
    ledger_path = args.ledger
    if ledger_path is None:
        try:
            report_config = load_config(args.config)
            ledger_path = report_config.ledger_path
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

        marks = _mark_positions(positions, args) if args.mark else {}
        print(f"\n## 当前持仓（{len(positions)} 个）")
        if not positions:
            print("（空仓）")
        tot_cost = tot_val = 0.0
        for p in positions:
            label = p.title or "(标题缺失)"
            line = (
                f"  {p.size:>10.2f} 份 @ {p.avg_cost:.3f}  成本 ${p.cost:>8.2f}  "
                f"已实现 ${p.realized_pnl:+8.2f}"
            )
            if p.token_id in marks:
                bid = marks[p.token_id]
                val = p.size * bid
                tot_cost += p.cost
                tot_val += val
                line += f"  现价 {bid:.3f} 市值 ${val:>7.2f} 浮盈亏 ${val - p.cost:+7.2f}"
            line += f"  [{p.outcome}] {label}"
            print(line)
        if marks:
            unrealized = tot_val - tot_cost
            print(
                f"  ── 持仓合计: 成本 ${tot_cost:.2f} 市值 ${tot_val:.2f} "
                f"未实现浮盈亏 ${unrealized:+.2f}"
            )
            print(f"  ── 纸面总盈亏（已实现 + 未实现）: ${total_pnl + unrealized:+.2f}")
        quality = ledger.execution_quality()
        if quality.n_fills:
            print(f"\n## 执行质量（{quality.n_fills} 笔成交）")
            print(
                f"  信号→成交延迟: 中位 {quality.median_delay_s:.1f}s / "
                f"均值 {quality.avg_delay_s:.1f}s / 最大 {quality.max_delay_s:.0f}s"
            )
            print(
                f"  跟入价 vs 目标价: 平均 {quality.avg_price_gap:+.4f}（正=比目标差），"
                f"延迟+滑点合计成本 ${quality.slippage_cost:.2f}"
            )
            print(
                f"  全额成交 {quality.full_fills}/{quality.n_fills}，"
                f"重试后成交 {quality.retried_fills} 笔"
            )

        _print_signal_flow(ledger, quality)
        if report_config is not None:
            _print_pool_status(ledger, report_config)

        if args.by_target:
            _print_by_target(ledger)

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


def _mark_positions(positions, args) -> dict[str, float]:
    """给每个持仓拉实时买一价（现在全平能拿回的价）做市值重估。

    单个 token 取不到盘口就跳过（该仓不参与浮盈亏合计，避免用 0 价虚增亏损）。
    """
    from .engine.clob import ClobError, ClobReadClient

    clob = ClobReadClient(base_url=args.clob_url)
    marks: dict[str, float] = {}
    for p in positions:
        try:
            book = clob.get_book(p.token_id)
        except ClobError as exc:
            logger.debug("持仓 %s 取盘口失败，跳过市值重估: %s", p.token_id[:12], exc)
            continue
        if book.bids:
            marks[p.token_id] = book.bids[0].price
    return marks


def _print_signal_flow(ledger, quality) -> None:
    """信号通道与被拦原因：哪条路在送信号、哪条路真正促成成交、拦下的都是什么。"""
    sources = ledger.signal_source_counts()
    reasons = ledger.filter_reason_stats(top=6)
    if not sources and not reasons:
        return
    print("\n## 信号通道与过滤")
    if sources:
        total = sum(sources.values())
        parts = " / ".join(
            f"{name} {n} 条" for name, n in sorted(sources.items(), key=lambda kv: -kv[1])
        )
        print(f"  信号来源（共 {total}）: {parts}")
    if quality.channels:
        parts = " / ".join(
            f"{ch.source} {ch.n_fills} 笔（中位 {ch.median_delay_s:.1f}s）"
            for ch in quality.channels
        )
        print(f"  成交经由: {parts}")
    if reasons:
        print("  被拦原因 Top:")
        for status, pattern, n in reasons:
            print(f"    {n:>4}× {status:<12} {pattern}")


def _print_pool_status(ledger, config) -> None:
    """池子状态：每个目标的来源（配置/招募）、当前状态与被停历史。"""
    import json as _json

    from .engine.engine import _recruited_path, merge_recruited_targets

    recruited_addresses: set[str] = set()
    recruited_file = _recruited_path(config)
    if recruited_file.exists():
        try:
            recruited_addresses = {
                str(e.get("address", "")).lower()
                for e in _json.loads(recruited_file.read_text(encoding="utf-8")) or []
                if isinstance(e, dict)
            }
        except (OSError, ValueError):
            pass
    merge_recruited_targets(config)
    events = ledger.target_event_summary()

    print(f"\n## 池子状态（{len(config.targets)} 个目标）")
    for target in config.targets:
        event = events.get(target.address, {})
        origin = "招募" if target.address in recruited_addresses else "配置"
        if target.paused:
            state = "⏸ 手动暂停"
        elif event.get("last_kind") == "health_pause":
            state = "⛔ 巡检暂停"
        else:
            state = "在跟"
        line = f"  {_short(target.address)}  {origin}  {state:<6} 被停 {event.get('pauses', 0)} 次"
        if event.get("last_kind") == "health_pause" and event.get("last_detail"):
            line += f"  {event['last_detail'][:44]}"
        print(line)
    print("  （状态按账本事件推断；引擎重启会临时复跟至下一轮巡检）")


def _print_by_target(ledger) -> None:
    """按目标归因：谁的纸面跟单真赚钱、谁的动作跟得上。"""
    reports, settle_pnl, settle_n = ledger.report_by_target()
    print(f"\n## 按目标归因（{len(reports)} 个目标）")
    if not reports:
        print("（暂无信号）")
    else:
        print(
            f"  {'目标':<14} {'已实现':>10}  {'累计买入':>10}  "
            f"{'执行/信号':>11}  {'跟单率':>6}  过滤/跳过/风控/轧差/无对手"
        )
        for t in reports:
            print(
                f"  {_short(t.target):<14} ${t.realized_pnl:>+9.2f}  ${t.bought_notional:>9.2f}  "
                f"{t.executed:>5}/{t.total_signals:<5}  {t.followable_ratio:>5.0%}  "
                f"{t.filtered}/{t.skipped}/{t.risk_blocked}/{t.netted}/{t.no_fill}"
            )
    if settle_n:
        print(
            f"\n  未按目标归属（市场结算入账）: ${settle_pnl:+.2f}（{settle_n} 笔 REDEEM）"
        )
    print(
        "  说明：卖出跟随平仓的盈亏已按目标归属；结算盈亏在持仓层入账"
        "（一个 token 可能多目标共建），不拆分到单个目标。"
    )


def _us_client(args: argparse.Namespace):
    from .us import UsApiClient

    return UsApiClient(base_url=args.us_url)


def _format_us_market(market) -> str:
    flags = []
    if not market.active:
        flags.append("inactive")
    if market.closed:
        flags.append("closed")
    suffix = f"  ({'/'.join(flags)})" if flags else ""
    event = (
        f"  · {market.event_title}"
        if market.event_title and market.event_title != market.title
        else ""
    )
    return (
        f"{market.slug:<44} [{market.outcome or '?'}] {market.title}{event}"
        f"  量 ${market.volume:,.0f}{suffix}"
    )


def cmd_us_markets(args: argparse.Namespace) -> int:
    client = _us_client(args)
    query = " ".join(args.query).strip()
    if query:
        status = None if args.include_closed else "active"
        markets = client.search_markets(query, status=status, limit=args.limit)[: args.limit]
    else:
        active = None if args.include_closed else True
        closed = None if args.include_closed else False
        markets = client.get_markets(limit=args.limit, active=active, closed=closed)
    for market in markets:
        print(
            json.dumps(market.to_dict(), ensure_ascii=False) if args.json
            else _format_us_market(market),
            flush=True,
        )
    if not markets:
        print("（没有匹配的市场）", file=sys.stderr)
    return 0


def cmd_us_book(args: argparse.Namespace) -> int:
    client = _us_client(args)
    book = client.get_book(args.slug)
    if args.json:
        print(json.dumps(book.to_dict(), ensure_ascii=False))
        return 0
    header = f"# {book.market_slug or args.slug}"
    if book.state:
        header += f"  状态 {book.state}"
    if book.last_trade_px:
        header += f"  最新成交 {book.last_trade_px:.3f}"
    print(header)
    for level in list(book.asks[: args.depth])[::-1]:
        print(f"  卖 {level.price:.3f} x {level.size:>10,.0f}")
    print("  " + "-" * 24)
    for level in book.bids[: args.depth]:
        print(f"  买 {level.price:.3f} x {level.size:>10,.0f}")
    if not book.bids and not book.asks:
        print("（订单簿为空）", file=sys.stderr)
    return 0


def cmd_us_bbo(args: argparse.Namespace) -> int:
    client = _us_client(args)
    bbo = client.get_bbo(args.slug)
    if args.json:
        print(json.dumps(bbo.to_dict(), ensure_ascii=False))
        return 0
    spread = f"{bbo.spread:.3f}" if bbo.spread is not None else "?"
    print(
        f"{bbo.market_slug or args.slug}  "
        f"买一 {bbo.best_bid:.3f}(挂 {bbo.bid_depth}) / 卖一 {bbo.best_ask:.3f}(挂 {bbo.ask_depth})  "
        f"价差 {spread}  最新 {bbo.last_trade_px:.3f}  "
        f"已成交 {bbo.shares_traded:,.0f} 份  未平仓 {bbo.open_interest:,.0f}"
    )
    return 0


def cmd_us_match(args: argparse.Namespace) -> int:
    from .us import UsApiError, match_us_markets

    client = _us_client(args)
    text = " ".join(args.text)
    matches = match_us_markets(client, text, outcome=args.outcome, top=args.top)
    if not matches:
        print("没有找到候选市场（可以换个说法或减少关键词再试）", file=sys.stderr)
        return 1
    for rank, match in enumerate(matches, 1):
        bbo = None
        if args.quote:
            try:
                bbo = client.get_bbo(match.market.slug)
            except UsApiError as exc:
                print(f"（{match.market.slug} 报价获取失败：{exc}）", file=sys.stderr)
        if args.json:
            data = match.to_dict()
            if bbo is not None:
                data["bbo"] = bbo.to_dict()
            print(json.dumps(data, ensure_ascii=False), flush=True)
        else:
            quote = (
                f"  买 {bbo.best_bid:.3f} / 卖 {bbo.best_ask:.3f}" if bbo is not None else ""
            )
            print(f"{rank:>3}. {match.score:>5.1f}分  {_format_us_market(match.market)}{quote}")
    print(
        "\n提醒：分数只是词面相似度排序，两站市场的口径、结算规则可能不同，下单前先人工确认。",
        file=sys.stderr,
    )
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
        "--skip-preflight", action="store_true",
        help="跳过启动自检（默认会先探 Data API / CLOB 是否可达）",
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
    p_report.add_argument(
        "--mark", action="store_true",
        help="拉实时买一价给持仓做市值重估，显示浮盈亏与纸面总盈亏（需要网络）",
    )
    p_report.add_argument(
        "--clob-url", default=None, help="CLOB 入口（--mark 用；默认官方/环境变量）",
    )
    p_report.add_argument(
        "--by-target", action="store_true",
        help="按目标拆分：每个目标的已实现盈亏、累计买入、信号归属（评估谁值得跟）",
    )
    p_report.set_defaults(func=cmd_report)

    p_us = sub.add_parser(
        "us",
        help="Polymarket US（美国合规站）：只读行情与主站市场匹配",
    )
    us_common = argparse.ArgumentParser(add_help=False)
    us_common.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出调试日志（放在子命令前后均可）",
    )
    us_common.add_argument(
        "--json", action="store_true",
        help="按 JSON lines 输出，方便接下游程序",
    )
    us_common.add_argument(
        "--us-url", default=None,
        help="Polymarket US gateway 地址（默认官方 gateway.polymarket.us，"
             "也可用环境变量 POLYCOPYCAT_US_URL 覆盖）",
    )
    us_sub = p_us.add_subparsers(dest="us_command", required=True)

    p_us_markets = us_sub.add_parser(
        "markets", parents=[us_common],
        help="列出或搜索 US 站市场",
    )
    p_us_markets.add_argument(
        "query", nargs="*",
        help="搜索关键词；留空则列出活跃市场",
    )
    p_us_markets.add_argument("--limit", type=int, default=20, help="最多输出多少个（默认 20）")
    p_us_markets.add_argument(
        "--include-closed", action="store_true",
        help="包含已关闭/未激活的市场（默认只看活跃）",
    )
    p_us_markets.set_defaults(func=cmd_us_markets)

    p_us_book = us_sub.add_parser("book", parents=[us_common], help="查看某市场订单簿")
    p_us_book.add_argument("slug", help="市场 slug（用 us markets / us match 查）")
    p_us_book.add_argument("--depth", type=int, default=10, help="每侧显示几档（默认 10）")
    p_us_book.set_defaults(func=cmd_us_book)

    p_us_bbo = us_sub.add_parser("bbo", parents=[us_common], help="查看某市场最优买卖价")
    p_us_bbo.add_argument("slug", help="市场 slug")
    p_us_bbo.set_defaults(func=cmd_us_bbo)

    p_us_match = us_sub.add_parser(
        "match", parents=[us_common],
        help="把主站市场（标题或 slug）匹配到 US 站对应市场",
    )
    p_us_match.add_argument(
        "text", nargs="+",
        help="主站市场标题、slug 或关键词（slug 会自动拆词）",
    )
    p_us_match.add_argument(
        "--outcome", default=None,
        help="结果名（如 Yes / No / 队名），参与打分",
    )
    p_us_match.add_argument("--top", type=int, default=5, help="输出前几名（默认 5）")
    p_us_match.add_argument(
        "--quote", action="store_true",
        help="同时拉取每个候选的最优买卖价（每个候选多一次请求）",
    )
    p_us_match.set_defaults(func=cmd_us_match)
    return parser


def use_os_trust_store() -> bool:
    """让 Python 用操作系统的信任库（而不是 certifi 自带的 Mozilla 根）。

    公司网络常做 TLS 中间人：代理换成自己的证书，签发它的公司根 CA 装在
    系统钥匙串里（所以 Safari/Chrome 能打开），但 requests/websocket 默认走
    certifi，看不到公司根，于是 CERTIFICATE_VERIFY_FAILED。装了 truststore
    就把 SSL 默认上下文切到系统信任库，requests 和 websocket 一起生效。

    未安装 truststore 时静默跳过（返回 False），不影响正常网络环境。
    可用 POLYCOPYCAT_NO_TRUSTSTORE=1 显式关闭。
    """
    import os as _os

    if _os.environ.get("POLYCOPYCAT_NO_TRUSTSTORE"):
        return False
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    logger.debug("已启用系统信任库（truststore），走 OS 证书校验")
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    use_os_trust_store()  # 公司 TLS 中间人环境下改用系统信任库，见函数说明
    try:
        return args.func(args)
    except DataApiError as exc:
        print(f"请求 Polymarket Data API 失败：{exc}", file=sys.stderr)
        return 1
    except HttpError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
