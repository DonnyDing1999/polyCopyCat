import json

import pytest

from polycopycat.models import Trade
from polycopycat.stream import TradeStream

A1 = "0x" + "a" * 40
A2 = "0x" + "b" * 40


def make_stream(addresses=None, **kwargs):
    return TradeStream(addresses or [A1], on_trade=lambda t: None, **kwargs)


def payload(wallet=A1, tx="0x1"):
    return {
        "proxyWallet": wallet, "side": "BUY", "size": 1, "price": 0.5,
        "timestamp": 100, "transactionHash": tx,
    }


def wrap(p):
    return json.dumps({"topic": "activity", "type": "trades", "payload": p})


def test_extracts_trade_from_activity_message():
    trades = make_stream()._extract_trades(wrap(payload()))
    assert len(trades) == 1 and isinstance(trades[0], Trade)
    assert trades[0].proxy_wallet == A1


def test_extracts_from_list_payload_and_list_message():
    stream = make_stream()
    raw = json.dumps([
        {"topic": "activity", "type": "trades",
         "payload": [payload(tx="0x1"), payload(tx="0x2")]},
    ])
    assert [t.transaction_hash for t in stream._extract_trades(raw)] == ["0x1", "0x2"]


def test_bare_trade_dict_is_accepted():
    assert len(make_stream()._extract_trades(json.dumps(payload()))) == 1


def test_ignores_pings_garbage_and_other_topics():
    stream = make_stream()
    assert stream._extract_trades("pong") == []
    assert stream._extract_trades("PING") == []
    assert stream._extract_trades(b"") == []
    assert stream._extract_trades("not json{{") == []
    assert stream._extract_trades(
        json.dumps({"topic": "comments", "type": "comment_created", "payload": payload()})
    ) == []
    assert stream._extract_trades(
        json.dumps({"topic": "activity", "type": "orders_matched", "payload": payload()})
    ) == []


def test_filters_unwatched_addresses():
    assert make_stream([A1])._extract_trades(wrap(payload(wallet=A2))) == []


def test_requires_addresses():
    with pytest.raises(ValueError):
        TradeStream([], on_trade=lambda t: None)


def test_backoff_grows_and_caps():
    stream = make_stream(max_backoff=8.0)
    assert [stream._next_backoff() for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
