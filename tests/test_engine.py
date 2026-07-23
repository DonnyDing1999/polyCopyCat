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
        # 无对手盘重试会 sleep，逐笔语义测试里关闭；重试见 test_dynamic_age_retry.py
        "execution": {"slippage_cap": 0.02, "retry_no_fill_s": None},
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


def _sig_with_title(title):
    from polycopycat.engine.config import TargetConfig
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=100, price=0.5, timestamp=int(time.time()), title=title, outcome="Up",
        transaction_hash="0xt",
    )
    return Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())


def test_skip_title_patterns_filters_short_term_markets():
    from polycopycat.engine.config import FilterConfig
    from polycopycat.engine.signals import SignalFilter
    flt = SignalFilter(FilterConfig(skip_title_patterns=["up or down", "opens up"]))
    ok, reason = flt.check(_sig_with_title("S&P 500 (SPX) Opens Up or Down on July 16?"))
    assert not ok and "短期盘" in reason
    # 大小写不敏感
    ok2, _ = flt.check(_sig_with_title("SPY (SPY) UP OR DOWN on July 16?"))
    assert not ok2
    # 不命中的正常市场照常放行
    ok3, _ = flt.check(_sig_with_title("Will Spain win the 2026 FIFA World Cup?"))
    assert ok3


def test_skip_title_patterns_empty_by_default():
    from polycopycat.engine.config import FilterConfig
    from polycopycat.engine.signals import SignalFilter
    flt = SignalFilter(FilterConfig())  # 默认空 → 不因标题过滤
    ok, _ = flt.check(_sig_with_title("Bitcoin Up or Down - 5m"))
    assert ok


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


# ---- 特性1：BUY 期限闸（filters.min_days_to_resolution）----

def _market_with_end(days_from_now):
    """距结束 days_from_now 天的市场；days_from_now=None → end_ts=0（元数据缺失）。"""
    end_ts = 0.0 if days_from_now is None else time.time() + days_from_now * 86400
    return MarketInfo(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=True, closed=False, end_ts=end_ts,
    )


def test_buy_blocked_when_resolution_too_close(rig):
    engine, ledger, notifier, clob = rig
    engine.config.filters.min_days_to_resolution = 3.0
    clob.market = _market_with_end(1.0)  # 距结算 1 天 < 下限 3 天
    submit_sync(engine, make_trade(tx="0xh1"))
    assert ledger.signal_counts() == {"filtered": 1}
    assert ledger.positions() == [] and notifier.messages == []
    detail = ledger._conn.execute("SELECT detail FROM signals LIMIT 1").fetchone()["detail"]
    assert "距结算" in detail and "短周期" in detail


def test_buy_allowed_when_resolution_far_enough(rig):
    engine, ledger, _, clob = rig
    engine.config.filters.min_days_to_resolution = 3.0
    clob.market = _market_with_end(10.0)  # 距结算 10 天 ≥ 下限
    submit_sync(engine, make_trade(tx="0xh2"))
    assert ledger.signal_counts() == {"executed": 1}


def test_buy_blocked_when_end_ts_missing(rig):
    engine, ledger, _, clob = rig
    engine.config.filters.min_days_to_resolution = 3.0
    clob.market = _market_with_end(None)  # end_ts=0 → 保守不开新仓
    submit_sync(engine, make_trade(tx="0xh3"))
    assert ledger.signal_counts() == {"filtered": 1}
    detail = ledger._conn.execute("SELECT detail FROM signals LIMIT 1").fetchone()["detail"]
    assert "缺少结束时间元数据" in detail


def test_horizon_gate_off_by_default(rig):
    engine, ledger, _, clob = rig
    assert engine.config.filters.min_days_to_resolution is None  # 默认关闭
    clob.market = _market_with_end(0.1)  # 距结算约 2.4 小时，但闸关着
    submit_sync(engine, make_trade(tx="0xh4"))
    assert ledger.signal_counts() == {"executed": 1}


def test_sell_not_blocked_by_horizon_gate(rig):
    engine, ledger, notifier, clob = rig
    engine.config.filters.min_days_to_resolution = 3.0
    clob.market = _market_with_end(10.0)  # 长线市场先建仓（BUY 过闸）
    seed_buy(engine, ledger)
    engine._mirror.replace(ADDR, {"tok1": 1000})
    clob.market = _market_with_end(0.5)  # 翻成短周期：BUY 会被拦，SELL 不该受影响
    submit_sync(engine, make_trade(tx="0xh5", side="SELL", size=500))
    assert ledger.positions()[0].size < 48.07  # 已跟随减仓
    assert any("跟随卖出" in m for m in notifier.messages)


# ---- 特性2：持仓时 SELL 绕过时效闸与标题过滤 ----

def _make_signal(*, side="SELL", title="Will X happen?", age_s=0.0):
    from polycopycat.engine.config import TargetConfig
    trade = Trade(
        proxy_wallet=ADDR, side=side, asset="tok1", condition_id="0xcond",
        size=100, price=0.5, timestamp=int(time.time() - age_s),
        title=title, outcome="Yes", transaction_hash="0xt",
    )
    return Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())


def test_filter_holding_sell_bypasses_age_and_title():
    from polycopycat.engine.config import FilterConfig
    from polycopycat.engine.signals import SignalFilter
    flt = SignalFilter(FilterConfig(
        max_signal_age_s=30, long_horizon_age_s=None, skip_title_patterns=["up or down"]
    ))
    stale = _make_signal(side="SELL", age_s=3600)
    assert not flt.check(stale, holding=False)[0]   # 未持仓：超龄被拦
    assert flt.check(stale, holding=True)[0]         # 持仓：放行
    titled = _make_signal(side="SELL", title="SPX Up or Down?")
    assert not flt.check(titled, holding=False)[0]   # 未持仓：命中标题被拦
    assert flt.check(titled, holding=True)[0]         # 持仓：放行


def test_filter_holding_does_not_bypass_buy():
    from polycopycat.engine.config import FilterConfig
    from polycopycat.engine.signals import SignalFilter
    flt = SignalFilter(FilterConfig(
        max_signal_age_s=30, long_horizon_age_s=None, skip_title_patterns=["up or down"]
    ))
    # holding 只放行 SELL；BUY 仍照常过时效闸与标题过滤
    assert not flt.check(_make_signal(side="BUY", age_s=3600), holding=True)[0]
    assert not flt.check(_make_signal(side="BUY", title="SPX Up or Down?"), holding=True)[0]


def test_holding_sell_bypasses_age_gate_end_to_end(rig):
    engine, ledger, notifier, _ = rig
    seed_buy(engine, ledger)  # 自己持有 tok1
    engine._mirror.replace(ADDR, {"tok1": 1000})
    # 目标卖出信号超龄 1 小时：未持仓会被时效闸拦，持仓则放行
    submit_sync(engine, make_trade(tx="0xb1", side="SELL", size=500, ts=time.time() - 3600))
    assert ledger.positions()[0].size < 48.07
    assert any("跟随卖出" in m for m in notifier.messages)


def test_holding_sell_bypasses_title_filter_end_to_end():
    config = make_config(filters={
        "min_target_notional_usdc": 20, "max_signal_age_s": 60,
        "skip_title_patterns": ["up or down"],
    })
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier)
    submit_sync(engine, make_trade(tx="0xseed"))  # 建仓（标题不命中过滤规则）
    assert ledger.positions()[0].size == 48.07
    engine._mirror.replace(ADDR, {"tok1": 1000})
    sell = make_trade(tx="0xb2", side="SELL", size=500)
    object.__setattr__(sell, "title", "SPX Up or Down today?")  # 命中过滤规则
    submit_sync(engine, sell)
    assert ledger.positions()[0].size < 48.07  # 持仓 → 绕过标题过滤，照常跟卖
    ledger.close()
