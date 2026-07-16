"""T2 升级：按市场期限的动态时效闸 + 限价内无对手盘重试。"""

import time

import pytest

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import ConfigError, EngineConfig, FilterConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.engine.signals import Signal, SignalFilter
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


def market(end_in_s=None, **kw):
    return MarketInfo(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=True, closed=False,
        end_ts=(time.time() + end_in_s) if end_in_s else 0.0, **kw,
    )


# ---- 时效上限选择逻辑 ----

def test_age_limit_by_horizon():
    flt = SignalFilter(FilterConfig(max_signal_age_s=30, long_horizon_age_s=120,
                                    long_horizon_days=7))
    assert flt.age_limit_for(market(end_in_s=30 * 86400)) == 120  # 30 天后结束：长线
    assert flt.age_limit_for(market(end_in_s=3600)) == 30         # 1 小时后结束：短线
    assert flt.age_limit_for(market(end_in_s=None)) == 30         # 没给结束时间：保守
    assert flt.max_age_ceiling == 120


def test_age_limit_disabled_when_null():
    flt = SignalFilter(FilterConfig(max_signal_age_s=30, long_horizon_age_s=None))
    assert flt.age_limit_for(market(end_in_s=30 * 86400)) == 30
    assert flt.max_age_ceiling == 30


def test_config_rejects_tightening():
    with pytest.raises(ConfigError):
        FilterConfig(max_signal_age_s=30, long_horizon_age_s=10)


def test_ceiling_lets_semifresh_pass_precheck():
    flt = SignalFilter(FilterConfig(max_signal_age_s=30, long_horizon_age_s=120))
    trade = Trade(proxy_wallet=ADDR, side="BUY", asset="t", condition_id="0xc",
                  size=100, price=0.5, timestamp=int(time.time() - 100),
                  title="T", outcome="Yes", transaction_hash="0x1")
    from polycopycat.engine.config import TargetConfig
    ok, _ = flt.check(Signal(trade=trade, target=TargetConfig(address=ADDR),
                             received_at=time.time()))
    assert ok  # 100s < 120s 放宽上限，粗筛放行（精确判在拿到市场后）


def test_market_info_parses_end_date_iso():
    m = MarketInfo.from_api({"condition_id": "0xc", "end_date_iso": "2030-01-01T00:00:00Z"})
    assert m.end_ts > time.time()
    assert MarketInfo.from_api({"condition_id": "0xc"}).end_ts == 0.0
    assert MarketInfo.from_api({"condition_id": "0xc", "end_date_iso": "垃圾"}).end_ts == 0.0


# ---- 引擎集成 ----

class FakeClob:
    def __init__(self, mkt=None, books=None):
        self.market = mkt or market(end_in_s=30 * 86400)
        self.books = books  # None = 恒定深书；list = 按调用顺序弹出
        self.book_calls = 0

    def get_market(self, condition_id, *, fresh=False):
        return self.market

    def get_book(self, token_id):
        self.book_calls += 1
        if self.books is None:
            return OrderBook(asks=(BookLevel(0.51, 1000),), bids=(BookLevel(0.49, 1000),))
        return self.books.pop(0)


class ListNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def make_engine(clob, **overrides):
    raw = {
        "targets": [{"address": ADDR, "ratio": 0.5}],
        "sizing": {"ratio": 0.5, "max_per_trade_usdc": 500},
        "filters": {"min_target_notional_usdc": 20, "max_signal_age_s": 30,
                    "long_horizon_age_s": 120, "long_horizon_days": 7},
        "risk": {"kill_switch_file": "", "max_market_exposure_usdc": 10000,
                 "max_total_exposure_usdc": 10000, "daily_max_loss_usdc": 10000},
        "aggregate": {"window_s": 0},
        "execution": {"slippage_cap": 0.02, "retry_no_fill_s": None},
    }
    raw.update(overrides)
    config = EngineConfig.from_dict(raw)
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier)
    return engine, ledger, notifier


def buy(tx, age_s, size=100.0):
    return Trade(proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
                 size=size, price=0.50, timestamp=int(time.time() - age_s),
                 title="Will X happen by 2027?", outcome="Yes", transaction_hash=tx)


def submit(engine, trade):
    engine._process(Signal(trade=trade, target=engine._targets[ADDR],
                           received_at=time.time()))


def test_long_horizon_market_follows_60s_old_signal():
    engine, ledger, _ = make_engine(FakeClob(market(end_in_s=30 * 86400)))
    submit(engine, buy("0x1", age_s=60))
    assert ledger.signal_counts() == {"executed": 1}


def test_short_horizon_market_filters_same_signal():
    engine, ledger, _ = make_engine(FakeClob(market(end_in_s=3600)))
    submit(engine, buy("0x1", age_s=60))
    counts = ledger.signal_counts()
    assert counts == {"filtered": 1}
    row = ledger._conn.execute("SELECT detail FROM signals").fetchone()
    assert "该市场时效上限 30s" in row["detail"]


def test_group_drops_stale_members_and_follows_fresh():
    engine, ledger, _ = make_engine(FakeClob(market(end_in_s=3600)))
    # 短线市场（上限 30s）：10s 的新鲜、60s 的超龄——批内一起到
    engine._process_batch([
        Signal(trade=buy("0xa", age_s=10, size=100), target=engine._targets[ADDR],
               received_at=time.time()),
        Signal(trade=buy("0xb", age_s=60, size=200), target=engine._targets[ADDR],
               received_at=time.time()),
    ])
    counts = ledger.signal_counts()
    assert counts == {"executed": 1, "filtered": 1}
    order = ledger.recent_orders()[0]
    # 只按新鲜那笔定量：$50×0.5=$25 @0.52 → 48.07 份（若把 200 也并进来会是 3 倍）
    assert order["req_size"] == pytest.approx(48.07, abs=0.02)


# ---- no_fill 重试 ----

EMPTY = OrderBook(asks=(), bids=())
LIQUID = OrderBook(asks=(BookLevel(0.51, 1000),), bids=(BookLevel(0.49, 1000),))


def test_retry_fills_when_book_comes_back():
    clob = FakeClob(books=[EMPTY, LIQUID])
    engine, ledger, _ = make_engine(
        clob, execution={"slippage_cap": 0.02, "retry_no_fill_s": 0.01}
    )
    submit(engine, buy("0x1", age_s=5))
    assert ledger.signal_counts() == {"executed": 1}
    order = ledger.recent_orders()[0]
    assert order["status"] == "filled" and "重试" in order["detail"]
    assert clob.book_calls == 2


def test_retry_disabled_records_no_fill_once():
    clob = FakeClob(books=[EMPTY, LIQUID])
    engine, ledger, _ = make_engine(clob)  # retry_no_fill_s=None
    submit(engine, buy("0x1", age_s=5))
    assert ledger.signal_counts() == {"no_fill": 1}
    assert clob.book_calls == 1  # 没有第二次


def test_retry_still_dry_appends_note():
    clob = FakeClob(books=[EMPTY, EMPTY])
    engine, ledger, _ = make_engine(
        clob, execution={"slippage_cap": 0.02, "retry_no_fill_s": 0.01}
    )
    submit(engine, buy("0x1", age_s=5))
    assert ledger.signal_counts() == {"no_fill": 1}
    order = ledger.recent_orders()[0]
    assert "已重试 1 次" in order["detail"]


def test_retry_cap_validated():
    with pytest.raises(ConfigError):
        EngineConfig.from_dict({
            "targets": [{"address": ADDR}],
            "execution": {"retry_no_fill_s": 60},
        })
