"""polyCopyCat —— Polymarket 跟单系统。

当前已实现：读取任意地址在 Polymarket 上的下单（成交记录），
并支持轮询监控新成交，作为后续自动跟单的信号源。
"""

from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade
from .watcher import TradeWatcher

__version__ = "0.1.0"

__all__ = [
    "DataApiClient",
    "DataApiError",
    "Trade",
    "TradeWatcher",
    "normalize_address",
    "__version__",
]
