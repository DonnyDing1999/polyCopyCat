"""把一个地址的公开成交带回放成可比较的战绩指标。

回放用与账本相同的均价法：买入摊薄均价，卖出按（卖价 - 均价）计
已实现盈亏。窗口外建的仓（tape 里只见卖不见买）盈亏未知，单独记
unmatched，不掺进胜率——只对能自证的部分下结论。

「快进快出占比」是识别做市/套利地址的主要信号：买入后几分钟内就
卖掉的平仓占比很高的地址，赚的是价差和返佣，方向性毫无参考价值，
跟单这类地址基本必亏。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Trade

DEFAULT_QUICK_WINDOW_S = 600.0


@dataclass
class TraderStats:
    address: str
    n_trades: int = 0
    n_buys: int = 0
    n_sells: int = 0
    matched_sells: int = 0    # 能与窗口内买入配对的卖出（战绩可信的部分）
    unmatched_sells: int = 0  # 平的是窗口外建的仓，盈亏未知
    wins: int = 0             # 配对卖出中卖价高于持仓均价的笔数
    quick_flips: int = 0      # 配对卖出中持仓时长低于阈值的笔数
    realized_pnl: float = 0.0
    notional: float = 0.0
    avg_trade_usdc: float = 0.0
    n_markets: int = 0
    active_days: int = 0
    first_ts: int = 0
    last_ts: int = 0
    median_holding_s: float = 0.0
    holdings_s: list[float] = field(default_factory=list, repr=False)

    @property
    def win_rate(self) -> float | None:
        """配对卖出的胜率；没有配对卖出（纯买入持有）时未知。"""
        return self.wins / self.matched_sells if self.matched_sells else None

    @property
    def quick_flip_ratio(self) -> float:
        return self.quick_flips / self.matched_sells if self.matched_sells else 0.0

    @property
    def trades_per_day(self) -> float:
        return self.n_trades / max(1, self.active_days)


def replay(
    address: str,
    trades: list[Trade],
    *,
    quick_window_s: float = DEFAULT_QUICK_WINDOW_S,
) -> TraderStats:
    """按时间回放成交带（输入顺序不限，内部会排序）。"""
    stats = TraderStats(address=address.lower())
    ordered = sorted(trades, key=lambda t: t.timestamp)
    # asset -> [持仓份额, 持仓均价, 加权建仓时间]
    book: dict[str, list[float]] = {}
    markets: set[str] = set()
    days: set[str] = set()

    for trade in ordered:
        if trade.side not in ("BUY", "SELL") or trade.size <= 0 or trade.price <= 0:
            continue
        stats.n_trades += 1
        stats.notional += trade.notional
        markets.add(trade.condition_id)
        days.add(
            datetime.fromtimestamp(trade.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        )
        if stats.n_trades == 1:
            stats.first_ts = trade.timestamp
        stats.last_ts = trade.timestamp

        size, avg_cost, entry_ts = book.get(trade.asset, [0.0, 0.0, 0.0])
        if trade.side == "BUY":
            stats.n_buys += 1
            new_size = size + trade.size
            avg_cost = (size * avg_cost + trade.size * trade.price) / new_size
            entry_ts = (size * entry_ts + trade.size * trade.timestamp) / new_size
            book[trade.asset] = [new_size, avg_cost, entry_ts]
            continue

        stats.n_sells += 1
        closable = min(trade.size, size)
        if closable <= 1e-9:
            stats.unmatched_sells += 1
            continue
        stats.matched_sells += 1
        stats.realized_pnl += closable * (trade.price - avg_cost)
        if trade.price > avg_cost:
            stats.wins += 1
        holding = max(0.0, trade.timestamp - entry_ts)
        stats.holdings_s.append(holding)
        if holding < quick_window_s:
            stats.quick_flips += 1
        remaining = size - closable
        if remaining <= 1e-9:
            book.pop(trade.asset, None)
        else:
            book[trade.asset] = [remaining, avg_cost, entry_ts]

    stats.n_markets = len(markets)
    stats.active_days = len(days)
    stats.avg_trade_usdc = stats.notional / stats.n_trades if stats.n_trades else 0.0
    stats.median_holding_s = statistics.median(stats.holdings_s) if stats.holdings_s else 0.0
    return stats
