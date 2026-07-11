"""Polymarket Data API 客户端（只读、公开数据、无需鉴权）。

Polymarket 的成交都在 Polygon 链上，官方 Data API 把任意地址的
成交/持仓做成了公开接口，读别人的下单就是查这个：

    GET https://data-api.polymarket.com/trades?user=<proxy_wallet>

地址用 Polymarket 个人主页 URL 里的那个 0x 地址（proxy wallet）。
实时性要求更高时后续可换 CLOB WebSocket，这里先用轮询打底。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

from ._http import HttpError, get_json
from .models import Position, Trade

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data-api.polymarket.com"
ENV_BASE_URL = "POLYCOPYCAT_DATA_API_URL"

# /trades 单页条数上限
MAX_PAGE_LIMIT = 500

_ADDRESS_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


class DataApiError(HttpError):
    """Data API 请求失败（重试后仍失败或返回不可用数据）。"""


def normalize_address(address: str) -> str:
    """校验并归一化地址（Polymarket 的 proxy wallet），统一转小写。"""
    address = address.strip()
    if not _ADDRESS_RE.match(address):
        raise ValueError(f"不是合法的地址: {address!r}（应为 0x + 40 位十六进制）")
    return address.lower()


class DataApiClient:
    """带超时和重试的 Data API 客户端。

    base_url 可通过参数或环境变量 ``POLYCOPYCAT_DATA_API_URL`` 覆盖，
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
            base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
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

    def get_trades(
        self,
        user: str,
        *,
        limit: int = 100,
        offset: int = 0,
        taker_only: bool = True,
        market: str | None = None,
        side: str | None = None,
    ) -> list[Trade]:
        """读取某地址最近的成交记录，按时间从新到旧返回。

        - taker_only=True（Data API 默认）只看主动成交，避免同一笔
          交易的 maker 侧重复出现；想看全部成交传 False。
        - market 传市场的 condition id 可只看某个市场。
        """
        params: dict[str, Any] = {
            "user": normalize_address(user),
            "limit": max(1, min(int(limit), MAX_PAGE_LIMIT)),
            "offset": max(0, int(offset)),
            "takerOnly": "true" if taker_only else "false",
        }
        if market:
            params["market"] = market
        if side:
            params["side"] = side.upper()
        data = self._get("/trades", params)
        if not isinstance(data, list):
            raise DataApiError(f"预期 /trades 返回列表，实际是: {data!r:.200}")
        return [Trade.from_api(item) for item in data if isinstance(item, dict)]

    def get_positions(
        self,
        user: str,
        *,
        limit: int = MAX_PAGE_LIMIT,
        offset: int = 0,
        market: str | None = None,
    ) -> list[Position]:
        """读取某地址当前持仓（跟单里用于目标持仓镜像与实盘对账）。"""
        params: dict[str, Any] = {
            "user": normalize_address(user),
            "limit": max(1, min(int(limit), MAX_PAGE_LIMIT)),
            "offset": max(0, int(offset)),
        }
        if market:
            params["market"] = market
        data = self._get("/positions", params)
        if not isinstance(data, list):
            raise DataApiError(f"预期 /positions 返回列表，实际是: {data!r:.200}")
        return [Position.from_api(item) for item in data if isinstance(item, dict)]

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            return get_json(
                self._session,
                f"{self.base_url}{path}",
                params=params,
                timeout=self.timeout,
                max_retries=self.max_retries,
                backoff=self.backoff,
            )
        except DataApiError:
            raise
        except HttpError as exc:
            raise DataApiError(str(exc)) from exc
