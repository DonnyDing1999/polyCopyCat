"""Polymarket US（美国合规站）公开行情客户端（gateway，无需鉴权）。

Polymarket US 与主站是两个独立平台：跑在 QCEX（CFTC 持牌交易所）上，
账户 KYC、订单簿中心化，没有公开的按用户成交数据，因此不能像主站
那样按地址跟单；能做的是行情消费与两站市场对照。公开行情走 gateway：

    GET https://gateway.polymarket.us/v1/markets
    GET https://gateway.polymarket.us/v1/markets/{slug}/book

价格量纲与主站一致（0~1 美元），但金额字段是对象形式：
``{"value": "0.55", "currency": "USD"}``；订单簿的卖侧键名是
``offers`` 而不是 ``asks``。交易与实时推送在 api.polymarket.us，
需要 Ed25519 API key（polymarket.us/developer 申请，官方 SDK
``polymarket-us``），不在本模块范围内。
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

from .._http import HttpError, get_json

logger = logging.getLogger(__name__)

DEFAULT_US_URL = "https://gateway.polymarket.us"
ENV_US_URL = "POLYCOPYCAT_US_URL"


class UsApiError(HttpError):
    """Polymarket US gateway 请求失败或返回不可用数据。"""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _amount(value: Any, default: float = 0.0) -> float:
    """解析金额对象 {"value": "0.55", "currency": "USD"}，也容忍裸数字。"""
    if isinstance(value, dict):
        value = value.get("value")
    return _as_float(value, default)


@dataclass(frozen=True)
class UsMarket:
    """US 站一个市场（一个 outcome 合约，角色近似主站的一个 token）。"""

    id: int = 0
    slug: str = ""
    title: str = ""
    outcome: str = ""
    active: bool = True
    closed: bool = False
    liquidity: float = 0.0
    volume: float = 0.0
    event_slug: str = ""
    event_title: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any], event: dict[str, Any] | None = None) -> "UsMarket":
        event = event or {}
        return cls(
            id=_as_int(raw.get("id")),
            slug=str(raw.get("slug", "")),
            title=str(raw.get("title", "")),
            outcome=str(raw.get("outcome", "")),
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            liquidity=_as_float(raw.get("liquidity")),
            volume=_as_float(raw.get("volume")),
            event_slug=str(raw.get("eventSlug") or event.get("slug", "")),
            event_title=str(event.get("title", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class UsLevel:
    price: float
    size: float


@dataclass(frozen=True)
class UsBook:
    """订单簿快照；bids 按价格从高到低，asks 从低到高（最优在前）。"""

    market_slug: str = ""
    bids: tuple[UsLevel, ...] = field(default_factory=tuple)
    asks: tuple[UsLevel, ...] = field(default_factory=tuple)
    state: str = ""
    last_trade_px: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "UsBook":
        def levels(items: Any, reverse: bool) -> tuple[UsLevel, ...]:
            out = []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                price = _amount(item.get("px"))
                size = _as_float(item.get("qty"))
                if price > 0 and size > 0:
                    out.append(UsLevel(price=price, size=size))
            out.sort(key=lambda lv: lv.price, reverse=reverse)
            return tuple(out)

        stats = raw.get("stats")
        return cls(
            market_slug=str(raw.get("marketSlug", "")),
            bids=levels(raw.get("bids"), reverse=True),
            asks=levels(raw.get("offers") or raw.get("asks"), reverse=False),
            state=str(raw.get("state", "")),
            last_trade_px=_amount(stats.get("lastTradePx")) if isinstance(stats, dict) else 0.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "state": self.state,
            "last_trade_px": self.last_trade_px,
            "bids": [dataclasses.asdict(lv) for lv in self.bids],
            "asks": [dataclasses.asdict(lv) for lv in self.asks],
        }


@dataclass(frozen=True)
class UsBbo:
    """最优买卖价（GET /v1/markets/{slug}/bbo）。"""

    market_slug: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_depth: int = 0
    ask_depth: int = 0
    last_trade_px: float = 0.0
    shares_traded: float = 0.0
    open_interest: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "UsBbo":
        return cls(
            market_slug=str(raw.get("marketSlug", "")),
            best_bid=_amount(raw.get("bestBid")),
            best_ask=_amount(raw.get("bestAsk")),
            bid_depth=_as_int(raw.get("bidDepth")),
            ask_depth=_as_int(raw.get("askDepth")),
            last_trade_px=_amount(raw.get("lastTradePx")),
            shares_traded=_as_float(raw.get("sharesTraded")),
            open_interest=_as_float(raw.get("openInterest")),
        )

    @property
    def spread(self) -> float | None:
        """买卖价差；有一侧没挂单时为 None。"""
        if self.best_bid > 0 and self.best_ask > 0:
            return round(self.best_ask - self.best_bid, 6)
        return None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["spread"] = self.spread
        return data


@dataclass(frozen=True)
class UsSettlement:
    """市场结算信息（GET /v1/markets/{slug}/settlement）。"""

    market_slug: str = ""
    settlement_price: float = 0.0
    settled_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "UsSettlement":
        return cls(
            market_slug=str(raw.get("marketSlug", "")),
            settlement_price=_amount(raw.get("settlementPrice")),
            settled_at=str(raw.get("settledAt", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class UsApiClient:
    """Polymarket US gateway 只读客户端（结构对齐 DataApiClient）。

    base_url 可通过参数或环境变量 ``POLYCOPYCAT_US_URL`` 覆盖，
    方便走自建代理或在测试里指向本地 mock 服务。
    """

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
            base_url or os.environ.get(ENV_US_URL) or DEFAULT_US_URL
        ).rstrip("/")
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "polyCopyCat (+https://github.com/DonnyDing1999/polyCopyCat)",
                    "Accept": "application/json",
                }
            )
        self._session = session
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.backoff = backoff

    def get_markets(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        active: bool | None = True,
        closed: bool | None = False,
        event_slug: str | None = None,
    ) -> list[UsMarket]:
        """列出市场；active/closed 传 None 表示不按该维度过滤。"""
        params: dict[str, Any] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if active is not None:
            params["active"] = "true" if active else "false"
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        if event_slug:
            params["eventSlug"] = event_slug
        data = self._get("/v1/markets", params)
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, list):
            raise UsApiError(f"预期 /v1/markets 返回 {{markets: [...]}}，实际是: {data!r:.200}")
        return [UsMarket.from_api(m) for m in markets if isinstance(m, dict)]

    def get_market(self, slug: str) -> UsMarket:
        """按 slug 读取单个市场。"""
        data = self._get(f"/v1/market/slug/{slug}")
        market = data.get("market") if isinstance(data, dict) else None
        if not isinstance(market, dict):
            raise UsApiError(f"预期 /v1/market 返回 {{market: {{...}}}}，实际是: {data!r:.200}")
        return UsMarket.from_api(market)

    def get_book(self, slug: str) -> UsBook:
        """订单簿快照（卖侧键名 offers，解析时归一成 asks）。"""
        data = self._get(f"/v1/markets/{slug}/book")
        if not isinstance(data, dict):
            raise UsApiError(f"预期 /book 返回对象，实际是: {data!r:.200}")
        return UsBook.from_api(data)

    def get_bbo(self, slug: str) -> UsBbo:
        """最优买卖价。"""
        data = self._get(f"/v1/markets/{slug}/bbo")
        if not isinstance(data, dict):
            raise UsApiError(f"预期 /bbo 返回对象，实际是: {data!r:.200}")
        return UsBbo.from_api(data)

    def get_settlement(self, slug: str) -> UsSettlement:
        """市场结算价（未结算的市场服务端会报错）。"""
        data = self._get(f"/v1/markets/{slug}/settlement")
        if not isinstance(data, dict):
            raise UsApiError(f"预期 /settlement 返回对象，实际是: {data!r:.200}")
        return UsSettlement.from_api(data)

    def search_markets(
        self,
        query: str,
        *,
        status: str | None = "active",
        limit: int | None = None,
    ) -> list[UsMarket]:
        """全文搜索事件并摊平成市场列表（附带事件标题，供匹配打分用）。

        status 取 active / closed / upcoming，传 None 不过滤。
        """
        params: dict[str, Any] = {"query": query}
        if status:
            params["status"] = status
        if limit:
            params["limit"] = max(1, int(limit))
        data = self._get("/v1/search", params)
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            raise UsApiError(f"预期 /v1/search 返回 {{events: [...]}}，实际是: {data!r:.200}")
        out: list[UsMarket] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if isinstance(market, dict):
                    out.append(UsMarket.from_api(market, event=event))
        return out

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return get_json(
                self._session,
                f"{self.base_url}{path}",
                params=params,
                timeout=self.timeout,
                max_retries=self.max_retries,
                backoff=self.backoff,
            )
        except UsApiError:
            raise
        except HttpError as exc:
            raise UsApiError(str(exc)) from exc
