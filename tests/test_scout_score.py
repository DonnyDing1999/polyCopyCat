import time

from polycopycat.models import Position, Trade
from polycopycat.scout.metrics import replay
from polycopycat.scout.score import ScoutConfig, evaluate

ADDR = "0x" + "a" * 40
NOW = 1_800_000_000
DAY = 86400


def tape_winner(n_markets=10, size=500, buy=0.40, sell=0.55):
    """跨多个市场、持仓两天、全部止盈的理想目标。"""
    tape = []
    for i in range(n_markets):
        base = NOW - (n_markets - i) * DAY
        tape.append(Trade(proxy_wallet=ADDR, side="BUY", asset=f"tok{i}",
                          condition_id=f"0xc{i}", size=size, price=buy,
                          timestamp=base, transaction_hash=f"0xb{i}"))
        tape.append(Trade(proxy_wallet=ADDR, side="SELL", asset=f"tok{i}",
                          condition_id=f"0xc{i}", size=size, price=sell,
                          timestamp=base + 2 * DAY if base + 2 * DAY < NOW else NOW - 3600,
                          transaction_hash=f"0xs{i}"))
    return tape


def test_winner_is_eligible_and_scored():
    stats = replay(ADDR, tape_winner())
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert verdict.eligible, verdict.reasons
    assert verdict.score > 50
    assert verdict.to_dict()["win_rate"] == 1.0


def test_small_sample_excluded():
    stats = replay(ADDR, tape_winner(n_markets=2))
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert not verdict.eligible
    assert any("样本不足" in r for r in verdict.reasons)


def test_small_notional_excluded():
    stats = replay(ADDR, tape_winner(size=5))
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert any("成交额太小" in r for r in verdict.reasons)


def test_inactive_excluded():
    old = [Trade(proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xc",
                 size=100, price=0.5, timestamp=NOW - 30 * DAY, transaction_hash="0x1")]
    stats = replay(ADDR, tape_winner()[:-1] + old)
    stats.last_ts = NOW - 30 * DAY
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert any("不活跃" in r for r in verdict.reasons)


def test_market_maker_excluded():
    tape = []
    for i in range(10):
        base = NOW - 3600 + i * 120
        tape.append(Trade(proxy_wallet=ADDR, side="BUY", asset="tok1", condition_id="0xc",
                          size=200, price=0.50, timestamp=base, transaction_hash=f"0xb{i}"))
        tape.append(Trade(proxy_wallet=ADDR, side="SELL", asset="tok1", condition_id="0xc",
                          size=200, price=0.505, timestamp=base + 20, transaction_hash=f"0xs{i}"))
    stats = replay(ADDR, tape)
    verdict = evaluate(stats, [], ScoutConfig(min_trades=10, min_notional_usdc=100), now=NOW)
    assert not verdict.eligible
    assert any("做市" in r for r in verdict.reasons)


def test_loser_excluded():
    stats = replay(ADDR, tape_winner(buy=0.55, sell=0.40))
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert any("亏损" in r for r in verdict.reasons)


def test_low_win_rate_excluded():
    # 5 胜 + 7 负 → 胜率 42%，低于 50% 阈值
    tape = tape_winner(n_markets=5, buy=0.40, sell=0.55) + tape_winner(
        n_markets=7, buy=0.55, sell=0.40
    )
    for i, t in enumerate(tape[10:]):  # 后 7 个市场换 id，避免与前 5 个混仓
        object.__setattr__(t, "asset", f"tokx{i // 2}")
        object.__setattr__(t, "condition_id", f"0xcx{i // 2}")
    stats = replay(ADDR, tape)
    assert stats.matched_sells == 12 and stats.wins == 5
    verdict = evaluate(stats, [], ScoutConfig(min_realized_pnl=-10_000), now=NOW)
    assert any("胜率过低" in r for r in verdict.reasons)


def test_structural_arb_win_rate_excluded():
    # 60 笔配对卖出、100% 胜率：人类做不到，是结构性套利的signature
    stats = replay(ADDR, tape_winner(n_markets=60))
    assert stats.matched_sells == 60 and stats.win_rate == 1.0
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert not verdict.eligible
    assert any("结构性套利" in r for r in verdict.reasons)


def test_dead_position_drag_excluded():
    # 持仓成本 $500，按市价浮亏 98% → 死仓/藏亏嫌疑
    positions = [Position(proxy_wallet=ADDR, asset="tok1", condition_id="0xc",
                          size=1000, avg_price=0.50, cur_price=0.01)]
    stats = replay(ADDR, tape_winner())
    verdict = evaluate(stats, positions, ScoutConfig(), now=NOW)
    assert not verdict.eligible
    assert any("死仓" in r for r in verdict.reasons)


def test_small_exposure_skips_drawdown_rule():
    # 持仓成本 $40 低于判定门槛，浮亏比例再高也不触发
    positions = [Position(proxy_wallet=ADDR, asset="tok1", condition_id="0xc",
                          size=100, avg_price=0.40, cur_price=0.01)]
    stats = replay(ADDR, tape_winner())
    verdict = evaluate(stats, positions, ScoutConfig(), now=NOW)
    assert verdict.eligible, verdict.reasons


def test_frequency_cap_tightened():
    # 每天 120 笔（>100）应被判为机器人
    tape = []
    for i in range(120):
        tape.append(Trade(proxy_wallet=ADDR, side="BUY", asset=f"t{i}",
                          condition_id=f"0xc{i}", size=100, price=0.5,
                          timestamp=NOW - 3600 + i * 20, transaction_hash=f"0x{i}"))
    stats = replay(ADDR, tape)
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert any("机器人" in r for r in verdict.reasons)


def test_buy_and_hold_gets_neutral_win_score():
    tape = [Trade(proxy_wallet=ADDR, side="BUY", asset=f"tok{i}", condition_id=f"0xc{i}",
                  size=300, price=0.5, timestamp=NOW - i * 3600, transaction_hash=f"0x{i}")
            for i in range(25)]
    stats = replay(ADDR, tape)
    verdict = evaluate(stats, [], ScoutConfig(), now=NOW)
    assert verdict.eligible
    assert verdict.to_dict()["win_rate"] is None


def test_positions_exposure_and_unrealized():
    positions = [Position(proxy_wallet=ADDR, asset="tok1", condition_id="0xc",
                          size=100, avg_price=0.40, cur_price=0.50)]
    stats = replay(ADDR, tape_winner())
    verdict = evaluate(stats, positions, ScoutConfig(), now=NOW)
    assert abs(verdict.exposure_usdc - 40.0) < 1e-9
    assert abs(verdict.unrealized_pnl - 10.0) < 1e-9


def test_fresh_activity_scores_higher_than_stale():
    stats = replay(ADDR, tape_winner())
    fresh = evaluate(stats, [], ScoutConfig(), now=stats.last_ts + 3600).score
    stale = evaluate(stats, [], ScoutConfig(max_inactive_days=30),
                     now=stats.last_ts + 6 * DAY).score
    assert fresh > stale


# ---- evaluate_health：试用期考核口径（活仓浮亏 + 窗口净盈亏，老死仓不追溯）----

def _pos(asset, size, avg, cur, wallet="0x" + "a" * 40):
    from polycopycat.models import Position
    return Position(proxy_wallet=wallet, asset=asset, condition_id="0xc",
                    size=size, avg_price=avg, cur_price=cur)


def _tape_with_assets(wallet, assets, n_per=12, notional_each=300.0):
    """给定资产列表造一条健康成交带（窗口内买过这些资产）。"""
    import time as _t
    now = int(_t.time())
    trades = []
    i = 0
    for asset in assets:
        for _ in range(n_per):
            trades.append(Trade(
                proxy_wallet=wallet, side="BUY", asset=asset, condition_id=f"0xc{asset}",
                size=notional_each * 2, price=0.5, timestamp=now - i * 1800,
                title="T", outcome="Yes", transaction_hash=f"0xh{i}",
            ))
            i += 1
    return trades


def test_health_old_corpses_not_counted():
    """老死仓（窗口内没买过的资产）不追溯——b5b3 案例。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    tape = _tape_with_assets(wallet, ["live1", "live2"])
    stats = replay(wallet, tape)
    positions = [
        _pos("oldDead", 2000, 0.5, 0.0),   # $1000 老死仓，asset 不在窗口内
        _pos("live1", 100, 0.5, 0.48),     # 活仓小幅浮亏
    ]
    verdict = evaluate_health(stats, positions, tape, ScoutConfig())
    assert verdict.eligible, verdict.reasons


def test_health_recent_corpses_trigger_window_loss():
    """窗口内买入后归零 → 窗口净亏损 → 暂停——90c2 案例。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    tape = _tape_with_assets(wallet, ["deadA", "deadB", "deadC"])
    stats = replay(wallet, tape)
    positions = [
        _pos("deadA", 1000, 0.5, 0.0),
        _pos("deadB", 800, 0.5, 0.0),
        _pos("deadC", 600, 0.5, 0.0),
    ]
    verdict = evaluate_health(stats, positions, tape, ScoutConfig())
    assert not verdict.eligible
    assert any("窗口净亏损" in r for r in verdict.reasons)


def test_health_recent_wins_offset_corpses():
    """窗口内也有未赎回赢仓，净额为正 → 不暂停。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    tape = _tape_with_assets(wallet, ["dead1", "won1"])
    stats = replay(wallet, tape)
    positions = [
        _pos("dead1", 400, 0.5, 0.0),    # -$200
        _pos("won1", 600, 0.5, 1.0),     # +$300 未赎回
    ]
    verdict = evaluate_health(stats, positions, tape, ScoutConfig())
    assert verdict.eligible, verdict.reasons


def test_health_live_drawdown_still_pauses():
    """活仓被套超过阈值 → 暂停（规则保留）。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    tape = _tape_with_assets(wallet, ["deep1"])
    stats = replay(wallet, tape)
    positions = [_pos("deep1", 2000, 0.5, 0.20)]  # 成本 $1000，浮亏 60%
    verdict = evaluate_health(stats, positions, tape, ScoutConfig())
    assert not verdict.eligible
    assert any("活仓浮亏" in r for r in verdict.reasons)


def test_health_small_corpse_sample_tolerated():
    """孤立一具小尸体（样本不足）不触发窗口亏损规则。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    tape = _tape_with_assets(wallet, ["x1", "x2"])
    stats = replay(wallet, tape)   # 纯买入：matched_sells=0
    positions = [_pos("x1", 20, 0.5, 0.0)]  # 仅 -$10、1 个事件 < min_pnl_sample
    verdict = evaluate_health(stats, positions, tape, ScoutConfig())
    assert verdict.eligible, verdict.reasons


def test_health_base_exclusions_still_apply():
    """招聘版的其余排除（不活跃/频率等）在考核版照常生效。"""
    from polycopycat.scout.score import evaluate_health
    wallet = "0x" + "a" * 40
    verdict = evaluate_health(
        replay(wallet, []), [], [], ScoutConfig()
    )
    assert not verdict.eligible  # 空成交带 → 样本不足


# ---- 跨场馆套利单腿指纹排除 ----

def _tape_high_close(wallet, n_win, sell_price, buy_price=0.5, markets=25):
    """造 n_win 笔「买入后高价平仓」的赢单，跨多个市场，胜率100%。"""
    import time as _t
    now = int(_t.time())
    trades = []
    for i in range(n_win):
        asset = f"tok{i}"; cond = f"0xc{i % markets}"
        trades.append(Trade(proxy_wallet=wallet, side="BUY", asset=asset, condition_id=cond,
                            size=200, price=buy_price, timestamp=now - i*7200 - 3600,
                            title="M", outcome="Yes", transaction_hash=f"0xb{i}"))
        trades.append(Trade(proxy_wallet=wallet, side="SELL", asset=asset, condition_id=cond,
                            size=200, price=sell_price, timestamp=now - i*7200,
                            title="M", outcome="Yes", transaction_hash=f"0xs{i}"))
    return trades


def test_arb_single_leg_excluded():
    """胜率100% + 全在0.95平仓 + 大样本 → 疑似套利单腿，排除。"""
    wallet = "0x" + "3" * 40
    stats = replay(wallet, _tape_high_close(wallet, 30, sell_price=0.95))
    assert stats.high_close_ratio == 1.0 and stats.win_rate == 1.0
    v = evaluate(stats, [], ScoutConfig())
    assert not v.eligible
    assert any("套利单腿" in r for r in v.reasons)


def test_directional_winner_not_flagged_as_arb():
    """真方向性赢家：胜率72%、卖价分散 → 不该被套利规则误杀。"""
    wallet = "0x" + "f" * 40
    import time as _t
    now = int(_t.time()); trades=[]
    # 40 笔：29 赢（卖价0.6-0.8）、11 亏（卖价0.3），胜率72%，高价平仓占比低
    for i in range(40):
        asset=f"t{i}"; cond=f"0xc{i%20}"; win = i % 10 < 7
        trades.append(Trade(proxy_wallet=wallet, side="BUY", asset=asset, condition_id=cond,
                            size=200, price=0.5, timestamp=now-i*7200-3600, title="M",
                            outcome="Yes", transaction_hash=f"0xb{i}"))
        trades.append(Trade(proxy_wallet=wallet, side="SELL", asset=asset, condition_id=cond,
                            size=200, price=(0.70 if win else 0.30), timestamp=now-i*7200,
                            title="M", outcome="Yes", transaction_hash=f"0xs{i}"))
    stats = replay(wallet, trades)
    assert stats.high_close_ratio == 0.0  # 卖价都<0.9
    v = evaluate(stats, [], ScoutConfig())
    assert not any("套利单腿" in r for r in v.reasons)  # 不被套利规则命中


def test_arb_rule_needs_sample():
    """样本不足（<20 配对卖出）时套利规则不触发，避免误杀小样本。"""
    wallet = "0x" + "7" * 40
    stats = replay(wallet, _tape_high_close(wallet, 12, sell_price=0.97))
    assert stats.win_rate == 1.0 and stats.high_close_ratio == 1.0
    v = evaluate(stats, [], ScoutConfig())
    assert not any("套利单腿" in r for r in v.reasons)


def test_high_close_ratio_metric():
    wallet = "0x" + "9" * 40
    import time as _t
    now=int(_t.time())
    # 2 笔高价平仓 + 2 笔低价平仓 → ratio 0.5
    trades=[]
    for i,(sp) in enumerate([0.95,0.92,0.6,0.55]):
        trades.append(Trade(proxy_wallet=wallet,side="BUY",asset=f"t{i}",condition_id="0xc",
                            size=100,price=0.5,timestamp=now-i*100-50,title="M",outcome="Y",
                            transaction_hash=f"b{i}"))
        trades.append(Trade(proxy_wallet=wallet,side="SELL",asset=f"t{i}",condition_id="0xc",
                            size=100,price=sp,timestamp=now-i*100,title="M",outcome="Y",
                            transaction_hash=f"s{i}"))
    stats=replay(wallet,trades)
    assert stats.high_close_sells == 2 and stats.high_close_ratio == 0.5


# ---- 慢速做市/流动性提供排除 ----

def _tape_market_maker(wallet, n_tokens=15, cycles=3, spread=0.03):
    """做市：每个 token 反复买卖多轮，薄点差。"""
    import time as _t
    now=int(_t.time()); trades=[]; k=0
    for tk in range(n_tokens):
        asset=f"mm{tk}"; cond=f"0xmm{tk}"
        for cyc in range(cycles):
            trades.append(Trade(proxy_wallet=wallet,side="BUY",asset=asset,condition_id=cond,
                                size=200,price=0.50,timestamp=now-(k:=k+1)*300,title="M",
                                outcome="Y",transaction_hash=f"b{tk}_{cyc}"))
            trades.append(Trade(proxy_wallet=wallet,side="SELL",asset=asset,condition_id=cond,
                                size=200,price=0.50+spread,timestamp=now-(k:=k+1)*300,title="M",
                                outcome="Y",transaction_hash=f"s{tk}_{cyc}"))
    return trades


def test_slow_market_maker_excluded():
    """同一 token 反复双向循环、薄点差、慢速（持仓时长长）→ 做市排除。"""
    wallet="0x"+"e"*40
    # 拉大持仓间隔避免被 quick_flip 抓（证明是新规则抓的）
    stats=replay(wallet,_tape_market_maker(wallet,n_tokens=15,cycles=3,spread=0.03))
    assert stats.churn_notional_ratio > 0.9
    assert stats.median_two_side_spread is not None and stats.median_two_side_spread < 0.06
    v=evaluate(stats,[],ScoutConfig())
    assert not v.eligible
    assert any("慢速做市" in r for r in v.reasons)


def test_directional_with_wide_spread_not_mm():
    """双向成交但点差宽（真进出）→ 不判做市。"""
    wallet="0x"+"d"*40
    stats=replay(wallet,_tape_market_maker(wallet,n_tokens=15,cycles=3,spread=0.25))
    assert stats.churn_notional_ratio > 0.9   # 也在双向循环
    assert stats.median_two_side_spread > 0.06  # 但点差宽
    v=evaluate(stats,[],ScoutConfig())
    assert not any("慢速做市" in r for r in v.reasons)  # 点差宽不算做市


def test_single_roundtrip_not_mm():
    """每个 token 只买一次卖一次（非深度循环）→ 不算做市。"""
    wallet="0x"+"a"*40
    import time as _t
    now=int(_t.time()); trades=[]
    for i in range(50):
        trades.append(Trade(proxy_wallet=wallet,side="BUY",asset=f"t{i}",condition_id=f"c{i}",
                            size=100,price=0.5,timestamp=now-i*600-300,title="M",outcome="Y",
                            transaction_hash=f"b{i}"))
        trades.append(Trade(proxy_wallet=wallet,side="SELL",asset=f"t{i}",condition_id=f"c{i}",
                            size=100,price=0.55,timestamp=now-i*600,title="M",outcome="Y",
                            transaction_hash=f"s{i}"))
    stats=replay(wallet,trades)
    assert stats.churn_notional_ratio == 0.0   # 每 token 只买1卖1，无深度循环
    v=evaluate(stats,[],ScoutConfig())
    assert not any("慢速做市" in r for r in v.reasons)


def test_churn_metrics_computed():
    wallet="0x"+"b"*40
    stats=replay(wallet,_tape_market_maker(wallet,n_tokens=5,cycles=2,spread=0.02))
    assert 0.99 < stats.churn_notional_ratio <= 1.0
    assert abs(stats.median_two_side_spread - 0.02) < 1e-6
