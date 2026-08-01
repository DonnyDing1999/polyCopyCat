"""目标健康巡检：scout 排除规则复查在跟目标，自动暂停/复跟；零跟单率自动解聘。"""

import json
import time

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import EngineConfig, TargetConfig
from polycopycat.engine.engine import CopyEngine
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.engine.signals import PAUSED_SIGNAL_DETAIL, Signal
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


# ---- 招募/发现相关的公用 fake（票池 + 涓流发现的测试见 tests/test_discover.py）----

NEW1 = "0x" + "c" * 40   # 健康新面孔
NEW2 = "0x" + "d" * 40   # 不合格新面孔（空成交带）


class DiscoverData(FakeDataClient):
    def __init__(self, firehose, **kwargs):
        super().__init__(**kwargs)
        self.firehose = firehose

    def get_recent_trades(self, limit=500, offset=0, **kwargs):
        return self.firehose if offset == 0 else []


# ---- 自动招募档案的重启并回（merge_recruited_targets，与发现架构无关）----

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


# ---- 零跟单率自动解聘（只对自动招募的目标）----

RECRUIT = "0x" + "e" * 40


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def make_recruit_engine(tmp_path, *, hours_ago=48.0, recruited_at=..., **health):
    """一个在跟 ADDR_A（配置目标）+ RECRUIT（自动招募，档案已落盘）的引擎。

    recruited_at 默认按 hours_ago 小时前写入；显式传 None 模拟 0.33 之前缺该字段的老档案。
    """
    entry = {"address": RECRUIT, "ratio": 0.05, "max_per_trade_usdc": 25, "score": 88.0}
    if recruited_at is ...:
        entry["recruited_at"] = _iso(time.time() - hours_ago * 3600)
    elif recruited_at is not None:
        entry["recruited_at"] = recruited_at
    (tmp_path / "recruited.json").write_text(json.dumps([entry]), encoding="utf-8")
    data = FakeDataClient(
        tapes={a: healthy_tape(a) for a in (ADDR_A, RECRUIT)}
    )
    config = EngineConfig.from_dict({
        "targets": [{"address": ADDR_A}, {"address": RECRUIT}],
        "risk": {"kill_switch_file": ""},
        "aggregate": {"window_s": 0},
        "health": {"check_interval_s": 21600, **health},
        "ledger_path": str(tmp_path / "copycat.sqlite3"),
    })
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger, executor=PaperExecutor(clob),
                        notifier=notifier, data_client=data)
    assert RECRUIT in engine._recruited  # 招募身份被认出来了，否则后面测的都是空
    return engine, notifier, ledger


def seed_signals(ledger, target, n, *, status="filtered", detail="命中短期盘过滤规则「vs 」，不跟",
                 tag="a"):
    """给 target 落 n 条信号（默认：被标题过滤器拦下，即零跟单率的典型形态）。"""
    now = time.time()
    for i in range(n):
        trade = Trade(
            proxy_wallet=target, side="BUY", asset=f"tok{tag}{i}", condition_id="0xc",
            size=100, price=0.5, timestamp=int(now), title="T", outcome="Yes",
            transaction_hash=f"0x{target[-3:]}{tag}{i}",
        )
        sid, _ = ledger.record_signal(
            Signal(trade=trade, target=TargetConfig(address=target), received_at=now)
        )
        ledger.update_signal(sid, status, detail)


def test_dismiss_zero_follow_recruit_at_threshold(tmp_path):
    engine, notifier, led = make_recruit_engine(tmp_path)
    seed_signals(led, RECRUIT, 49)
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets      # 49 < 50：还差一条，不解聘

    seed_signals(led, RECRUIT, 1, tag="b")
    engine._dismiss_idle_recruits()
    assert RECRUIT not in engine._targets  # 达阈值 → 解聘，名额释放
    assert RECRUIT not in engine._recruited
    assert json.loads((tmp_path / "recruited.json").read_text()) == []   # 档案重写
    assert led.target_event_summary()[RECRUIT]["last_kind"] == "recruit_dismiss"
    assert any("自动解聘" in m and "50 条" in m for m in notifier.messages)
    assert RECRUIT in json.loads(led.get_state("recruit_dismissed"))     # 进冷却名单


def test_dismiss_requires_min_hours(tmp_path):
    # 信号数早就够了，但招募才 12 小时：观察时长不够，不算给过机会
    engine, _, led = make_recruit_engine(tmp_path, hours_ago=12)
    seed_signals(led, RECRUIT, 200)
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets


def test_dismiss_excludes_paused_period_signals(tmp_path):
    # 暂停期被自动拦下的信号不算数——那是暂停造成的零执行，不是「品类跟不了」
    engine, _, led = make_recruit_engine(tmp_path)
    seed_signals(led, RECRUIT, 200, detail=PAUSED_SIGNAL_DETAIL)
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets

    seed_signals(led, RECRUIT, 50, tag="real")   # 补上 50 条真·有效信号
    engine._dismiss_idle_recruits()
    assert RECRUIT not in engine._targets


def test_dismiss_spares_target_with_any_execution(tmp_path):
    engine, _, led = make_recruit_engine(tmp_path)
    seed_signals(led, RECRUIT, 199)
    seed_signals(led, RECRUIT, 1, status="executed", detail="", tag="ok")
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets  # 跟成过一笔就不是「零跟单率」


def test_dismiss_never_touches_config_targets(tmp_path):
    # 配置目标是用户手写的：跟单率列已经摆在 report 里，该不该留由他自己判断
    engine, _, led = make_recruit_engine(tmp_path)
    seed_signals(led, ADDR_A, 200)
    engine._dismiss_idle_recruits()
    assert ADDR_A in engine._targets


def test_dismiss_disabled_by_non_positive_threshold(tmp_path):
    engine, _, led = make_recruit_engine(tmp_path, recruit_dismiss_min_signals=0)
    seed_signals(led, RECRUIT, 500)
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets


def test_dismiss_skips_legacy_entry_without_recruited_at(tmp_path):
    # 老档案没有 recruited_at：无从判断观察时长，保守不动
    engine, _, led = make_recruit_engine(tmp_path, recruited_at=None)
    seed_signals(led, RECRUIT, 500)
    engine._dismiss_idle_recruits()
    assert RECRUIT in engine._targets


def test_dismiss_clears_health_pause_state(tmp_path):
    engine, _, led = make_recruit_engine(tmp_path)
    engine._targets[RECRUIT].paused = True
    engine._health_paused.add(RECRUIT)
    engine._persist_health_paused()
    seed_signals(led, RECRUIT, 50)
    engine._dismiss_idle_recruits()
    assert RECRUIT not in engine._targets
    assert RECRUIT not in engine._health_paused
    assert json.loads(led.get_state("health_paused")) == []  # 持久化也一并清掉


def test_dismiss_runs_inside_health_check(tmp_path):
    # 解聘挂在巡检节拍上：巡检跑一次即生效，且不会顺带把配置目标停掉
    engine, _, led = make_recruit_engine(tmp_path)
    seed_signals(led, RECRUIT, 50)
    engine.check_targets_health()
    assert RECRUIT not in engine._targets
    assert engine._targets[ADDR_A].paused is False


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
