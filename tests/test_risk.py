import time

import pytest

from polycopycat.engine.clob import MarketInfo
from polycopycat.engine.config import RiskConfig, TargetConfig
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.risk import RiskGate, day_start_ts
from polycopycat.engine.signals import OrderIntent, Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


@pytest.fixture
def ledger():
    ledger = Ledger(":memory:")
    yield ledger
    ledger.close()


def market(**kwargs):
    defaults = dict(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=True, closed=False, slug="will-x-happen",
    )
    defaults.update(kwargs)
    return MarketInfo(**defaults)


def intent(side="BUY", limit=0.50, size=100.0, cond="0xcond", token="tok1"):
    return OrderIntent(
        token_id=token, condition_id=cond, side=side, limit_price=limit,
        size=size, ref_price=0.5, neg_risk=False, title="T", outcome="Yes",
    )


def seed_position(ledger, cost_size=100.0, avg=0.5, token="tok1", cond="0xcond"):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset=token, condition_id=cond,
        size=cost_size, price=avg, timestamp=int(time.time()), transaction_hash=f"0x{token}",
    )
    sid, _ = ledger.record_signal(
        Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())
    )
    ledger.record_order(sid, intent(size=cost_size, limit=avg, token=token, cond=cond),
                        mode="paper", status="filled", filled_size=cost_size, avg_price=avg)
    return sid


def test_kill_switch(tmp_path, ledger):
    stop_file = tmp_path / "STOP"
    gate = RiskGate(RiskConfig(kill_switch_file=str(stop_file)), ledger)
    ok, _ = gate.check(intent(), market())
    assert ok
    stop_file.touch()
    ok, reason = gate.check(intent(), market())
    assert not ok and "停机" in reason


def test_market_state_blocks(ledger):
    gate = RiskGate(RiskConfig(kill_switch_file=""), ledger)
    assert not gate.check(intent(), market(closed=True))[0]
    assert not gate.check(intent(), market(accepting_orders=False))[0]


def test_blacklist_by_condition_or_slug(ledger):
    gate = RiskGate(RiskConfig(kill_switch_file="", market_blacklist=["0xCOND"]), ledger)
    assert not gate.check(intent(), market())[0]
    gate = RiskGate(RiskConfig(kill_switch_file="", market_blacklist=["will-x-happen"]), ledger)
    ok, reason = gate.check(intent(), market())
    assert not ok and "黑名单" in reason


def test_market_exposure_cap(ledger):
    seed_position(ledger, cost_size=100, avg=0.5)  # 该市场成本 $50
    gate = RiskGate(RiskConfig(kill_switch_file="", max_market_exposure_usdc=60.0), ledger)
    ok, _ = gate.check(intent(size=10, limit=0.5), market())      # 50+5 <= 60
    assert ok
    ok, reason = gate.check(intent(size=30, limit=0.5), market())  # 50+15 > 60
    assert not ok and "单市场敞口" in reason


def test_total_exposure_cap(ledger):
    seed_position(ledger, cost_size=100, avg=0.5, token="tok1", cond="0xc1")
    seed_position(ledger, cost_size=100, avg=0.5, token="tok2", cond="0xc2")  # 总成本 $100
    gate = RiskGate(RiskConfig(kill_switch_file="", max_total_exposure_usdc=110.0), ledger)
    ok, reason = gate.check(intent(size=30, limit=0.5, cond="0xc3", token="tok3"), market())
    assert not ok and "总敞口" in reason


def test_daily_loss_blocks_buys_not_sells(ledger):
    seed_position(ledger, cost_size=100, avg=0.5)
    ledger.record_order(1, intent(side="SELL", size=100, limit=0.3), mode="paper",
                        status="filled", filled_size=100, avg_price=0.3)  # 实现 -20
    gate = RiskGate(RiskConfig(kill_switch_file="", daily_max_loss_usdc=10.0), ledger)
    ok, reason = gate.check(intent(side="BUY"), market())
    assert not ok and "熔断" in reason
    ok, _ = gate.check(intent(side="SELL"), market())  # 减仓放行
    assert ok


def test_day_start_is_today_midnight():
    start = day_start_ts()
    local = time.localtime(start)
    assert (local.tm_hour, local.tm_min, local.tm_sec) == (0, 0, 0)
    assert time.time() - start < 86400 + 1
