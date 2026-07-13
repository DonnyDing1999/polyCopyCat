"""信号聚合（M3）：把窗口内的碎片成交合并成等效的一笔。

目标经常把一次建仓拆成多笔小额成交（几秒内同市场同方向连打数单），
逐笔跟单会被尘埃单过滤拦掉大半、或者打出一串比目标本意零碎得多的
小单。聚合把同一窗口内「同目标 + 同 token + 同方向」的成交合成一笔
等效成交（数量求和、价格按 VWAP），金额过滤和仓位计算都在合并后做。

账本幂等不受影响：每笔原始成交仍按自己的 trade_key 逐条落库，
合并只发生在执行层（一组信号产出一张订单）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..models import Trade
from .config import TargetConfig
from .signals import Signal


@dataclass
class PendingSignal:
    """已落库、已过基础过滤、等待并单执行的一笔信号。"""

    signal_id: int
    signal: Signal
    prev_target_size: float  # 该笔成交前目标在此 token 的镜像持仓（卖出分数用）


@dataclass(frozen=True)
class MergedGroup:
    """同目标 + 同 token + 同方向的一组信号，合并成一笔等效成交。"""

    members: tuple[PendingSignal, ...]
    trade: Trade              # 合成成交：总量 + VWAP + 最新时间戳
    target: TargetConfig

    @property
    def signal_ids(self) -> tuple[int, ...]:
        return tuple(m.signal_id for m in self.members)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def prev_target_size(self) -> float:
        """组内首笔成交前的目标持仓：卖出跟随的分母。"""
        return self.members[0].prev_target_size

    @property
    def earliest_received(self) -> float:
        return min(m.signal.received_at for m in self.members)


def group_key(signal: Signal) -> tuple[str, str, str]:
    trade = signal.trade
    return (signal.target.address, trade.asset, trade.side)


def merge_pending(members: list[PendingSignal]) -> MergedGroup:
    """把同组信号合成一笔等效成交（成员按到达顺序传入）。"""
    trades = [m.signal.trade for m in members]
    total = sum(t.size for t in trades)
    vwap = sum(t.size * t.price for t in trades) / total if total > 0 else 0.0
    merged_trade = dataclasses.replace(
        trades[-1],
        size=round(total, 9),
        price=round(vwap, 9),
        timestamp=max(t.timestamp for t in trades),
    )
    return MergedGroup(
        members=tuple(members),
        trade=merged_trade,
        target=members[0].signal.target,
    )
