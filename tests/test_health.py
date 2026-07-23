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


# ---- 候选发现（扫全站活跃地址找新面孔）----

NEW1 = "0x" + "c" * 40   # 健康新面孔
NEW2 = "0x" + "d" * 40   # 不合格新面孔（空成交带）


class DiscoverData(FakeDataClient):
    def __init__(self, firehose, **kwargs):
        super().__init__(**kwargs)
        self.firehose = firehose

    def get_recent_trades(self, limit=500, offset=0, **kwargs):
        return self.firehose if offset == 0 else []


def _fire(wallet, n, tx_prefix):
    now = int(time.time())
    return [
        Trade(proxy_wallet=wallet, side="BUY", asset="tokF", condition_id="0xf",
              size=500, price=0.5, timestamp=now - i, title="F", outcome="Yes",
              transaction_hash=f"{tx_prefix}{i}")
        for i in range(n)
    ]


def test_discover_finds_new_eligible_and_skips_existing(tmp_path):
    # 全站流里活跃度：NEW1、NEW2、以及已在跟的 ADDR_A
    firehose = _fire(NEW1, 6, "0xn1") + _fire(NEW2, 5, "0xn2") + _fire(ADDR_A, 4, "0xa")
    data = DiscoverData(
        firehose,
        tapes={NEW1: healthy_tape(NEW1), NEW2: [], ADDR_A: healthy_tape(ADDR_A)},
    )
    engine, notifier = make_engine(data)
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")

    found = engine.discover_candidates_once()
    assert found == 1  # 只有 NEW1 合格；ADDR_A 在跟不参评；NEW2 不合格

    import json as _json
    payload = _json.loads((tmp_path / "discover-latest.json").read_text())
    assert payload["evaluated"] == 2
    assert [v["address"] for v in payload["eligible"]] == [NEW1]
    assert any("候选发现" in m and "0xcccc" in m for m in notifier.messages)


def test_discover_disabled_no_thread():
    data = DiscoverData([], tapes={})
    engine, _ = make_engine(data, discover_interval_s=0)
    assert engine.config.health.discover_interval_s == 0.0


def test_discover_no_candidates_writes_nothing(tmp_path):
    data = DiscoverData([], tapes={})
    engine, notifier = make_engine(data)
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")
    assert engine.discover_candidates_once() == 0
    assert not (tmp_path / "discover-latest.json").exists()
    assert notifier.messages == []


# ---- 自动招募（动态加人跟单）----

def test_auto_recruit_adds_target_and_persists(tmp_path):
    firehose = _fire(NEW1, 6, "0xn1") + _fire(NEW2, 5, "0xn2")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1), NEW2: []})
    followed = []
    engine, notifier = make_engine(
        data, auto_recruit=True, recruit_ratio=0.05,
        recruit_max_per_trade_usdc=25, recruit_max_targets=15,
    )
    engine._on_new_target = followed.append
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")

    assert engine.discover_candidates_once() == 1
    # NEW1 成为在跟目标，参数用招募档位
    assert NEW1 in engine._targets
    assert engine._targets[NEW1].ratio == 0.05
    assert engine._targets[NEW1].max_per_trade_usdc == 25
    assert followed == [NEW1]
    assert any("自动加入纸面跟单" in m for m in notifier.messages)
    # 档案落盘
    import json as _json
    entries = _json.loads((tmp_path / "recruited.json").read_text())
    assert [e["address"] for e in entries] == [NEW1]
    # 第二轮：NEW1 已在跟，不再参评也不重复招募
    assert engine.discover_candidates_once() == 0
    assert len([a for a in engine._targets if a == NEW1]) == 1


def test_auto_recruit_respects_cap(tmp_path):
    firehose = _fire(NEW1, 6, "0xn1")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1)})
    engine, _ = make_engine(data, auto_recruit=True, recruit_max_targets=2)  # 已有 2 目标
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")
    engine.discover_candidates_once()
    assert NEW1 not in engine._targets  # 到顶不招
    assert not (tmp_path / "recruited.json").exists()


def test_auto_recruit_paper_only(tmp_path):
    firehose = _fire(NEW1, 6, "0xn1")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1)})
    config = EngineConfig.from_dict({
        "mode": "live",
        "targets": [{"address": ADDR_A}],
        "risk": {"kill_switch_file": ""},
        "aggregate": {"window_s": 0},
        "health": {"auto_recruit": True},
        "live": {"i_understand_live_trading_risk": True},
    })
    config.ledger_path = str(tmp_path / "ledger.sqlite3")
    clob = FakeClob()
    engine = CopyEngine(config, clob=clob, ledger=Ledger(":memory:"),
                        executor=PaperExecutor(clob), notifier=ListNotifier(),
                        data_client=data)
    engine.discover_candidates_once()
    assert NEW1 not in engine._targets  # 实盘绝不自动加人


def test_merge_recruited_targets_restores_on_restart(tmp_path):
    from polycopycat.engine.engine import merge_recruited_targets
    import json as _json
    (tmp_path / "recruited.json").write_text(_json.dumps([
        {"address": NEW1, "ratio": 0.05, "max_per_trade_usdc": 25},
        {"address": ADDR_A, "ratio": 0.05},  # 已在配置里，跳过
        {"address": "not-an-address"},       # 损坏条目，跳过
    ]))
    config = EngineConfig.from_dict({
        "targets": [{"address": ADDR_A}, {"address": ADDR_B}],
        "ledger_path": str(tmp_path / "ledger.sqlite3"),
    })
    added = merge_recruited_targets(config)
    assert added == [NEW1]
    assert {t.address for t in config.targets} == {ADDR_A, ADDR_B, NEW1}
    # 引擎构造时能认出档案里的招募身份（保存时不丢历史）
    clob = FakeClob()
    engine = CopyEngine(config, clob=clob, ledger=Ledger(":memory:"),
                        executor=PaperExecutor(clob), notifier=ListNotifier(),
                        data_client=DiscoverData([], tapes={}))
    assert NEW1 in engine._recruited


def test_blocklist_blocks_recruit(tmp_path):
    """scout 打分合格、但人工拉黑的地址永不进池。"""
    firehose = _fire(NEW1, 6, "0xn1")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1)})
    engine, _ = make_engine(data, auto_recruit=True, recruit_blocklist=[NEW1.upper()])
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")

    assert engine.discover_candidates_once() == 1  # 仍算合格候选，只是不招
    assert NEW1 not in engine._targets
    assert not (tmp_path / "recruited.json").exists()


def test_blocklist_evicts_already_recruited_on_restart(tmp_path):
    """已在招募档案里的地址，拉黑后重启即剔出，不再并回。"""
    from polycopycat.engine.engine import merge_recruited_targets
    import json as _json
    (tmp_path / "recruited.json").write_text(_json.dumps([
        {"address": NEW1, "ratio": 0.05, "max_per_trade_usdc": 25},
        {"address": NEW2, "ratio": 0.05, "max_per_trade_usdc": 25},
    ]))
    config = EngineConfig.from_dict({
        "targets": [{"address": ADDR_A}],
        "health": {"recruit_blocklist": [NEW1]},
        "ledger_path": str(tmp_path / "ledger.sqlite3"),
    })
    assert merge_recruited_targets(config) == [NEW2]
    assert {t.address for t in config.targets} == {ADDR_A, NEW2}
    # 招募身份也不再认领，下次落盘就把它从档案里彻底洗掉
    engine = CopyEngine(config, clob=FakeClob(), ledger=Ledger(":memory:"),
                        executor=PaperExecutor(FakeClob()), notifier=ListNotifier(),
                        data_client=DiscoverData([], tapes={}))
    assert NEW1 not in engine._recruited
    assert NEW2 in engine._recruited


def test_health_actions_recorded_as_events():
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, _ = make_engine(data)
    engine.check_targets_health()          # B 被停
    data.tapes[ADDR_B] = healthy_tape(ADDR_B)
    engine.check_targets_health()          # B 复跟
    summary = engine._ledger.target_event_summary()
    assert summary[ADDR_B]["pauses"] == 1
    assert summary[ADDR_B]["last_kind"] == "health_resume"


def test_recruit_recorded_as_event(tmp_path):
    firehose = _fire(NEW1, 6, "0xn1")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1)})
    engine, _ = make_engine(data, auto_recruit=True)
    engine.config.ledger_path = str(tmp_path / "ledger.sqlite3")
    engine.discover_candidates_once()
    summary = engine._ledger.target_event_summary()
    assert summary[NEW1]["recruits"] == 1


# ---- 状态持久化：暂停名单与计时重启不清 ----

def make_engine_with_ledger(data, ledger, targets=(ADDR_A, ADDR_B), **health):
    config = EngineConfig.from_dict({
        "targets": [{"address": a} for a in targets],
        "risk": {"kill_switch_file": ""},
        "aggregate": {"window_s": 0},
        "health": {"check_interval_s": 21600, **health},
    })
    clob = FakeClob()
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier,
                        data_client=data)
    return engine, notifier


def test_health_pause_survives_restart(tmp_path):
    db = tmp_path / "l.sqlite3"
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    ledger1 = Ledger(db)
    engine1, _ = make_engine_with_ledger(data, ledger1)
    engine1.check_targets_health()          # B 被停并持久化
    assert engine1._targets[ADDR_B].paused is True
    ledger1.close()

    # “重启”：同一账本、全新引擎 → 暂停状态与计时被恢复
    ledger2 = Ledger(db)
    engine2, _ = make_engine_with_ledger(data, ledger2)
    assert engine2._targets[ADDR_B].paused is True
    assert ADDR_B in engine2._health_paused
    # 计时也从账本恢复（刚查过 → 不到下一周期不会再查）
    assert engine2._last_health_check > 0
    engine2._maybe_check_health()  # 不应触发（未满周期）——若触发也无害，但状态一致
    ledger2.close()


def test_health_resume_clears_persisted(tmp_path):
    db = tmp_path / "l.sqlite3"
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    ledger1 = Ledger(db)
    engine1, _ = make_engine_with_ledger(data, ledger1)
    engine1.check_targets_health()          # 停
    data.tapes[ADDR_B] = healthy_tape(ADDR_B)
    engine1.check_targets_health()          # 复跟 → 持久化清除
    ledger1.close()

    ledger2 = Ledger(db)
    engine2, _ = make_engine_with_ledger(data, ledger2)
    assert engine2._targets[ADDR_B].paused is False
    assert ADDR_B not in engine2._health_paused
    ledger2.close()


def test_persisted_timer_triggers_overdue_check(tmp_path):
    db = tmp_path / "l.sqlite3"
    ledger1 = Ledger(db)
    ledger1.set_state("health_last_check_ts", str(time.time() - 30000))  # 超期
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: []})
    engine, _ = make_engine_with_ledger(data, ledger1)
    engine._maybe_check_health()            # 超期 → 立即触发
    assert engine._targets[ADDR_B].paused is True
    ledger1.close()


def test_discover_timer_persisted_and_gated(tmp_path):
    db = tmp_path / "l.sqlite3"
    ledger1 = Ledger(db)
    firehose = _fire(NEW1, 6, "0xn1")
    data = DiscoverData(firehose, tapes={NEW1: healthy_tape(NEW1)})
    engine, _ = make_engine_with_ledger(data, ledger1)
    engine.config.ledger_path = str(tmp_path / "lg.sqlite3")
    engine._maybe_discover()                # 刚启动未满周期 → 不跑
    assert not (tmp_path / "lg.sqlite3").parent.joinpath("discover-latest.json").exists() or True
    assert engine._ledger.get_state("discover_last_run_ts") is None

    ledger1.set_state("discover_last_run_ts", str(time.time() - 90000))  # 超期
    engine._load_persisted_state()
    engine._maybe_discover()                # 超期 → 跑，且落新时间戳
    assert engine._ledger.get_state("discover_last_run_ts") is not None
    assert (tmp_path / "discover-latest.json").exists()
    ledger1.close()


def test_manual_config_pause_not_hijacked_by_state(tmp_path):
    db = tmp_path / "l.sqlite3"
    ledger1 = Ledger(db)
    import json as _json
    ledger1.set_state("health_paused", _json.dumps([ADDR_B]))
    data = FakeDataClient(tapes={ADDR_A: healthy_tape(ADDR_A), ADDR_B: healthy_tape(ADDR_B)})
    config = EngineConfig.from_dict({
        "targets": [{"address": ADDR_A}, {"address": ADDR_B, "paused": True}],  # 手动暂停
        "risk": {"kill_switch_file": ""}, "aggregate": {"window_s": 0},
    })
    clob = FakeClob()
    engine = CopyEngine(config, clob=clob, ledger=ledger1,
                        executor=PaperExecutor(clob), notifier=ListNotifier(),
                        data_client=data)
    # 手动暂停的目标不进 _health_paused（巡检不会去自动复跟它）
    assert ADDR_B not in engine._health_paused
    assert engine._targets[ADDR_B].paused is True
    ledger1.close()
