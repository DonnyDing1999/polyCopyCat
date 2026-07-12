import json

import pytest
import requests

from polycopycat.arb import ArbError, ArbScanner
from polycopycat.engine.clob import BookLevel, ClobReadClient, OrderBook


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
    """get/post 各自一条响应队列。"""

    def __init__(self, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.get_requests = []
        self.post_requests = []

    def get(self, url, params=None, timeout=None):
        self.get_requests.append((url, dict(params or {})))
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None):
        self.post_requests.append((url, json))
        item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gamma_market(cid="0xc1", yes="tokY", no="tokN", neg_risk=False, question="Q?"):
    return {
        "conditionId": cid, "question": question, "negRisk": neg_risk,
        "clobTokenIds": json.dumps([yes, no]),  # Gamma 返回字符串化的数组
    }


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_books(self, token_ids):
        return {t: self.books[t] for t in token_ids if t in self.books}


def book(bids=(), asks=()):
    return OrderBook(
        bids=tuple(BookLevel(*b) for b in bids),
        asks=tuple(BookLevel(*a) for a in asks),
    )


def make_scanner(markets, books):
    # 翻页逻辑会在非空页后再要一页，末尾补一页空响应表示到底
    session = FakeSession(gets=[FakeResponse(payload=markets), FakeResponse(payload=[])])
    scanner = ArbScanner(FakeClob(books), gamma_url="https://gamma.test", session=session)
    return scanner, session


def test_buy_pair_opportunity_math():
    books = {
        "tokY": book(asks=[(0.48, 100)]),
        "tokN": book(asks=[(0.49, 40)]),
    }
    scanner, session = make_scanner([gamma_market()], books)
    opps = scanner.scan(min_edge=0.005, min_profit=0.5)
    assert len(opps) == 1
    opp = opps[0]
    assert opp.kind == "buy_pair"
    assert abs(opp.edge_per_pair - 0.03) < 1e-9   # 1 - 0.97
    assert opp.max_pairs == 40                    # 受深度小的一边限制
    assert abs(opp.profit_usdc - 1.2) < 1e-9      # 0.03 × 40
    url, params = session.get_requests[0]
    assert url == "https://gamma.test/markets"
    assert params["active"] == "true" and params["closed"] == "false"


def test_mint_sell_opportunity():
    books = {
        "tokY": book(bids=[(0.55, 30)]),
        "tokN": book(bids=[(0.50, 60)]),
    }
    scanner, _ = make_scanner([gamma_market()], books)
    opps = scanner.scan(min_edge=0.01, min_profit=0.5)
    assert len(opps) == 1
    assert opps[0].kind == "mint_sell"
    assert abs(opps[0].edge_per_pair - 0.05) < 1e-9
    assert opps[0].max_pairs == 30


def test_fair_prices_produce_nothing():
    books = {
        "tokY": book(bids=[(0.49, 100)], asks=[(0.50, 100)]),
        "tokN": book(bids=[(0.49, 100)], asks=[(0.51, 100)]),
    }
    scanner, _ = make_scanner([gamma_market()], books)
    assert scanner.scan() == []


def test_min_profit_filters_dust():
    books = {
        "tokY": book(asks=[(0.48, 1)]),
        "tokN": book(asks=[(0.49, 1)]),  # 边际 0.03 × 1 对 = $0.03
    }
    scanner, _ = make_scanner([gamma_market()], books)
    assert scanner.scan(min_profit=0.5) == []
    assert len(scanner_with := make_scanner([gamma_market()], books)[0].scan(min_profit=0.01)) == 1


def test_missing_book_or_bad_tokens_skipped():
    markets = [
        gamma_market(cid="0xc1", yes="tokY", no="tokN"),
        {"conditionId": "0xc2", "question": "bad", "clobTokenIds": "not json"},
        {"conditionId": "0xc3", "question": "three", "clobTokenIds": json.dumps(["a", "b", "c"])},
    ]
    scanner, _ = make_scanner(markets, {"tokY": book(asks=[(0.4, 10)])})  # tokN 无订单簿
    assert scanner.scan() == []


def test_discovery_pages_past_server_cap():
    # 服务端把单页封顶在 2 条（低于请求的 5 条）——要继续翻页而不是当成到底
    pages = [
        [gamma_market(cid=f"0xc{i}", yes=f"y{i}", no=f"n{i}") for i in range(2)],
        [gamma_market(cid=f"0xc{i}", yes=f"y{i}", no=f"n{i}") for i in range(2, 4)],
        [gamma_market(cid="0xc4", yes="y4", no="n4")],
    ]
    session = FakeSession(gets=[FakeResponse(payload=p) for p in pages])
    scanner = ArbScanner(FakeClob({}), gamma_url="https://gamma.test", session=session)
    markets = scanner.discover_markets(limit=5)
    assert len(markets) == 5
    assert len(session.get_requests) == 3
    assert session.get_requests[1][1]["offset"] == 2  # 第二页从 2 开始


def test_discovery_stops_on_empty_page():
    page1 = [gamma_market(cid=f"0xc{i}", yes=f"y{i}", no=f"n{i}") for i in range(3)]
    session = FakeSession(gets=[FakeResponse(payload=page1), FakeResponse(payload=[])])
    scanner = ArbScanner(FakeClob({}), gamma_url="https://gamma.test", session=session)
    assert len(scanner.discover_markets(limit=500)) == 3
    assert len(session.get_requests) == 2


def test_discovery_stops_when_server_ignores_offset():
    page = [gamma_market(cid="0xc1", yes="y1", no="n1")]
    session = FakeSession(gets=[FakeResponse(payload=page)] * 5)
    scanner = ArbScanner(FakeClob({}), gamma_url="https://gamma.test", session=session)
    markets = scanner.discover_markets(limit=500)
    assert len(markets) == 1
    assert len(session.get_requests) == 2  # 第二页全是重复 → 停


def test_gamma_failure_raises_arb_error():
    session = FakeSession(gets=[FakeResponse(status_code=500)] * 3)
    scanner = ArbScanner(FakeClob({}), gamma_url="https://gamma.test", session=session)
    with pytest.raises(ArbError):
        scanner.discover_markets()


def test_get_books_batch_and_fallback():
    batch_payload = [
        {"asset_id": "tokY", "asks": [{"price": "0.5", "size": "10"}], "bids": []},
    ]
    session = FakeSession(posts=[FakeResponse(payload=batch_payload)])
    client = ClobReadClient("https://clob.test", session=session, backoff=0.0)
    books = client.get_books(["tokY"])
    assert books["tokY"].asks[0].price == 0.5
    assert session.post_requests[0][0] == "https://clob.test/books"

    # 批量接口挂了 → 逐个 GET 降级
    session = FakeSession(
        posts=[FakeResponse(status_code=500)] * 3,
        gets=[FakeResponse(payload={"asks": [{"price": "0.6", "size": "5"}], "bids": []})],
    )
    client = ClobReadClient("https://clob.test", session=session, backoff=0.0)
    books = client.get_books(["tokY"])
    assert books["tokY"].asks[0].price == 0.6
    assert len(session.get_requests) == 1
