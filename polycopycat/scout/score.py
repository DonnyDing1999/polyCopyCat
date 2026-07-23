"""把战绩指标变成「跟不跟」的结论：先排除，再打分。

排除规则宁严勿松（跟错人比漏掉人贵得多）；分数只在合格地址之间
排序用，公式刻意简单透明，别把它当成精确的期望收益。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ..models import Position, Trade
from .metrics import DEFAULT_QUICK_WINDOW_S, TraderStats

# cur_price 低于此视为已归零的死仓（输方结算价），高于 0.999 视为待赎回的赢仓
_DEAD_PRICE = 0.001
_WON_PRICE = 0.999


@dataclass
class ScoutConfig:
    min_trades: int = 20                  # 样本下限
    min_notional_usdc: float = 2000.0     # 窗口内总成交额下限
    max_inactive_days: float = 7.0        # 最近活跃要求
    max_quick_flip_ratio: float = 0.5     # 快进快出占比上限（做市嫌疑）
    min_quick_sample: int = 5             # 快进快出判定所需的最少配对卖出数
    max_trades_per_day: float = 100.0     # 频率上限（机器人嫌疑）
    min_realized_pnl: float = 0.0         # 回放盈亏下限
    min_pnl_sample: int = 3               # 盈亏判定所需的最少配对卖出数
    min_win_rate: float = 0.5             # 胜率下限
    min_win_sample: int = 10              # 胜率判定所需的最少配对卖出数
    max_win_rate: float = 0.95            # 胜率上限：大样本下高得离谱 = 结构性套利
    max_win_rate_sample: int = 50         # 套利判定所需的最少配对卖出数
    max_unrealized_drawdown_ratio: float = 0.5  # 持仓浮亏/成本 超过此比例 = 疑似死仓
    min_exposure_for_drawdown_usdc: float = 500.0  # 死仓判定所需的最小持仓成本
    # 跨场馆/跨账户套利单腿指纹：几乎不割肉（胜率异常高）+ 全在贴近1.0平仓。
    # 这类账户的输腿在别处（另一账户或场外博彩），本钱包只见幸存的赢腿，
    # 招聘版按盈亏/死仓完全看不穿，会给满分——专门一条规则筛掉。
    arb_min_win_rate: float = 0.9          # 胜率高于此才触发套利嫌疑判定
    arb_min_high_close_ratio: float = 0.85  # 卖出里贴近1.0平仓占比高于此
    arb_min_sample: int = 20               # 套利指纹判定所需的最少配对卖出数
    # 慢速做市/流动性提供：同一 token 反复双向成交、薄点差吃价差（快进快出抓不到的慢速版）
    max_churn_notional_ratio: float = 0.35  # 深度双向循环 token 成交额占比超此 = 做市嫌疑
    mm_thin_spread: float = 0.06            # 且双向点差薄于此（吃价差而非方向进出）
    mm_min_trades: int = 40                 # 做市判定所需的最少成交笔数（样本足才可信）
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


def evaluate_health(
    stats: TraderStats,
    positions: list[Position] | None,
    tape: list[Trade],
    config: ScoutConfig,
    *,
    now: float | None = None,
) -> Verdict:
    """在跟目标的试用期考核：与招聘版 evaluate 只差在死仓与盈亏口径。

    归零死仓（输方结算，cur_price≈0）永远不会从持仓接口消失，招聘版的
    「浮亏占成本」实际衡量的是历史累计尸体——用它考核在跟目标会把
    很久以前亏过钱、如今交易得很好的人永久踢出（且 auto_resume 永远
    等不到）。考核版改为：

    - **活仓浮亏**：只看未结算仓位（cur>0）的被套程度，阈值沿用
      max_unrealized_drawdown_ratio；
    - **窗口净盈亏**：死仓只追溯「本次回放窗口内买入后归零」的（老账
      不追溯），与回放已实现盈亏、窗口内未赎回的赢仓合并成窗口净
      盈亏判亏损——补上回放看不见的结算亏损，且随窗口滚动可恢复。
      （已赎回的赢仓从持仓接口消失、无法计入，口径略偏保守。）
    """
    base_config = replace(
        config,
        min_exposure_for_drawdown_usdc=float("inf"),  # 关掉招聘版死仓规则
        min_realized_pnl=float("-inf"),               # 盈亏改用窗口净口径判
    )
    verdict = evaluate(stats, positions, base_config, now=now)
    reasons = list(verdict.reasons)
    positions = positions or []

    live = [p for p in positions if p.cur_price > _DEAD_PRICE]
    live_exposure = sum(p.size * p.avg_price for p in live)
    live_unrealized = sum(p.size * (p.cur_price - p.avg_price) for p in live)
    if live_exposure >= config.min_exposure_for_drawdown_usdc:
        drawdown = live_unrealized / live_exposure
        if drawdown < -config.max_unrealized_drawdown_ratio:
            reasons.append(f"活仓浮亏占成本 {-drawdown:.0%}（当前被套）")

    bought_in_window = {t.asset for t in tape if t.side == "BUY"}
    recent_dead = [
        p for p in positions
        if p.cur_price <= _DEAD_PRICE and p.asset in bought_in_window and p.size > 0
    ]
    recent_won = [
        p for p in positions
        if p.cur_price >= _WON_PRICE and p.asset in bought_in_window and p.size > 0
    ]
    dead_cost = sum(p.size * p.avg_price for p in recent_dead)
    won_gain = sum(p.size * (p.cur_price - p.avg_price) for p in recent_won)
    window_pnl = stats.realized_pnl + won_gain - dead_cost
    pnl_sample = stats.matched_sells + len(recent_dead) + len(recent_won)
    if pnl_sample >= config.min_pnl_sample and window_pnl < config.min_realized_pnl:
        reasons.append(
            f"窗口净亏损 ${window_pnl:,.2f}"
            f"（回放 {stats.realized_pnl:+,.2f}、近期归零 -{dead_cost:,.2f}、"
            f"未赎回盈利 +{won_gain:,.2f}）"
        )

    if not reasons:
        return verdict  # 基础排除与两条考核规则都没命中
    return Verdict(
        address=stats.address, eligible=False, score=0.0, reasons=reasons,
        stats=stats, exposure_usdc=verdict.exposure_usdc,
        unrealized_pnl=verdict.unrealized_pnl,
    )


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
    spread = stats.median_two_side_spread
    if (
        stats.n_trades >= config.mm_min_trades
        and stats.churn_notional_ratio > config.max_churn_notional_ratio
        and spread is not None
        and spread <= config.mm_thin_spread
    ):
        reasons.append(
            f"疑似慢速做市/流动性提供（{stats.churn_notional_ratio:.0%} 成交额在同一 token "
            f"反复双向循环、点差仅 {spread:.3f}——吃价差而非看方向）"
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
    if (
        win_rate is not None
        and stats.matched_sells >= config.max_win_rate_sample
        and win_rate > config.max_win_rate
    ):
        reasons.append(
            f"胜率 {win_rate:.0%}×{stats.matched_sells} 笔，高得不像方向性交易"
            "（疑似结构性套利）"
        )
    if (
        win_rate is not None
        and stats.matched_sells >= config.arb_min_sample
        and win_rate >= config.arb_min_win_rate
        and stats.high_close_ratio >= config.arb_min_high_close_ratio
    ):
        reasons.append(
            f"疑似跨场馆套利单腿（胜率 {win_rate:.0%}、"
            f"{stats.high_close_ratio:.0%} 的卖出贴近1.0平仓、几乎不割肉；"
            "输腿在别处，本钱包只见赢腿）"
        )
    if exposure >= config.min_exposure_for_drawdown_usdc:
        drawdown = unrealized / exposure
        if drawdown < -config.max_unrealized_drawdown_ratio:
            reasons.append(
                f"持仓浮亏占成本 {-drawdown:.0%}（疑似大量死仓/只认盈不认亏）"
            )

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
