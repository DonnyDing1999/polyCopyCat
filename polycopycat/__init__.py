"""polyCopyCat —— Polymarket 跟单系统。

当前已实现：读取任意地址在 Polymarket 上的下单（成交记录），
支持轮询监控与实时推送（WebSocket）两种通道，作为自动跟单的信号源。
"""

from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade
from .stream import TradeStream
from .us import UsApiClient, UsApiError
from .watcher import TradeWatcher

__version__ = "0.20.0"

__all__ = [
    "DataApiClient",
    "DataApiError",
    "Trade",
    "TradeStream",
    "TradeWatcher",
    "UsApiClient",
    "UsApiError",
    "normalize_address",
    "__version__",
]
