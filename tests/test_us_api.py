import pytest
import requests

from polycopycat.us import UsApiClient, UsApiError, UsBook


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
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


MARKETS_RAW = {
    "markets": [
        {
            "id": 7,
            "slug": "btc-100k",
            "title": "Bitcoin above $100k by Dec 31?",
            "outcome": "Yes",
            "active": True,
            "closed": False,
            "liquidity": 1234.5,
            "volume": 9876.5,
            "eventSlug": "btc-2026",
        }
    ]
}

BOOK_RAW = {
    "marketSlug": "btc-100k",
    "bids": [
        {"px": {"value": "0.48", "currency": "USD"}, "qty": "100"},
        {"px": {"value": "0.49", "currency": "USD"}, "qty": "50"},
    ],
    "offers": [
        {"px": {"value": "0.52", "currency": "USD"}, "qty": "80"},
        {"px": {"value": "0.51", "currency": "USD"}, "qty": "30"},
    ],
    "state": "MARKET_STATE_OPEN",
    "stats": {"lastTradePx": {"value": "0.50", "currency": "USD"}},
}

BBO_RAW = {
    "marketSlug": "btc-100k",
    "bestBid": {"value": "0.49", "currency": "USD"},
    "bestAsk": {"value": "0.51", "currency": "USD"},
    "bidDepth": 3,
    "askDepth": 2,
    "lastTradePx": {"value": "0.50", "currency": "USD"},
    "sharesTraded": "1200",
    "openInterest": "800",
}

SEARCH_RAW = {
    "events": [
        {
            "slug": "btc-2026",
            "title": "Bitcoin in 2026",
            "markets": [
                {"id": 7, "slug": "btc-100k", "title": "Above $100k", "outcome": "Yes"},
                {"id": 8, "slug": "btc-150k", "title": "Above $150k", "outcome": "Yes"},
            ],
        }
    ]
}

SETTLEMENT_RAW = {
    "marketSlug": "btc-100k",
    "settlementPrice": {"value": "1", "currency": "USD"},
    "settledAt": "2026-01-01T00:00:00Z",
}


def make_client(responses):
    session = FakeSession(responses)
    return UsApiClient("https://us.test", session=session, backoff=0.0), session


def test_markets_parse_and_default_filters():
    client, session = make_client([FakeResponse(payload=MARKETS_RAW)])
    markets = client.get_markets()
    assert len(markets) == 1
    m = markets[0]
    assert m.slug == "btc-100k"
    assert m.outcome == "Yes"
    assert m.volume == 9876.5
    assert m.event_slug == "btc-2026"
    url, params = session.requests[0]
    assert url == "https://us.test/v1/markets"
    # 布尔参数按 gateway 口径传小写字符串
    assert params["active"] == "true" and params["closed"] == "false"


def test_markets_no_filter_when_none():
    client, session = make_client([FakeResponse(payload=MARKETS_RAW)])
    client.get_markets(active=None, closed=None)
    _, params = session.requests[0]
    assert "active" not in params and "closed" not in params


def test_market_by_slug():
    client, session = make_client(
        [FakeResponse(payload={"market": MARKETS_RAW["markets"][0]})]
    )
    market = client.get_market("btc-100k")
    assert market.title.startswith("Bitcoin")
    assert session.requests[0][0] == "https://us.test/v1/market/slug/btc-100k"


def test_book_normalizes_offers_to_asks():
    client, session = make_client([FakeResponse(payload=BOOK_RAW)])
    book = client.get_book("btc-100k")
    assert session.requests[0][0] == "https://us.test/v1/markets/btc-100k/book"
    assert [lv.price for lv in book.bids] == [0.49, 0.48]  # 从高到低
    assert [lv.price for lv in book.asks] == [0.51, 0.52]  # 从低到高
    assert book.asks[0].size == 30.0
    assert book.state == "MARKET_STATE_OPEN"
    assert book.last_trade_px == 0.50


def test_book_skips_garbage_levels():
    raw = {
        "bids": [{"px": {"value": "x"}, "qty": "1"}, {"px": {"value": "0.4"}, "qty": "0"}],
        "offers": None,
    }
    book = UsBook.from_api(raw)
    assert book.bids == () and book.asks == ()


def test_bbo_parse_and_spread():
    client, _ = make_client([FakeResponse(payload=BBO_RAW)])
    bbo = client.get_bbo("btc-100k")
    assert bbo.best_bid == 0.49 and bbo.best_ask == 0.51
    assert bbo.spread == pytest.approx(0.02)
    assert bbo.shares_traded == 1200.0
    assert bbo.to_dict()["spread"] == pytest.approx(0.02)


def test_bbo_spread_none_when_one_sided():
    client, _ = make_client([FakeResponse(payload={"bestBid": {"value": "0.4"}})])
    bbo = client.get_bbo("btc-100k")
    assert bbo.spread is None


def test_search_markets_attaches_event():
    client, session = make_client([FakeResponse(payload=SEARCH_RAW)])
    markets = client.search_markets("bitcoin", limit=10)
    assert [m.slug for m in markets] == ["btc-100k", "btc-150k"]
    assert markets[0].event_title == "Bitcoin in 2026"
    assert markets[0].event_slug == "btc-2026"
    _, params = session.requests[0]
    assert params == {"query": "bitcoin", "status": "active", "limit": 10}


def test_settlement_parse():
    client, _ = make_client([FakeResponse(payload=SETTLEMENT_RAW)])
    settlement = client.get_settlement("btc-100k")
    assert settlement.settlement_price == 1.0
    assert settlement.settled_at.startswith("2026-01-01")


def test_env_override(monkeypatch):
    monkeypatch.setenv("POLYCOPYCAT_US_URL", "https://proxy.test/")
    client = UsApiClient(session=FakeSession([]))
    assert client.base_url == "https://proxy.test"


def test_bad_payload_raises():
    client, _ = make_client([FakeResponse(payload=[1, 2, 3])])
    with pytest.raises(UsApiError):
        client.get_markets()


def test_http_error_becomes_us_error():
    client, _ = make_client([FakeResponse(status_code=503)] * 3)
    with pytest.raises(UsApiError):
        client.get_bbo("btc-100k")
