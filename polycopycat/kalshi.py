"""Kalshi 只读客户端（公开行情，无需鉴权）。

    GET https://api.elections.kalshi.com/trade-api/v2/markets
    GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook

价格量纲：Kalshi 用整数美分（1~99），这里统一换算成 0~1 美元。
订单簿模型与 Polymarket 不同：只有 Yes 侧买单和 No 侧买单两列，
「买 Yes 的卖一价」= 1 − No 侧最高买价（吃掉对面的买单即成交）。

跨所套利要计费：Kalshi taker 手续费约为 0.07 × P × (1−P) 每张
（官方按总额向上取整到美分，这里用连续近似，偏保守側再由
min_edge 兜底）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

from ._http import HttpError, get_json

logger = logging.getLogger(__name__)

DEFAULT_KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
ENV_KALSHI_URL = "POLYCOPYCAT_KALSHI_URL"

_PAGE = 1000


class KalshiError(HttpError):
    """Kalshi 请求失败或返回不可用数据。"""


def taker_fee(price: float) -> float:
    """单张合约的 taker 手续费（美元，连续近似）。"""
    price = min(max(price, 0.0), 1.0)
    return 0.07 * price * (1.0 - price)


def _cents(value: Any) -> float | None:
    """整数美分 → 美元；0 / 缺失 / 越界视为无报价。"""
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return None
    if cents <= 0 or cents >= 100:
        return None
    return cents / 100.0


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    close_time: str        # ISO 字符串
    yes_bid: float | None  # 美元
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    volume_24h: float
    liquidity: float
    status: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "KalshiMarket":
        return cls(
            ticker=str(raw.get("ticker", "")),
            event_ticker=str(raw.get("event_ticker", "")),
            title=str(raw.get("title", "")),
            subtitle=str(raw.get("subtitle") or raw.get("yes_sub_title") or ""),
            close_time=str(raw.get("close_time", "")),
            yes_bid=_cents(raw.get("yes_bid")),
            yes_ask=_cents(raw.get("yes_ask")),
            no_bid=_cents(raw.get("no_bid")),
            no_ask=_cents(raw.get("no_ask")),
            volume_24h=float(raw.get("volume_24h") or 0),
            liquidity=float(raw.get("liquidity") or 0),
            status=str(raw.get("status", "")),
        )


@dataclass(frozen=True)
class KalshiLevel:
    price: float  # 美元
    count: float  # 张数


@dataclass(frozen=True)
class KalshiBook:
    """Kalshi 订单簿：两列买单（yes_bids / no_bids），按价格从高到低。

    - 买 Yes 的卖一价 = 1 − no_bids[0].price，可吃数量 = no_bids[0].count
    - 买 No  的卖一价 = 1 − yes_bids[0].price，同理
    """

    yes_bids: tuple[KalshiLevel, ...] = ()
    no_bids: tuple[KalshiLevel, ...] = ()

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "KalshiBook":
        def levels(rows: Any) -> tuple[KalshiLevel, ...]:
            out = []
            for row in rows or []:
                try:
                    price, count = _cents(row[0]), float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if price is not None and count > 0:
                    out.append(KalshiLevel(price=price, count=count))
            out.sort(key=lambda lv: lv.price, reverse=True)
            return tuple(out)

        book = raw.get("orderbook") if isinstance(raw.get("orderbook"), dict) else raw
        return cls(
            yes_bids=levels(book.get("yes")),
            no_bids=levels(book.get("no")),
        )

    def ask(self, side: str) -> KalshiLevel | None:
        """买入 side（yes/no）的最优卖价与可吃数量（由对面买单换算）。"""
        opposite = self.no_bids if side == "yes" else self.yes_bids
        if not opposite:
            return None
        best = opposite[0]
        return KalshiLevel(price=round(1.0 - best.price, 6), count=best.count)


class KalshiClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get(ENV_KALSHI_URL) or DEFAULT_KALSHI_URL
        ).rstrip("/")
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
        self._session = session
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff

    def get_markets(self, *, status: str = "open", max_markets: int = 1000) -> list[KalshiMarket]:
        """分页拉市场列表（自带顶档报价，扫描用它就够了）。"""
        markets: list[KalshiMarket] = []
        cursor = ""
        for _ in range(100):  # 翻页护栏
            if len(markets) >= max_markets:
                break
            params: dict[str, Any] = {
                "status": status,
                "limit": min(_PAGE, max_markets - len(markets)),
            }
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            rows = data.get("markets") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise KalshiError(f"预期 markets 列表，实际是: {data!r:.120}")
            if not rows:
                break
            markets.extend(
                KalshiMarket.from_api(row) for row in rows if isinstance(row, dict)
            )
            cursor = str(data.get("cursor") or "")
            if not cursor:
                break
        return markets[:max_markets]

    def get_orderbook(self, ticker: str) -> KalshiBook:
        data = self._get(f"/markets/{ticker}/orderbook", None)
        if not isinstance(data, dict):
            raise KalshiError(f"预期订单簿对象，实际是: {data!r:.120}")
        return KalshiBook.from_api(data)

    def _get(self, path: str, params: dict[str, Any] | None) -> Any:
        try:
            return get_json(
                self._session, f"{self.base_url}{path}", params=params,
                timeout=self.timeout, max_retries=self.max_retries, backoff=self.backoff,
            )
        except KalshiError:
            raise
        except HttpError as exc:
            raise KalshiError(str(exc)) from exc
