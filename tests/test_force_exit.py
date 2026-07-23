"""特性3：对账强制离场（force_exit_on_target_flat）。

当初建这笔仓的目标全部清仓、而我们的卖出信号漏了（超龄被拦/宕机/双通道都漏）时，
对账兜底强平自仓，避免拿到归零结算。全离线：CLOB 与 data-api 都是注入的假实现。
"""

import time

from polycopycat.data_api import DataApiError
from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import EngineConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.engine.signals import Signal
from polycopycat.models import Position, Trade

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
    """按 user 返回持仓；fail_for 里的地址抛错（模拟镜像拉取失败）。"""

    def __init__(self, positions_by_user, fail_for=None):
        self.positions_by_user = {k.lower(): v for k, v in positions_by_user.items()}
        self.fail_for = {a.lower() for a in (fail_for or set())}

    def get_positions(self, user, **kwargs):
        if user.lower() in self.fail_for:
            raise DataApiError(f"模拟拉取失败 {user}")
        return self.positions_by_user.get(user.lower(), [])


def make_config(targets=(ADDR_A,), **overrides):
    raw = {
        "targets": [{"address": a, "ratio": 0.5, "max_per_trade_usdc": 500} for a in targets],
        "sizing": {"mode": "proportional", "ratio": 0.5, "max_per_trade_usdc": 500},
        "filters": {"min_target_notional_usdc": 20, "max_signal_age_s": 60},
        "risk": {"kill_switch_file": "", "max_market_exposure_usdc": 100000,
                 "max_total_exposure_usdc": 100000, "daily_max_loss_usdc": 100000},
        "aggregate": {"window_s": 0},
        "execution": {"slippage_cap": 0.02, "retry_no_fill_s": None},
    }
    raw.update(overrides)
    return EngineConfig.from_dict(raw)


def make_trade(tx, *, wallet=ADDR_A, side="BUY", size=200.0, price=0.50, asset="tok1"):
    return Trade(
        proxy_wallet=wallet, side=side, asset=asset, condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time()),
        title="Will X happen?", outcome="Yes", transaction_hash=tx,
    )


def make_position(asset="tok1", size=0.0, wallet=ADDR_A):
    return Position(
        proxy_wallet=wallet, asset=asset, condition_id="0xcond", size=size,
        avg_price=0.4, realized_pnl=0.0, redeemable=False,
        title="Will X happen?", outcome="Yes",
    )


def make_rig(targets=(ADDR_A,), data=None, **overrides):
    config = make_config(targets=targets, **overrides)
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger, executor=PaperExecutor(clob),
                        notifier=notifier, data_client=data)
    return engine, ledger, notifier, clob


def seed_buy(engine, wallet=ADDR_A, tx="0xseed"):
    """让某目标建一笔仓：产生自有持仓 + 该目标对 tok1 的 executed BUY 信号。"""
    target = engine._targets[wallet]
    engine._process(Signal(trade=make_trade(tx, wallet=wallet), target=target,
                           received_at=time.time()))


def force_exit_orders(ledger):
    return ledger._conn.execute(
        "SELECT * FROM orders WHERE signal_id = 0 AND side = 'SELL'"
    ).fetchall()


def force_exit_events(ledger):
    return ledger._conn.execute("SELECT * FROM events WHERE kind = 'force_exit'").fetchall()


def test_two_round_confirmation_then_exit():
    """首轮只登记、次轮才强平；成功离场落 order(signal_id=0) + force_exit 事件。"""
    engine, ledger, notifier, _ = make_rig(data=FakeDataClient({ADDR_A: []}))
    seed_buy(engine)
    assert ledger.positions()[0].size > 0
    notifier.messages.clear()

    engine.reconcile_once()  # 首轮：只登记
    assert force_exit_orders(ledger) == []
    assert force_exit_events(ledger) == []
    assert "tok1" in engine._pending_force_exit
    assert ledger.positions()[0].size > 0

    engine.reconcile_once()  # 次轮：确认后强平
    exits = force_exit_orders(ledger)
    assert len(exits) == 1 and exits[0]["status"] == "filled"
    events = force_exit_events(ledger)
    assert len(events) == 1 and "强制离场" in events[0]["detail"]
    assert ledger.positions() == []  # 已清仓
    assert "tok1" not in engine._pending_force_exit
    assert any("对账离场" in m for m in notifier.messages)


def test_disabled_flag_never_exits():
    engine, ledger, _, _ = make_rig(
        data=FakeDataClient({ADDR_A: []}), force_exit_on_target_flat=False
    )
    seed_buy(engine)
    engine.reconcile_once()
    engine.reconcile_once()
    assert force_exit_orders(ledger) == []
    assert ledger.positions()[0].size > 0
    assert engine._pending_force_exit == set()


def test_partial_builders_still_holding_no_exit():
    """两个建仓者，A 清仓、B 仍持有 → 不动手（要求全部清仓）。"""
    data = FakeDataClient({ADDR_A: [], ADDR_B: [make_position(size=50, wallet=ADDR_B)]})
    engine, ledger, _, _ = make_rig(targets=(ADDR_A, ADDR_B), data=data)
    seed_buy(engine, wallet=ADDR_A, tx="0xa")
    seed_buy(engine, wallet=ADDR_B, tx="0xb")
    engine.reconcile_once()
    engine.reconcile_once()
    assert force_exit_orders(ledger) == []
    assert ledger.positions()[0].size > 0
    assert "tok1" not in engine._pending_force_exit


def test_empty_builder_intersection_no_exit():
    """建仓者已被用户移出配置 targets → 不越权强平。"""
    engine, ledger, _, _ = make_rig(data=FakeDataClient({ADDR_A: []}))
    seed_buy(engine)
    assert ledger.positions()[0].size > 0
    del engine._targets[ADDR_A]  # 用户把建仓者从配置删了
    engine.reconcile_once()
    engine.reconcile_once()
    assert force_exit_orders(ledger) == []
    assert ledger.positions()[0].size > 0


def test_stale_mirror_on_fetch_failure_skips():
    """确认轮遇到建仓者拉取失败 → 跳过，不拿陈旧镜像误杀。"""
    engine, ledger, _, _ = make_rig(data=FakeDataClient({ADDR_A: []}))
    seed_buy(engine)
    engine.reconcile_once()  # 首轮成功：镜像刷空、登记待确认
    assert "tok1" in engine._pending_force_exit
    engine._data = FakeDataClient({ADDR_A: []}, fail_for={ADDR_A})  # 次轮建仓者拉取失败
    engine.reconcile_once()
    assert force_exit_orders(ledger) == []
    assert ledger.positions()[0].size > 0
    assert "tok1" not in engine._pending_force_exit  # 无法确认，移出待确认集合


def test_builder_dust_residue_counts_as_flat():
    """目标常留渣：建仓者镜像剩 ≤ 1 份仍视为已清仓。"""
    engine, ledger, _, _ = make_rig(data=FakeDataClient({ADDR_A: [make_position(size=0.5)]}))
    seed_buy(engine)
    engine.reconcile_once()
    engine.reconcile_once()
    assert len(force_exit_orders(ledger)) == 1
    assert ledger.positions() == []


def test_resolved_market_skipped_quietly():
    """已结算市场交给结算/赎回路径，强制离场安静跳过（不下 SELL 单）。"""
    engine, ledger, _, clob = make_rig(
        data=FakeDataClient({ADDR_A: []}), auto_settle_resolved=False
    )
    seed_buy(engine)
    engine.reconcile_once()  # 登记
    # 市场结算：force_exit 应识别 resolved 并跳过（auto_settle 关着，避免结算路径先清仓）
    clob.market = MarketInfo(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=False, closed=True, winner_token_ids=("tok1",),
    )
    engine.reconcile_once()
    assert force_exit_orders(ledger) == []
    assert ledger.positions()[0].size > 0  # 未被强平
