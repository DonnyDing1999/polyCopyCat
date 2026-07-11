"""仓位计算：把一笔目标买入换算成自己的下单意图。

卖出跟随需要目标持仓镜像，在 engine 里单独处理（M2）。
"""

from __future__ import annotations

import math

from .clob import MarketInfo
from .config import ExecutionConfig, SizingConfig
from .signals import OrderIntent, Signal

# CLOB 份额精度：两位小数
SIZE_STEP = 0.01


def floor_to(value: float, step: float) -> float:
    return round(math.floor(value / step + 1e-9) * step, 9)


def ceil_to(value: float, step: float) -> float:
    return round(math.ceil(value / step - 1e-9) * step, 9)


def buy_limit_price(ref_price: float, market: MarketInfo, execution: ExecutionConfig) -> float:
    """买入限价 = 目标成交价 + 滑点上限，向下取整到 tick，并留在 (0, 1) 内。"""
    limit = min(ref_price + execution.slippage_cap, 1.0 - market.tick_size)
    return max(market.tick_size, floor_to(limit, market.tick_size))


def sell_limit_price(ref_price: float, market: MarketInfo, execution: ExecutionConfig) -> float:
    """卖出限价 = 目标成交价 - 滑点上限，向上取整到 tick，并留在 (0, 1) 内。"""
    limit = max(ref_price - execution.slippage_cap, market.tick_size)
    return min(1.0 - market.tick_size, ceil_to(limit, market.tick_size))


def plan_buy(
    signal: Signal,
    market: MarketInfo,
    sizing: SizingConfig,
    execution: ExecutionConfig,
) -> tuple[OrderIntent | None, str]:
    """把目标买入换算成自己的买入意图；不值得下的返回 (None, 原因)。"""
    trade = signal.trade
    ratio = signal.target.ratio if signal.target.ratio is not None else sizing.ratio
    cap = sizing.max_per_trade_usdc
    if signal.target.max_per_trade_usdc is not None:
        cap = min(cap, signal.target.max_per_trade_usdc)

    if sizing.mode == "fixed":
        notional = sizing.fixed_usdc
    else:
        notional = trade.notional * ratio
    notional = min(notional, cap)

    limit = buy_limit_price(trade.price, market, execution)
    if limit <= 0:
        return None, "限价计算结果非法"
    size = floor_to(notional / limit, SIZE_STEP)
    if size < market.min_size:
        return None, (
            f"计划量 {size:.2f} 份低于市场最小下单量 {market.min_size:.2f}"
            f"（计划金额 ${notional:.2f}）"
        )
    return (
        OrderIntent(
            token_id=trade.asset,
            condition_id=trade.condition_id,
            side="BUY",
            limit_price=limit,
            size=size,
            ref_price=trade.price,
            neg_risk=market.neg_risk,
            tick_size=market.tick_size,
            title=trade.title,
            outcome=trade.outcome,
        ),
        "",
    )
