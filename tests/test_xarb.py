import json

import pytest

from polycopycat.engine.clob import BookLevel, OrderBook
from polycopycat.kalshi import KalshiBook, KalshiMarket
from polycopycat.xarb import (
    PairConfig,
    PairsError,
    XarbScanner,
    close_time_gap_days,
    extract_features,
    is_parlay,
    load_pairs,
    similarity,
    structured_score,
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


def test_extract_features_crypto_hourly():
    f = extract_features("Bitcoin above 64,800 on July 12, 3AM ET?")
    assert f.asset == "btc"
    assert (f.month, f.day) == (7, 12)
    assert f.hour == 3
    assert f.thresholds == frozenset({"64800"})  # 年份/日期/钟点都不算阈值


def test_extract_features_pm_hour_and_entities():
    f = extract_features("Will Argentina win the 2026 FIFA World Cup by 9PM ET?")
    assert f.hour == 21
    assert f.asset == "" and f.thresholds == frozenset()
    assert {"Argentina", "World", "Cup"} <= set(f.entities)


def test_structured_score_matches_crypto_across_phrasings():
    a = extract_features("Bitcoin Up or Down - July 12, 3AM ET")
    b = extract_features("Bitcoin price today at 3am EDT (Jul 12)?")
    assert structured_score(a, b) >= 0.5  # 资产+日期+小时对齐


def test_structured_score_hard_conflicts_zero():
    base = extract_features("Bitcoin above 64800 on July 12, 3AM ET")
    assert structured_score(base, extract_features("Ethereum above 64800 July 12 3AM ET")) == 0
    assert structured_score(base, extract_features("Bitcoin above 64800 July 13 3AM ET")) == 0
    assert structured_score(base, extract_features("Bitcoin above 64800 July 12 4AM ET")) == 0
    assert structured_score(base, extract_features("Bitcoin above 65000 July 12 3AM ET")) == 0


def test_is_parlay_detects_multileg():
    assert is_parlay("yes Cody Bellinger: 2+,yes Will Warren: 2+")
    assert is_parlay("yes Pittsburgh,yes Baltimore,no Over 8.5 runs scored")
    assert not is_parlay("Will Argentina win the 2026 FIFA World Cup?")
    assert not is_parlay("Bitcoin price today at 3am EDT?")


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
    def __init__(self, books=None, markets=None, events=None, event_markets=None, error=None):
        self.books = books or {}
        self.markets = markets or []
        self.events = events or []
        self.event_markets = event_markets or {}
        self.error = error

    def get_orderbook(self, ticker):
        if self.error:
            raise self.error
        return self.books[ticker]

    def get_events(self, **kwargs):
        return self.events

    def get_markets(self, *, event_ticker=None, series_ticker=None, **kwargs):
        if event_ticker is not None:
            return self.event_markets.get(event_ticker, [])
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
    diagnostics = []
    assert scanner.scan_pairs([PairConfig("0xc1", "KX-1")], diagnostics_out=diagnostics) == []
    # 诊断给出每对最优组合的真实边际（负数），量化"差多远"
    assert len(diagnostics) == 1
    d = diagnostics[0]
    assert d["edge_per_pair"] is not None and d["edge_per_pair"] < 0
    assert abs(d["sum_cost"] - (1 - d["edge_per_pair"])) < 1e-9


def test_extract_features_asset_word_not_double_counted_as_entity():
    f = extract_features("Will Satoshi move any Bitcoin by 2027?")
    assert f.asset == "btc"
    assert "Bitcoin" not in f.entities  # 资产已单独计分，不再进实体加分
    g = extract_features("Will Bitcoin dip to $50,000 by December 31, 2026?")
    # 只剩资产一致（0.45），不再靠 Bitcoin 实体凑到 0.6 → 低于建议阈值
    assert structured_score(f, g) < 0.5


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


def kalshi_market(ticker="KX-1", title="Bitcoin above 65000 on July 14?",
                  event_ticker="EV-1", close_time="2026-07-14T12:00:00Z"):
    return KalshiMarket(
        ticker=ticker, event_ticker=event_ticker, title=title, subtitle="",
        close_time=close_time, yes_bid=0.4, yes_ask=0.45,
        no_bid=0.5, no_ask=0.6, volume_24h=100, liquidity=1000, status="open",
    )


def kalshi_event(event_ticker="EV-1", title="Bitcoin above 65000 on July 14?"):
    from polycopycat.kalshi import KalshiEvent

    return KalshiEvent(event_ticker=event_ticker, series_ticker="KX", title=title)


def test_suggest_pairs_event_funnel_matches_and_filters():
    poly_markets = [
        poly_market(cid="0xc1", question="Will Bitcoin be above 65000 on July 14?"),
        poly_market(cid="0xc2", question="Will it rain in Paris tomorrow?"),
    ]
    kalshi = FakeKalshi(
        events=[
            kalshi_event("EV-BTC", "Bitcoin above 65000 on July 14?"),
            kalshi_event("EV-NBA", "Lakers to win the NBA finals"),
        ],
        event_markets={
            "EV-BTC": [kalshi_market(ticker="KX-BTC", event_ticker="EV-BTC")],
            "EV-NBA": [kalshi_market(ticker="KX-NBA", event_ticker="EV-NBA",
                                     title="Lakers to win the NBA finals")],
        },
    )
    scanner = make_scanner(poly_markets, {}, kalshi)
    suggestions = scanner.suggest_pairs(min_score=0.5, top=10)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["poly_condition_id"] == "0xc1" and s["kalshi_ticker"] == "KX-BTC"
    assert s["score"] >= 0.8 and s["close_gap_days"] == 0.5


def test_suggest_pairs_structured_beats_weak_text():
    poly_markets = [poly_market(cid="0xbtc", question="Bitcoin Up or Down - July 12, 3AM ET")]
    kalshi = FakeKalshi(
        events=[kalshi_event("EV-BTC-H", "Bitcoin price today at 3am EDT (Jul 12)?")],
        event_markets={"EV-BTC-H": [
            kalshi_market(ticker="KX-BTC-H", event_ticker="EV-BTC-H",
                          title="Bitcoin price today at 3am EDT (Jul 12)?"),
        ]},
    )
    scanner = make_scanner(poly_markets, {}, kalshi)
    suggestions = scanner.suggest_pairs(min_score=0.5, top=10)
    assert len(suggestions) == 1
    assert suggestions[0]["match_type"] == "structured"
    assert suggestions[0]["kalshi_ticker"] == "KX-BTC-H"


def test_suggest_pairs_skips_parlay_events_and_markets():
    poly_markets = [poly_market(cid="0xc1", question="Will Cody Bellinger score 2+?")]
    kalshi = FakeKalshi(
        events=[
            kalshi_event("KXMVE-1", "Cody Bellinger: 2+"),   # 串关事件集合，直接跳过
            kalshi_event("EV-OK", "Cody Bellinger: 2+ home runs?"),
        ],
        event_markets={
            "KXMVE-1": [kalshi_market(ticker="KX-P1", event_ticker="KXMVE-1",
                                      title="yes Cody Bellinger: 2+,yes Will Warren: 2+")],
            "EV-OK": [kalshi_market(ticker="KX-P2", event_ticker="EV-OK",
                                    title="yes Cody Bellinger: 2+,yes Will Warren: 2+")],
        },
    )
    scanner = make_scanner(poly_markets, {}, kalshi)
    # KXMVE 事件被跳过；EV-OK 事件命中但旗下市场是串关 → 也被滤掉
    assert scanner.suggest_pairs(min_score=0.2, top=10) == []


def test_suggest_pairs_rejects_far_close_times():
    poly_markets = [poly_market(cid="0xc1", question="Bitcoin above 65000 on July 14?")]
    kalshi = FakeKalshi(
        events=[kalshi_event("EV-BTC", "Bitcoin above 65000 on July 14?")],
        event_markets={"EV-BTC": [
            kalshi_market(ticker="KX-BTC", event_ticker="EV-BTC",
                          close_time="2026-08-30T00:00:00Z"),
        ]},
    )
    scanner = make_scanner(poly_markets, {}, kalshi)
    assert scanner.suggest_pairs(min_score=0.5, max_gap_days=3.0) == []


def test_suggest_pairs_series_mode_bypasses_funnel():
    poly_markets = [poly_market(cid="0xc1", question="Bitcoin above 65000 on July 14?")]
    kalshi = FakeKalshi(markets=[kalshi_market(ticker="KX-BTC")])
    scanner = make_scanner(poly_markets, {}, kalshi)
    suggestions = scanner.suggest_pairs(min_score=0.5, kalshi_series="KXBTCD")
    assert len(suggestions) == 1 and suggestions[0]["kalshi_ticker"] == "KX-BTC"
