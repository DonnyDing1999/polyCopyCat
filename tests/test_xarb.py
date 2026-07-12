import json

import pytest

from polycopycat.engine.clob import BookLevel, OrderBook
from polycopycat.kalshi import KalshiBook, KalshiMarket
from polycopycat.xarb import (
    PairConfig,
    PairsError,
    XarbScanner,
    close_time_gap_days,
    load_pairs,
    similarity,
)


def test_similarity_basics():
    assert similarity("Will Bitcoin be above 65000 on July 14?",
                      "Bitcoin above 65000 on July 14") > 0.8
    assert similarity("Will Bitcoin be above 65000?",
                      "Will Ethereum be above 4000?") < 0.4
    # 数字不一致要重罚："above 64800" 和 "above 65000" 不是同一个市场
    high = similarity("Bitcoin above 65000 July 14", "Bitcoin above 65000 July 14")
    mismatch = similarity("Bitcoin above 64800 July 14", "Bitcoin above 65000 July 14")
    assert mismatch < high * 0.5


def test_close_time_gap():
    assert close_time_gap_days("2026-07-14T00:00:00Z", "2026-07-15T12:00:00Z") == 1.5
    assert close_time_gap_days("", "2026-07-15T12:00:00Z") is None
    assert close_time_gap_days("garbage", "2026-07-15T12:00:00Z") is None


def test_load_pairs(tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"pairs": [
        {"poly_condition_id": "0xc1", "kalshi_ticker": "KX-1", "note": "ok"},
    ]}), encoding="utf-8")
    pairs = load_pairs(path)
    assert pairs[0].poly_condition_id == "0xc1"
    assert pairs[0].poly_yes_index == 0

    with pytest.raises(PairsError, match="不存在"):
        load_pairs(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"pairs": [{"kalshi_ticker": "KX-1"}]}), encoding="utf-8")
    with pytest.raises(PairsError, match="缺少字段"):
        load_pairs(bad)


class FakePolyDiscovery:
    def __init__(self, markets):
        self.markets = markets

    def discover_markets(self, limit=1000):
        return self.markets[:limit]


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_books(self, token_ids):
        return {t: self.books[t] for t in token_ids if t in self.books}


class FakeKalshi:
    def __init__(self, books=None, markets=None, error=None):
        self.books = books or {}
        self.markets = markets or []
        self.error = error

    def get_orderbook(self, ticker):
        if self.error:
            raise self.error
        return self.books[ticker]

    def get_markets(self, **kwargs):
        return self.markets


def poly_market(cid="0xc1", yes="tokY", no="tokN", question="Will A happen?"):
    return {
        "condition_id": cid, "question": question, "neg_risk": False,
        "tokens": [yes, no], "outcomes": ["Yes", "No"],
        "end_date": "2026-07-14T00:00:00Z",
    }


def make_scanner(poly_markets, poly_books, kalshi):
    return XarbScanner(
        FakeClob(poly_books), kalshi,
        poly_discovery=FakePolyDiscovery(poly_markets),
    )


def test_scan_pairs_finds_cross_venue_edge():
    # Poly: Yes 卖一 0.38×100；Kalshi: Yes 买一 0.42 → 买 No 的卖一 = 0.58×80
    # 组合 poly_yes+kalshi_no：0.38 + 0.58 + fee(0.58)=0.017052 → 成本 0.977 → 边际 0.023
    poly_books = {"tokY": OrderBook(asks=(BookLevel(0.38, 100),)),
                  "tokN": OrderBook(asks=(BookLevel(0.65, 100),))}
    kalshi_book = KalshiBook.from_api({"orderbook": {"yes": [[42, 80]], "no": [[55, 60]]}})
    scanner = make_scanner([poly_market()], poly_books, FakeKalshi(books={"KX-1": kalshi_book}))
    opportunities = scanner.scan_pairs(
        [PairConfig("0xc1", "KX-1")], min_edge=0.01, min_profit=1.0
    )
    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.combo == "poly_yes+kalshi_no"
    assert opp.poly_price == 0.38 and opp.kalshi_price == 0.58
    assert abs(opp.edge_per_pair - (1 - 0.38 - 0.58 - opp.kalshi_fee)) < 1e-9
    assert opp.max_pairs == 80  # min(poly 100, kalshi 80)
    assert opp.profit_usdc == round(opp.edge_per_pair * 80, 2)


def test_scan_pairs_no_edge_when_fair():
    poly_books = {"tokY": OrderBook(asks=(BookLevel(0.45, 100),)),
                  "tokN": OrderBook(asks=(BookLevel(0.57, 100),))}
    kalshi_book = KalshiBook.from_api({"orderbook": {"yes": [[43, 80]], "no": [[53, 60]]}})
    scanner = make_scanner([poly_market()], poly_books, FakeKalshi(books={"KX-1": kalshi_book}))
    assert scanner.scan_pairs([PairConfig("0xc1", "KX-1")]) == []


def test_scan_pairs_respects_yes_index_invert():
    # Kalshi 的 Yes 对应 Poly 的第二个 outcome（poly_yes_index=1）
    poly_books = {"tokY": OrderBook(asks=(BookLevel(0.65, 100),)),
                  "tokN": OrderBook(asks=(BookLevel(0.38, 100),))}
    kalshi_book = KalshiBook.from_api({"orderbook": {"yes": [[42, 80]], "no": [[55, 60]]}})
    scanner = make_scanner([poly_market()], poly_books, FakeKalshi(books={"KX-1": kalshi_book}))
    opportunities = scanner.scan_pairs(
        [PairConfig("0xc1", "KX-1", poly_yes_index=1)], min_edge=0.01, min_profit=1.0
    )
    assert len(opportunities) == 1
    assert opportunities[0].poly_price == 0.38  # 用的是 tokN 那腿


def test_scan_pairs_skips_unknown_market_and_kalshi_failure():
    poly_books = {"tokY": OrderBook(asks=(BookLevel(0.38, 100),)),
                  "tokN": OrderBook(asks=(BookLevel(0.65, 100),))}
    scanner = make_scanner([poly_market()], poly_books, FakeKalshi(error=RuntimeError("down")))
    assert scanner.scan_pairs([PairConfig("0xmissing", "KX-1")]) == []
    assert scanner.scan_pairs([PairConfig("0xc1", "KX-1")]) == []  # kalshi 挂了跳过


def kalshi_market(ticker="KX-1", title="Bitcoin above 65000 on July 14?"):
    return KalshiMarket(
        ticker=ticker, event_ticker="KX", title=title, subtitle="",
        close_time="2026-07-14T12:00:00Z", yes_bid=0.4, yes_ask=0.45,
        no_bid=0.5, no_ask=0.6, volume_24h=100, liquidity=1000, status="open",
    )


def test_suggest_pairs_matches_and_filters():
    poly_markets = [
        poly_market(cid="0xc1", question="Will Bitcoin be above 65000 on July 14?"),
        poly_market(cid="0xc2", question="Will it rain in Paris tomorrow?"),
    ]
    kalshi = FakeKalshi(markets=[
        kalshi_market(ticker="KX-BTC", title="Bitcoin above 65000 on July 14?"),
        kalshi_market(ticker="KX-NBA", title="Lakers to win the NBA finals"),
    ])
    scanner = make_scanner(poly_markets, {}, kalshi)
    suggestions = scanner.suggest_pairs(min_score=0.5, top=10)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["poly_condition_id"] == "0xc1" and s["kalshi_ticker"] == "KX-BTC"
    assert s["score"] >= 0.8 and s["close_gap_days"] == 0.5


def test_suggest_pairs_rejects_far_close_times():
    poly_markets = [poly_market(cid="0xc1", question="Bitcoin above 65000 on July 14?")]
    km = KalshiMarket(
        ticker="KX-BTC", event_ticker="KX", title="Bitcoin above 65000 on July 14?",
        subtitle="", close_time="2026-08-30T00:00:00Z", yes_bid=0.4, yes_ask=0.45,
        no_bid=0.5, no_ask=0.6, volume_24h=100, liquidity=1000, status="open",
    )
    scanner = make_scanner(poly_markets, {}, FakeKalshi(markets=[km]))
    assert scanner.suggest_pairs(min_score=0.5, max_gap_days=3.0) == []
