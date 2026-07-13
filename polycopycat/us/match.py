"""把主站市场按标题匹配到 Polymarket US 的对应市场。

两个平台的市场列表、命名、slug 互相独立，而跟单信号来自主站（按地
址），想对照美国站盘口或将来在美国站执行，得先回答"这是不是同一个
问题"。匹配只看词面：标题归一化分词后按 词集重合 70% + 数字重合
20% + 结果名 10% 打分。分数是排序参考，不是同一性证明，两站的结算
口径可能不同，下单前务必人工确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .api import UsMarket

# 预测市场标题里的高频虚词，对区分市场没有帮助
_STOPWORDS = frozenset({
    "will", "the", "a", "an", "by", "in", "on", "at", "of", "to", "be",
    "is", "are", "or", "and", "for", "vs", "v", "before", "after", "than",
    "more", "less", "least", "most", "does", "do", "did", "what", "who",
    "when", "how", "it", "its", "this", "that",
})

_TOKEN_RE = re.compile(r"[a-z0-9$.,%]+")
_NUM_RE = re.compile(r"^\$?\d[\d,]*(?:\.\d+)?[km]?%?$")


def _norm_token(token: str) -> str:
    """数字统一口径：$100,000 / 100k / 100000 归一成同一个词。"""
    raw = token.strip(".,")
    if not _NUM_RE.match(raw):
        return raw
    text = raw.lstrip("$").rstrip("%")
    mult = 1.0
    if text.endswith("k"):
        mult, text = 1_000.0, text[:-1]
    elif text.endswith("m"):
        mult, text = 1_000_000.0, text[:-1]
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return raw
    return f"{number * mult:.10g}"


def tokenize(text: str) -> set[str]:
    """标题或 slug → 归一化词集（连字符当空格，数字统一口径，去虚词）。"""
    text = re.sub(r"[-_/]+", " ", text.lower())
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        token = _norm_token(raw)
        if token and token not in _STOPWORDS:
            tokens.add(token)
    return tokens


def _numbers(tokens: set[str]) -> set[str]:
    return {t for t in tokens if t[:1].isdigit()}


def score_match(query_tokens: set[str], market: UsMarket, outcome: str | None) -> float:
    """词面相似度打分（0~100）：词集 70 + 数字 20 + 结果名 10。

    数字单独加权是因为它们往往是市场的关键区分（价位、日期、场次）：
    "BTC above $100k" 和 "BTC above $150k" 词面几乎一样，答案完全不同。
    """
    cand_tokens = tokenize(f"{market.event_title} {market.title}")
    if not query_tokens or not cand_tokens:
        return 0.0
    word_score = len(query_tokens & cand_tokens) / len(query_tokens | cand_tokens)
    query_nums = _numbers(query_tokens)
    if query_nums:
        num_score = len(query_nums & _numbers(cand_tokens)) / len(query_nums)
    else:
        num_score = 1.0
    if outcome:
        wanted = outcome.strip().lower()
        got = market.outcome.strip().lower()
        outcome_score = 1.0 if wanted and got and (wanted in got or got in wanted) else 0.0
    else:
        outcome_score = 0.5  # 没指定结果名时不奖不罚
    return round(100 * (0.7 * word_score + 0.2 * num_score + 0.1 * outcome_score), 1)


class _SearchClient(Protocol):
    def search_markets(
        self, query: str, *, status: str | None = "active", limit: int | None = None
    ) -> list[UsMarket]: ...


@dataclass(frozen=True)
class UsMatch:
    """一个候选匹配：US 市场 + 相似度分。"""

    market: UsMarket
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, **self.market.to_dict()}


def match_us_markets(
    client: _SearchClient,
    text: str,
    *,
    outcome: str | None = None,
    top: int = 5,
    status: str | None = "active",
) -> list[UsMatch]:
    """在 US 站搜索候选市场并按相似度从高到低返回前 top 个。

    text 用主站市场的标题或 slug 都行（slug 会被拆词）。搜索先用完整
    词面，无结果时再拿信息量最大的几个词重试一次（标题太长时全文
    检索经常一个都搜不出来）。
    """
    query_tokens = tokenize(text)
    if not query_tokens:
        return []
    markets = client.search_markets(re.sub(r"[-_/]+", " ", text.strip()), status=status)
    if not markets and len(query_tokens) > 4:
        fallback = sorted(sorted(query_tokens), key=len, reverse=True)[:4]
        markets = client.search_markets(" ".join(fallback), status=status)
    seen: set[str] = set()
    matches: list[UsMatch] = []
    for market in markets:
        if not market.slug or market.slug in seen:
            continue
        seen.add(market.slug)
        matches.append(UsMatch(market=market, score=score_match(query_tokens, market, outcome)))
    matches.sort(key=lambda m: (m.score, m.market.volume), reverse=True)
    return matches[: max(1, int(top))]
