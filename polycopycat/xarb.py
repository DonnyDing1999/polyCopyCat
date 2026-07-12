"""Kalshi × Polymarket 跨所复合套利扫描器（只读，不下单）。

同一个现实事件在两个所都有二元市场时，「Polymarket 买 Yes + Kalshi
买 No」（或反向组合）构成一对完全对冲：事件真假各赎回一边的 $1。
若两腿成本（含 Kalshi 手续费）之和 < $1，即为跨所锁定利润。

与站内套利的本质区别：跨所价差**不能**被单个机器人瞬间抹平——
两边开户、KYC、资金分踞两个体系，摩擦保护了价差。但代价是一个
新的、更危险的风险源：**配对错误**。两边"看起来一样"的市场，
结算条款可能存在细微差异（数据源、截止时间、措辞），配错对不是
套利而是双边敞口，可能两腿全亏。

因此本模块分两层：

- 建议层 ``suggest_pairs``：按标题相似度 + 截止时间接近度自动提名
  候选配对，**只提名，不采信**；
- 精确层 ``scan_pairs``：只对人工确认过的配对文件（xarb-pairs.json）
  拉两边订单簿算精确价差（Kalshi taker 手续费计入成本）。

结算条款是否真正等价，永远需要人读完两边的规则文本再确认。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .arb import ArbScanner
from .engine.clob import ClobReadClient
from .kalshi import KalshiClient, taker_fee

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "on", "in", "of", "to", "at", "vs",
    "for", "or", "and", "before", "after", "does", "do", "is", "are", "than",
    "more", "up", "down", "2025", "2026",
}


class PairsError(ValueError):
    """配对文件缺失或格式错误。"""


@dataclass(frozen=True)
class PairConfig:
    """一条人工确认过的跨所配对。"""

    poly_condition_id: str
    kalshi_ticker: str
    poly_yes_index: int = 0  # Polymarket 哪个 outcome 对应 Kalshi 的 Yes
    note: str = ""


def load_pairs(path: str | Path) -> list[PairConfig]:
    path = Path(path)
    if not path.exists():
        raise PairsError(
            f"配对文件不存在: {path}（可从 xarb-pairs.example.json 复制；"
            "先用 --suggest 生成候选，人工核对两边结算条款后再填入）"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairsError(f"读取配对文件失败: {exc}") from exc
    rows = raw.get("pairs") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise PairsError("配对文件应为 {\"pairs\": [...]} 或数组")
    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pairs.append(PairConfig(
                poly_condition_id=str(row["poly_condition_id"]),
                kalshi_ticker=str(row["kalshi_ticker"]),
                poly_yes_index=int(row.get("poly_yes_index", 0)),
                note=str(row.get("note", "")),
            ))
        except KeyError as exc:
            raise PairsError(f"配对缺少字段 {exc}: {row!r}") from exc
    return pairs


@dataclass(frozen=True)
class XarbOpportunity:
    poly_condition_id: str
    kalshi_ticker: str
    combo: str            # poly_yes+kalshi_no / kalshi_yes+poly_no
    poly_price: float     # Polymarket 腿的卖一价
    kalshi_price: float   # Kalshi 腿的卖一价（由对面买单换算）
    kalshi_fee: float     # 每张手续费
    edge_per_pair: float  # 每对锁定利润（已扣费）
    max_pairs: float      # 两腿顶档深度的较小值
    profit_usdc: float
    poly_question: str = ""
    kalshi_title: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "poly_condition_id": self.poly_condition_id,
            "kalshi_ticker": self.kalshi_ticker,
            "combo": self.combo,
            "poly_price": self.poly_price,
            "kalshi_price": self.kalshi_price,
            "kalshi_fee": self.kalshi_fee,
            "edge_per_pair": self.edge_per_pair,
            "max_pairs": self.max_pairs,
            "profit_usdc": self.profit_usdc,
            "poly_question": self.poly_question,
            "kalshi_title": self.kalshi_title,
            "note": self.note,
        }


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", text.lower().replace(",", ""))  # 归一化 65,000
    return {w for w in words if w and w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """标题相似度：词集 Jaccard + 数字 token 严格一致性加权。

    数字集合必须完全相等才加分，任何不一致都重罚——
    "above 64800" 和 "above 65000" 是两个不同的市场，配错就是敞口。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    na = {t for t in ta if any(c.isdigit() for c in t)}
    nb = {t for t in tb if any(c.isdigit() for c in t)}
    if na or nb:
        if na != nb:
            return jaccard * 0.3
        jaccard = min(1.0, jaccard + 0.15)
    return jaccard


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def close_time_gap_days(a: str, b: str) -> float | None:
    ta, tb = _parse_iso(a), _parse_iso(b)
    if ta is None or tb is None:
        return None
    return abs((ta - tb).total_seconds()) / 86400.0


class XarbScanner:
    def __init__(
        self,
        clob: ClobReadClient,
        kalshi: KalshiClient,
        *,
        poly_discovery: ArbScanner | None = None,
        gamma_url: str | None = None,
    ) -> None:
        self._clob = clob
        self._kalshi = kalshi
        self._poly = poly_discovery or ArbScanner(clob, gamma_url=gamma_url)

    # ---- 建议层：提名候选配对 ----

    def suggest_pairs(
        self,
        *,
        max_poly: int = 300,
        max_kalshi: int = 1000,
        min_score: float = 0.5,
        max_gap_days: float = 3.0,
        top: int = 20,
    ) -> list[dict[str, Any]]:
        poly_markets = [
            m for m in self._poly.discover_markets(limit=max_poly)
            if [o.lower() for o in m.get("outcomes", [])[:2]] == ["yes", "no"]
        ]
        kalshi_markets = self._kalshi.get_markets(max_markets=max_kalshi)
        logger.info(
            "候选池：Polymarket %d 个（Yes/No 型），Kalshi %d 个",
            len(poly_markets), len(kalshi_markets),
        )
        suggestions = []
        for pm in poly_markets:
            best = None
            for km in kalshi_markets:
                score = similarity(pm["question"], f"{km.title} {km.subtitle}")
                if score < min_score:
                    continue
                gap = close_time_gap_days(pm.get("end_date", ""), km.close_time)
                if gap is not None and gap > max_gap_days:
                    continue
                if best is None or score > best[0]:
                    best = (score, km, gap)
            if best is not None:
                score, km, gap = best
                suggestions.append({
                    "score": round(score, 3),
                    "close_gap_days": round(gap, 2) if gap is not None else None,
                    "poly_condition_id": pm["condition_id"],
                    "poly_question": pm["question"],
                    "kalshi_ticker": km.ticker,
                    "kalshi_title": f"{km.title} {km.subtitle}".strip(),
                })
        suggestions.sort(key=lambda s: -s["score"])
        return suggestions[:top]

    # ---- 精确层：对确认配对算价差 ----

    def scan_pairs(
        self,
        pairs: list[PairConfig],
        *,
        min_edge: float = 0.01,
        min_profit: float = 1.0,
    ) -> list[XarbOpportunity]:
        if not pairs:
            return []
        poly_markets = {
            m["condition_id"]: m
            for m in self._poly.discover_markets(limit=1000)
        }
        opportunities: list[XarbOpportunity] = []
        for pair in pairs:
            pm = poly_markets.get(pair.poly_condition_id)
            if pm is None:
                logger.warning("配对 %s：Polymarket 市场不在活跃目录里，跳过", pair.poly_condition_id)
                continue
            yes_index = 0 if pair.poly_yes_index == 0 else 1
            token_yes = pm["tokens"][yes_index]
            token_no = pm["tokens"][1 - yes_index]
            books = self._clob.get_books([token_yes, token_no])
            try:
                kalshi_book = self._kalshi.get_orderbook(pair.kalshi_ticker)
            except Exception as exc:  # noqa: BLE001 —— 单个配对失败不拖累整轮
                logger.warning("配对 %s：拉取 Kalshi 订单簿失败: %s", pair.kalshi_ticker, exc)
                continue

            combos = [
                # (组合名, poly 腿 token, kalshi 买入侧)
                ("poly_yes+kalshi_no", token_yes, "no"),
                ("kalshi_yes+poly_no", token_no, "yes"),
            ]
            for combo, poly_token, kalshi_side in combos:
                poly_book = books.get(poly_token)
                if poly_book is None or not poly_book.asks:
                    continue
                kalshi_ask = kalshi_book.ask(kalshi_side)
                if kalshi_ask is None:
                    continue
                poly_ask = poly_book.asks[0]
                fee = round(taker_fee(kalshi_ask.price), 6)
                cost = poly_ask.price + kalshi_ask.price + fee
                edge = 1.0 - cost
                if edge < min_edge:
                    continue
                depth = min(poly_ask.size, kalshi_ask.count)
                profit = edge * depth
                if profit < min_profit:
                    continue
                opportunities.append(XarbOpportunity(
                    poly_condition_id=pair.poly_condition_id,
                    kalshi_ticker=pair.kalshi_ticker,
                    combo=combo,
                    poly_price=poly_ask.price,
                    kalshi_price=kalshi_ask.price,
                    kalshi_fee=fee,
                    edge_per_pair=round(edge, 6),
                    max_pairs=round(depth, 2),
                    profit_usdc=round(profit, 2),
                    poly_question=pm["question"],
                    kalshi_title="",
                    note=pair.note,
                ))
        opportunities.sort(key=lambda o: -o.profit_usdc)
        return opportunities
