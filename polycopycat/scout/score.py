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


def wilson_lower_bound(wins: int, n: int, *, z: float = 1.96) -> float:
    """胜率的 Wilson 得分区间下界（默认 z=1.96，即 95% 置信）。

    比裸胜率稳健：样本越小，下界被往下压得越狠——自动惩罚「胜率高但笔数少」的
    运气户（10 战全胜下界仅 ~0.72，100 战 90 胜能到 ~0.82）。n≤0 记 0。
    """
    if n <= 0:
        return 0.0
    phat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1.0 - phat) + z2 / (4 * n)) / n)
    return (centre - margin) / denom


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _holding_fraction(median_s: float, floor_s: float, target_s: float) -> float:
    """持仓时长得分系数：≤floor 得 0，≥target 得 1，中间按 log10 线性插值。"""
    if median_s <= floor_s:
        return 0.0
    if median_s >= target_s:
        return 1.0
    lo, hi = math.log10(floor_s), math.log10(target_s)
    return (math.log10(median_s) - lo) / (hi - lo)


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
    # 套利判定所需的最少配对卖出数。50→15：实证漏网户（100%×19 笔）恰卡在 50 以下全规则
    # 通过；P(15/15 | 真实胜率 85%) ≈ 8.7%，作为保护资金的排除，这点误杀率可接受。
    max_win_rate_sample: int = 15
    max_unrealized_drawdown_ratio: float = 0.5  # 持仓浮亏/成本 超过此比例 = 疑似死仓
    min_exposure_for_drawdown_usdc: float = 500.0  # 死仓判定所需的最小持仓成本
    # 可判性闸（只在招聘口径）：配对卖出少于此数，胜率/盈亏都无从谈起——纯买入或
    # 撒币账户能把所有「有卖出才判」的规则全绕过（500 笔全买入、零卖出的撸空投账户
    # 现在能全规则通过）。堵这个洞。evaluate_health 用 replace(min_judgeable_sells=0)
    # 关掉：在跟目标另有窗口净盈亏规则兜底，不受此闸影响。
    min_judgeable_sells: int = 3
    # 跨场馆/跨账户套利单腿指纹：几乎不割肉（胜率异常高）+ 全在贴近1.0平仓。
    # 这类账户的输腿在别处（另一账户或场外博彩），本钱包只见幸存的赢腿，
    # 招聘版按盈亏/死仓完全看不穿，会给满分——专门一条规则筛掉。
    arb_min_win_rate: float = 0.9          # 胜率高于此才触发套利嫌疑判定
    arb_min_high_close_ratio: float = 0.85  # 卖出里贴近1.0平仓占比高于此
    arb_min_sample: int = 10               # 套利指纹判定所需的最少配对卖出数（20→10，同步收紧）
    # 慢速做市/流动性提供：同一 token 反复双向成交、薄点差吃价差（快进快出抓不到的慢速版）
    max_churn_notional_ratio: float = 0.35  # 深度双向循环 token 成交额占比超此 = 做市嫌疑
    mm_thin_spread: float = 0.06            # 且双向点差薄于此（吃价差而非方向进出）
    mm_min_trades: int = 40                 # 做市判定所需的最少成交笔数（样本足才可信）
    # 持仓中位时长闸：配对卖出≥min_quick_sample 且持仓中位低于此秒数 → 排除。方向无法
    # 延迟复制（跟单必然接刀）。速刷户实证：100%×19 笔×中位 0.5h、100%×9 笔×0.9h——胜率闸
    # 各样本口径都够呛卡得住，这条按持仓时长直接筛。复用 min_quick_sample 作样本下限
    # （同属「快速交易」判定）。evaluate_health 不豁免此规则（在跟目标速刷化就该停）。
    min_median_holding_s: float = 3600.0
    # ---- 打分权重与端点（满分 100 = ROI 30 + Wilson 20 + 持仓 15 + 极端价 10 + 一致性 15 +
    # 新鲜度 10；改权重时自行保证求和）。设计意图见 evaluate 打分段注释：奖励「可延迟复制的
    # 判断」，不奖励「资金大/买热门/手速快」。所有阈值走字段、不在函数里写死魔法数。----
    score_roi_weight: float = 30.0
    score_roi_target: float = 0.10          # 窗口 ROI（realized_pnl / 总成交额）达此拉满
    score_wilson_weight: float = 20.0
    score_wilson_floor: float = 0.5         # Wilson 胜率下界 0.5 起步
    score_wilson_target: float = 0.8        # 0.8 拉满
    score_holding_weight: float = 15.0
    score_holding_floor_s: float = 1800.0   # 持仓中位 ≤ 此得 0
    score_holding_target_s: float = 86400.0  # ≥ 此拉满（中间按 log10 插值）
    score_extreme_weight: float = 10.0
    score_extreme_floor: float = 0.10       # 极端价买入占比 ≤ 此拉满
    score_extreme_ceil: float = 0.50        # ≥ 此得 0
    score_consistency_weight: float = 15.0
    score_consistency_floor: float = 0.20   # top_win_share ≤ 此拉满（盈利分散）
    score_consistency_ceil: float = 0.80    # ≥ 此得 0（一把梭）
    score_recency_weight: float = 10.0
    score_recency_window_h: float = 168.0   # 新鲜度线性衰减窗口（小时）
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
        min_judgeable_sells=0,                        # 考核口径关掉可判性闸（窗口净盈亏兜底）
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
    if (
        stats.matched_sells >= config.min_quick_sample
        and stats.median_holding_s < config.min_median_holding_s
    ):
        reasons.append(
            f"持仓中位仅 {stats.median_holding_s / 60:.0f} 分钟，"
            "方向无法延迟复制（速刷/做市）"
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
    if stats.matched_sells < config.min_judgeable_sells:
        reasons.append(
            f"配对卖出仅 {stats.matched_sells} 笔（< {config.min_judgeable_sells}），"
            "战绩无法自证（纯买入/撒币账户）"
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

    # 打分（满分 100）= ROI 30 + Wilson胜率 20 + 可跟性 25（持仓 15 + 极端价 10）
    #   + 一致性 15 + 新鲜度 10。刻意奖励「可延迟复制的判断」而非「资金大/买热门/手速快」：
    #   绝对盈亏→ROI、裸胜率→Wilson 下界，新增持仓时长/极端价位/盈利集中度三轴，并删掉规模分。
    # 到这里必有配对卖出——招聘口径 min_judgeable_sells≥3 的可判性闸已挡掉纯买入/撒币户；
    # 考核口径 evaluate_health 关掉该闸，纯买入目标在依赖卖出的三轴（ROI/Wilson/一致性）自然
    # 得 0（考核只判 eligible、不按分排序，得 0 无碍），故不再保留 win_rate is None 的兼容分支。
    roi = stats.realized_pnl / stats.notional if stats.notional > 0 else 0.0
    roi_score = config.score_roi_weight * _clamp01(roi / config.score_roi_target)
    win_score = config.score_wilson_weight * _clamp01(
        (wilson_lower_bound(stats.wins, stats.matched_sells) - config.score_wilson_floor)
        / (config.score_wilson_target - config.score_wilson_floor)
    )
    holding_score = config.score_holding_weight * _holding_fraction(
        stats.median_holding_s, config.score_holding_floor_s, config.score_holding_target_s
    )
    extreme_score = config.score_extreme_weight * _clamp01(
        (config.score_extreme_ceil - stats.extreme_price_buy_ratio)
        / (config.score_extreme_ceil - config.score_extreme_floor)
    )
    consistency_score = config.score_consistency_weight * _clamp01(
        (config.score_consistency_ceil - stats.top_win_share)
        / (config.score_consistency_ceil - config.score_consistency_floor)
    )
    hours_idle = (now - stats.last_ts) / 3600 if stats.last_ts else config.score_recency_window_h
    recency_score = config.score_recency_weight * max(
        0.0, 1.0 - hours_idle / config.score_recency_window_h
    )
    score = (
        roi_score + win_score + holding_score
        + extreme_score + consistency_score + recency_score
    )
    return Verdict(
        address=stats.address, eligible=True, score=round(score, 1),
        stats=stats, exposure_usdc=exposure, unrealized_pnl=unrealized,
    )
