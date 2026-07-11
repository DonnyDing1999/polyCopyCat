"""把战绩指标变成「跟不跟」的结论：先排除，再打分。

排除规则宁严勿松（跟错人比漏掉人贵得多）；分数只在合格地址之间
排序用，公式刻意简单透明，别把它当成精确的期望收益。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import Position
from .metrics import DEFAULT_QUICK_WINDOW_S, TraderStats


@dataclass
class ScoutConfig:
    min_trades: int = 20                  # 样本下限
    min_notional_usdc: float = 2000.0     # 窗口内总成交额下限
    max_inactive_days: float = 7.0        # 最近活跃要求
    max_quick_flip_ratio: float = 0.5     # 快进快出占比上限（做市嫌疑）
    min_quick_sample: int = 5             # 快进快出判定所需的最少配对卖出数
    max_trades_per_day: float = 150.0     # 频率上限（机器人嫌疑）
    min_realized_pnl: float = 0.0         # 回放盈亏下限
    min_pnl_sample: int = 3               # 盈亏判定所需的最少配对卖出数
    min_win_rate: float = 0.5             # 胜率下限
    min_win_sample: int = 10              # 胜率判定所需的最少配对卖出数
    quick_window_s: float = DEFAULT_QUICK_WINDOW_S
    request_delay_s: float = 0.15         # 逐地址评估时的限速间隔


@dataclass
class Verdict:
    address: str
    eligible: bool
    score: float
    reasons: list[str] = field(default_factory=list)  # 排除原因（合格则为空）
    stats: TraderStats | None = None
    exposure_usdc: float = 0.0    # 当前持仓成本
    unrealized_pnl: float = 0.0   # 当前持仓浮盈（按 curPrice 粗算）

    def to_dict(self) -> dict[str, Any]:
        s = self.stats
        return {
            "address": self.address,
            "eligible": self.eligible,
            "score": round(self.score, 1),
            "reasons": self.reasons,
            "realized_pnl": round(s.realized_pnl, 2) if s else None,
            "win_rate": round(s.win_rate, 4) if s and s.win_rate is not None else None,
            "matched_sells": s.matched_sells if s else None,
            "n_trades": s.n_trades if s else None,
            "n_markets": s.n_markets if s else None,
            "avg_trade_usdc": round(s.avg_trade_usdc, 2) if s else None,
            "quick_flip_ratio": round(s.quick_flip_ratio, 4) if s else None,
            "median_holding_s": round(s.median_holding_s, 1) if s else None,
            "last_ts": s.last_ts if s else None,
            "exposure_usdc": round(self.exposure_usdc, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
        }


def evaluate(
    stats: TraderStats,
    positions: list[Position] | None,
    config: ScoutConfig,
    *,
    now: float | None = None,
) -> Verdict:
    now = now if now is not None else time.time()
    exposure = sum(p.size * p.avg_price for p in positions or [])
    unrealized = sum(p.size * (p.cur_price - p.avg_price) for p in positions or [])
    reasons: list[str] = []

    if stats.n_trades < config.min_trades:
        reasons.append(f"样本不足（窗口内仅 {stats.n_trades} 笔 < {config.min_trades}）")
    if stats.notional < config.min_notional_usdc:
        reasons.append(
            f"成交额太小（${stats.notional:,.0f} < ${config.min_notional_usdc:,.0f}）"
        )
    inactive_days = (now - stats.last_ts) / 86400 if stats.last_ts else float("inf")
    if inactive_days > config.max_inactive_days:
        reasons.append(f"已 {inactive_days:.1f} 天不活跃（阈值 {config.max_inactive_days:.0f} 天）")
    if (
        stats.matched_sells >= config.min_quick_sample
        and stats.quick_flip_ratio > config.max_quick_flip_ratio
    ):
        reasons.append(
            f"疑似做市/套利（{config.quick_window_s / 60:.0f} 分钟内快进快出占比 "
            f"{stats.quick_flip_ratio:.0%}）"
        )
    if stats.trades_per_day > config.max_trades_per_day:
        reasons.append(
            f"频率过高疑似机器人（{stats.trades_per_day:.0f} 笔/天 "
            f"> {config.max_trades_per_day:.0f}）"
        )
    if stats.matched_sells >= config.min_pnl_sample and stats.realized_pnl < config.min_realized_pnl:
        reasons.append(f"回放已实现亏损（${stats.realized_pnl:,.2f}）")
    win_rate = stats.win_rate
    if (
        win_rate is not None
        and stats.matched_sells >= config.min_win_sample
        and win_rate < config.min_win_rate
    ):
        reasons.append(f"胜率过低（{win_rate:.0%} < {config.min_win_rate:.0%}）")

    if reasons:
        return Verdict(
            address=stats.address, eligible=False, score=0.0, reasons=reasons,
            stats=stats, exposure_usdc=exposure, unrealized_pnl=unrealized,
        )

    # 打分（满分 100）：盈利 40 + 胜率 25 + 市场广度 15 + 活跃度 10 + 单笔规模 10
    pnl_score = 40.0 * min(1.0, math.log10(1.0 + max(0.0, stats.realized_pnl)) / 4.0)
    if win_rate is None:
        win_score = 10.0  # 纯买入持有，胜率未知给中性偏低
    else:
        win_score = 25.0 * win_rate
    breadth_score = 15.0 * min(1.0, stats.n_markets / 10.0)
    hours_idle = (now - stats.last_ts) / 3600 if stats.last_ts else 168.0
    recency_score = 10.0 * max(0.0, 1.0 - hours_idle / 168.0)
    size_score = 10.0 * min(1.0, stats.avg_trade_usdc / 500.0)
    score = pnl_score + win_score + breadth_score + recency_score + size_score
    return Verdict(
        address=stats.address, eligible=True, score=round(score, 1),
        stats=stats, exposure_usdc=exposure, unrealized_pnl=unrealized,
    )
