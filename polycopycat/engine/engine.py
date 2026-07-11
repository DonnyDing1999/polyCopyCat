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

from ..models import Trade
from .clob import ClobError, ClobReadClient
from .config import EngineConfig
from .executor import ExecutionResult
from .ledger import Ledger
from .notify import Notifier
from .risk import RiskGate
from .signals import OrderIntent, Signal, SignalFilter
from .sizing import plan_buy

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
    ) -> None:
        self.config = config
        self._clob = clob
        self._ledger = ledger
        self._executor = executor
        self._notifier = notifier
        self._filter = SignalFilter(config.filters)
        self._risk = RiskGate(config.risk, ledger)
        self._targets = {t.address: t for t in config.targets}
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

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
        self._thread = threading.Thread(target=self._loop, name="polycopycat-engine", daemon=True)
        self._thread.start()
        logger.info(
            "跟单引擎已启动（%s 模式，%d 个目标，滑点上限 %.3f）",
            self.config.mode, len(self._targets), self.config.execution.slippage_cap,
        )

    def stop(self, timeout: float = 30.0) -> None:
        """处理完队列中已有信号后停止。"""
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
            intent, reason = self._plan_sell(signal, market)
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

    def _plan_sell(self, signal: Signal, market) -> tuple[OrderIntent | None, str]:
        """卖出跟随需要目标持仓镜像，M2 实现；先明确跳过。"""
        return None, "卖出跟随将在 M2（持仓镜像）中提供"

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
