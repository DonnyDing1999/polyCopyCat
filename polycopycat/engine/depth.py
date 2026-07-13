"""订单簿深度分析：跟单能在滑点内吃多大的量。

跟单放大的真相：跟单放大不了目标的**收益率**，只能在同一条边上按更大
的量吃同一口利润，而能吃多大由盘口深度决定——超过书里在你滑点内能
承接的量，多下的部分只会把价格打坏、反噬自己。所以深度对跟单有两个
作用：

- **安全上限**：任何一笔跟单都不该下超过「限价内盘口总量」，否则 FAK
  要么吃到限价外（滑点破顶）、要么大量未成交。
- **放大依据**：书够深、而你按 ratio 跟得偏小时，可以在上限内加大绝对
  金额，用同样的价差抓更大的本（收益率不变、本金变大）。

这里只算 BUY 侧（卖出跟随由自己的持仓量决定，不涉及加仓放大）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .clob import OrderBook


@dataclass(frozen=True)
class DepthFill:
    """按限价逐档吃盘口的结果。"""

    shares: float        # 限价内可成交份额
    notional: float      # 对应金额（USDC）
    avg_price: float     # 加权成交均价（0 表示无可成交盘口）
    levels_used: int     # 吃掉几档

    @property
    def empty(self) -> bool:
        return self.shares <= 0


def fillable_within_limit(book: OrderBook, side: str, limit_price: float) -> DepthFill:
    """限价内能吃到的总深度（BUY 吃 asks≤限价，SELL 吃 bids≥限价）。

    这就是 FAK 单在该盘口能成交的上限——跟单量不该超过它。
    """
    if side == "BUY":
        levels = [lv for lv in book.asks if lv.price <= limit_price + 1e-9]
    elif side == "SELL":
        levels = [lv for lv in book.bids if lv.price >= limit_price - 1e-9]
    else:
        return DepthFill(0.0, 0.0, 0.0, 0)

    shares = 0.0
    cost = 0.0
    used = 0
    for lv in levels:
        shares += lv.size
        cost += lv.size * lv.price
        used += 1
    avg = cost / shares if shares > 0 else 0.0
    return DepthFill(
        shares=round(shares, 9), notional=round(cost, 9),
        avg_price=round(avg, 9), levels_used=used,
    )


def depth_capped_notional(
    desired_notional: float,
    book: OrderBook,
    side: str,
    limit_price: float,
) -> tuple[float, float]:
    """把想跟的金额压到盘口在限价内能承接的容量以内。

    返回 (可下金额, 盘口容量)。可下金额 = min(想下, 容量)；容量为 0
    表示限价内无对手盘。调用方据此判断是否放大、是否够最小下单量。
    """
    capacity = fillable_within_limit(book, side, limit_price).notional
    return min(desired_notional, capacity), capacity
