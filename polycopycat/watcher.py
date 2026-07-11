"""轮询监控目标地址的新成交。"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Iterable

from .data_api import DataApiClient, DataApiError, normalize_address
from .models import Trade

logger = logging.getLogger(__name__)

OnTrade = Callable[[Trade], None]

# 每个地址在内存里保留多少条已见成交键用于去重
_SEEN_CAP = 5000
_DEFAULT_PAGE = 100


class TradeWatcher:
    """盯住若干地址，发现新成交时回调 ``on_trade``（跟单逻辑之后挂这里）。

    实现：定期拉取每个地址最近一页成交（Data API 按时间从新到旧返回），
    用「已见成交键」做增量去重。首次轮询只建立基线，不把历史刷成新成交；
    传 ``backfill=N`` 可以在启动时先回放最近 N 条历史。
    """

    def __init__(
        self,
        client: DataApiClient,
        addresses: Iterable[str],
        *,
        on_trade: OnTrade | None = None,
        poll_interval: float = 10.0,
        backfill: int = 0,
        page_size: int = _DEFAULT_PAGE,
    ) -> None:
        unique: dict[str, None] = {}
        for address in addresses:
            unique.setdefault(normalize_address(address), None)
        self._addresses = list(unique)
        if not self._addresses:
            raise ValueError("至少需要一个监控地址")
        self._client = client
        self._on_trade = on_trade
        self.poll_interval = float(poll_interval)
        self.backfill = max(0, int(backfill))
        self.page_size = page_size
        self._seen: dict[str, set[tuple]] = {a: set() for a in self._addresses}
        self._seen_order: dict[str, deque[tuple]] = {a: deque() for a in self._addresses}
        self._baselined: set[str] = set()

    @property
    def addresses(self) -> list[str]:
        return list(self._addresses)

    def poll_once(self) -> list[Trade]:
        """轮询一轮所有地址，按时间升序返回（并回调）新增成交。

        某个地址请求失败只影响本轮该地址，下一轮继续。
        """
        new: list[Trade] = []
        for address in self._addresses:
            try:
                trades = self._client.get_trades(address, limit=self.page_size)
            except DataApiError as exc:
                logger.warning("拉取 %s 的成交失败，本轮跳过: %s", address, exc)
                continue
            new.extend(self._diff(address, trades))
        new.sort(key=lambda t: t.timestamp)
        if self._on_trade:
            for trade in new:
                self._on_trade(trade)
        return new

    def run_forever(self) -> None:
        logger.info(
            "开始监控 %d 个地址，每 %.1f 秒轮询一次: %s",
            len(self._addresses), self.poll_interval, ", ".join(self._addresses),
        )
        while True:
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.poll_interval - elapsed))

    def _diff(self, address: str, trades: list[Trade]) -> list[Trade]:
        """把一页成交（新→旧）与已见集合比对，返回本轮要上报的部分。"""
        first_poll = address not in self._baselined
        seen = self._seen[address]
        order = self._seen_order[address]
        fresh: list[Trade] = []
        for trade in trades:
            if trade.key in seen:
                continue
            seen.add(trade.key)
            order.append(trade.key)
            fresh.append(trade)
        while len(order) > _SEEN_CAP:
            seen.discard(order.popleft())

        if first_poll:
            self._baselined.add(address)
            logger.info("%s 基线建立完成，已缓存最近 %d 条成交", address, len(fresh))
            # 页内新→旧，取最前面的 backfill 条即最近的 N 条
            return fresh[: self.backfill] if self.backfill else []

        if fresh and len(fresh) == len(trades):
            logger.warning(
                "%s 本轮 %d 条全部是新成交，可能超出了单页窗口有遗漏，"
                "建议缩短轮询间隔或调大 page_size",
                address, len(fresh),
            )
        return fresh
