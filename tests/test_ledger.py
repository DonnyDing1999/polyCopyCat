import time

import pytest

from polycopycat.engine.config import TargetConfig
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.signals import OrderIntent, Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


@pytest.fixture
def ledger():
    ledger = Ledger(":memory:")
    yield ledger
    ledger.close()


def make_signal(tx="0x1", price=0.5, size=100.0):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash=tx,
    )
    return Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())


def intent(side="BUY", token="tok1", cond="0xcond", limit=0.52, size=100.0):
    return OrderIntent(
        token_id=token, condition_id=cond, side=side, limit_price=limit,
        size=size, ref_price=0.5, neg_risk=False, title="T", outcome="Yes",
    )


def test_signal_dedupe(ledger):
    sid, fresh = ledger.record_signal(make_signal("0x1"))
    assert fresh and sid is not None
    sid2, fresh2 = ledger.record_signal(make_signal("0x1"))
    assert not fresh2 and sid2 == sid
    _, fresh3 = ledger.record_signal(make_signal("0x2"))
    assert fresh3
    ledger.update_signal(sid, "executed", "ok")
    assert ledger.signal_counts() == {"executed": 1, "received": 1}


def test_buy_updates_avg_cost(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.5)
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.6)
    positions = ledger.positions()
    assert len(positions) == 1
    assert positions[0].size == 200
    assert abs(positions[0].avg_cost - 0.55) < 1e-9
    assert abs(ledger.market_cost("0xcond") - 110) < 1e-9
    assert abs(ledger.total_cost() - 110) < 1e-9


def test_sell_realizes_pnl(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.5)
    realized = ledger.record_order(sid, intent(side="SELL", size=40), mode="paper",
                                   status="filled", filled_size=40, avg_price=0.7)
    assert abs(realized - 40 * 0.2) < 1e-9
    position = ledger.positions()[0]
    assert position.size == 60
    assert abs(position.realized_pnl - 8.0) < 1e-9
    assert abs(ledger.realized_pnl_total() - 8.0) < 1e-9
    assert abs(ledger.realized_pnl_since(time.time() - 60) - 8.0) < 1e-9
    assert ledger.realized_pnl_since(time.time() + 60) == 0


def test_oversell_clamped(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=50), mode="paper", status="filled",
                        filled_size=50, avg_price=0.5)
    realized = ledger.record_order(sid, intent(side="SELL", size=500), mode="paper",
                                   status="filled", filled_size=500, avg_price=0.6)
    assert abs(realized - 50 * 0.1) < 1e-9  # 只按实际持仓 50 份结算
    assert ledger.position_size("tok1") == 0
    assert ledger.positions() == []  # size=0 不再显示


def test_live_submitted_does_not_touch_positions(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="live", status="submitted",
                        filled_size=0, avg_price=0, apply_fill=False)
    assert ledger.positions() == []
    assert len(ledger.recent_orders()) == 1
