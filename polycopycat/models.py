"""Data API 返回数据的结构化模型。"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


@dataclass(frozen=True)
class Trade:
    """目标地址在 Polymarket 上的一笔成交。

    字段对应 Data API ``GET /trades`` 的返回（camelCase 转 snake_case）。
    解析对缺失/异型字段宽容处理，上游加减字段不至于让监控进程崩掉。
    """

    proxy_wallet: str
    side: str            # BUY / SELL
    asset: str           # outcome token id
    condition_id: str    # 市场 condition id
    size: float          # 成交数量（份额）
    price: float         # 成交价格（0 ~ 1）
    timestamp: int       # unix 秒
    title: str = ""      # 市场标题
    slug: str = ""
    event_slug: str = ""
    outcome: str = ""    # 买的哪个结果，如 "Yes"
    outcome_index: int = -1
    transaction_hash: str = ""
    trader_name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Trade":
        return cls(
            proxy_wallet=str(raw.get("proxyWallet", "")).lower(),
            side=str(raw.get("side", "")).upper(),
            asset=str(raw.get("asset", "")),
            condition_id=str(raw.get("conditionId", "")),
            size=_as_float(raw.get("size")),
            price=_as_float(raw.get("price")),
            timestamp=_as_int(raw.get("timestamp")),
            title=str(raw.get("title", "")),
            slug=str(raw.get("slug", "")),
            event_slug=str(raw.get("eventSlug", "")),
            outcome=str(raw.get("outcome", "")),
            outcome_index=_as_int(raw.get("outcomeIndex"), default=-1),
            transaction_hash=str(raw.get("transactionHash", "")),
            trader_name=str(raw.get("name") or raw.get("pseudonym") or ""),
        )

    @property
    def notional(self) -> float:
        """成交金额（USDC）= 数量 × 价格。"""
        return self.size * self.price

    @property
    def key(self) -> tuple:
        """去重键。

        同一笔链上交易（transactionHash）可能拆出多条成交记录，
        所以把资产、方向、数量、价格、时间一并纳入。
        """
        return (
            self.transaction_hash,
            self.asset,
            self.side,
            f"{self.size:.10g}",
            f"{self.price:.10g}",
            self.timestamp,
        )

    @property
    def time_utc(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["notional"] = round(self.notional, 6)
        data["time_utc"] = self.time_utc
        return data


@dataclass(frozen=True)
class Position:
    """某地址在一个 outcome token 上的持仓（Data API ``GET /positions``）。"""

    proxy_wallet: str
    asset: str           # outcome token id
    condition_id: str
    size: float          # 持有份额
    avg_price: float     # 平均建仓价
    cur_price: float = 0.0
    realized_pnl: float = 0.0
    redeemable: bool = False  # 市场已结算，可赎回
    title: str = ""
    outcome: str = ""
    event_slug: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Position":
        return cls(
            proxy_wallet=str(raw.get("proxyWallet", "")).lower(),
            asset=str(raw.get("asset", "")),
            condition_id=str(raw.get("conditionId", "")),
            size=_as_float(raw.get("size")),
            avg_price=_as_float(raw.get("avgPrice")),
            cur_price=_as_float(raw.get("curPrice")),
            realized_pnl=_as_float(raw.get("realizedPnl")),
            redeemable=bool(raw.get("redeemable", False)),
            title=str(raw.get("title", "")),
            outcome=str(raw.get("outcome", "")),
            event_slug=str(raw.get("eventSlug", "")),
        )
