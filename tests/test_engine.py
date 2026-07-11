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


def test_sell_skipped_in_m0(rig):
    engine, ledger, _, _ = rig
    submit_sync(engine, make_trade(tx="0x5", side="SELL"))
    counts = ledger.signal_counts()
    assert counts == {"skipped": 1}


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
