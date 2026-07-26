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


# ---- 打分 v2 新指标：sell_pnls / top_win_share / extreme_price_buy_ratio ----

def test_sell_pnls_recorded_same_as_realized():
    """每笔配对卖出的盈亏进 sell_pnls，累加与 realized_pnl 同口径。"""
    tape = [
        trade("BUY", 100, 0.40, 1000),
        trade("SELL", 100, 0.60, 1000 + 2 * DAY),   # +20
        trade("BUY", 100, 0.50, 3000, asset="tok2"),
        trade("SELL", 100, 0.30, 3000 + 2 * DAY, asset="tok2"),  # -20
    ]
    stats = replay(ADDR, tape)
    assert len(stats.sell_pnls) == 2
    assert abs(stats.sell_pnls[0] - 20.0) < 1e-9 and abs(stats.sell_pnls[1] + 20.0) < 1e-9
    assert abs(sum(stats.sell_pnls) - stats.realized_pnl) < 1e-9


def test_top_win_share_one_big_win():
    """盈亏几乎全靠一把梭 → top_win_share 接近 1（专杀 longshot 幸存者）。"""
    tape = [
        trade("BUY", 100, 0.10, 1000, asset="big"),
        trade("SELL", 100, 0.90, 1000 + DAY, asset="big"),   # +80 一把梭
    ]
    for i in range(4):  # 4 笔零星小盈利
        tape.append(trade("BUY", 100, 0.50, 2000 + i, asset=f"s{i}"))
        tape.append(trade("SELL", 100, 0.52, 2000 + i + DAY, asset=f"s{i}"))  # +2 each
    stats = replay(ADDR, tape)
    assert abs(stats.sell_pnls[0] - 80.0) < 1e-9
    # max 80 / (80 + 2*4) = 80/88 ≈ 0.909
    assert abs(stats.top_win_share - 80.0 / 88.0) < 1e-9


def test_top_win_share_spread_and_no_positive():
    # 盈利分散：5 笔各 +2 → top_win_share = 2/10 = 0.2
    tape = []
    for i in range(5):
        tape.append(trade("BUY", 100, 0.50, 1000 + i, asset=f"t{i}"))
        tape.append(trade("SELL", 100, 0.52, 1000 + i + DAY, asset=f"t{i}"))
    stats = replay(ADDR, tape)
    assert abs(stats.top_win_share - 0.2) < 1e-9
    # 全亏损（无正盈利）→ 记 1.0（无从判断分散度，保守当作最坏）
    loss = replay(ADDR, [trade("BUY", 100, 0.60, 1000),
                         trade("SELL", 100, 0.40, 1000 + DAY)])
    assert loss.top_win_share == 1.0


def test_extreme_price_buy_ratio_notional_weighted():
    """极端价买入占比按 notional 加权，不是按笔数。"""
    tape = [
        # 1 笔大额买在高价 0.90：notional = 2000×0.90 = 1800
        trade("BUY", 2000, 0.90, 1000, asset="hi"),
        # 9 笔小额买在常规价 0.50：合计 notional = 9×(100×0.50) = 450
    ]
    for i in range(9):
        tape.append(trade("BUY", 100, 0.50, 1100 + i, asset=f"n{i}"))
    stats = replay(ADDR, tape)
    # 按 notional 加权 = 1800 / (1800 + 450) = 0.8；按笔数则是 1/10 = 0.1
    assert abs(stats.extreme_price_buy_ratio - 1800.0 / 2250.0) < 1e-9
    assert abs(stats.extreme_price_buy_ratio - 0.8) < 1e-9


def test_extreme_price_low_end_counted():
    """极低价（<0.10）买入也算极端（longshot，滑点占比毁灭）。"""
    tape = [
        trade("BUY", 1000, 0.05, 1000, asset="lo"),   # notional 50，极端
        trade("BUY", 100, 0.50, 1100, asset="mid"),   # notional 50，常规
    ]
    stats = replay(ADDR, tape)
    assert abs(stats.extreme_price_buy_ratio - 0.5) < 1e-9
