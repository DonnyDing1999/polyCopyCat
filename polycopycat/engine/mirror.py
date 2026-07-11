"""目标持仓镜像：跟踪每个目标地址在各 outcome token 上的持仓量。

卖出跟随的核心依据：目标卖掉了他持仓的百分之几，我们就卖掉自己
持仓的百分之几。镜像有两条更新路径：

- 每笔目标成交实时累加/扣减（engine 处理信号时调用 apply_trade）；
- 定期用 Data API ``/positions`` 全量对账覆盖（reconcile），兜住
  merge/redeem 等不会出现在成交流里的仓位变化。

引擎线程与对账线程并发读写，内部加锁。
"""

from __future__ import annotations

import logging
import threading

from ..models import Trade

logger = logging.getLogger(__name__)


class TargetMirror:
    def __init__(self) -> None:
        self._sizes: dict[tuple[str, str], float] = {}  # (地址, token) -> 份额
        self._lock = threading.Lock()

    def size_of(self, address: str, token_id: str) -> float:
        with self._lock:
            return self._sizes.get((address.lower(), token_id), 0.0)

    def apply_trade(self, trade: Trade) -> float:
        """按一笔目标成交更新镜像，返回更新前的持仓量（卖出分数用）。"""
        key = (trade.proxy_wallet, trade.asset)
        with self._lock:
            prev = self._sizes.get(key, 0.0)
            if trade.side == "BUY":
                new = prev + trade.size
            elif trade.side == "SELL":
                new = max(0.0, prev - trade.size)
            else:
                return prev
            if new <= 1e-9:
                self._sizes.pop(key, None)
            else:
                self._sizes[key] = new
            return prev

    def replace(self, address: str, sizes: dict[str, float]) -> None:
        """用全量快照覆盖某地址的镜像（对账路径）。"""
        address = address.lower()
        with self._lock:
            for key in [k for k in self._sizes if k[0] == address]:
                del self._sizes[key]
            for token_id, size in sizes.items():
                if size > 1e-9:
                    self._sizes[(address, token_id)] = float(size)

    def snapshot(self, address: str) -> dict[str, float]:
        address = address.lower()
        with self._lock:
            return {k[1]: v for k, v in self._sizes.items() if k[0] == address}
