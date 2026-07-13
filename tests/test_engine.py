"""引擎集成测试：直接驱动 _process，全链路走真实的过滤/计算/风控/账本。"""

import time

import pytest

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import EngineConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.engine.signals import Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


class FakeClob:
    def __init__(self):
        self.market = MarketInfo(
            condition_id="0xcond", tick_size=0.01, min_size=5.0,
            neg_risk=False, accepting_orders=True, closed=False,
        )
        self.book = OrderBook(asks=(BookLevel(0.51, 30), BookLevel(0.52, 100)),
                              bids=(BookLevel(0.49, 100),))

    def get_market(self, condition_id):
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
        "targets": [{"address": ADDR, "ratio": 0.5, "max_per_trade_usdc": 100}],
        "sizing": {"mode": "proportional", "ratio": 0.5, "max_per_trade_usdc": 100},
        "filters": {"min_target_notional_usdc": 20, "max_signal_age_s": 60},
        "risk": {"kill_switch_file": "", "max_market_exposure_usdc": 1000,
                 "max_total_exposure_usdc": 1000, "daily_max_loss_usdc": 1000},
        # 本文件验证逐笔语义，聚合窗口显式关闭；聚合与轧差见 test_engine_m3.py
        "aggregate": {"window_s": 0},
    }
    raw.update(overrides)
    return EngineConfig.from_dict(raw)


def make_trade(tx="0x1", side="BUY", size=100.0, price=0.50, ts=None):
    return Trade(
        proxy_wallet=ADDR, side=side, asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(ts if ts is not None else time.time()),
        title="Will X happen?", outcome="Yes", transaction_hash=tx,
    )


@pytest.fixture
def rig():
    config = make_config()
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier)
    yield engine, ledger, notifier, clob
    ledger.close()


def submit_sync(engine, trade):
    """绕过线程直接处理一笔信号。"""
    target = engine._targets[trade.proxy_wallet]
    engine._process(Signal(trade=trade, target=target, received_at=time.time()))


def test_buy_happy_path(rig):
    engine, ledger, notifier, _ = rig
    submit_sync(engine, make_trade())
    # $50 × 0.5 = $25 @ 限价 0.52 → 48.07 份；盘口 30@0.51 + 18.07@0.52 全部成交
    positions = ledger.positions()
    assert len(positions) == 1 and positions[0].size == 48.07
    assert ledger.signal_counts() == {"executed": 1}
    assert len(notifier.messages) == 1
    assert "纸面" in notifier.messages[0] and "成交 48.07" in notifier.messages[0]


def test_duplicate_signal_processed_once(rig):
    engine, ledger, notifier, _ = rig
    trade = make_trade()
    submit_sync(engine, trade)
    submit_sync(engine, trade)
    assert len(ledger.recent_orders()) == 1
    assert len(notifier.messages) == 1


def test_small_trade_filtered(rig):
    engine, ledger, notifier, _ = rig
    submit_sync(engine, make_trade(tx="0x2", size=10, price=0.5))  # $5 < 阈值 $20
    assert ledger.signal_counts() == {"filtered": 1}
    assert ledger.positions() == [] and notifier.messages == []


def test_stale_signal_filtered(rig):
    engine, ledger, _, _ = rig
    submit_sync(engine, make_trade(tx="0x3", ts=time.time() - 3600))
    assert ledger.signal_counts() == {"filtered": 1}


def test_risk_block_notifies(rig):
    engine, ledger, notifier, _ = rig
    engine.config.risk.max_market_exposure_usdc = 10.0
    engine._risk._config.max_market_exposure_usdc = 10.0
    submit_sync(engine, make_trade(tx="0x4"))
    assert ledger.signal_counts() == {"risk_blocked": 1}
    assert any("风控拦截" in m for m in notifier.messages)
    assert ledger.positions() == []


def seed_buy(engine, ledger):
    """先跟一笔买入建仓（48.07 份 @ 平均 0.5138）。"""
    submit_sync(engine, make_trade(tx="0xseed"))
    assert ledger.positions()[0].size == 48.07


def test_sell_follows_target_fraction(rig):
    engine, ledger, notifier, _ = rig
    seed_buy(engine, ledger)
    engine._mirror.replace(ADDR, {"tok1": 1000})
    submit_sync(engine, make_trade(tx="0x5", side="SELL", size=500))  # 目标卖出 50%
    position = ledger.positions()[0]
    assert abs(position.size - 24.04) < 1e-9  # 48.07 - 24.03
    assert position.realized_pnl < 0  # 0.49 卖出低于建仓均价 0.5138
    assert any("跟随卖出 50%" in m for m in notifier.messages)


def test_sell_without_own_position_skipped(rig):
    engine, ledger, _, _ = rig
    engine._mirror.replace(ADDR, {"tok1": 1000})
    submit_sync(engine, make_trade(tx="0x5", side="SELL", size=500))
    assert ledger.signal_counts() == {"skipped": 1}


def test_sell_unknown_mirror_closes_all(rig):
    engine, ledger, notifier, _ = rig
    seed_buy(engine, ledger)
    engine._mirror.replace(ADDR, {})  # 镜像没有记录
    submit_sync(engine, make_trade(tx="0x5", side="SELL", size=100))
    assert ledger.positions() == []  # 全平
    assert any("跟随卖出 100%" in m for m in notifier.messages)


def test_sell_dust_remainder_closes_all(rig):
    engine, ledger, _, _ = rig
    seed_buy(engine, ledger)
    engine._mirror.replace(ADDR, {"tok1": 1000})
    # 跟随 90% 应卖 43.26，但剩 4.81 份 < 最小单 5 份 → 全平
    submit_sync(engine, make_trade(tx="0x5", side="SELL", size=900))
    assert ledger.positions() == []


def test_sell_can_be_disabled_by_config(rig):
    engine, ledger, _, _ = rig
    seed_buy(engine, ledger)
    engine._filter._config.follow_sells = False
    submit_sync(engine, make_trade(tx="0x5", side="SELL", size=500))
    counts = ledger.signal_counts()
    assert counts.get("filtered") == 1


def test_mirror_updated_even_for_filtered_signals(rig):
    engine, _, _, _ = rig
    submit_sync(engine, make_trade(tx="0x9", size=10, price=0.5))  # $5 尘埃单被过滤
    assert engine._mirror.size_of(ADDR, "tok1") == 10  # 但镜像照常累加


def test_no_liquidity_records_no_fill(rig):
    engine, ledger, notifier, clob = rig
    clob.book = OrderBook(asks=(BookLevel(0.60, 100),), bids=())
    submit_sync(engine, make_trade(tx="0x6"))
    assert ledger.signal_counts() == {"no_fill": 1}
    assert ledger.positions() == []
    rows = ledger.recent_orders()
    assert len(rows) == 1 and rows[0]["status"] == "rejected"


def test_queue_thread_end_to_end(rig):
    engine, ledger, _, _ = rig
    engine.start()
    engine.submit(make_trade(tx="0x7"))
    engine.drain()
    engine.stop()
    assert ledger.signal_counts() == {"executed": 1}


def test_unwatched_wallet_ignored(rig):
    engine, ledger, _, _ = rig
    other = make_trade(tx="0x8")
    object.__setattr__(other, "proxy_wallet", "0x" + "b" * 40)
    engine.submit(other)  # 不入队
    assert engine._queue.qsize() == 0


class FakeDataClient:
    def __init__(self, positions_by_user):
        self.positions_by_user = {k.lower(): v for k, v in positions_by_user.items()}
        self.calls = []

    def get_positions(self, user, **kwargs):
        self.calls.append(user.lower())
        return self.positions_by_user.get(user.lower(), [])


def make_position(asset="tok1", size=1000.0, wallet=ADDR, redeemable=False, avg=0.4):
    from polycopycat.models import Position

    return Position(
        proxy_wallet=wallet, asset=asset, condition_id="0xcond", size=size,
        avg_price=avg, realized_pnl=1.5, redeemable=redeemable,
        title="Will X happen?", outcome="Yes",
    )


def test_reconcile_refreshes_mirror(rig):
    engine, ledger, _, clob = rig
    engine._data = FakeDataClient({ADDR: [make_position(size=777)]})
    engine.reconcile_once()
    assert engine._mirror.size_of(ADDR, "tok1") == 777


def test_reconcile_live_syncs_ledger_and_notifies_redeemable_once():
    own = "0x" + "c" * 40
    config = make_config(mode="live")
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    data = FakeDataClient({
        ADDR: [make_position(size=500)],
        own: [make_position(asset="tokX", size=88, wallet=own, redeemable=True, avg=0.3)],
    })
    engine = CopyEngine(config, clob=clob, ledger=ledger, executor=PaperExecutor(clob),
                        notifier=notifier, data_client=data, own_address=own)
    engine.reconcile_once()
    engine.reconcile_once()  # 第二轮不应重复提醒

    position = ledger.positions()[0]
    assert position.token_id == "tokX" and position.size == 88
    assert abs(position.avg_cost - 0.3) < 1e-9
    redeems = [m for m in notifier.messages if "可赎回" in m]
    assert len(redeems) == 1 and "88.00" in redeems[0]
    ledger.close()
