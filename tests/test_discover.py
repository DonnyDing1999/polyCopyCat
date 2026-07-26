"""票池 + 涓流发现：累积器、票池表读写、涓流评估优先级、排行榜降级、
可判性闸、冷启动填充、每小时摘要去重、discover-latest.json 滚动结构、全关。

全部离线：stream 直接喂 _extract_trades；引擎侧 DataApi/排行榜/CLOB 都是 fake，
排行榜默认被 autouse fixture 断开（不打真网络），要测降级的用例再自行 patch。
"""

import json
import time

import pytest

from polycopycat.engine.clob import BookLevel, MarketInfo, OrderBook
from polycopycat.engine.config import EngineConfig
from polycopycat.engine.engine import CopyEngine, _short
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.notify import Notifier
from polycopycat.models import Position, Trade
from polycopycat.scout.metrics import TraderStats, replay
from polycopycat.scout.score import ScoutConfig, Verdict, evaluate, evaluate_health
from polycopycat.stream import TradeStream

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
W3 = "0x" + "3" * 40
TRACKED = "0x" + "a" * 40
BLOCKED = "0x" + "b" * 40
GOOD = "0x" + "c" * 40
BAD = "0x" + "d" * 40


# ---------------------------------------------------------------------------
# 公用 fake / 造数据
# ---------------------------------------------------------------------------

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


class FakeData:
    """按地址返回预置成交带/持仓；firehose 供 candidates_from_recent_trades。"""

    def __init__(self, *, firehose=None, tapes=None, positions=None, errors=None):
        self.firehose = firehose or []
        self.tapes = {k.lower(): v for k, v in (tapes or {}).items()}
        self.positions = {k.lower(): v for k, v in (positions or {}).items()}
        self.errors = {k.lower() for k in (errors or [])}

    def get_recent_trades(self, *, limit=500, offset=0, taker_only=True):
        return self.firehose if offset == 0 else []

    def get_trades(self, user, *, limit=500, offset=0, **kwargs):
        from polycopycat.data_api import DataApiError
        if user.lower() in self.errors:
            raise DataApiError("boom")
        return self.tapes.get(user.lower(), [])[offset:offset + limit]

    def get_positions(self, user, **kwargs):
        return self.positions.get(user.lower(), [])


def _one(wallet, *, side="BUY", size=100.0, price=0.5, ts=None, asset="tokX", cond="0xc"):
    return Trade(
        proxy_wallet=wallet, side=side, asset=asset, condition_id=cond,
        size=size, price=price, timestamp=int(ts if ts is not None else time.time()),
        title="T", outcome="Yes", transaction_hash=f"0x{wallet[-3:]}{side}{size}{ts}",
    )


def _fire(wallet, n):
    """n 笔纯买入（撑起活跃度/成交额，供 firehose 与「纯买入」用例）。"""
    now = int(time.time())
    return [
        Trade(proxy_wallet=wallet, side="BUY", asset="tokF", condition_id="0xf",
              size=500, price=0.5, timestamp=now - i, title="F", outcome="Yes",
              transaction_hash=f"0x{wallet[-3:]}f{i}")
        for i in range(n)
    ]


def elig_tape(wallet, markets=12):
    """一条能过 scout 招聘全部规则的成交带（含配对卖出、近期活跃、非做市）。

    持仓 2h：需高于 min_median_holding_s（1h）的速刷/做市持仓时长闸，否则会被排除。
    """
    now = int(time.time())
    tape = []
    for i in range(markets):
        base = now - (markets - i) * 3600 - 7200
        tape.append(Trade(proxy_wallet=wallet, side="BUY", asset=f"tk{i}",
                          condition_id=f"0xc{i}", size=500, price=0.40, timestamp=base,
                          title="M", outcome="Yes", transaction_hash=f"0x{wallet[-3:]}b{i}"))
        tape.append(Trade(proxy_wallet=wallet, side="SELL", asset=f"tk{i}",
                          condition_id=f"0xc{i}", size=500, price=0.55, timestamp=base + 7200,
                          title="M", outcome="Yes", transaction_hash=f"0x{wallet[-3:]}s{i}"))
    return tape


def _verdict(addr, score):
    stats = TraderStats(
        address=addr, matched_sells=5, wins=4, realized_pnl=120.0,
        avg_trade_usdc=200.0, n_markets=5, last_ts=int(time.time()),
    )
    return Verdict(address=addr, eligible=True, score=score, stats=stats)


def _row(led, wallet):
    return led._conn.execute(
        "SELECT * FROM discover_universe WHERE wallet = ?", (wallet.lower(),)
    ).fetchone()


def make_engine(data, *, tmp_path, targets=(TRACKED,), mode="paper", **health):
    config = EngineConfig.from_dict({
        "mode": mode,
        "targets": [{"address": a} for a in targets],
        "risk": {"kill_switch_file": ""},
        "aggregate": {"window_s": 0},
        "health": {"discover_interval_s": 86400, **health},
    })
    config.ledger_path = str(tmp_path / "copycat.sqlite3")
    clob = FakeClob()
    ledger = Ledger(":memory:")
    notifier = ListNotifier()
    engine = CopyEngine(config, clob=clob, ledger=ledger,
                        executor=PaperExecutor(clob), notifier=notifier,
                        data_client=data)
    return engine, notifier, ledger


@pytest.fixture(autouse=True)
def _no_real_leaderboard(monkeypatch):
    """默认让排行榜源不可用（绝不打真网络）；测降级的用例自行再 patch 覆盖。"""
    from polycopycat.scout.runner import ScoutError

    def _boom(*args, **kwargs):
        raise ScoutError("leaderboard offline in tests")

    monkeypatch.setattr("polycopycat.scout.runner.candidates_from_leaderboard", _boom)


# ---------------------------------------------------------------------------
# 1) 累积器钩子：on_all_trade 在地址过滤之前触发；None 向后兼容
# ---------------------------------------------------------------------------

def _wrap(wallet, tx="0x1"):
    return json.dumps({
        "topic": "activity", "type": "trades",
        "payload": {"proxyWallet": wallet, "side": "BUY", "size": 1, "price": 0.5,
                    "timestamp": 100, "transactionHash": tx},
    })


def test_on_all_trade_fires_before_address_filter():
    seen = []
    hit = []
    s = TradeStream([W1], on_trade=hit.append, on_all_trade=seen.append)
    # 非监控地址的成交：on_trade 侧被过滤掉，但累积器 on_all_trade 照收
    trades = s._extract_trades(_wrap(W2))
    assert trades == []
    assert [t.proxy_wallet for t in seen] == [W2]
    # 监控地址的成交：两个回调都收到
    s._handle_message(None, _wrap(W1))
    assert [t.proxy_wallet for t in hit] == [W1]
    assert [t.proxy_wallet for t in seen] == [W2, W1]


def test_on_all_trade_none_is_backward_compatible():
    hit = []
    s = TradeStream([W1], on_trade=hit.append)  # 不传 on_all_trade
    assert s._extract_trades(_wrap(W2)) == []      # 非目标：不炸、不回调
    assert len(s._extract_trades(_wrap(W1))) == 1  # 目标：照常过滤出


# ---------------------------------------------------------------------------
# 2) 票池 upsert 累加 + 来源并列
# ---------------------------------------------------------------------------

def test_universe_upsert_accumulates_and_merges_source():
    led = Ledger(":memory:")
    led.discover_universe_upsert(
        [{"wallet": W1, "notional": 100, "trades": 2, "source": "stream"}], now=1000
    )
    led.discover_universe_upsert(
        [{"wallet": W1, "notional": 50, "trades": 1, "source": "leaderboard"}], now=2000
    )
    row = _row(led, W1)
    assert abs(row["notional"] - 150) < 1e-9 and row["trades"] == 3
    assert row["first_seen"] == 1000 and row["last_seen"] == 2000  # first_seen 不动、last_seen 刷新
    assert row["source"] == "stream+leaderboard"
    # 同源重复不叠字符串
    led.discover_universe_upsert([{"wallet": W1, "source": "stream"}], now=3000)
    assert _row(led, W1)["source"] == "stream+leaderboard"
    led.close()


# ---------------------------------------------------------------------------
# 3) 修剪：时间（>7 天）+ 容量（超上限删最老 last_seen）
# ---------------------------------------------------------------------------

def test_universe_prune_by_age_then_capacity():
    led = Ledger(":memory:")
    now = 1_000_000.0
    led.discover_universe_upsert([{"wallet": W1, "source": "s"}], now=now - 8 * 86400)  # 超龄
    led.discover_universe_upsert([{"wallet": W2, "source": "s"}], now=now - 1 * 86400)
    led.discover_universe_upsert([{"wallet": W3, "source": "s"}], now=now)
    removed = led.discover_universe_prune(max_age_s=7 * 86400, max_rows=100, now=now)
    assert removed == 1
    left = {r["wallet"] for r in led._conn.execute("SELECT wallet FROM discover_universe")}
    assert left == {W2, W3}
    # 容量修剪：上限 1 → 删掉 last_seen 最老的 W2，留最新的 W3
    led.discover_universe_prune(max_age_s=10 ** 9, max_rows=1, now=now)
    left = {r["wallet"] for r in led._conn.execute("SELECT wallet FROM discover_universe")}
    assert left == {W3}
    led.close()


# ---------------------------------------------------------------------------
# 4) 涓流评估优先级：未评估 > 冷却到期；冷却内跳过；universe_size / exclude
# ---------------------------------------------------------------------------

def test_due_never_evaluated_first_by_notional():
    led = Ledger(":memory:")
    now = 1_000_000.0
    led.discover_universe_upsert([
        {"wallet": W1, "notional": 1000, "source": "s"},
        {"wallet": W2, "notional": 10, "source": "s"},
        {"wallet": W3, "notional": 500, "source": "s"},
    ], now=now)
    led.discover_universe_record_eval(W1, score=80, eligible=True, now=now)  # 刚评估过
    due = led.discover_universe_due(
        limit=10, universe_size=100, reeval_after_s=3 * 86400, exclude=(), now=now
    )
    # 未评估的在前、按 notional 降序（W3>W2）；刚评估的 W1 在冷却内不出现
    assert due == [W3, W2]
    led.close()


def test_due_cooldown_gate_and_reeval_oldest_first():
    led = Ledger(":memory:")
    now = 1_000_000.0
    led.discover_universe_upsert([
        {"wallet": W1, "notional": 100, "source": "s"},
        {"wallet": W2, "notional": 200, "source": "s"},
    ], now=now)
    led.discover_universe_record_eval(W1, score=1, eligible=False, now=now - 4 * 86400)  # 4 天前
    led.discover_universe_record_eval(W2, score=1, eligible=False, now=now - 1 * 86400)  # 1 天前
    due = led.discover_universe_due(
        limit=10, universe_size=100, reeval_after_s=3 * 86400, exclude=(), now=now
    )
    # 冷却 3 天：W1（4 天前）到期可复评，W2（1 天前）冷却内跳过
    assert due == [W1]
    led.close()


def test_due_respects_universe_size_and_exclude():
    led = Ledger(":memory:")
    now = 1_000_000.0
    led.discover_universe_upsert([
        {"wallet": W1, "notional": 1000, "source": "s"},
        {"wallet": W2, "notional": 500, "source": "s"},
        {"wallet": W3, "notional": 10, "source": "s"},
    ], now=now)
    # universe_size=2 → 只看 notional 前二（W1、W2）；exclude 掉 W1 → 只剩 W2；W3 在范围外
    due = led.discover_universe_due(
        limit=10, universe_size=2, reeval_after_s=3 * 86400, exclude=[W1], now=now
    )
    assert due == [W2]
    led.close()


# ---------------------------------------------------------------------------
# 5) 名录（roster）：只合格、按分数降序、带 evaluated_at、排除在跟
# ---------------------------------------------------------------------------

def test_roster_only_eligible_sorted_with_evaluated_at():
    led = Ledger(":memory:")
    now = 1_000_000.0
    led.discover_universe_upsert(
        [{"wallet": w, "source": "s"} for w in (W1, W2, W3)], now=now
    )
    led.discover_universe_record_eval(W1, score=70, eligible=True,
                                      verdict={"address": W1, "score": 70}, now=now)
    led.discover_universe_record_eval(W2, score=90, eligible=True,
                                      verdict={"address": W2, "score": 90}, now=now)
    led.discover_universe_record_eval(W3, score=40, eligible=False,
                                      verdict={"address": W3}, now=now)
    roster = led.discover_universe_roster(exclude=())
    assert [e["address"] for e in roster] == [W2, W1]  # 只合格、分数降序
    assert all(e["evaluated_at"] is not None for e in roster)
    assert led.discover_universe_roster(exclude=[W2]) == [
        e for e in roster if e["address"] == W1
    ]
    led.close()


# ---------------------------------------------------------------------------
# 6) 可判性闸：招聘口径拦纯买入，考核口径不拦
# ---------------------------------------------------------------------------

def test_judgeability_gate_blocks_recruit_not_health():
    wallet = "0x" + "e" * 40
    tape = _fire(wallet, 30)  # 30 笔纯买入、零配对卖出（撸空投指纹）
    stats = replay(wallet, tape)
    assert stats.matched_sells == 0
    verdict = evaluate(stats, [], ScoutConfig())          # 招聘口径
    assert not verdict.eligible
    assert any("无法自证" in r for r in verdict.reasons)
    health = evaluate_health(stats, [], tape, ScoutConfig())  # 考核口径：闸关掉
    assert health.eligible, health.reasons


# ---------------------------------------------------------------------------
# 7) 冷启动填充：全新账本立刻用快照 + 排行榜并轨灌池（不等周期）
# ---------------------------------------------------------------------------

def test_cold_start_bootstraps_from_snapshot(tmp_path):
    data = FakeData(firehose=_fire(W1, 3) + _fire(W2, 2))
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_eval_per_min=0)
    engine._discover_tick()
    wallets = {r["wallet"] for r in led._conn.execute("SELECT wallet FROM discover_universe")}
    assert W1 in wallets and W2 in wallets
    assert _row(led, W1)["source"] == "snapshot"
    # 冷启动落时间戳，下轮不再当冷启动重复灌
    assert led.get_state("discover_last_run_ts") is not None


def test_cold_start_merges_three_sources(tmp_path, monkeypatch):
    snap_only = "0x" + "7" * 40
    overlap = "0x" + "8" * 40
    lb_only = "0x" + "9" * 40
    monkeypatch.setattr(
        "polycopycat.scout.runner.candidates_from_leaderboard",
        lambda *a, window, rank_type, limit, **k: [overlap, lb_only],
    )
    data = FakeData(firehose=_fire(snap_only, 3) + _fire(overlap, 2))
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_eval_per_min=0)
    engine._discover_tick()
    assert _row(led, snap_only)["source"] == "snapshot"
    assert _row(led, lb_only)["source"] == "leaderboard"
    # 快照先灌、排行榜后并 → 并列来源
    assert _row(led, overlap)["source"] == "snapshot+leaderboard"


# ---------------------------------------------------------------------------
# 8) 排行榜降级：500 → 100 → 50 → 跳过
# ---------------------------------------------------------------------------

def test_leaderboard_degrades_50_25(tmp_path, monkeypatch):
    # 接口 limit 硬上限 50（2026-07 实测），首档直接 50；阶梯只留防御——
    # 上限再降时自动退到 25 而不是整源熄火
    calls = []
    from polycopycat.scout.runner import ScoutError

    def fake(*a, window, rank_type, limit, **k):
        calls.append(limit)
        if limit > 25:
            raise ScoutError(f"HTTP 400 at limit={limit}")
        return [W1]

    monkeypatch.setattr("polycopycat.scout.runner.candidates_from_leaderboard", fake)
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path)
    assert engine._leaderboard_fetch("7d", "pnl") == [W1]
    assert calls == [50, 25]  # 逐档降级直到成功
    led.close()


def test_leaderboard_all_fail_skips_source(tmp_path):
    # autouse fixture 已让排行榜恒抛 ScoutError → 各档全失败 → 跳过（返回空）
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path)
    assert engine._leaderboard_fetch("30d", "vol") == []
    assert engine._leaderboard_join() == 0
    led.close()


# ---------------------------------------------------------------------------
# 9) 涓流评估端到端：写回票池 + 剔除在跟/黑名单 + 速率闸
# ---------------------------------------------------------------------------

def test_trickle_evaluates_and_writes_back(tmp_path):
    data = FakeData(
        firehose=_fire(GOOD, 3) + _fire(BAD, 2),
        tapes={GOOD: elig_tape(GOOD), BAD: []},
    )
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_eval_per_min=2)
    engine._discover_tick()  # 冷启动灌 GOOD/BAD + 涓流评估 2 个
    g, b = _row(led, GOOD), _row(led, BAD)
    assert g["last_eligible"] == 1 and g["last_evaluated"] > 0 and g["last_verdict"]
    assert b["last_eligible"] == 0 and b["last_evaluated"] > 0  # 空成交带 → 不合格


def test_trickle_excludes_in_tracking_and_blocklist(tmp_path):
    data = FakeData(
        firehose=_fire(TRACKED, 3) + _fire(BLOCKED, 3) + _fire(GOOD, 3),
        tapes={TRACKED: elig_tape(TRACKED), BLOCKED: elig_tape(BLOCKED),
               GOOD: elig_tape(GOOD)},
    )
    engine, _, led = make_engine(
        data, targets=(TRACKED,), tmp_path=tmp_path,
        discover_eval_per_min=5, recruit_blocklist=[BLOCKED.upper()],
    )
    engine._discover_tick()
    assert _row(led, GOOD)["last_evaluated"] > 0        # 只有它被评估
    assert _row(led, TRACKED)["last_evaluated"] == 0    # 在跟：不评估
    assert _row(led, BLOCKED)["last_evaluated"] == 0    # 黑名单：不评估


def test_trickle_disabled_by_zero_rate(tmp_path):
    data = FakeData(firehose=_fire(W1, 3), tapes={W1: elig_tape(W1)})
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_eval_per_min=0)
    engine._discover_tick()
    assert led.discover_universe_count() >= 1          # 灌池照常
    assert _row(led, W1)["last_evaluated"] == 0        # 涓流关闭 → 不评估


# ---------------------------------------------------------------------------
# 10) 累积器：逐笔进内存不写库；冲刷才批量落库并清空
# ---------------------------------------------------------------------------

def test_observe_accumulates_in_memory_then_flushes(tmp_path):
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path)
    for t in (_one(W1, size=100, ts=10), _one(W1, size=200, ts=20), _one(W2, size=10, ts=5)):
        engine.observe_site_trade(t)
    assert engine._universe_accum[W1]["trades"] == 2
    assert abs(engine._universe_accum[W1]["notional"] - 150) < 1e-9  # 50 + 100
    assert engine._universe_accum[W1]["last_seen"] == 20
    assert led.discover_universe_count() == 0  # 绝不逐笔写库

    n = engine._flush_universe_accumulator()
    assert n == 2 and engine._universe_accum == {}
    assert abs(_row(led, W1)["notional"] - 150) < 1e-9
    assert _row(led, W1)["trades"] == 2 and _row(led, W1)["source"] == "stream"
    assert engine._flush_universe_accumulator() == 0  # 再冲刷（空）


# ---------------------------------------------------------------------------
# 11) 每小时最多一条摘要 + 同一地址 7 天内不重复上榜
# ---------------------------------------------------------------------------

def _summaries(notifier):
    return [m for m in notifier.messages if "候选发现摘要" in m]


def test_summary_hourly_limit_and_7day_dedup(tmp_path):
    engine, notifier, _ = make_engine(FakeData(), tmp_path=tmp_path)
    now = time.time()

    # 首播：过了 1 小时且有新合格者 → 发一条，W1/W2 上榜
    engine._discover_new_eligible = {W1: _verdict(W1, 80), W2: _verdict(W2, 70)}
    engine._last_discover_notify = now - 3601
    engine._maybe_discover_summary(now)
    assert len(_summaries(notifier)) == 1
    assert _short(W1) in _summaries(notifier)[0] and _short(W2) in _summaries(notifier)[0]

    # 未过 1 小时：即便又有合格者也不发第二条
    engine._discover_new_eligible = {W3: _verdict(W3, 60)}
    engine._maybe_discover_summary(now + 10)
    assert len(_summaries(notifier)) == 1

    # 过了 1 小时，但只有 W1/W2 复现 → 7 天去重挡下，不发
    engine._discover_new_eligible = {W1: _verdict(W1, 80), W2: _verdict(W2, 70)}
    engine._maybe_discover_summary(now + 3700)
    assert len(_summaries(notifier)) == 1

    # 过 1 小时且出现全新面孔 W3 → 发第二条，只列 W3
    engine._discover_new_eligible = {W3: _verdict(W3, 60)}
    engine._maybe_discover_summary(now + 7400)
    msgs = _summaries(notifier)
    assert len(msgs) == 2
    assert _short(W3) in msgs[1] and _short(W1) not in msgs[1]


def test_dedup_prunes_entries_older_than_7_days(tmp_path):
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path)
    now = time.time()
    # 预置一个 8 天前通知过的地址 → 读写时应被清掉
    led.set_state("discover_notified", json.dumps({W1: now - 8 * 86400}))
    fresh = engine._dedup_notify([_verdict(W1, 50)], now)
    assert [v.address for v in fresh] == [W1]  # 过期项已清 → W1 视作新面孔重新上榜
    stored = json.loads(led.get_state("discover_notified"))
    assert set(stored) == {W1} and stored[W1] == now


# ---------------------------------------------------------------------------
# 12) discover-latest.json 滚动结构
# ---------------------------------------------------------------------------

def test_discover_latest_rolling_structure(tmp_path):
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path)
    led.discover_universe_upsert([{"wallet": W1, "source": "s"}, {"wallet": W2, "source": "s"}])
    led.discover_universe_record_eval(
        W1, score=88, eligible=True, verdict={"address": W1, "score": 88, "win_rate": 0.7}
    )
    led.discover_universe_record_eval(W2, score=30, eligible=False, verdict={"address": W2})
    engine._write_discover_roster(time.time())

    payload = json.loads((tmp_path / "discover-latest.json").read_text())
    assert set(payload) == {"updated_at", "universe_size", "evaluated_24h", "eligible"}
    assert payload["universe_size"] == 2 and payload["evaluated_24h"] == 2
    assert [e["address"] for e in payload["eligible"]] == [W1]  # 只列合格
    assert payload["eligible"][0]["score"] == 88
    assert payload["eligible"][0]["evaluated_at"] is not None


def test_roster_excludes_in_tracking_targets(tmp_path):
    # TRACKED 是在跟目标：即便票池里它合格，也不该出现在名录里
    engine, _, led = make_engine(FakeData(), targets=(TRACKED,), tmp_path=tmp_path)
    led.discover_universe_upsert([{"wallet": TRACKED, "source": "s"}, {"wallet": W1, "source": "s"}])
    led.discover_universe_record_eval(TRACKED, score=99, eligible=True,
                                      verdict={"address": TRACKED, "score": 99})
    led.discover_universe_record_eval(W1, score=50, eligible=True,
                                      verdict={"address": W1, "score": 50})
    engine._write_discover_roster(time.time())
    payload = json.loads((tmp_path / "discover-latest.json").read_text())
    assert [e["address"] for e in payload["eligible"]] == [W1]  # 排除在跟的 TRACKED


# ---------------------------------------------------------------------------
# 13) discover_interval_s <= 0：累积器落库/涓流/灌池全关
# ---------------------------------------------------------------------------

def test_discover_interval_zero_disables_everything(tmp_path):
    data = FakeData(firehose=_fire(W1, 3), tapes={W1: elig_tape(W1)})
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_interval_s=0)
    assert engine.config.health.discover_interval_s == 0.0
    # 累积器丢弃（不在内存累积）
    engine.observe_site_trade(_one(W1))
    assert engine._universe_accum == {}
    # tick 直接返回：不灌池、不评估、不落时间戳、不写文件
    engine._discover_tick()
    assert led.discover_universe_count() == 0
    assert led.get_state("discover_last_run_ts") is None
    assert not (tmp_path / "discover-latest.json").exists()


# ---------------------------------------------------------------------------
# 14) 到点刷新：stream 增量落库 + 修剪；未到点不刷新（沿用持久化计时）
# ---------------------------------------------------------------------------

def test_refresh_gated_by_interval_and_flushes_accumulator(tmp_path):
    data = FakeData()
    engine, _, led = make_engine(data, tmp_path=tmp_path, discover_eval_per_min=0)
    # 先冷启动把票池弄成非空（否则每次都走冷启动分支）
    led.discover_universe_upsert([{"wallet": W2, "source": "seed"}], now=time.time())
    engine._last_discover = time.time()  # 刚刷过

    # 内存里攒一笔 stream 增量
    engine.observe_site_trade(_one(W1, size=100, price=0.5))
    # 未到刷新周期 → 不落库
    engine._discover_tick()
    assert _row(led, W1) is None
    assert engine._universe_accum.get(W1) is not None

    # 把上次刷新时间拨回一个周期前 → 到点刷新，增量落库
    engine._last_discover -= engine.config.health.discover_interval_s + 1
    engine._discover_tick()
    assert _row(led, W1) is not None and engine._universe_accum == {}


# ---------------------------------------------------------------------------
# 15) stream 增量落库的 ~10 分钟独立节拍：不等每日刷新，新地址十分钟内进候选
# ---------------------------------------------------------------------------

def test_universe_flush_cadence_independent_of_daily_refresh(tmp_path):
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path, discover_eval_per_min=0)
    now = time.time()
    # 票池非空（避开冷启动分支）且每日刷新刚做过（远未到点）
    led.discover_universe_upsert([{"wallet": W2, "source": "seed"}], now=now)
    engine._last_discover = now

    # stream 上来一个新活跃地址 → 进内存增量
    engine.observe_site_trade(_one(W1, size=200, price=0.5))

    # 落库节拍还新鲜（<600s）→ tick 不落库，新地址进不了涓流候选队列
    engine._discover_tick()
    assert _row(led, W1) is None
    due = led.discover_universe_due(limit=10, universe_size=100,
                                    reeval_after_s=3 * 86400, exclude=(), now=time.time())
    assert W1 not in due

    # 把落库节拍拨旧（>600s）→ 每日刷新仍未到点，但独立节拍到点 → 落库
    engine._last_universe_flush -= 601
    engine._discover_tick()
    assert _row(led, W1) is not None       # 增量已落库
    assert engine._universe_accum == {}    # 内存已清空
    # 新地址十分钟内进入涓流评估候选（而非等 24h 每日刷新）
    due = led.discover_universe_due(limit=10, universe_size=100,
                                    reeval_after_s=3 * 86400, exclude=(), now=time.time())
    assert W1 in due
    # 独立落库没有改动每日刷新计时（仍是刚才设的 now）
    assert abs(engine._last_discover - now) < 1e-6


# ---------------------------------------------------------------------------
# 16) 招募路径（_recruit 经每小时摘要点触发；直接塞 Verdict 进本小时新合格集合驱动）
# ---------------------------------------------------------------------------

def _drive_summary(engine, eligible, *, now=None):
    """把合格者塞进本小时新合格集合、拨旧摘要计时，触发一次摘要点（内含招募）。"""
    now = time.time() if now is None else now
    engine._discover_new_eligible = {v.address: v for v in eligible}
    engine._last_discover_notify = now - 3601
    engine._maybe_discover_summary(now)


def test_recruit_adds_target_persists_and_calls_hook(tmp_path):
    engine, notifier, _ = make_engine(
        FakeData(), tmp_path=tmp_path, auto_recruit=True,
        recruit_ratio=0.05, recruit_max_per_trade_usdc=25, recruit_max_targets=15,
    )
    followed = []
    engine._on_new_target = followed.append
    _drive_summary(engine, [_verdict(GOOD, 80)])

    assert GOOD in engine._targets                        # 入池
    assert engine._targets[GOOD].ratio == 0.05            # 用招募档位
    assert engine._targets[GOOD].max_per_trade_usdc == 25
    assert followed == [GOOD]                             # 通知监控层去盯新地址
    assert any("自动加入纸面跟单" in m for m in notifier.messages)
    entries = json.loads((tmp_path / "recruited.json").read_text())  # 落盘持久化
    assert [e["address"] for e in entries] == [GOOD]


def test_recruit_respects_max_targets(tmp_path):
    # 已有 2 个目标 + 上限 2 → 到顶不招
    engine, _, _ = make_engine(
        FakeData(), targets=(TRACKED, BLOCKED), tmp_path=tmp_path,
        auto_recruit=True, recruit_max_targets=2,
    )
    _drive_summary(engine, [_verdict(GOOD, 80)])
    assert GOOD not in engine._targets
    assert not (tmp_path / "recruited.json").exists()


def test_recruit_paper_only(tmp_path):
    # 实盘模式绝不自动加人
    engine, _, _ = make_engine(FakeData(), tmp_path=tmp_path, mode="live", auto_recruit=True)
    _drive_summary(engine, [_verdict(GOOD, 80)])
    assert GOOD not in engine._targets
    assert not (tmp_path / "recruited.json").exists()


def test_recruit_blocklist_blocks(tmp_path):
    # scout 合格但人工拉黑 → _recruit 的黑名单闸拦下（涓流侧已排除，这里守 recruit 本身）
    engine, _, _ = make_engine(
        FakeData(), tmp_path=tmp_path, auto_recruit=True,
        recruit_blocklist=[GOOD.upper()],
    )
    _drive_summary(engine, [_verdict(GOOD, 80)])
    assert GOOD not in engine._targets
    assert not (tmp_path / "recruited.json").exists()


def test_recruit_recorded_as_event(tmp_path):
    engine, _, led = make_engine(FakeData(), tmp_path=tmp_path, auto_recruit=True)
    _drive_summary(engine, [_verdict(GOOD, 80)])
    summary = led.target_event_summary()
    assert summary[GOOD]["recruits"] == 1
