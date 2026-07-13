"""订单簿深度分析 + 深度感知跟单定量。"""

import time

import pytest

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import ConfigError, ExecutionConfig, SizingConfig, TargetConfig
from polycopycat.engine.depth import depth_capped_notional, fillable_within_limit
from polycopycat.engine.sizing import plan_buy
from polycopycat.engine.signals import Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


def book(asks=(), bids=()):
    return OrderBook(
        asks=tuple(BookLevel(p, s) for p, s in asks),
        bids=tuple(BookLevel(p, s) for p, s in bids),
    )


# ---- fillable_within_limit ----

def test_fillable_buy_sums_asks_within_limit():
    b = book(asks=[(0.51, 100), (0.52, 200), (0.55, 500)])
    fill = fillable_within_limit(b, "BUY", limit_price=0.52)
    assert fill.shares == 300                 # 100 + 200，0.55 档超限价被排除
    assert fill.notional == pytest.approx(0.51 * 100 + 0.52 * 200)
    assert fill.avg_price == pytest.approx((51 + 104) / 300)
    assert fill.levels_used == 2


def test_fillable_sell_sums_bids_above_limit():
    b = book(bids=[(0.49, 100), (0.48, 200), (0.45, 500)])
    fill = fillable_within_limit(b, "SELL", limit_price=0.48)
    assert fill.shares == 300                 # 0.49 + 0.48，0.45 低于限价排除
    assert fill.levels_used == 2


def test_fillable_empty_when_nothing_in_range():
    b = book(asks=[(0.60, 100)])
    fill = fillable_within_limit(b, "BUY", limit_price=0.52)
    assert fill.empty and fill.notional == 0 and fill.avg_price == 0


def test_depth_capped_caps_to_capacity():
    b = book(asks=[(0.51, 100), (0.52, 100)])   # 容量 = 51 + 52 = $103
    capped, capacity = depth_capped_notional(500, b, "BUY", 0.52)
    assert capacity == pytest.approx(103)
    assert capped == pytest.approx(103)          # 想要 $500，只能吃 $103
    capped2, _ = depth_capped_notional(40, b, "BUY", 0.52)
    assert capped2 == 40                          # 想要 $40，书够深，原样


# ---- 深度感知 plan_buy ----

def sig(size=100.0, price=0.50, ratio=None):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash="0x1",
    )
    return Signal(trade=trade, target=TargetConfig(address=ADDR, ratio=ratio),
                  received_at=time.time())


MARKET = MarketInfo(condition_id="0xcond", tick_size=0.01, min_size=5.0,
                    neg_risk=False, accepting_orders=True, closed=False)
EXEC = ExecutionConfig(slippage_cap=0.02)


def test_depth_aware_off_ignores_book():
    sizing = SizingConfig(ratio=0.1, max_per_trade_usdc=100)  # depth_aware 默认 False
    deep = book(asks=[(0.50, 10000)])
    intent, _ = plan_buy(sig(size=100, price=0.50), MARKET, sizing, EXEC, book=deep)
    # 目标金额 $50 × 0.1 = $5 @ 限价 0.52 → 9.61 份，与深度无关
    assert intent is not None and intent.size == pytest.approx(9.61, abs=0.01)
    assert intent.note == ""


def test_depth_amplify_when_book_deep():
    sizing = SizingConfig(ratio=0.1, max_per_trade_usdc=100,
                          depth_aware=True, max_follow_multiple=3.0)
    deep = book(asks=[(0.51, 10000)])            # 盘口极深
    intent, _ = plan_buy(sig(size=100, price=0.50), MARKET, sizing, EXEC, book=deep)
    # 基准 $5，放大 3× = $15（未超 $100 上限，书也够深）→ @0.52 = 28.84 份
    assert intent is not None
    assert intent.size == pytest.approx(28.84, abs=0.01)
    assert "深度放大 3.0×" in intent.note


def test_depth_amplify_capped_by_book():
    sizing = SizingConfig(ratio=0.1, max_per_trade_usdc=100,
                          depth_aware=True, max_follow_multiple=5.0)
    thin = book(asks=[(0.51, 20), (0.52, 10)])   # 容量 = 0.51*20 + 0.52*10 = $15.4
    intent, _ = plan_buy(sig(size=100, price=0.50), MARKET, sizing, EXEC, book=thin)
    # 想放大到 $50，但盘口只有 $15.4 → 封到 $15.4 @0.52 ≈ 29.61 份
    assert intent is not None
    assert intent.size == pytest.approx(29.61, abs=0.05)
    assert "深度封顶" in intent.note or "深度放大" in intent.note


def test_depth_aware_no_liquidity_skips():
    sizing = SizingConfig(ratio=0.1, depth_aware=True, max_follow_multiple=2.0)
    empty = book(asks=[(0.60, 100)])             # 限价 0.52 内无盘口
    intent, reason = plan_buy(sig(size=100, price=0.50), MARKET, sizing, EXEC, book=empty)
    assert intent is None and "无盘口深度" in reason


def test_depth_aware_still_honors_per_trade_cap():
    sizing = SizingConfig(ratio=0.1, max_per_trade_usdc=20,
                          depth_aware=True, max_follow_multiple=10.0)
    deep = book(asks=[(0.51, 100000)])
    intent, _ = plan_buy(sig(size=100, price=0.50), MARKET, sizing, EXEC, book=deep)
    # 放大想到 $100，但单笔上限 $20 → @0.52 = 38.46 份
    assert intent is not None and intent.size == pytest.approx(38.46, abs=0.01)


def test_multiple_below_one_rejected():
    with pytest.raises(ConfigError):
        SizingConfig(max_follow_multiple=0.5)
