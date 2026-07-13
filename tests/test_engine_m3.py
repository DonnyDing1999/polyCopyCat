"""引擎 M3：信号聚合（并单）、多目标轧差、纸面自动结算。"""

import time

import pytest

from polycopycat.engine.aggregate import PendingSignal, merge_pending
from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import ConfigError, EngineConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.engine.signals import Signal
from polycopycat.models import Trade

ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40


class FakeClob:
    """支持 fresh 参数与可配置 winner 的假 CLOB。"""

    def __init__(self):
        self.market = MarketInfo(
            condition_id="0xcond", tick_size=0.01, min_size=5.0,
            neg_risk=False, accepting_orders=True, closed=False,
        )
        self.book = OrderBook(
            asks=(BookLevel(0.51, 500), BookLevel(0.52, 500)),
            bids=(BookLevel(0.49, 500),),
        )

    def get_market(self, condition_id, *, fresh=False):
        return self.market

    def get_book(self, token_id):
        return self.book


class ListNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def make_config(**overrides):
    raw = {
        "targets": [
            {"address": ADDR_A, "ratio": 0.5},
            {"address": ADDR_B, "ratio": 0.5},
        ],
        "sizing": {"mode": "proportional", "ratio": 0.5, "max_per_trade_usdc": 500},
        "filters": {"min_target_notional_usdc": 50, "max_signal_age_s": 60},
        "risk": {"kill_switch_file": "", "max_market_exposure_usdc": 10000,
                 "max_total_exposure_usdc": 10000, "daily_max_loss_usdc": 10000},
        "aggregate": {"window_s": 0, "net_across_targets": True},
    }
    raw.update(overrides)
    return EngineConfig.from_dict(raw)


def make_trade(tx, *, wallet=ADDR_A, side="BUY", size=100.0, price=0.50,
               asset="tok1", condition="0xcond", ts=None):
    return Trade(
        proxy_wallet=wallet, side=side, asset=asset, condition_id=condition,
        size=size, price=price, timestamp=int(ts if ts is not None else time.time()),
        title="Will X happen?", outcome="Yes", transaction_hash=tx,
    )


def make_rig(**overrides):
    config = make_config(**overrides)
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier)
    return engine, ledger, notifier, clob


def to_signals(engine, trades):
    return [
        Signal(trade=t, target=engine._targets[t.proxy_wallet], received_at=time.time())
        for t in trades
    ]


# ---- 合并本身 ----

def test_merge_pending_vwap_and_latest_ts():
    t1 = make_trade("0x1", size=100, price=0.40, ts=1000)
    t2 = make_trade("0x2", size=300, price=0.60, ts=2000)
    target_stub = type("T", (), {"address": ADDR_A})()
    members = [
        PendingSignal(signal_id=i + 1,
                      signal=Signal(trade=t, target=target_stub, received_at=float(i)),
                      prev_target_size=0.0)
        for i, t in enumerate((t1, t2))
    ]
    group = merge_pending(members)
    assert group.trade.size == 400
    assert group.trade.price == pytest.approx(0.55)  # VWAP=(100*0.4+300*0.6)/400
    assert group.trade.timestamp == 2000
    assert group.signal_ids == (1, 2)
    assert group.earliest_received == 0.0


# ---- 并单（分批建仓合并跟单） ----

def test_burst_buys_merge_into_one_order():
    engine, ledger, notifier, _ = make_rig()
    # 三笔 $13/$13/$40 的碎片买入：单笔都低于 $50 阈值，合并 $66 通过
    trades = [
        make_trade("0x1", size=26, price=0.50),
        make_trade("0x2", size=26, price=0.50),
        make_trade("0x3", size=80, price=0.50),
    ]
    engine._process_batch(to_signals(engine, trades))
    assert ledger.signal_counts() == {"executed": 3}
    orders = ledger.recent_orders()
    assert len(orders) == 1  # 三笔信号一张订单
    assert len(notifier.messages) == 1
    assert "并单 3 笔" in notifier.messages[0]
    # $66 × 0.5 = $33 @ 限价 0.52 → 63.46 份
    assert ledger.positions()[0].size == pytest.approx(63.46)


def test_merged_dust_still_filtered():
    engine, ledger, notifier, _ = make_rig()
    trades = [make_trade("0x1", size=10, price=0.5), make_trade("0x2", size=10, price=0.5)]
    engine._process_batch(to_signals(engine, trades))
    counts = ledger.signal_counts()
    assert counts == {"filtered": 2}
    assert notifier.messages == []
    # 理由按合计口径描述
    row = ledger._conn.execute("SELECT detail FROM signals LIMIT 1").fetchone()
    assert "合并 2 笔" in row["detail"]


def test_different_sides_not_merged_same_target():
    """同目标同 token 的买与卖不并组（方向不同），但会进轧差。"""
    engine, ledger, _, _ = make_rig(aggregate={"window_s": 0, "net_across_targets": False})
    engine._process(Signal(trade=make_trade("0xseed", size=200), target=engine._targets[ADDR_A],
                           received_at=time.time()))
    assert ledger.positions()[0].size > 0
    trades = [
        make_trade("0x1", size=100, price=0.50),
        make_trade("0x2", side="SELL", size=100, price=0.50),
    ]
    engine._mirror.replace(ADDR_A, {"tok1": 200})
    engine._process_batch(to_signals(engine, trades))
    orders = ledger.recent_orders()
    assert len(orders) == 3  # seed 买 + 本批一买一卖
    assert {o["side"] for o in orders} == {"BUY", "SELL"}


# ---- 多目标轧差 ----

def seed_position(engine, ledger, size_shares=96.15):
    """先建一笔自有持仓（$100×0.5=50 @0.51/0.52 → 96.15 份）。"""
    engine._process(Signal(trade=make_trade("0xseed", size=200, price=0.50),
                           target=engine._targets[ADDR_A], received_at=time.time()))
    assert ledger.positions()[0].size == pytest.approx(size_shares)


def test_netting_full_offset_executes_nothing():
    engine, ledger, notifier, _ = make_rig()
    seed_position(engine, ledger)
    n_orders = len(ledger.recent_orders())
    notifier.messages.clear()
    # B 目标持有 100 份并全部卖出 → 跟随卖出自有全仓 96.15 份
    # A 目标同窗口买入 $100 → 计划买 96.15 份 → 完全对冲
    engine._mirror.replace(ADDR_B, {"tok1": 100})
    trades = [
        make_trade("0xb1", wallet=ADDR_B, side="SELL", size=100, price=0.50),
        make_trade("0xa1", wallet=ADDR_A, side="BUY", size=200, price=0.50),
    ]
    engine._process_batch(to_signals(engine, trades))
    counts = ledger.signal_counts()
    assert counts.get("netted") == 2
    assert len(ledger.recent_orders()) == n_orders  # 没有新订单
    assert notifier.messages == []  # 轧差不发执行通知
    assert ledger.positions()[0].size == pytest.approx(96.15)  # 持仓原样


def test_netting_partial_scales_dominant_side():
    engine, ledger, notifier, _ = make_rig()
    seed_position(engine, ledger)
    notifier.messages.clear()
    # A 买 $200×0.5=$100 @0.52 → 192.30 份；B 卖出其镜像 200 份中的 100 份
    # （$50 过阈值）→ 跟随卖 50% = 48.07 份；净买 144.23 份，买单按比例缩量
    engine._mirror.replace(ADDR_B, {"tok1": 200})
    trades = [
        make_trade("0xa1", wallet=ADDR_A, side="BUY", size=400, price=0.50),
        make_trade("0xb1", wallet=ADDR_B, side="SELL", size=100, price=0.50),
    ]
    engine._process_batch(to_signals(engine, trades))
    counts = ledger.signal_counts()
    assert counts.get("netted") == 1
    assert counts.get("executed") == 2  # seed 建仓 + 缩量后的净买单
    order = ledger.recent_orders()[0]
    assert order["side"] == "BUY"
    assert order["req_size"] == pytest.approx(144.22, abs=0.02)
    assert any("轧差缩量" in m for m in notifier.messages)


def test_netting_disabled_executes_both_sides():
    engine, ledger, _, _ = make_rig(
        aggregate={"window_s": 0, "net_across_targets": False}
    )
    seed_position(engine, ledger)
    engine._mirror.replace(ADDR_B, {"tok1": 100})
    trades = [
        make_trade("0xb1", wallet=ADDR_B, side="SELL", size=100, price=0.50),
        make_trade("0xa1", wallet=ADDR_A, side="BUY", size=200, price=0.50),
    ]
    engine._process_batch(to_signals(engine, trades))
    assert ledger.signal_counts().get("executed") == 3  # seed + 两笔都执行


# ---- 纸面自动结算 ----

def resolve_market(clob, winner_token):
    clob.market = MarketInfo(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=False, closed=True,
        winner_token_ids=(winner_token,),
    )


def test_settle_win_and_loss_paths():
    engine, ledger, notifier, clob = make_rig()
    seed_position(engine, ledger)  # tok1: 96.15 份 @ ~0.5152
    avg_cost = ledger.positions()[0].avg_cost
    notifier.messages.clear()

    resolve_market(clob, winner_token="tok1")
    engine.reconcile_once()  # data=None 也应执行纸面结算
    assert ledger.positions() == []
    orders = ledger.recent_orders()
    assert orders[0]["side"] == "REDEEM" and orders[0]["status"] == "settled"
    expected = 96.15 * (1.0 - avg_cost)
    assert orders[0]["realized_pnl"] == pytest.approx(expected, abs=0.01)
    assert ledger.realized_pnl_total() == pytest.approx(expected, abs=0.01)
    assert any("市场已结算（赢）" in m for m in notifier.messages)

    # 再对账一轮不应重复结算
    engine.reconcile_once()
    assert len([o for o in ledger.recent_orders() if o["side"] == "REDEEM"]) == 1


def test_settle_losing_position_books_full_loss():
    engine, ledger, notifier, clob = make_rig()
    seed_position(engine, ledger)
    avg_cost = ledger.positions()[0].avg_cost
    resolve_market(clob, winner_token="tokOTHER")
    engine.reconcile_once()
    assert ledger.positions() == []
    order = ledger.recent_orders()[0]
    assert order["avg_price"] == 0.0
    assert order["realized_pnl"] == pytest.approx(-96.15 * avg_cost, abs=0.01)
    assert any("市场已结算（输）" in m for m in notifier.messages)


def test_settle_skips_unresolved_and_respects_flag():
    engine, ledger, _, clob = make_rig()
    seed_position(engine, ledger)
    # 已关闭但没有 winner（结算中）→ 不动
    clob.market = MarketInfo(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=False, closed=True,
    )
    engine.reconcile_once()
    assert len(ledger.positions()) == 1
    # 有 winner 但配置关闭 → 也不动
    resolve_market(clob, "tok1")
    engine.config.auto_settle_resolved = False
    engine.reconcile_once()
    assert len(ledger.positions()) == 1


# ---- 配置与线程路径 ----

def test_window_too_large_rejected():
    with pytest.raises(ConfigError):
        make_config(aggregate={"window_s": 120})


def test_window_null_means_off():
    config = make_config(aggregate={"window_s": None})
    assert config.aggregate.window_s == 0.0


def test_thread_path_aggregates_within_window():
    engine, ledger, notifier, _ = make_rig(
        aggregate={"window_s": 0.3, "net_across_targets": True}
    )
    engine.start()
    try:
        for i, size in enumerate((26, 26, 80)):  # 三笔碎片买入
            engine.submit(make_trade(f"0x{i}", size=size, price=0.50))
        engine.drain()
    finally:
        engine.stop()
    assert ledger.signal_counts() == {"executed": 3}
    assert len(ledger.recent_orders()) == 1
    assert "并单 3 笔" in notifier.messages[0]


def test_stop_mid_window_processes_pending_batch():
    engine, ledger, _, _ = make_rig(
        aggregate={"window_s": 5.0, "net_across_targets": True}
    )
    engine.start()
    engine.submit(make_trade("0x1", size=200, price=0.50))
    time.sleep(0.05)  # 让引擎线程进入聚合窗口
    engine.stop(timeout=10)  # 不等窗口结束，停止时应处理完已收信号
    assert ledger.signal_counts() == {"executed": 1}
