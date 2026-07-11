from polycopycat.engine.mirror import TargetMirror
from polycopycat.models import Trade

A1 = "0x" + "a" * 40


def trade(side, size, token="tok1", wallet=A1):
    return Trade(
        proxy_wallet=wallet, side=side, asset=token, condition_id="0xcond",
        size=size, price=0.5, timestamp=100, transaction_hash="0x1",
    )


def test_apply_trade_tracks_and_returns_prev():
    mirror = TargetMirror()
    assert mirror.apply_trade(trade("BUY", 100)) == 0.0
    assert mirror.size_of(A1, "tok1") == 100
    assert mirror.apply_trade(trade("BUY", 50)) == 100
    assert mirror.apply_trade(trade("SELL", 30)) == 150
    assert mirror.size_of(A1, "tok1") == 120


def test_sell_clamps_at_zero_and_cleans_up():
    mirror = TargetMirror()
    mirror.apply_trade(trade("BUY", 10))
    mirror.apply_trade(trade("SELL", 999))
    assert mirror.size_of(A1, "tok1") == 0
    assert mirror.snapshot(A1) == {}


def test_replace_overwrites_address_snapshot():
    mirror = TargetMirror()
    mirror.apply_trade(trade("BUY", 100, token="tok1"))
    mirror.apply_trade(trade("BUY", 50, token="tok2"))
    mirror.replace(A1, {"tok1": 777, "tok3": 5})
    assert mirror.snapshot(A1) == {"tok1": 777, "tok3": 5}


def test_addresses_are_isolated():
    other = "0x" + "b" * 40
    mirror = TargetMirror()
    mirror.apply_trade(trade("BUY", 100, wallet=A1))
    mirror.apply_trade(trade("BUY", 7, wallet=other))
    mirror.replace(A1, {})
    assert mirror.size_of(other, "tok1") == 7
