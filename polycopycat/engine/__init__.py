"""跟单引擎：把监控到的目标成交变成自己的下单决策并执行。

流水线：信号过滤 → 仓位计算 → 风控闸门 → 执行器（纸面/实盘）→ 账本。
"""

from .config import ConfigError, EngineConfig, load_config
from .engine import CopyEngine

__all__ = ["ConfigError", "CopyEngine", "EngineConfig", "load_config"]
