"""目标健康巡检：scout 排除规则复查在跟目标，自动暂停/复跟。"""

import time

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import EngineConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.models import Trade

ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40


class FakeClob:
    def __init__(self):
        self.market = MarketInfo(
            condition_id="0xcond", tick_size=0.01, min_size=5.0,
            neg_risk=False, accepting_orders=True, closed=False,
        )
        self.book = OrderBook(asks=(BookLevel(0.51, 500),), bids=(BookLevel(0.49, 500),))

    def get_market(self, condition_id, *, fresh=False):
        return self.market

    def get_book(self, token_id):
        return self.book


class ListNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class FakeDataClient:
    """按地址返回预置的成交带与持仓。"""

    def __init__(self, tapes=None, positions=None):
        self.tapes = {k.lower(): v for k, v in (tapes or {}).items()}
        self.positions = {k.lower(): v for k, v in (positions or {}).items()}

    def get_trades(self, user, **kwargs):
        return self.tapes.get(user.lower(), [])

    def get_positions(self, user, **kwargs):
        return self.positions.get(user.lower(), [])


def healthy_tape(wallet, n=30):
    """一条能过 scout 排除规则的成交带：样本足、金额够、近期活跃、纯买入。"""
    now = int(time.time())
    return [
        Trade(
            proxy_wallet=wallet, side="BUY", asset=f"tok{i % 5}",
            condition_id=f"0xc{i % 5}", size=300, price=0.5,
            timestamp=now - i * 3600, title=f"M{i}", outcome="Yes",
            transaction_hash=f"0x{i:x}",
        )
        for i in range(n)
    ]


def make_engine(data, targets=(ADDR_A, ADDR_B), **health):
    config = EngineConfig.from_dict({
        "targets": [{"address": a} for a in targets],
        "risk": {"kill_switch_file": ""},
        "aggregate": {"window_s": 0},
        "health": {"check_interval_s": 21600, **health},
    })
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier,
                        data_client=data)
    return engine, notifier


def test_unhealthy_target_auto_paused():
    # A 健康；B 空成交带 → 样本不足 → 暂停
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, notifier = make_engine(data)
    engine.check_targets_health()
    assert engine._targets[ADDR_A].paused is False
    assert engine._targets[ADDR_B].paused is True
    assert ADDR_B in engine._health_paused
    assert any("自动暂停" in m and "0xbbbb" in m for m in notifier.messages)


def test_paused_target_filters_signals():
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, _ = make_engine(data)
    engine.check_targets_health()
    trade = Trade(
        proxy_wallet=ADDR_B, side="BUY", asset="tok1", condition_id="0xcond",
        size=100, price=0.5, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash="0xz",
    )
    from polycopycat.engine.signals import Signal
    engine._process(Signal(trade=trade, target=engine._targets[ADDR_B],
                           received_at=time.time()))
    counts = engine._ledger.signal_counts()
    assert counts == {"filtered": 1}


def test_recovered_target_auto_resumed():
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, notifier = make_engine(data)
    engine.check_targets_health()
    assert engine._targets[ADDR_B].paused is True
    # B 恢复健康
    data.tapes[ADDR_B] = healthy_tape(ADDR_B)
    engine.check_targets_health()
    assert engine._targets[ADDR_B].paused is False
    assert ADDR_B not in engine._health_paused
    assert any("自动复跟" in m for m in notifier.messages)


def test_manual_pause_untouched():
    # 手动暂停的目标：即便数据健康也不复跟、即便不健康也不重复动作
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: healthy_tape(ADDR_B)})
    engine, notifier = make_engine(data)
    engine._targets[ADDR_B].paused = True  # 模拟配置手动暂停
    engine.check_targets_health()
    assert engine._targets[ADDR_B].paused is True  # 不被巡检解开
    assert not any("0xbbbb" in m for m in notifier.messages)


def test_auto_pause_off_only_notifies():
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, notifier = make_engine(data, auto_pause=False)
    engine.check_targets_health()
    assert engine._targets[ADDR_B].paused is False  # 没停
    assert any("人工复查" in m for m in notifier.messages)


def test_fetch_failure_skips_target():
    class FlakyData(FakeDataClient):
        def get_trades(self, user, **kwargs):
            from polycopycat.data_api import DataApiError
            if user.lower() == ADDR_B:
                raise DataApiError("boom")
            return super().get_trades(user, **kwargs)

    data = FlakyData(tapes={ADDR_A: healthy_tape(ADDR_A)})
    engine, notifier = make_engine(data)
    engine.check_targets_health()
    assert engine._targets[ADDR_B].paused is False  # 网络抖动绝不误停
    assert not any("0xbbbb" in m for m in notifier.messages)


def test_interval_gate():
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, _ = make_engine(data)
    # 刚启动：未满周期不查
    engine._maybe_check_health()
    assert engine._targets[ADDR_B].paused is False
    # 把上次巡检时间拨回一个周期前 → 触发
    engine._last_health_check -= engine.config.health.check_interval_s + 1
    engine._maybe_check_health()
    assert engine._targets[ADDR_B].paused is True


def test_disabled_by_zero_interval():
    data = FakeDataClient(tapes={ADDR_B: []})
    engine, _ = make_engine(data, check_interval_s=0)
    engine._last_health_check -= 10**6
    engine._maybe_check_health()
    assert engine._targets[ADDR_B].paused is False
