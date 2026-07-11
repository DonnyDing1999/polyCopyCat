import pytest
import requests

from polycopycat.engine.clob import ClobError, ClobReadClient, MarketInfo, OrderBook


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


MARKET_RAW = {
    "condition_id": "0xcond",
    "minimum_tick_size": "0.001",
    "minimum_order_size": "5",
    "neg_risk": True,
    "accepting_orders": True,
    "closed": False,
    "market_slug": "will-x-happen",
    "question": "Will X happen?",
}

BOOK_RAW = {
    "bids": [{"price": "0.48", "size": "100"}, {"price": "0.49", "size": "50"}],
    "asks": [{"price": "0.52", "size": "80"}, {"price": "0.51", "size": "30"}],
}


def make_client(responses):
    session = FakeSession(responses)
    return ClobReadClient("https://clob.test", session=session, backoff=0.0), session


def test_market_parse_and_cache():
    client, session = make_client([FakeResponse(payload=MARKET_RAW)])
    market = client.get_market("0xcond")
    assert market.tick_size == 0.001
    assert market.min_size == 5.0
    assert market.neg_risk is True
    assert market.slug == "will-x-happen"
    again = client.get_market("0xcond")  # 命中缓存，不再发请求
    assert again is market
    assert len(session.requests) == 1
    assert session.requests[0][0] == "https://clob.test/markets/0xcond"


def test_market_defaults_when_fields_missing():
    client, _ = make_client([FakeResponse(payload={})])
    market = client.get_market("0xcond")
    assert market.tick_size == 0.01
    assert market.min_size == 5.0
    assert market.accepting_orders is True
    assert market.closed is False


def test_book_normalizes_order():
    client, session = make_client([FakeResponse(payload=BOOK_RAW)])
    book = client.get_book("token123")
    assert [lv.price for lv in book.bids] == [0.49, 0.48]   # 从高到低
    assert [lv.price for lv in book.asks] == [0.51, 0.52]   # 从低到高
    assert session.requests[0][1] == {"token_id": "token123"}


def test_book_skips_garbage_levels():
    raw = {"bids": [{"price": "x", "size": "1"}, {"price": "0.4", "size": "0"}], "asks": None}
    book = OrderBook.from_api(raw)
    assert book.bids == () and book.asks == ()


def test_non_dict_payload_raises():
    client, _ = make_client([FakeResponse(payload=[1, 2])])
    with pytest.raises(ClobError):
        client.get_market("0xcond")


def test_http_error_becomes_clob_error():
    client, _ = make_client([FakeResponse(status_code=503)] * 3)
    with pytest.raises(ClobError):
        client.get_book("token123")
