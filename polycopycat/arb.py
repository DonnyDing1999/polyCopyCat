"""Polymarket 互补对套利扫描器（只读研究工具，不下单）。

原理：二元市场的 Yes 与 No 两个 token 恰有一边最终赎回 $1，所以
任何时刻两边合理价格之和应约等于 $1。偏离就是套利空间：

- ``ask(Yes) + ask(No) < $1``：两边同时买入，无论结果如何锁定利润
  （买对冲，buy_pair）
- ``bid(Yes) + bid(No) > $1``：用 $1 抵押铸出一对 Yes+No 分别卖掉
  （铸造套利，mint_sell——需要链上 splitPosition，本工具只提示）

定位是研究工具：这类价差是速度游戏，肉眼可见的机会通常几百毫秒内
被专业机器人吃掉。扫描的意义是量化"现在还剩多少、量级多大"，为
要不要做执行版提供数据；顺带解释了为什么有些地址胜率能到 100%。

市场发现走 Gamma API（官方市场目录，可按活跃度/成交量过滤），
订单簿走 CLOB 批量接口。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

from ._http import HttpError, get_json
from .engine.clob import ClobReadClient

logger = logging.getLogger(__name__)

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
ENV_GAMMA_URL = "POLYCOPYCAT_GAMMA_URL"

_GAMMA_PAGE = 500


class ArbError(HttpError):
    """市场发现或扫描失败。"""


@dataclass(frozen=True)
class ArbOpportunity:
    kind: str            # buy_pair / mint_sell
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    price_yes: float     # buy_pair 用卖一价，mint_sell 用买一价
    price_no: float
    edge_per_pair: float  # 每对锁定的利润（USDC）
    max_pairs: float      # 顶档深度能吃到的对数
    profit_usdc: float    # edge × pairs
    neg_risk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "condition_id": self.condition_id,
            "question": self.question,
            "yes_token": self.yes_token,
            "no_token": self.no_token,
            "price_yes": self.price_yes,
            "price_no": self.price_no,
            "edge_per_pair": self.edge_per_pair,
            "max_pairs": self.max_pairs,
            "profit_usdc": self.profit_usdc,
            "neg_risk": self.neg_risk,
        }


def _parse_json_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


class ArbScanner:
    def __init__(
        self,
        clob: ClobReadClient,
        *,
        gamma_url: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._clob = clob
        self.gamma_url = (
            gamma_url or os.environ.get(ENV_GAMMA_URL) or DEFAULT_GAMMA_URL
        ).rstrip("/")
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
        self._session = session
        self.timeout = timeout

    def discover_markets(self, limit: int = 500) -> list[dict[str, Any]]:
        """从 Gamma 拉活跃二元市场（按 24h 成交量降序），解析出 token 对。

        注意：服务端会对单页条数封顶（实测 100，低于请求值），所以
        「页没满」不代表到底了——只有空页或整页重复才停。
        """
        markets: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        for _ in range(100):  # 翻页护栏
            if len(markets) >= limit:
                break
            try:
                data = get_json(
                    self._session, f"{self.gamma_url}/markets",
                    params={
                        "active": "true", "closed": "false",
                        "order": "volume24hr", "ascending": "false",
                        "limit": min(_GAMMA_PAGE, limit - len(markets)),
                        "offset": offset,
                    },
                    timeout=self.timeout,
                )
            except HttpError as exc:
                raise ArbError(f"拉取市场目录失败: {exc}") from exc
            if not isinstance(data, list):
                raise ArbError(f"预期市场目录返回列表，实际是: {data!r:.120}")
            if not data:
                break
            fresh = 0
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                condition_id = str(raw.get("conditionId", ""))
                if not condition_id or condition_id in seen:
                    continue
                seen.add(condition_id)
                tokens = [str(t) for t in _parse_json_list(raw.get("clobTokenIds"))]
                if len(tokens) != 2:
                    continue  # 只做二元互补对
                fresh += 1
                markets.append({
                    "condition_id": condition_id,
                    "question": str(raw.get("question", "")),
                    "neg_risk": bool(raw.get("negRisk", False)),
                    "tokens": tokens,
                })
            if fresh == 0:
                break  # 服务端忽略 offset 或已到底，防止空转
            offset += len(data)
        return markets[:limit]

    def scan(
        self,
        *,
        max_markets: int = 500,
        min_edge: float = 0.005,
        min_profit: float = 0.5,
    ) -> list[ArbOpportunity]:
        """扫一轮快照，返回按可锁定利润降序的机会列表。

        - min_edge：每对至少要有的价差（价格量纲，0.005 = 半分钱）
        - min_profit：顶档深度下至少可锁定的总利润（USDC）
        """
        markets = self.discover_markets(limit=max_markets)
        token_ids = [token for m in markets for token in m["tokens"]]
        logger.info("市场 %d 个（token %d 个），拉取订单簿……", len(markets), len(token_ids))
        books = self._clob.get_books(token_ids)

        opportunities: list[ArbOpportunity] = []
        for market in markets:
            yes_token, no_token = market["tokens"]
            book_yes = books.get(yes_token)
            book_no = books.get(no_token)
            if book_yes is None or book_no is None:
                continue

            if book_yes.asks and book_no.asks:
                ask_yes, ask_no = book_yes.asks[0], book_no.asks[0]
                total = ask_yes.price + ask_no.price
                if total < 1.0 - min_edge:
                    pairs = min(ask_yes.size, ask_no.size)
                    profit = (1.0 - total) * pairs
                    if profit >= min_profit:
                        opportunities.append(ArbOpportunity(
                            kind="buy_pair",
                            condition_id=market["condition_id"],
                            question=market["question"],
                            yes_token=yes_token, no_token=no_token,
                            price_yes=ask_yes.price, price_no=ask_no.price,
                            edge_per_pair=round(1.0 - total, 6),
                            max_pairs=round(pairs, 2),
                            profit_usdc=round(profit, 2),
                            neg_risk=market["neg_risk"],
                        ))

            if book_yes.bids and book_no.bids:
                bid_yes, bid_no = book_yes.bids[0], book_no.bids[0]
                total = bid_yes.price + bid_no.price
                if total > 1.0 + min_edge:
                    pairs = min(bid_yes.size, bid_no.size)
                    profit = (total - 1.0) * pairs
                    if profit >= min_profit:
                        opportunities.append(ArbOpportunity(
                            kind="mint_sell",
                            condition_id=market["condition_id"],
                            question=market["question"],
                            yes_token=yes_token, no_token=no_token,
                            price_yes=bid_yes.price, price_no=bid_no.price,
                            edge_per_pair=round(total - 1.0, 6),
                            max_pairs=round(pairs, 2),
                            profit_usdc=round(profit, 2),
                            neg_risk=market["neg_risk"],
                        ))

        opportunities.sort(key=lambda o: -o.profit_usdc)
        return opportunities
