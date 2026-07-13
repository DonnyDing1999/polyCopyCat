"""Polymarket CLOB 只读客户端：市场元数据与订单簿（公开接口，无需鉴权）。

    GET https://clob.polymarket.com/markets/{condition_id}
    GET https://clob.polymarket.com/book?token_id=...

下单前必须过一遍市场元数据（tick、最小量、negRisk、是否接单），
纸面模式还要用订单簿模拟成交。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .._http import HttpError, get_json

logger = logging.getLogger(__name__)

DEFAULT_CLOB_URL = "https://clob.polymarket.com"
ENV_CLOB_URL = "POLYCOPYCAT_CLOB_URL"

_MARKET_CACHE_TTL = 600.0


class ClobError(HttpError):
    """CLOB 请求失败或返回不可用数据。"""


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MarketInfo:
    """下单需要的市场约束（以及结算后的 winner，用于纸面自动结算）。"""

    condition_id: str
    tick_size: float          # 价格步长（0.01 或 0.001）
    min_size: float           # 最小下单份额
    neg_risk: bool            # 多结果事件走 NegRisk 合约，下单要带标志
    accepting_orders: bool
    closed: bool
    slug: str = ""
    question: str = ""
    winner_token_ids: tuple[str, ...] = ()  # 已结算市场的获胜 token（未结算为空）

    @property
    def resolved(self) -> bool:
        """已关闭且知道谁赢了，纸面持仓可以按结算价入账。"""
        return self.closed and bool(self.winner_token_ids)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "MarketInfo":
        winners = tuple(
            str(t.get("token_id", ""))
            for t in raw.get("tokens") or []
            if isinstance(t, dict) and t.get("winner") and t.get("token_id")
        )
        return cls(
            condition_id=str(raw.get("condition_id") or raw.get("conditionId") or ""),
            tick_size=_as_float(raw.get("minimum_tick_size"), 0.01),
            min_size=_as_float(raw.get("minimum_order_size"), 5.0),
            neg_risk=bool(raw.get("neg_risk", False)),
            accepting_orders=bool(raw.get("accepting_orders", True)),
            closed=bool(raw.get("closed", False)),
            slug=str(raw.get("market_slug", "")),
            question=str(raw.get("question", "")),
            winner_token_ids=winners,
        )


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    """订单簿快照；bids 按价格从高到低，asks 从低到高（最优在前）。"""

    bids: tuple[BookLevel, ...] = field(default_factory=tuple)
    asks: tuple[BookLevel, ...] = field(default_factory=tuple)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "OrderBook":
        def levels(key: str, reverse: bool) -> tuple[BookLevel, ...]:
            out = []
            for item in raw.get(key) or []:
                if not isinstance(item, dict):
                    continue
                price = _as_float(item.get("price"), 0.0)
                size = _as_float(item.get("size"), 0.0)
                if price > 0 and size > 0:
                    out.append(BookLevel(price=price, size=size))
            out.sort(key=lambda lv: lv.price, reverse=reverse)
            return tuple(out)

        return cls(bids=levels("bids", reverse=True), asks=levels("asks", reverse=False))


class ClobReadClient:
    """带缓存的 CLOB 只读客户端；市场元数据缓存 10 分钟，订单簿实时拉。"""

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
            base_url or os.environ.get(ENV_CLOB_URL) or DEFAULT_CLOB_URL
        ).rstrip("/")
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
        self._session = session
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._market_cache: dict[str, tuple[float, MarketInfo]] = {}

    def ping(self) -> tuple[bool, str]:
        """启动自检：探测 CLOB 是否可达，区分「正常可达」与「被拦截」。

        引擎离不开 CLOB（市场元数据 + 订单簿），而它常被公司网关/地区
        策略单独封掉（返回 403 + HTML 拦截页）。返回 (是否可达, 说明)。
        """
        try:
            r = self._session.get(f"{self.base_url}/ok", timeout=min(self.timeout, 8))
        except requests.RequestException as exc:
            return False, f"连接失败（{exc.__class__.__name__}）"
        headers = getattr(r, "headers", {}) or {}
        ctype = str(headers.get("content-type", "")).lower()
        if r.status_code in (401, 403, 407, 451) and "html" in ctype:
            server = headers.get("Server") or headers.get("server") or ""
            hint = f"（网关 {server}）" if server else ""
            return False, (
                f"被拦截 HTTP {r.status_code}{hint}——疑似网络/公司网关/地区策略封了 clob 域"
            )
        if r.status_code >= 500:
            return False, f"服务端错误 HTTP {r.status_code}"
        return True, f"可达（HTTP {r.status_code}）"

    def get_market(self, condition_id: str, *, fresh: bool = False) -> MarketInfo:
        """fresh=True 跳过缓存直查（结算检查用：缓存里的市场可能刚刚关闭）。"""
        cached = self._market_cache.get(condition_id)
        if not fresh and cached and time.monotonic() - cached[0] < _MARKET_CACHE_TTL:
            return cached[1]
        data = self._get(f"/markets/{condition_id}")
        if not isinstance(data, dict):
            raise ClobError(f"预期 /markets 返回对象，实际是: {data!r:.200}")
        market = MarketInfo.from_api(data)
        self._market_cache[condition_id] = (time.monotonic(), market)
        return market

    def get_book(self, token_id: str) -> OrderBook:
        data = self._get("/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            raise ClobError(f"预期 /book 返回对象，实际是: {data!r:.200}")
        return OrderBook.from_api(data)

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
        except ClobError:
            raise
        except HttpError as exc:
            raise ClobError(str(exc)) from exc
