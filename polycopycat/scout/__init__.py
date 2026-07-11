"""scout：寻找值得跟单的地址。

从全站成交流 / 排行榜收集候选地址，回放每个地址的公开成交带评估
战绩与风格，排除做市/套利/亏损/低样本地址，按分数排序输出。
"""

from .metrics import TraderStats, replay
from .runner import (
    ScoutError,
    candidates_from_leaderboard,
    candidates_from_recent_trades,
    scout_addresses,
    targets_snippet,
)
from .score import ScoutConfig, Verdict, evaluate

__all__ = [
    "ScoutConfig",
    "ScoutError",
    "TraderStats",
    "Verdict",
    "candidates_from_leaderboard",
    "candidates_from_recent_trades",
    "evaluate",
    "replay",
    "scout_addresses",
    "targets_snippet",
]
