import time

from polycopycat.engine.clob import MarketInfo
from polycopycat.engine.config import ExecutionConfig, SizingConfig, TargetConfig
from polycopycat.engine.signals import Signal
from polycopycat.engine.sizing import plan_buy
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


def make_signal(size=100.0, price=0.50, target_kwargs=None):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash="0x1",
    )
    target = TargetConfig(address=ADDR, **(target_kwargs or {}))
    return Signal(trade=trade, target=target, received_at=time.time())


def market(tick=0.01, min_size=5.0, neg_risk=False):
    return MarketInfo(
        condition_id="0xcond", tick_size=tick, min_size=min_size,
        neg_risk=neg_risk, accepting_orders=True, closed=False,
    )


def test_proportional_with_cap():
    sizing = SizingConfig(mode="proportional", ratio=0.5, max_per_trade_usdc=100)
    execution = ExecutionConfig(slippage_cap=0.02)
    intent, reason = plan_buy(make_signal(size=100, price=0.50), market(), sizing, execution)
    assert intent is not None, reason
    # 目标金额 $50 × 0.5 = $25，限价 0.52 → 48.07 份（向下取到 0.01 份）
    assert intent.limit_price == 0.52
    assert intent.size == 48.07
    assert intent.side == "BUY" and intent.token_id == "tok1"


def test_cap_applies():
    sizing = SizingConfig(mode="proportional", ratio=1.0, max_per_trade_usdc=10)
    intent, _ = plan_buy(make_signal(size=1000, price=0.50), market(), sizing, ExecutionConfig())
    assert intent is not None
    assert intent.size * intent.limit_price <= 10 + 1e-6


def test_target_overrides_ratio_and_cap():
    sizing = SizingConfig(mode="proportional", ratio=0.5, max_per_trade_usdc=100)
    intent, _ = plan_buy(
        make_signal(size=100, price=0.50, target_kwargs={"ratio": 0.1, "max_per_trade_usdc": 3}),
        market(min_size=1.0), sizing, ExecutionConfig(),
    )
    assert intent is not None
    assert intent.size * intent.limit_price <= 3 + 1e-6


def test_fixed_mode():
    sizing = SizingConfig(mode="fixed", fixed_usdc=20, max_per_trade_usdc=100)
    intent, _ = plan_buy(make_signal(size=10000, price=0.50), market(), sizing, ExecutionConfig())
    assert intent is not None
    assert abs(intent.size * intent.limit_price - 20) < 0.6  # 取整损耗以内


def test_below_min_size_skips():
    sizing = SizingConfig(mode="fixed", fixed_usdc=1, max_per_trade_usdc=100)
    intent, reason = plan_buy(make_signal(price=0.50), market(min_size=5.0), sizing, ExecutionConfig())
    assert intent is None
    assert "最小下单量" in reason


def test_limit_price_respects_tick_and_bounds():
    sizing = SizingConfig(mode="fixed", fixed_usdc=20, max_per_trade_usdc=100)
    # 0.505 + 0.02 = 0.525 → tick 0.01 向下取整 0.52
    intent, _ = plan_buy(make_signal(price=0.505), market(tick=0.01), sizing, ExecutionConfig(0.02))
    assert intent.limit_price == 0.52
    # tick 0.001 保留 0.525
    intent, _ = plan_buy(make_signal(price=0.505), market(tick=0.001), sizing, ExecutionConfig(0.02))
    assert intent.limit_price == 0.525
    # 贴近上界：0.985 + 0.02 → 封在 1 - tick = 0.99
    intent, _ = plan_buy(make_signal(price=0.985), market(tick=0.01), sizing, ExecutionConfig(0.02))
    assert intent.limit_price == 0.99


def test_neg_risk_flag_propagates():
    sizing = SizingConfig(mode="fixed", fixed_usdc=20, max_per_trade_usdc=100)
    intent, _ = plan_buy(make_signal(), market(neg_risk=True), sizing, ExecutionConfig())
    assert intent.neg_risk is True
