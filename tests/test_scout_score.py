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
