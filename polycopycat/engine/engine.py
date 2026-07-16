"""跟单引擎主体：消费信号队列，串起过滤 → 聚合 → 计算 → 风控 → 执行 → 记账 → 通知。

引擎跑在自己的线程里，watcher/stream 线程只往队列里塞信号，
慢速的 CLOB 请求不会拖住行情监控。任何一笔信号处理抛异常都只
影响该笔（记为 error），引擎线程本身永不退出。

M3 聚合：收到首笔信号后先等一个窗口（aggregate.window_s，默认 2s），
把窗口内「同目标 + 同 token + 同方向」的碎片成交合并成一笔等效成交
再走后续管道；窗口内多目标对同一 token 的反向信号可轧差，只执行
净头寸。窗口设 0 即退化为逐笔跟单。
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time

from ..data_api import DataApiClient, DataApiError
from ..models import Trade
from .aggregate import MergedGroup, PendingSignal, group_key, merge_pending
from .clob import ClobError, ClobReadClient, MarketInfo
from .config import EngineConfig
from .executor import ExecutionResult
from .ledger import Ledger
from .mirror import TargetMirror
from .notify import Notifier
from .risk import RiskGate
from .signals import OrderIntent, Signal, SignalFilter
from .sizing import SIZE_STEP, floor_to, plan_buy, sell_limit_price

logger = logging.getLogger(__name__)

_STOP = object()


def _short(text: str) -> str:
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


@dataclasses.dataclass
class _Planned:
    """完成仓位计算、等待风控与执行的一组信号。"""

    group: MergedGroup
    intent: OrderIntent
    market: MarketInfo


class CopyEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        clob: ClobReadClient,
        ledger: Ledger,
        executor,
        notifier: Notifier,
        mirror: TargetMirror | None = None,
        data_client: DataApiClient | None = None,
        own_address: str | None = None,
    ) -> None:
        self.config = config
        self._clob = clob
        self._ledger = ledger
        self._executor = executor
        self._notifier = notifier
        self._mirror = mirror if mirror is not None else TargetMirror()
        self._data = data_client   # 对账数据源；None 则不启动对账线程
        self._own_address = own_address  # 实盘时自己的资金地址，用于持仓对账
        self._filter = SignalFilter(config.filters)
        self._risk = RiskGate(config.risk, ledger)
        self._targets = {t.address: t for t in config.targets}
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()
        self._redeem_notified: set[str] = set()

    # ---- 对外接口 ----

    def submit(self, trade: Trade) -> None:
        """接收一笔目标成交（作为 watcher 的 on_trade 回调，多线程安全）。"""
        target = self._targets.get(trade.proxy_wallet)
        if target is None:
            return
        self._queue.put(Signal(trade=trade, target=target, received_at=time.time()))

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("引擎已经启动过")
        if self._data is not None:
            logger.info("正在初始化目标持仓镜像……")
            self.reconcile_once()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop, name="polycopycat-reconcile", daemon=True
            )
            self._reconcile_thread.start()
        self._thread = threading.Thread(target=self._loop, name="polycopycat-engine", daemon=True)
        self._thread.start()
        logger.info(
            "跟单引擎已启动（%s 模式，%d 个目标，滑点上限 %.3f，聚合窗口 %.1fs）",
            self.config.mode, len(self._targets), self.config.execution.slippage_cap,
            self.config.aggregate.window_s,
        )

    def stop(self, timeout: float = 30.0) -> None:
        """处理完队列中已有信号后停止。"""
        self._reconcile_stop.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5)
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)

    def drain(self) -> None:
        """等队列清空（测试与验证用）。"""
        self._queue.join()

    # ---- 主循环 ----

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            batch = [item]
            stop_after = self._collect_window(batch)
            try:
                self._process_batch(batch)
            except Exception:  # noqa: BLE001 —— 一批失败不能拖垮引擎
                logger.exception("处理信号批失败（%d 笔）", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stop_after:
                self._queue.task_done()  # 对应 _STOP 那次 get
                return

    def _collect_window(self, batch: list) -> bool:
        """聚合窗口内继续攒信号；遇到 _STOP 返回 True（处理完当前批后退出）。"""
        window = self.config.aggregate.window_s
        if window <= 0:
            return False
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                return False
            if item is _STOP:
                return True
            batch.append(item)

    def _process(self, signal: Signal) -> None:
        """处理单笔信号（等价于长度为 1 的批；测试与旧路径入口）。"""
        self._process_batch([signal])

    def _process_batch(self, batch: list[Signal]) -> None:
        pending = self._prepare(batch)
        if not pending:
            return
        planned: list[_Planned] = []
        groups: dict[tuple, list[PendingSignal]] = {}
        for item in pending:
            groups.setdefault(group_key(item.signal), []).append(item)
        for members in groups.values():
            group = merge_pending(members)
            try:
                item = self._plan_group(group)
            except Exception:  # noqa: BLE001
                logger.exception("信号组仓位计算失败: %s", self._label(group))
                self._mark_group(group, "error", "仓位计算异常，详见日志")
                continue
            if item is not None:
                planned.append(item)

        for item in self._net_planned(planned):
            try:
                self._execute_planned(item)
            except Exception:  # noqa: BLE001
                logger.exception("执行失败: %s", self._label(item.group))
                self._mark_group(item.group, "error", "执行异常，详见日志")

    def _prepare(self, batch: list[Signal]) -> list[PendingSignal]:
        """逐笔落库、更新镜像、过基础过滤；返回待并单的信号。"""
        out: list[PendingSignal] = []
        for signal in batch:
            trade = signal.trade
            signal_id, fresh = self._ledger.record_signal(signal)
            if not fresh or signal_id is None:
                logger.debug("信号已处理过，跳过: %s", trade.key)
                continue
            # 无论这笔最终跟不跟，目标的持仓镜像都要如实更新
            prev = self._mirror.apply_trade(trade)
            ok, reason = self._filter.check(signal)
            if not ok:
                self._ledger.update_signal(signal_id, "filtered", reason)
                logger.info("过滤: %s —— %s", reason, self._trade_label(trade))
                continue
            out.append(PendingSignal(signal_id=signal_id, signal=signal, prev_target_size=prev))
        return out

    def _plan_group(self, group: MergedGroup) -> _Planned | None:
        """市场元数据 + 按市场期限复核时效 + 金额过滤 + 仓位计算。"""
        label = self._label(group)
        try:
            market = self._clob.get_market(group.trade.condition_id)
        except ClobError as exc:
            self._mark_group(group, "error", f"拉取市场元数据失败: {exc}")
            logger.warning("拉取市场元数据失败，放弃该信号组: %s —— %s", exc, label)
            return None

        narrowed = self._enforce_market_age(group, market)
        if narrowed is None:
            return None
        group = narrowed
        trade = group.trade
        label = self._label(group)

        ok, reason = self._filter.check_notional(trade.notional, group.count)
        if not ok:
            self._mark_group(group, "filtered", reason)
            return None

        merged_signal = Signal(
            trade=trade, target=group.target, received_at=group.earliest_received
        )
        if trade.side == "BUY":
            book = None
            if self.config.sizing.depth_aware:
                try:
                    book = self._clob.get_book(trade.asset)
                except ClobError as exc:
                    logger.debug("深度封顶取订单簿失败，退回按 ratio 定量: %s", exc)
            intent, reason = plan_buy(
                merged_signal, market, self.config.sizing, self.config.execution, book=book
            )
            if intent is not None and group.count > 1:
                merge_note = f"并单 {group.count} 笔"
                note = f"{intent.note}，{merge_note}" if intent.note else merge_note
                intent = dataclasses.replace(intent, note=note)
        else:
            intent, reason = self._plan_sell(merged_signal, market, group.prev_target_size)
            if intent is not None and group.count > 1:
                intent = dataclasses.replace(intent, note=f"{intent.note}，并单 {group.count} 笔")
        if intent is None:
            self._mark_group(group, "skipped", reason)
            return None
        return _Planned(group=group, intent=intent, market=market)

    def _enforce_market_age(self, group: MergedGroup, market: MarketInfo) -> MergedGroup | None:
        """按该市场的精确时效上限复核信号组（逐笔粗筛只按放宽上限放行）。

        长线市场放宽、短线维持严格（见 SignalFilter.age_limit_for）。
        超龄成员标 filtered，剩余成员重新并组；全部超龄返回 None。
        """
        limit = self._filter.age_limit_for(market)
        now = time.time()
        fresh: list[PendingSignal] = []
        for member in group.members:
            age = now - member.signal.trade.timestamp
            if age > limit:
                self._ledger.update_signal(
                    member.signal_id, "filtered",
                    f"信号已过期 {age:.0f}s（该市场时效上限 {limit:.0f}s）",
                )
            else:
                fresh.append(member)
        if not fresh:
            logger.info(
                "过滤: 信号组全部超过该市场时效上限 %.0fs —— %s", limit, self._label(group)
            )
            return None
        if len(fresh) == len(group.members):
            return group
        return merge_pending(fresh)

    def _net_planned(self, planned: list[_Planned]) -> list[_Planned]:
        """多目标轧差：同一 token 在本批内同时有买有卖时，只执行净头寸。"""
        if not self.config.aggregate.net_across_targets or len(planned) < 2:
            return planned
        by_token: dict[str, list[_Planned]] = {}
        for item in planned:
            by_token.setdefault(item.intent.token_id, []).append(item)
        out: list[_Planned] = []
        for items in by_token.values():
            buys = [p for p in items if p.intent.side == "BUY"]
            sells = [p for p in items if p.intent.side == "SELL"]
            if not buys or not sells:
                out.extend(items)
                continue
            sum_buy = sum(p.intent.size for p in buys)
            sum_sell = sum(p.intent.size for p in sells)
            net = sum_buy - sum_sell
            dominant, offset = (buys, sells) if net >= 0 else (sells, buys)
            base = f"多目标轧差：窗口内买 {sum_buy:.2f} / 卖 {sum_sell:.2f} 份对冲"
            for p in offset:
                self._mark_group(p.group, "netted", base)
            remaining = abs(net)
            min_size = items[0].market.min_size
            if remaining < min_size:
                for p in dominant:
                    self._mark_group(
                        p.group, "netted",
                        f"{base}，净额 {remaining:.2f} 份低于最小下单量 {min_size:.2f}",
                    )
                continue
            factor = remaining / sum(p.intent.size for p in dominant)
            for p in dominant:
                if factor >= 1.0 - 1e-9:
                    out.append(p)
                    continue
                new_size = floor_to(p.intent.size * factor, SIZE_STEP)
                if new_size < min_size:
                    self._mark_group(
                        p.group, "netted",
                        f"{base}，缩量后 {new_size:.2f} 份低于最小下单量 {min_size:.2f}",
                    )
                    continue
                note = p.intent.note
                note = f"{note}，轧差缩量" if note else "轧差缩量"
                out.append(dataclasses.replace(
                    p, intent=dataclasses.replace(p.intent, size=new_size, note=note),
                ))
        return out

    def _execute_planned(self, item: _Planned) -> None:
        group, intent = item.group, item.intent
        ok, reason = self._risk.check(intent, item.market)
        if not ok:
            self._mark_group(group, "risk_blocked", reason)
            self._notifier.send(f"⛔ 风控拦截：{reason} —— {self._label(group)}")
            return

        result: ExecutionResult = self._executor.execute(intent)
        retry_s = self.config.execution.retry_no_fill_s
        if result.status == "rejected" and retry_s:
            # 盘口是流动的：限价内暂时没对手盘，隔几秒常会回来；只重试这一种干净失败
            logger.info("限价内无对手盘，%.1fs 后重试一次 —— %s", retry_s, self._label(group))
            time.sleep(retry_s)
            second = self._executor.execute(intent)
            if second.status == "rejected":
                result = dataclasses.replace(second, detail=f"{second.detail}（已重试 1 次）")
            else:
                result = dataclasses.replace(
                    second, detail=second.detail or "首次限价内无对手盘，重试 1 次后成交"
                )
        self._ledger.record_order(
            group.signal_ids[0], intent,
            mode=self._executor.mode, status=result.status,
            filled_size=result.filled_size, avg_price=result.avg_price,
            slippage=result.slippage, detail=result.detail,
            apply_fill=getattr(self._executor, "applies_fills", True),
        )
        status = "executed" if result.ok else "no_fill"
        detail = result.detail
        if group.count > 1:
            suffix = f"并单 {group.count} 笔 → 订单挂在信号 #{group.signal_ids[0]}"
            detail = f"{detail}；{suffix}" if detail else suffix
        for signal_id in group.signal_ids:
            self._ledger.update_signal(signal_id, status, detail)
        self._notifier.send(self._describe(intent, result, group))

    def _plan_sell(
        self, signal: Signal, market: MarketInfo, prev_target_size: float
    ) -> tuple[OrderIntent | None, str]:
        """卖出跟随：目标卖掉他持仓的 x%，我们也卖自己持仓的 x%。"""
        trade = signal.trade
        own = self._ledger.position_size(trade.asset)
        if own <= 0:
            return None, "自己没有该市场持仓，忽略卖出信号"
        if prev_target_size > 0:
            fraction = min(1.0, trade.size / prev_target_size)
        else:
            # 镜像里没有记录（对方启动前建的仓且对账还没跑到）——按全平跟随，保守离场
            fraction = 1.0
            logger.info(
                "目标 %s 卖出了镜像未记录的持仓，按全平跟随",
                _short(trade.proxy_wallet),
            )
        sell_size = floor_to(own * fraction, SIZE_STEP)
        if own - sell_size < market.min_size:
            sell_size = own  # 余量将低于最小下单量成为死仓，干脆全平
        if sell_size < market.min_size:
            return None, (
                f"计划卖出 {sell_size:.2f} 份低于市场最小下单量 "
                f"{market.min_size:.2f}（持仓 {own:.2f}）"
            )
        limit = sell_limit_price(trade.price, market, self.config.execution)
        return (
            OrderIntent(
                token_id=trade.asset,
                condition_id=trade.condition_id,
                side="SELL",
                limit_price=limit,
                size=round(sell_size, 2),
                ref_price=trade.price,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
                title=trade.title,
                outcome=trade.outcome,
                note=f"跟随卖出 {fraction:.0%}",
            ),
            "",
        )

    # ---- 对账 ----

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(self.config.reconcile_interval_s):
            try:
                self.reconcile_once()
            except Exception:  # noqa: BLE001 —— 对账失败下轮再试
                logger.exception("对账失败，下一轮继续")

    def reconcile_once(self) -> None:
        """刷新目标持仓镜像；纸面自动结算已出结果的持仓；实盘同步持仓并提醒可赎回。"""
        if self._data is not None:
            for address in self._targets:
                try:
                    positions = self._data.get_positions(address)
                except DataApiError as exc:
                    logger.warning("拉取 %s 持仓失败，镜像维持现状: %s", _short(address), exc)
                    continue
                self._mirror.replace(address, {p.asset: p.size for p in positions})
                logger.debug("镜像已刷新: %s 持有 %d 个仓位", _short(address), len(positions))

        if self.config.mode != "live" and self.config.auto_settle_resolved:
            self._settle_resolved_positions()

        if self.config.mode != "live" or not self._own_address or self._data is None:
            return
        try:
            own_positions = self._data.get_positions(self._own_address)
        except DataApiError as exc:
            logger.warning("拉取自己持仓失败，账本维持现状: %s", exc)
            return
        self._ledger.sync_positions(own_positions)
        logger.info("实盘持仓已对账（%d 个仓位）", len(own_positions))
        redeemable = [
            p for p in own_positions
            if p.redeemable and p.size > 0 and p.asset not in self._redeem_notified
        ]
        for p in redeemable:
            self._redeem_notified.add(p.asset)
            self._notifier.send(
                f"🎉 市场已结算可赎回：{p.size:.2f} 份 [{p.outcome}] {p.title} —— "
                "请在 Polymarket 页面 redeem 换回 USDC"
            )

    def _settle_resolved_positions(self) -> None:
        """纸面自动结算：市场关闭且有 winner 后，按 1.00/0.00 把持仓入账清仓。

        实盘不走这里——真实 redeem 是链上交易（需要 gas），仍由上面的
        可赎回提醒人工处理。
        """
        for position in self._ledger.positions():
            try:
                market = self._clob.get_market(position.condition_id, fresh=True)
            except ClobError as exc:
                logger.debug("结算检查拉取市场失败 %s: %s", position.condition_id, exc)
                continue
            if not market.resolved:
                continue
            price = 1.0 if position.token_id in market.winner_token_ids else 0.0
            realized = self._ledger.settle_position(
                position.token_id, price, mode=self._executor.mode
            )
            if realized is None:
                continue
            word = "赢" if price > 0 else "输"
            self._notifier.send(
                f"🧾 市场已结算（{word}）：{position.size:.2f} 份 [{position.outcome}] "
                f"{position.title} 按 {price:.2f} 入账，已实现 {realized:+.2f}"
            )

    # ---- 展示 ----

    def _trade_label(self, trade: Trade) -> str:
        return (
            f"{_short(trade.proxy_wallet)} {trade.side} {trade.size:.2f} @ {trade.price:.3f} "
            f"[{trade.outcome}] {trade.title}"
        )

    def _label(self, group: MergedGroup) -> str:
        label = self._trade_label(group.trade)
        if group.count > 1:
            label += f"（并 {group.count} 笔）"
        return label

    def _mark_group(self, group: MergedGroup, status: str, detail: str) -> None:
        for signal_id in group.signal_ids:
            self._ledger.update_signal(signal_id, status, detail)
        logger.info("%s: %s —— %s", status, detail, self._label(group))

    def _describe(self, intent: OrderIntent, result: ExecutionResult, group: MergedGroup) -> str:
        prefix = "📝 纸面" if self._executor.mode == "paper" else "💰 实盘"
        head = (
            f"{prefix} {intent.side} {intent.size:.2f} 份 @≤{intent.limit_price:.3f} "
            f"[{intent.outcome}] {intent.title}"
        )
        if result.status in ("filled", "partial"):
            body = (
                f"→ 成交 {result.filled_size:.2f} @ {result.avg_price:.3f}"
                f"（${result.notional:.2f}，滑点 {result.slippage:+.3f}）"
            )
            if result.status == "partial":
                body += f"；{result.detail}"
        elif result.status == "submitted":
            body = f"→ 已提交 {result.detail}"
        else:
            body = f"→ 未成交：{result.detail}"
        note = f"（{intent.note}）" if intent.note else ""
        return f"{head} {body}{note} 跟随 {_short(group.trade.proxy_wallet)}"
