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


def _score_token_sets(ta: set[str], tb: set[str]) -> float:
    """词集 Jaccard + 数字 token 严格一致性加权。

    数字集合必须完全相等才加分，任何不一致都重罚——
    "above 64800" 和 "above 65000" 是两个不同的市场，配错就是敞口。
    """
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


def similarity(a: str, b: str) -> float:
    """标题文本相似度（0~1）。"""
    return _score_token_sets(_tokens(a), _tokens(b))


# ---- 结构化特征匹配：文本措辞差异大时靠硬特征配对 ----

_CRYPTO_ASSETS = {
    "bitcoin": "btc", "btc": "btc",
    "ethereum": "eth", "eth": "eth",
    "solana": "sol", "sol": "sol",
    "xrp": "xrp", "ripple": "xrp",
    "dogecoin": "doge", "doge": "doge",
}
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_ENTITY_SKIP = {
    "Will", "Yes", "No", "The", "Up", "Down", "Or", "On", "In", "Be", "Above",
    "Below", "Price", "Today", "Tomorrow", "What", "Who", "How", "Exact", "Score",
} | {m.capitalize() for m in _MONTHS}


@dataclass(frozen=True)
class Features:
    asset: str = ""                       # 归一化的加密资产名
    month: int = 0
    day: int = 0
    hour: int = -1                        # 0~23，-1 表示未知
    thresholds: frozenset = frozenset()   # 价格阈值数字（排除日期/年份/钟点）
    entities: frozenset = frozenset()     # 专有名词（队名/人名/国家等）


def extract_features(text: str) -> Features:
    lower = text.lower().replace(",", "")
    tokens = [t for t in re.split(r"[^a-z0-9$.]+", lower) if t]
    asset = next((_CRYPTO_ASSETS[t] for t in tokens if t in _CRYPTO_ASSETS), "")

    month = day = 0
    for i, token in enumerate(tokens):
        if token in _MONTHS:
            month = _MONTHS[token]
            for j in (i + 1, i - 1):  # "July 12" 或 "12 July"
                if 0 <= j < len(tokens) and tokens[j].isdigit() and 1 <= int(tokens[j]) <= 31:
                    day = int(tokens[j])
                    break
            break

    hour = -1
    clock = re.search(r"(\d{1,2})\s*(am|pm)", lower)
    if clock:
        hour = int(clock.group(1)) % 12 + (12 if clock.group(2) == "pm" else 0)

    thresholds = set()
    for token in tokens:
        stripped = token.strip("$.")
        if not stripped or not stripped.replace(".", "", 1).isdigit():
            continue
        value = float(stripped)
        if 1900 <= value <= 2100:      # 年份
            continue
        if value == day and day:      # 日期里的"日"
            continue
        if clock and stripped == clock.group(1):  # 钟点
            continue
        if value >= 100 or "." in stripped:       # 价格阈值特征
            thresholds.add(stripped)

    entities = frozenset(
        w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text)
        if w not in _ENTITY_SKIP and w.lower() not in _CRYPTO_ASSETS  # 资产已单独计分
    )
    return Features(
        asset=asset, month=month, day=day, hour=hour,
        thresholds=frozenset(thresholds), entities=entities,
    )


def structured_score(fa: Features, fb: Features) -> float:
    """结构化匹配分：任何硬特征冲突直接 0，其余按证据累加。"""
    if fa.asset and fb.asset and fa.asset != fb.asset:
        return 0.0
    if fa.month and fb.month and (fa.month, fa.day) != (fb.month, fb.day):
        return 0.0
    if fa.hour >= 0 and fb.hour >= 0 and fa.hour != fb.hour:
        return 0.0
    if fa.thresholds and fb.thresholds and fa.thresholds != fb.thresholds:
        return 0.0
    score = 0.0
    if fa.asset and fa.asset == fb.asset:
        score += 0.45
    if fa.month and fa.day and (fa.month, fa.day) == (fb.month, fb.day):
        score += 0.25
    if fa.hour >= 0 and fa.hour == fb.hour:
        score += 0.15
    if fa.thresholds and fa.thresholds == fb.thresholds:
        score += 0.15
    common_entities = fa.entities & fb.entities
    if common_entities:
        score += min(0.3, 0.15 * len(common_entities))
    return min(1.0, score)


def is_parlay(title: str) -> bool:
    """Kalshi 自动生成的多腿串关市场（无法与单事件市场配对，直接滤掉）。"""
    lower = title.lower()
    return "," in lower and ("yes " in lower or "no " in lower)


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
        max_kalshi: int = 3000,
        min_score: float = 0.5,
        max_gap_days: float = 3.0,
        top: int = 20,
        kalshi_series: str | None = None,
        event_probe: int = 40,
        query: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """两级漏斗提名候选配对。

        Kalshi 的市场空间被自动生成的串关盘淹没（实测 1 万个市场里
        单事件市场只有几十个），直接翻市场列表找不到可配对象。改走
        事件目录：事件数量少且没有串关，先用事件标题粗配，再只对
        命中的事件拉取旗下市场做精配。指定 kalshi_series 时跳过漏斗
        直接在该系列的市场里配。
        """
        poly_markets = [
            m for m in self._poly.discover_markets(limit=max_poly)
            if [o.lower() for o in m.get("outcomes", [])[:2]] == ["yes", "no"]
        ]
        if query:
            needles = [q.lower() for q in query]
            poly_markets = [
                m for m in poly_markets
                if any(needle in m["question"].lower() for needle in needles)
            ]
            logger.info("按关键词 %s 过滤后 Polymarket 剩 %d 个", query, len(poly_markets))
        poly_prepped = [
            (pm, _tokens(pm["question"]), extract_features(pm["question"]))
            for pm in poly_markets
        ]

        if kalshi_series:
            kalshi_markets = [
                m for m in self._kalshi.get_markets(
                    max_markets=max_kalshi, series_ticker=kalshi_series
                )
                if not is_parlay(f"{m.title} {m.subtitle}")
            ]
            logger.info(
                "候选池：Polymarket %d 个，Kalshi 系列 %s 市场 %d 个",
                len(poly_prepped), kalshi_series, len(kalshi_markets),
            )
            candidates = [("", km) for km in kalshi_markets]
            return self._match_markets(
                poly_prepped, candidates, min_score=min_score,
                max_gap_days=max_gap_days, top=top,
            )

        events = [
            e for e in self._kalshi.get_events(max_events=max_kalshi)
            if not e.event_ticker.startswith("KXMVE")  # 串关事件集合
        ]
        if query:
            needles = [q.lower() for q in query]
            events = [
                e for e in events
                if any(needle in f"{e.title} {e.sub_title}".lower() for needle in needles)
            ]
            logger.info("按关键词过滤后 Kalshi 事件剩 %d 个", len(events))
        logger.info(
            "候选池：Polymarket %d 个（Yes/No 型），Kalshi 事件 %d 个",
            len(poly_prepped), len(events),
        )
        for pool, rows in (
            ("Poly", [m["question"] for m in poly_markets[:3]]),
            ("Kalshi事件", [f"{e.title} {e.sub_title}".strip() for e in events[:3]]),
        ):
            logger.info("%s 样例: %s", pool, " | ".join(rows) or "（空）")

        # 一级：事件标题粗配
        coarse_floor = max(0.25, min_score * 0.6)
        event_prepped = [
            (ev, _tokens(title), extract_features(title))
            for ev in events
            for title in [f"{ev.title} {ev.sub_title}".strip()]
        ]
        coarse_hits: dict[str, float] = {}
        for _, poly_tokens, poly_features in poly_prepped:
            for ev, ev_tokens, ev_features in event_prepped:
                score = max(
                    _score_token_sets(poly_tokens, ev_tokens),
                    structured_score(poly_features, ev_features),
                )
                if score >= coarse_floor:
                    coarse_hits[ev.event_ticker] = max(
                        coarse_hits.get(ev.event_ticker, 0.0), score
                    )
        probe_events = sorted(coarse_hits, key=lambda t: -coarse_hits[t])[:event_probe]
        logger.info(
            "事件粗配命中 %d 个，拉取前 %d 个事件的市场做精配",
            len(coarse_hits), len(probe_events),
        )

        # 二级：命中事件旗下市场精配（用「事件标题 + 市场标题」做文本，
        # 因为 Kalshi 的市场标题常常只是子标题，如 "Argentina"）
        event_by_ticker = {e.event_ticker: e for e in events}
        candidates: list[tuple[str, Any]] = []
        for ticker in probe_events:
            try:
                rows = self._kalshi.get_markets(event_ticker=ticker, max_markets=200)
            except Exception as exc:  # noqa: BLE001 —— 单事件失败不拖累整轮
                logger.warning("拉取事件 %s 的市场失败: %s", ticker, exc)
                continue
            event_title = ""
            if ticker in event_by_ticker:
                ev = event_by_ticker[ticker]
                event_title = f"{ev.title} {ev.sub_title}".strip()
            candidates.extend(
                (event_title, km) for km in rows
                if not is_parlay(f"{km.title} {km.subtitle}")
            )
        return self._match_markets(
            poly_prepped, candidates, min_score=min_score,
            max_gap_days=max_gap_days, top=top,
        )

    def _match_markets(
        self,
        poly_prepped: list[tuple],
        candidates: list[tuple[str, Any]],
        *,
        min_score: float,
        max_gap_days: float,
        top: int,
    ) -> list[dict[str, Any]]:
        """市场级精配：candidates 为 (所属事件标题, KalshiMarket)。"""
        prepped = []
        for event_title, km in candidates:
            full = f"{event_title} {km.title} {km.subtitle}".strip()
            prepped.append((km, full, _tokens(full), extract_features(full)))
        suggestions = []
        for pm, poly_tokens, poly_features in poly_prepped:
            best = None
            for km, full, kalshi_tokens, kalshi_features in prepped:
                text_score = _score_token_sets(poly_tokens, kalshi_tokens)
                struct_score = structured_score(poly_features, kalshi_features)
                score = max(text_score, struct_score)
                if score < min_score:
                    continue
                gap = close_time_gap_days(pm.get("end_date", ""), km.close_time)
                if gap is not None and gap > max_gap_days:
                    continue
                if best is None or score > best[0]:
                    best = (
                        score, km, full, gap,
                        "structured" if struct_score > text_score else "text",
                    )
            if best is not None:
                score, km, full, gap, match_type = best
                suggestions.append({
                    "score": round(score, 3),
                    "match_type": match_type,
                    "close_gap_days": round(gap, 2) if gap is not None else None,
                    "poly_condition_id": pm["condition_id"],
                    "poly_question": pm["question"],
                    "kalshi_ticker": km.ticker,
                    "kalshi_title": full,
                })
        suggestions.sort(key=lambda s: -s["score"])
        return suggestions[:top]

    # ---- 精确层：对确认配对算价差 ----

    def poly_market_index(self, limit: int = 1000) -> dict[str, dict]:
        """condition_id → 市场信息；循环监控时缓存一次，避免每轮重拉目录。"""
        return {m["condition_id"]: m for m in self._poly.discover_markets(limit=limit)}

    def scan_pairs(
        self,
        pairs: list[PairConfig],
        *,
        min_edge: float = 0.01,
        min_profit: float = 1.0,
        diagnostics_out: list | None = None,
        poly_markets: dict[str, dict] | None = None,
    ) -> list[XarbOpportunity]:
        if not pairs:
            return []
        if poly_markets is None:
            poly_markets = self.poly_market_index()
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
            best_diag = None
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
                if diagnostics_out is not None and (
                    best_diag is None or edge > best_diag["edge_per_pair"]
                ):
                    best_diag = {
                        "poly_condition_id": pair.poly_condition_id,
                        "kalshi_ticker": pair.kalshi_ticker,
                        "note": pair.note,
                        "combo": combo,
                        "poly_price": poly_ask.price,
                        "kalshi_price": kalshi_ask.price,
                        "kalshi_fee": fee,
                        "sum_cost": round(cost, 6),
                        "edge_per_pair": round(edge, 6),
                        "depth": round(min(poly_ask.size, kalshi_ask.count), 2),
                        "poly_question": pm["question"],
                    }
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
            if diagnostics_out is not None:
                if best_diag is None:
                    poly_yes_book = books.get(token_yes)
                    poly_no_book = books.get(token_no)
                    legs = (
                        f"poly_yes_asks={len(poly_yes_book.asks) if poly_yes_book else '无簿'}, "
                        f"poly_no_asks={len(poly_no_book.asks) if poly_no_book else '无簿'}, "
                        f"kalshi_yes_bids={len(kalshi_book.yes_bids)}, "
                        f"kalshi_no_bids={len(kalshi_book.no_bids)}"
                    )
                    best_diag = {
                        "poly_condition_id": pair.poly_condition_id,
                        "kalshi_ticker": pair.kalshi_ticker,
                        "note": pair.note,
                        "combo": None,
                        "edge_per_pair": None,
                        "poly_question": pm["question"],
                        "detail": f"至少一边没有可成交报价（{legs}）",
                    }
                diagnostics_out.append(best_diag)
        opportunities.sort(key=lambda o: -o.profit_usdc)
        return opportunities
