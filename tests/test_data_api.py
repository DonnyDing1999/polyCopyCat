import pytest
import requests

from polycopycat.data_api import (
    MAX_PAGE_LIMIT,
    DataApiClient,
    DataApiError,
    normalize_address,
)

ADDR = "0x" + "A" * 40


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


def make_client(responses, **kwargs):
    session = FakeSession(responses)
    kwargs.setdefault("backoff", 0.0)
    return DataApiClient("https://fake.test", session=session, **kwargs), session


def test_normalize_address():
    assert normalize_address(f"  {ADDR} ") == ADDR.lower()
    for bad in ["", "0x123", "not-an-address", "1" * 42]:
        with pytest.raises(ValueError):
            normalize_address(bad)


def test_get_trades_builds_params_and_parses():
    payload = [{"proxyWallet": ADDR, "side": "BUY", "size": 5, "price": 0.5,
                "timestamp": 100, "transactionHash": "0x1"}]
    client, session = make_client([FakeResponse(payload=payload)])
    trades = client.get_trades(ADDR, limit=7, taker_only=True)

    url, params = session.requests[0]
    assert url == "https://fake.test/trades"
    assert params["user"] == ADDR.lower()
    assert params["limit"] == 7
    assert params["takerOnly"] == "true"
    assert len(trades) == 1 and trades[0].size == 5.0


def test_get_trades_clamps_limit():
    client, session = make_client([FakeResponse(payload=[])])
    client.get_trades(ADDR, limit=99999)
    assert session.requests[0][1]["limit"] == MAX_PAGE_LIMIT


def test_get_trades_rejects_bad_address():
    client, session = make_client([FakeResponse(payload=[])])
    with pytest.raises(ValueError):
        client.get_trades("0xzz")
    assert not session.requests


def test_retries_on_server_error_then_succeeds():
    client, session = make_client(
        [FakeResponse(status_code=500),
         requests.ConnectionError("boom"),
         FakeResponse(payload=[])]
    )
    assert client.get_trades(ADDR) == []
    assert len(session.requests) == 3


def test_raises_after_exhausting_retries():
    client, _ = make_client(
        [FakeResponse(status_code=503)] * 3, max_retries=3
    )
    with pytest.raises(DataApiError):
        client.get_trades(ADDR)


def test_non_list_payload_raises():
    client, _ = make_client([FakeResponse(payload={"error": "nope"})])
    with pytest.raises(DataApiError):
        client.get_trades(ADDR)


def test_client_error_is_not_retried():
    client, session = make_client([FakeResponse(status_code=404, payload={})])
    with pytest.raises(DataApiError):
        client.get_trades(ADDR)
    assert len(session.requests) == 1
