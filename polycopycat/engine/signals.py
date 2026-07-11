"""信号与下单意图的数据结构，以及信号级过滤。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..models import Trade
from .config import FilterConfig, TargetConfig


@dataclass(frozen=True)
class Signal:
    """一笔目标成交 + 它对应的跟单目标配置。"""

    trade: Trade
    target: TargetConfig
    received_at: float  # 引擎收到信号的 unix 时间

    @property
    def age_s(self) -> float:
        """成交发生到现在过了多久（用成交自带的链上时间戳算）。"""
        return max(0.0, time.time() - self.trade.timestamp)


@dataclass(frozen=True)
class OrderIntent:
    """经过仓位计算后、待风控和执行的下单意图。"""

    token_id: str
    condition_id: str
    side: str           # BUY / SELL
    limit_price: float  # 最差可接受价（FAK 限价）
    size: float         # 份额
    ref_price: float    # 目标的成交价，滑点基准
    neg_risk: bool
    tick_size: float = 0.01
    title: str = ""
    outcome: str = ""
    note: str = ""      # 附注，如「跟随卖出 50%」

    @property
    def notional(self) -> float:
        """按限价算的最大金额（风控用保守口径）。"""
        return self.size * self.limit_price


class SignalFilter:
    """信号级过滤：不值得跟的信号在这里拦下，返回 (是否放行, 原因)。"""

    def __init__(self, config: FilterConfig) -> None:
        self._config = config

    def check(self, signal: Signal) -> tuple[bool, str]:
        trade = signal.trade
        if signal.target.paused:
            return False, "目标地址已暂停跟单"
        if trade.side not in ("BUY", "SELL"):
            return False, f"未知方向 {trade.side!r}"
        if trade.side == "SELL" and not self._config.follow_sells:
            return False, "已配置不跟随卖出"
        if trade.price <= 0 or trade.size <= 0:
            return False, "成交价格或数量非法"
        if trade.notional < self._config.min_target_notional_usdc:
            return False, (
                f"目标成交金额 ${trade.notional:.2f} 低于阈值 "
                f"${self._config.min_target_notional_usdc:.2f}"
            )
        if signal.age_s > self._config.max_signal_age_s:
            return False, f"信号已过期 {signal.age_s:.0f}s（阈值 {self._config.max_signal_age_s:.0f}s）"
        return True, ""
