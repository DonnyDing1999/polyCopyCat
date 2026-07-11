from polycopycat.models import Trade
from polycopycat.scout.metrics import replay

ADDR = "0x" + "a" * 40
DAY = 86400


def trade(side, size, price, ts, asset="tok1", cond=None):
    return Trade(
        proxy_wallet=ADDR, side=side, asset=asset,
        condition_id=cond or f"0xc-{asset}", size=size, price=price,
        timestamp=ts, transaction_hash=f"0x{ts}",
    )


def test_replay_realized_pnl_and_win_rate():
    tape = [
        trade("BUY", 100, 0.40, 1000),
        trade("BUY", 100, 0.50, 2000),          # 均价 0.45
        trade("SELL", 100, 0.60, 1000 + 2 * DAY),  # 赢 +15
        trade("SELL", 100, 0.40, 1000 + 3 * DAY),  # 亏 -5
    ]
    stats = replay(ADDR, tape)
    assert stats.n_trades == 4 and stats.n_buys == 2 and stats.n_sells == 2
    assert stats.matched_sells == 2 and stats.unmatched_sells == 0
    assert stats.wins == 1 and stats.win_rate == 0.5
    assert abs(stats.realized_pnl - 10.0) < 1e-9  # +15 - 5
    assert stats.quick_flips == 0
    assert stats.median_holding_s > DAY


def test_replay_input_order_does_not_matter():
    tape = [
        trade("SELL", 50, 0.60, 3000),
        trade("BUY", 50, 0.40, 1000),
    ]
    stats = replay(ADDR, tape)  # 新→旧输入也能配对
    assert stats.matched_sells == 1 and stats.wins == 1
    assert abs(stats.realized_pnl - 50 * 0.2) < 1e-9


def test_unmatched_sell_not_counted_in_win_rate():
    stats = replay(ADDR, [trade("SELL", 100, 0.70, 1000)])
    assert stats.unmatched_sells == 1 and stats.matched_sells == 0
    assert stats.win_rate is None
    assert stats.realized_pnl == 0.0


def test_quick_flip_detection():
    tape = []
    for i in range(6):
        base = 1000 + i * 3600
        tape.append(trade("BUY", 100, 0.50, base, asset=f"tok{i}"))
        tape.append(trade("SELL", 100, 0.51, base + 30, asset=f"tok{i}"))  # 30 秒平仓
    stats = replay(ADDR, tape, quick_window_s=600)
    assert stats.matched_sells == 6
    assert stats.quick_flips == 6
    assert stats.quick_flip_ratio == 1.0


def test_partial_close_keeps_avg_and_entry():
    tape = [
        trade("BUY", 100, 0.40, 1000),
        trade("SELL", 40, 0.50, 2000),   # 平 40，剩 60@0.40
        trade("SELL", 60, 0.30, 3000),   # 平剩余，亏
    ]
    stats = replay(ADDR, tape)
    assert stats.matched_sells == 2
    assert abs(stats.realized_pnl - (40 * 0.1 - 60 * 0.1)) < 1e-9
    assert stats.wins == 1


def test_breadth_days_and_notional():
    tape = [
        trade("BUY", 100, 0.50, 0, asset="tok1"),
        trade("BUY", 100, 0.50, 2 * DAY, asset="tok2"),
        trade("BUY", 100, 0.50, 2 * DAY + 60, asset="tok3"),
    ]
    stats = replay(ADDR, tape)
    assert stats.n_markets == 3
    assert stats.active_days == 2
    assert abs(stats.notional - 150.0) < 1e-9
    assert abs(stats.avg_trade_usdc - 50.0) < 1e-9
    assert stats.first_ts == 0 and stats.last_ts == 2 * DAY + 60
