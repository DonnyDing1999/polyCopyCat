import time

import pytest

from polycopycat.engine.config import TargetConfig
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.signals import OrderIntent, Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


@pytest.fixture
def ledger():
    ledger = Ledger(":memory:")
    yield ledger
    ledger.close()


def make_signal(tx="0x1", price=0.5, size=100.0):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash=tx,
    )
    return Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())


def intent(side="BUY", token="tok1", cond="0xcond", limit=0.52, size=100.0):
    return OrderIntent(
        token_id=token, condition_id=cond, side=side, limit_price=limit,
        size=size, ref_price=0.5, neg_risk=False, title="T", outcome="Yes",
    )


def test_signal_dedupe(ledger):
    sid, fresh = ledger.record_signal(make_signal("0x1"))
    assert fresh and sid is not None
    sid2, fresh2 = ledger.record_signal(make_signal("0x1"))
    assert not fresh2 and sid2 == sid
    _, fresh3 = ledger.record_signal(make_signal("0x2"))
    assert fresh3
    ledger.update_signal(sid, "executed", "ok")
    assert ledger.signal_counts() == {"executed": 1, "received": 1}


def test_buy_updates_avg_cost(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.5)
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.6)
    positions = ledger.positions()
    assert len(positions) == 1
    assert positions[0].size == 200
    assert abs(positions[0].avg_cost - 0.55) < 1e-9
    assert abs(ledger.market_cost("0xcond") - 110) < 1e-9
    assert abs(ledger.total_cost() - 110) < 1e-9


def test_sell_realizes_pnl(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.5)
    realized = ledger.record_order(sid, intent(side="SELL", size=40), mode="paper",
                                   status="filled", filled_size=40, avg_price=0.7)
    assert abs(realized - 40 * 0.2) < 1e-9
    position = ledger.positions()[0]
    assert position.size == 60
    assert abs(position.realized_pnl - 8.0) < 1e-9
    assert abs(ledger.realized_pnl_total() - 8.0) < 1e-9
    assert abs(ledger.realized_pnl_since(time.time() - 60) - 8.0) < 1e-9
    assert ledger.realized_pnl_since(time.time() + 60) == 0


def test_oversell_clamped(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=50), mode="paper", status="filled",
                        filled_size=50, avg_price=0.5)
    realized = ledger.record_order(sid, intent(side="SELL", size=500), mode="paper",
                                   status="filled", filled_size=500, avg_price=0.6)
    assert abs(realized - 50 * 0.1) < 1e-9  # 只按实际持仓 50 份结算
    assert ledger.position_size("tok1") == 0
    assert ledger.positions() == []  # size=0 不再显示


def test_sync_positions_replaces_table(ledger):
    from polycopycat.models import Position

    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="paper", status="filled",
                        filled_size=100, avg_price=0.5)
    snapshot = [
        Position(proxy_wallet=ADDR, asset="tokA", condition_id="0xc9", size=42,
                 avg_price=0.33, realized_pnl=2.5, title="A", outcome="Yes"),
        Position(proxy_wallet=ADDR, asset="tokB", condition_id="0xc9", size=0,
                 avg_price=0.5, title="B", outcome="No"),  # size 0 不落表
    ]
    ledger.sync_positions(snapshot)
    positions = ledger.positions()
    assert [p.token_id for p in positions] == ["tokA"]
    assert positions[0].size == 42 and abs(positions[0].avg_cost - 0.33) < 1e-9
    assert ledger.position_size("tok1") == 0  # 旧持仓被覆盖清掉


def test_live_submitted_does_not_touch_positions(ledger):
    sid, _ = ledger.record_signal(make_signal("0x1"))
    ledger.record_order(sid, intent(size=100), mode="live", status="submitted",
                        filled_size=0, avg_price=0, apply_fill=False)
    assert ledger.positions() == []
    assert len(ledger.recent_orders()) == 1


# ---- 按目标归因（report --by-target 的数据层）----

def _signal(ledger, tx, wallet, side="BUY", token="tok1", cond="0xcond",
            size=100.0, price=0.5, status="executed"):
    trade = Trade(
        proxy_wallet=wallet, side=side, asset=token, condition_id=cond,
        size=size, price=price, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash=tx,
    )
    sig = Signal(trade=trade, target=TargetConfig(address=wallet), received_at=time.time())
    sid, _ = ledger.record_signal(sig)
    ledger.update_signal(sid, status)
    return sid


def test_report_by_target_attributes_pnl_and_counts(ledger):
    a = "0x" + "a" * 40
    b = "0x" + "b" * 40
    # A：买入建仓 + 卖出平仓（实现 +盈亏），外加一条被过滤的信号
    sid_a_buy = _signal(ledger, "0xa1", a, side="BUY", size=100, price=0.50)
    ledger.record_order(sid_a_buy, intent(side="BUY", size=100, limit=0.50),
                        mode="paper", status="filled", filled_size=100, avg_price=0.50)
    sid_a_sell = _signal(ledger, "0xa2", a, side="SELL", size=100, price=0.60)
    ledger.record_order(sid_a_sell, intent(side="SELL", size=100, limit=0.60),
                        mode="paper", status="filled", filled_size=100, avg_price=0.60)
    _signal(ledger, "0xa3", a, size=5, price=0.5, status="filtered")
    # B：只买入未平仓，另有一条 netted
    sid_b_buy = _signal(ledger, "0xb1", b, side="BUY", size=80, price=0.40)
    ledger.record_order(sid_b_buy, intent(side="BUY", token="tok2", size=80, limit=0.40),
                        mode="paper", status="filled", filled_size=80, avg_price=0.40)
    _signal(ledger, "0xb2", b, side="BUY", status="netted")

    reports, settle_pnl, settle_n = ledger.report_by_target()
    assert settle_pnl == 0 and settle_n == 0
    by = {r.target: r for r in reports}
    assert reports[0].target == a  # A 有 +10 盈亏，排在前

    ra = by[a]
    assert abs(ra.realized_pnl - 10.0) < 1e-9      # 100 × (0.60 - 0.50)
    assert abs(ra.bought_notional - 50.0) < 1e-9   # 100 × 0.50（只算买入腿）
    assert ra.executed == 2 and ra.filtered == 1
    assert ra.total_signals == 3
    assert abs(ra.followable_ratio - 2 / 3) < 1e-9

    rb = by[b]
    assert rb.realized_pnl == 0.0
    assert abs(rb.bought_notional - 32.0) < 1e-9   # 80 × 0.40
    assert rb.executed == 1 and rb.netted == 1


def test_report_by_target_buckets_settlement_separately(ledger):
    a = "0x" + "a" * 40
    sid = _signal(ledger, "0xa1", a, side="BUY", size=100, price=0.50)
    ledger.record_order(sid, intent(side="BUY", size=100, limit=0.50),
                        mode="paper", status="filled", filled_size=100, avg_price=0.50)
    # 市场结算赢：settle_position 写 signal_id=0 的 REDEEM 订单
    realized = ledger.settle_position("tok1", 1.0, mode="paper")
    assert abs(realized - 50.0) < 1e-9  # 100 × (1.0 - 0.50)

    reports, settle_pnl, settle_n = ledger.report_by_target()
    assert settle_n == 1
    assert abs(settle_pnl - 50.0) < 1e-9
    # 结算盈亏不算进目标 A 的可归因盈亏（signal_id=0 不 join）
    assert reports[0].target == a and reports[0].realized_pnl == 0.0
    # 但总账仍然对得上：可归因 + 未归属 = 全部
    assert abs(ledger.realized_pnl_total() - 50.0) < 1e-9


def test_report_by_target_empty(ledger):
    reports, settle_pnl, settle_n = ledger.report_by_target()
    assert reports == [] and settle_pnl == 0 and settle_n == 0


# ---- 执行质量（report 的「执行质量」小节数据层）----

def _aged_signal(ledger, tx, age_s, price=0.5, size=100.0):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=size, price=price, timestamp=int(time.time() - age_s), title="T",
        outcome="Yes", transaction_hash=tx,
    )
    sid, _ = ledger.record_signal(
        Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())
    )
    return sid


def test_execution_quality_empty(ledger):
    q = ledger.execution_quality()
    assert q.n_fills == 0 and q.slippage_cost == 0.0


def test_execution_quality_metrics(ledger):
    # 延迟 ~10s、滑点 +0.01、全额成交
    sid1 = _aged_signal(ledger, "0xq1", age_s=10)
    ledger.record_order(sid1, intent(size=100, limit=0.52), mode="paper", status="filled",
                        filled_size=100, avg_price=0.51, slippage=0.01)
    # 延迟 ~30s、滑点 +0.02、部分成交、带重试标注
    sid2 = _aged_signal(ledger, "0xq2", age_s=30)
    ledger.record_order(sid2, intent(size=50, limit=0.52), mode="paper", status="partial",
                        filled_size=20, avg_price=0.52, slippage=0.02,
                        detail="首次限价内无对手盘，重试 1 次后成交")
    q = ledger.execution_quality()
    assert q.n_fills == 2
    assert q.full_fills == 1
    assert q.retried_fills == 1
    assert 9 <= q.avg_delay_s <= 22          # (10+30)/2 ≈ 20，容忍执行耗时
    assert q.max_delay_s >= 29
    assert abs(q.avg_price_gap - 0.015) < 1e-9
    assert abs(q.slippage_cost - (0.01 * 100 + 0.02 * 20)) < 1e-9  # $1.40


def test_execution_quality_excludes_redeem_and_unfilled(ledger):
    sid = _aged_signal(ledger, "0xq1", age_s=5)
    ledger.record_order(sid, intent(size=100, limit=0.52), mode="paper", status="filled",
                        filled_size=100, avg_price=0.51, slippage=0.01)
    # 未成交订单（rejected）不计入
    sid2 = _aged_signal(ledger, "0xq2", age_s=5)
    ledger.record_order(sid2, intent(size=50, limit=0.52), mode="paper", status="rejected",
                        filled_size=0, avg_price=0)
    # 市场结算 REDEEM（signal_id=0）不计入
    ledger.settle_position("tok1", 1.0, mode="paper")
    q = ledger.execution_quality()
    assert q.n_fills == 1


# ---- 信号通道 / 过滤原因 / 事件档案 ----

def test_signal_source_persisted_and_counted(ledger):
    import dataclasses
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xcond",
        size=100, price=0.5, timestamp=int(time.time()), title="T", outcome="Yes",
        transaction_hash="0xsrc", source="stream",
    )
    sid, _ = ledger.record_signal(
        Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())
    )
    trade2 = dataclasses.replace(trade, transaction_hash="0xsrc2", source="poll")
    ledger.record_signal(
        Signal(trade=trade2, target=TargetConfig(address=ADDR), received_at=time.time())
    )
    assert ledger.signal_source_counts() == {"stream": 1, "poll": 1}
    # 成交经由通道拆分
    ledger.record_order(sid, intent(size=100, limit=0.52), mode="paper", status="filled",
                        filled_size=100, avg_price=0.51, slippage=0.01)
    q = ledger.execution_quality()
    assert len(q.channels) == 1
    assert q.channels[0].source == "stream" and q.channels[0].n_fills == 1


def test_filter_reason_stats_normalizes_numbers(ledger):
    for i, detail in enumerate([
        "信号已过期 146s（阈值 30s）",
        "信号已过期 337s（阈值 30s）",
        "目标成交金额 $5.00 低于阈值 $30.00",
    ]):
        sid = _aged_signal(ledger, f"0xf{i}", age_s=5)
        ledger.update_signal(sid, "filtered", detail)
    stats = ledger.filter_reason_stats()
    assert stats[0] == ("filtered", "信号已过期 ~s（阈值 ~s）", 2)
    assert stats[1][2] == 1


def test_events_recorded_and_summarized(ledger):
    a = "0x" + "a" * 40
    ledger.record_event("health_pause", a, "窗口净亏损 $-100")
    ledger.record_event("health_resume", a, "恢复合格")
    ledger.record_event("health_pause", a, "又亏了")
    ledger.record_event("recruit", "0x" + "b" * 40, "分88")
    summary = ledger.target_event_summary()
    assert summary[a]["pauses"] == 2
    assert summary[a]["last_kind"] == "health_pause"
    assert summary[a]["last_detail"] == "又亏了"
    assert summary["0x" + "b" * 40]["recruits"] == 1


def test_migration_adds_source_column(tmp_path):
    import sqlite3 as _sq
    db = tmp_path / "old.sqlite3"
    conn = _sq.connect(db)
    # 造一个没有 source 列、没有 events 表的老账本
    conn.executescript("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_key TEXT UNIQUE NOT NULL,
            created_ts REAL NOT NULL, trade_ts INTEGER NOT NULL, target TEXT NOT NULL,
            condition_id TEXT NOT NULL, token_id TEXT NOT NULL, title TEXT, outcome TEXT,
            side TEXT NOT NULL, ref_price REAL NOT NULL, ref_size REAL NOT NULL,
            ref_notional REAL NOT NULL, status TEXT NOT NULL, detail TEXT DEFAULT '');
        INSERT INTO signals (trade_key, created_ts, trade_ts, target, condition_id,
            token_id, side, ref_price, ref_size, ref_notional, status)
        VALUES ('k', 1, 1, '0xt', '0xc', 'tok', 'BUY', 0.5, 10, 5, 'executed');
    """)
    conn.commit(); conn.close()
    migrated = Ledger(db)
    try:
        assert migrated.signal_source_counts() == {"未知": 1}  # 老数据 source 为空
        migrated.record_event("recruit", "0xt", "ok")          # events 表已建
        assert migrated.target_event_summary()["0xt"]["recruits"] == 1
    finally:
        migrated.close()
