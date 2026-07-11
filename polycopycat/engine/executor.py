"""执行器：把下单意图变成成交结果。

PaperExecutor 用实时订单簿模拟 FAK（立即成交能成的部分、剩余取消），
不动真钱，但滑点是按真实盘口算的——这正是纸面阶段要回答的问题：
「跟这个地址，滑点会吃掉多少」。实盘执行器见 live.py（M1）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .clob import ClobError, ClobReadClient
from .signals import OrderIntent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    status: str            # filled / partial / rejected / submitted / error
    filled_size: float = 0.0
    avg_price: float = 0.0
    slippage: float = 0.0  # 相对 ref_price，正数=比目标多付（买）/少收（卖）
    detail: str = ""

    @property
    def notional(self) -> float:
        return self.filled_size * self.avg_price

    @property
    def ok(self) -> bool:
        return self.status in ("filled", "partial", "submitted")


class PaperExecutor:
    """纸面执行器：拉当前订单簿，按限价把 FAK 单“撮合”出来。"""

    mode = "paper"
    applies_fills = True  # 成交结果可直接记入账本持仓

    def __init__(self, clob: ClobReadClient) -> None:
        self._clob = clob

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        try:
            book = self._clob.get_book(intent.token_id)
        except ClobError as exc:
            return ExecutionResult(status="error", detail=f"拉取订单簿失败: {exc}")

        if intent.side == "BUY":
            levels = [lv for lv in book.asks if lv.price <= intent.limit_price + 1e-9]
        else:
            levels = [lv for lv in book.bids if lv.price >= intent.limit_price - 1e-9]
        if not levels:
            return ExecutionResult(
                status="rejected",
                detail=f"限价 {intent.limit_price:.3f} 内无对手盘（滑点保护生效）",
            )

        remaining = intent.size
        cost = 0.0
        filled = 0.0
        for level in levels:
            take = min(remaining, level.size)
            filled += take
            cost += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        avg_price = cost / filled if filled > 0 else 0.0
        if intent.side == "BUY":
            slippage = avg_price - intent.ref_price
        else:
            slippage = intent.ref_price - avg_price
        status = "filled" if remaining <= 1e-9 else "partial"
        detail = "" if status == "filled" else (
            f"盘口深度不足，剩余 {remaining:.2f} 份未成交（FAK 已取消）"
        )
        return ExecutionResult(
            status=status,
            filled_size=round(filled, 6),
            avg_price=round(avg_price, 6),
            slippage=round(slippage, 6),
            detail=detail,
        )
