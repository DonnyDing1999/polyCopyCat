"""跟单引擎主体：消费信号队列，串起过滤 → 计算 → 风控 → 执行 → 记账 → 通知。

引擎跑在自己的线程里，watcher/stream 线程只往队列里塞信号，
慢速的 CLOB 请求不会拖住行情监控。任何一笔信号处理抛异常都只
影响该笔（记为 error），引擎线程本身永不退出。
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from ..data_api import DataApiClient, DataApiError
from ..models import Trade
from .clob import ClobError, ClobReadClient
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
            "跟单引擎已启动（%s 模式，%d 个目标，滑点上限 %.3f）",
            self.config.mode, len(self._targets), self.config.execution.slippage_cap,
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
            try:
                if item is _STOP:
                    return
                try:
                    self._process(item)
                except Exception:  # noqa: BLE001 —— 单笔信号失败不能拖垮引擎
                    logger.exception("处理信号失败: %s", item)
            finally:
                self._queue.task_done()

    def _process(self, signal: Signal) -> None:
        trade = signal.trade
        signal_id, fresh = self._ledger.record_signal(signal)
        if not fresh or signal_id is None:
            logger.debug("信号已处理过，跳过: %s", trade.key)
            return
        # 无论这笔最终跟不跟，目标的持仓镜像都要如实更新
        prev_target_size = self._mirror.apply_trade(trade)
        label = (
            f"{_short(trade.proxy_wallet)} {trade.side} {trade.size:.2f} @ {trade.price:.3f} "
            f"[{trade.outcome}] {trade.title}"
        )

        ok, reason = self._filter.check(signal)
        if not ok:
            self._ledger.update_signal(signal_id, "filtered", reason)
            logger.info("过滤: %s —— %s", reason, label)
            return

        try:
            market = self._clob.get_market(trade.condition_id)
        except ClobError as exc:
            self._ledger.update_signal(signal_id, "error", f"拉取市场元数据失败: {exc}")
            logger.warning("拉取市场元数据失败，放弃该信号: %s —— %s", exc, label)
            return

        if trade.side == "BUY":
            intent, reason = plan_buy(signal, market, self.config.sizing, self.config.execution)
        else:
            intent, reason = self._plan_sell(signal, market, prev_target_size)
        if intent is None:
            self._ledger.update_signal(signal_id, "skipped", reason)
            logger.info("跳过: %s —— %s", reason, label)
            return

        ok, reason = self._risk.check(intent, market)
        if not ok:
            self._ledger.update_signal(signal_id, "risk_blocked", reason)
            self._notifier.send(f"⛔ 风控拦截：{reason} —— {label}")
            return

        result: ExecutionResult = self._executor.execute(intent)
        self._ledger.record_order(
            signal_id, intent,
            mode=self._executor.mode, status=result.status,
            filled_size=result.filled_size, avg_price=result.avg_price,
            slippage=result.slippage, detail=result.detail,
            apply_fill=getattr(self._executor, "applies_fills", True),
        )
        self._ledger.update_signal(
            signal_id, "executed" if result.ok else "no_fill", result.detail
        )
        self._notifier.send(self._describe(intent, result, signal))

    def _plan_sell(
        self, signal: Signal, market, prev_target_size: float
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
        """刷新目标持仓镜像；实盘模式下同步自己的持仓并提醒可赎回仓位。"""
        if self._data is None:
            return
        for address in self._targets:
            try:
                positions = self._data.get_positions(address)
            except DataApiError as exc:
                logger.warning("拉取 %s 持仓失败，镜像维持现状: %s", _short(address), exc)
                continue
            self._mirror.replace(address, {p.asset: p.size for p in positions})
            logger.debug("镜像已刷新: %s 持有 %d 个仓位", _short(address), len(positions))

        if self.config.mode != "live" or not self._own_address:
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

    def _describe(self, intent: OrderIntent, result: ExecutionResult, signal: Signal) -> str:
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
        return f"{head} {body}{note} 跟随 {_short(signal.trade.proxy_wallet)}"
