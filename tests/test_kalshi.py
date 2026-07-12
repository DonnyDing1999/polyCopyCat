import pytest
import requests

from polycopycat.kalshi import KalshiBook, KalshiClient, KalshiError, taker_fee


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        return self.responses.pop(0)


def market_row(ticker="KX-1", yes_bid=42, yes_ask=45, no_bid=54, no_ask=58):
    return {
        "ticker": ticker, "event_ticker": "KX", "title": "Will A happen?",
        "subtitle": "", "close_time": "2026-07-14T00:00:00Z", "status": "open",
        "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask,
        "volume_24h": 1000, "liquidity": 5000,
    }


def test_markets_parse_cents_to_dollars():
    payload = {"cursor": "", "markets": [market_row()]}
    client = KalshiClient("https://kalshi.test", session=FakeSession([FakeResponse(payload=payload)]),
                          backoff=0.0)
    markets = client.get_markets()
    m = markets[0]
    assert m.yes_bid == 0.42 and m.yes_ask == 0.45
    assert m.no_bid == 0.54 and m.no_ask == 0.58
    assert m.close_time.startswith("2026-07-14")


def test_zero_or_invalid_prices_become_none():
    payload = {"cursor": "", "markets": [market_row(yes_bid=0, yes_ask=100, no_bid="x", no_ask=None)]}
    client = KalshiClient("https://kalshi.test", session=FakeSession([FakeResponse(payload=payload)]),
                          backoff=0.0)
    m = client.get_markets()[0]
    assert m.yes_bid is None and m.yes_ask is None
    assert m.no_bid is None and m.no_ask is None


def test_markets_pagination_follows_cursor():
    pages = [
        {"cursor": "c2", "markets": [market_row(ticker="A")]},
        {"cursor": "", "markets": [market_row(ticker="B")]},
    ]
    session = FakeSession([FakeResponse(payload=p) for p in pages])
    client = KalshiClient("https://kalshi.test", session=session, backoff=0.0)
    markets = client.get_markets(max_markets=10)
    assert [m.ticker for m in markets] == ["A", "B"]
    assert "cursor" not in session.requests[0][1]
    assert session.requests[1][1]["cursor"] == "c2"


def test_orderbook_ask_derivation():
    raw = {"orderbook": {
        "yes": [[42, 100], [40, 50]],   # Yes 侧买单
        "no": [[55, 30], [50, 200]],    # No 侧买单
    }}
    book = KalshiBook.from_api(raw)
    ask_yes = book.ask("yes")   # = 1 - 最高 No 买价 0.55
    assert ask_yes.price == 0.45 and ask_yes.count == 30
    ask_no = book.ask("no")     # = 1 - 最高 Yes 买价 0.42
    assert ask_no.price == 0.58 and ask_no.count == 100


def test_orderbook_empty_side():
    book = KalshiBook.from_api({"orderbook": {"yes": [], "no": None}})
    assert book.ask("yes") is None and book.ask("no") is None


def test_orderbook_garbage_levels_skipped():
    book = KalshiBook.from_api({"orderbook": {"yes": [["x", 1], [0, 5], [30, 0], [35, 10]]}})
    assert len(book.yes_bids) == 1 and book.yes_bids[0].price == 0.35


def test_taker_fee_formula():
    assert abs(taker_fee(0.5) - 0.0175) < 1e-9   # 峰值 1.75 分
    assert abs(taker_fee(0.9) - 0.0063) < 1e-9
    assert taker_fee(0.0) == 0.0


def test_http_error_becomes_kalshi_error():
    client = KalshiClient("https://kalshi.test",
                          session=FakeSession([FakeResponse(status_code=500)] * 3), backoff=0.0)
    with pytest.raises(KalshiError):
        client.get_markets()
