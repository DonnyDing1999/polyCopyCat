import time

import pytest

from polycopycat.engine.clob import MarketInfo
from polycopycat.engine.config import RiskConfig, TargetConfig
from polycopycat.engine.ledger import Ledger
from polycopycat.engine.risk import RiskGate, day_start_ts
from polycopycat.engine.signals import OrderIntent, Signal
from polycopycat.models import Trade

ADDR = "0x" + "a" * 40


@pytest.fixture
def ledger():
    ledger = Ledger(":memory:")
    yield ledger
    ledger.close()


def market(**kwargs):
    defaults = dict(
        condition_id="0xcond", tick_size=0.01, min_size=5.0,
        neg_risk=False, accepting_orders=True, closed=False, slug="will-x-happen",
    )
    defaults.update(kwargs)
    return MarketInfo(**defaults)


def intent(side="BUY", limit=0.50, size=100.0, cond="0xcond", token="tok1", title="T"):
    return OrderIntent(
        token_id=token, condition_id=cond, side=side, limit_price=limit,
        size=size, ref_price=0.5, neg_risk=False, title=title, outcome="Yes",
    )


def seed_position(ledger, cost_size=100.0, avg=0.5, token="tok1", cond="0xcond", title="T"):
    trade = Trade(
        proxy_wallet=ADDR, side="BUY", asset=token, condition_id=cond,
        size=cost_size, price=avg, timestamp=int(time.time()), transaction_hash=f"0x{token}",
    )
    sid, _ = ledger.record_signal(
        Signal(trade=trade, target=TargetConfig(address=ADDR), received_at=time.time())
    )
    ledger.record_order(sid, intent(size=cost_size, limit=avg, token=token, cond=cond, title=title),
                        mode="paper", status="filled", filled_size=cost_size, avg_price=avg)
    return sid


def test_kill_switch(tmp_path, ledger):
    stop_file = tmp_path / "STOP"
    gate = RiskGate(RiskConfig(kill_switch_file=str(stop_file)), ledger)
    ok, _ = gate.check(intent(), market())
    assert ok
    stop_file.touch()
    ok, reason = gate.check(intent(), market())
    assert not ok and "停机" in reason


def test_market_state_blocks(ledger):
    gate = RiskGate(RiskConfig(kill_switch_file=""), ledger)
    assert not gate.check(intent(), market(closed=True))[0]
    assert not gate.check(intent(), market(accepting_orders=False))[0]


def test_blacklist_by_condition_or_slug(ledger):
    gate = RiskGate(RiskConfig(kill_switch_file="", market_blacklist=["0xCOND"]), ledger)
    assert not gate.check(intent(), market())[0]
    gate = RiskGate(RiskConfig(kill_switch_file="", market_blacklist=["will-x-happen"]), ledger)
    ok, reason = gate.check(intent(), market())
    assert not ok and "黑名单" in reason


def test_market_exposure_cap(ledger):
    seed_position(ledger, cost_size=100, avg=0.5)  # 该市场成本 $50
    gate = RiskGate(RiskConfig(kill_switch_file="", max_market_exposure_usdc=60.0), ledger)
    ok, _ = gate.check(intent(size=10, limit=0.5), market())      # 50+5 <= 60
    assert ok
    ok, reason = gate.check(intent(size=30, limit=0.5), market())  # 50+15 > 60
    assert not ok and "单市场敞口" in reason


def test_total_exposure_cap(ledger):
    seed_position(ledger, cost_size=100, avg=0.5, token="tok1", cond="0xc1")
    seed_position(ledger, cost_size=100, avg=0.5, token="tok2", cond="0xc2")  # 总成本 $100
    gate = RiskGate(RiskConfig(kill_switch_file="", max_total_exposure_usdc=110.0), ledger)
    ok, reason = gate.check(intent(size=30, limit=0.5, cond="0xc3", token="tok3"), market())
    assert not ok and "总敞口" in reason


def test_daily_loss_blocks_buys_not_sells(ledger):
    seed_position(ledger, cost_size=100, avg=0.5)
    ledger.record_order(1, intent(side="SELL", size=100, limit=0.3), mode="paper",
                        status="filled", filled_size=100, avg_price=0.3)  # 实现 -20
    gate = RiskGate(RiskConfig(kill_switch_file="", daily_max_loss_usdc=10.0), ledger)
    ok, reason = gate.check(intent(side="BUY"), market())
    assert not ok and "熔断" in reason
    ok, _ = gate.check(intent(side="SELL"), market())  # 减仓放行
    assert ok


def test_day_start_is_today_midnight():
    start = day_start_ts()
    local = time.localtime(start)
    assert (local.tm_hour, local.tm_min, local.tm_sec) == (0, 0, 0)
    assert time.time() - start < 86400 + 1


# ---- 单目标单市场敞口闸 ----

T1 = "0x" + "1" * 40
T2 = "0x" + "2" * 40


def seed_fill(ledger, target, side, size, price, cond="0xcond", token="tok1", apply_fill=True):
    """记一笔归属到 target、在 cond 市场的成交订单（经 signal 归因）。"""
    tr = Trade(proxy_wallet=target, side=side, asset=token, condition_id=cond,
               size=size, price=price, timestamp=int(time.time()),
               transaction_hash=f"0x{side}{token}{size}{price}")
    sid, _ = ledger.record_signal(
        Signal(trade=tr, target=TargetConfig(address=target), received_at=time.time())
    )
    ledger.record_order(sid, intent(side=side, size=size, limit=price, token=token, cond=cond),
                        mode="paper", status="filled", filled_size=size, avg_price=price,
                        apply_fill=apply_fill)


def test_target_market_net_cost_buy_minus_sell(ledger):
    seed_fill(ledger, T1, "BUY", 100, 0.5)                 # +50
    seed_fill(ledger, T1, "SELL", 40, 0.5)                 # -20 → 净 30
    assert abs(ledger.target_market_net_cost(T1, "0xcond") - 30.0) < 1e-9
    assert ledger.target_market_net_cost(T2, "0xcond") == 0.0    # 另一目标独立归因
    assert ledger.target_market_net_cost(T1, "0xother") == 0.0   # 另一市场独立


def test_target_market_net_cost_floored_at_zero(ledger):
    seed_fill(ledger, T1, "BUY", 40, 0.5)                            # +20
    seed_fill(ledger, T1, "SELL", 100, 0.5, apply_fill=False)        # -50 → -30 → 下限 0
    assert ledger.target_market_net_cost(T1, "0xcond") == 0.0


def test_per_target_exposure_disabled_by_default(ledger):
    seed_fill(ledger, T1, "BUY", 1000, 0.5)  # 该目标该市场已 $500
    gate = RiskGate(RiskConfig(kill_switch_file="", max_market_exposure_per_target_usdc=None,
                               max_market_exposure_usdc=None, max_total_exposure_usdc=None),
                    ledger)
    ok, _ = gate.check(intent(size=1000, limit=0.5), market(), T1)  # 再买 $500，闸关不拦
    assert ok


def test_per_target_exposure_caps_buy_allows_sell(ledger):
    seed_fill(ledger, T1, "BUY", 100, 0.5)  # T1 该市场净成本 $50
    gate = RiskGate(RiskConfig(kill_switch_file="", max_market_exposure_per_target_usdc=60.0,
                               max_market_exposure_usdc=None, max_total_exposure_usdc=None),
                    ledger)
    ok, _ = gate.check(intent(size=10, limit=0.5), market(), T1)       # 50+5 ≤ 60
    assert ok
    ok, reason = gate.check(intent(size=30, limit=0.5), market(), T1)  # 50+15 > 60
    assert not ok and "单目标单市场" in reason and "1111" in reason
    # 卖出永不拦，即使远超上限
    ok, _ = gate.check(intent(side="SELL", size=1000, limit=0.5), market(), T1)
    assert ok
    # 另一目标在同一市场独立计额度：T2 净成本为 0，同样的买入放行
    ok, _ = gate.check(intent(size=30, limit=0.5), market(), T2)
    assert ok


def test_per_target_exposure_deducts_sells(ledger):
    seed_fill(ledger, T1, "BUY", 100, 0.5)   # +50
    seed_fill(ledger, T1, "SELL", 40, 0.5)   # -20 → 净 30
    gate = RiskGate(RiskConfig(kill_switch_file="", max_market_exposure_per_target_usdc=60.0,
                               max_market_exposure_usdc=None, max_total_exposure_usdc=None),
                    ledger)
    # 净成本 30：再买 notional 30（30+30=60）恰好压线放行；若不抵扣卖出（50+30=80）会被拦
    ok, _ = gate.check(intent(size=60, limit=0.5), market(), T1)
    assert ok
    ok, reason = gate.check(intent(size=64, limit=0.5), market(), T1)  # 30+32 > 60
    assert not ok and "单目标单市场" in reason


# ---- 事件族敞口闸 ----

IRAN = {"name": "伊朗", "patterns": ["iran", "hormuz"], "max_usdc": 80.0}
MIDEAST = {"name": "中东", "patterns": ["iran", "israel"], "max_usdc": 60.0}


def group_gate(ledger, *groups):
    """只开事件族闸、其余额度全关，隔离被测规则。"""
    return RiskGate(
        RiskConfig(
            kill_switch_file="", max_market_exposure_usdc=None,
            max_market_exposure_per_target_usdc=None, max_total_exposure_usdc=None,
            daily_max_loss_usdc=None, exposure_groups=[dict(g) for g in groups],
        ),
        ledger,
    )


def test_exposure_group_merges_related_markets(ledger):
    # 同一叙事分散在两个市场：单市场闸各自都不超，只有合并计额度才拦得住
    seed_position(ledger, cost_size=100, avg=0.5, token="t1", cond="0xc1",
                  title="Will Iran and Israel reach a ceasefire?")   # $50
    seed_position(ledger, cost_size=40, avg=0.5, token="t2", cond="0xc2",
                  title="Strait of Hormuz closed in 2026?")          # $20 → 族内共 $70
    gate = group_gate(ledger, IRAN)
    ok, _ = gate.check(intent(size=20, limit=0.5, cond="0xc3", token="t3",
                              title="Iran nuclear deal?"), market())      # 70+10 ≤ 80
    assert ok
    ok, reason = gate.check(intent(size=40, limit=0.5, cond="0xc3", token="t3",
                                   title="Iran nuclear deal?"), market())  # 70+20 > 80
    assert not ok and "伊朗" in reason and "90.00" in reason and "80.00" in reason


def test_exposure_group_ignores_unmatched_intent(ledger):
    # 族内早就爆了，但这笔标题不属于该族 → 这一族根本不参与判定
    seed_position(ledger, cost_size=1000, avg=0.5, token="t1", cond="0xc1",
                  title="Iran ceasefire?")   # $500
    gate = group_gate(ledger, IRAN)
    ok, _ = gate.check(intent(size=1000, limit=0.5, cond="0xc9", token="t9",
                              title="Will BTC hit $200k?"), market())
    assert ok


def test_exposure_group_checks_every_matching_group(ledger):
    # 一笔 intent 同时命中两族：伊朗族额度还够、中东族已满 → 逐组过闸才拦得下
    seed_position(ledger, cost_size=100, avg=0.5, token="t1", cond="0xc1",
                  title="Israel strikes?")   # 只命中中东族，$50
    gate = group_gate(ledger, IRAN, MIDEAST)
    ok, reason = gate.check(intent(size=40, limit=0.5, cond="0xc2", token="t2",
                                   title="Iran ceasefire?"), market())
    assert not ok and "中东" in reason   # 伊朗族 0+20 ≤ 80 放行，中东族 50+20 > 60 拦下


def test_exposure_group_allows_sells(ledger):
    seed_position(ledger, cost_size=1000, avg=0.5, token="t1", cond="0xc1",
                  title="Iran ceasefire?")   # 族内 $500，远超上限
    gate = group_gate(ledger, IRAN)
    ok, _ = gate.check(intent(side="SELL", size=1000, limit=0.5, cond="0xc1",
                              token="t1", title="Iran ceasefire?"), market())
    assert ok   # 减仓永不拦


def test_exposure_group_skips_untitled_positions(ledger):
    # 无标题的持仓不参与匹配（成交推送偶尔缺 title，靠对账回填后才纳入）
    seed_position(ledger, cost_size=1000, avg=0.5, token="t1", cond="0xc1", title="")
    gate = group_gate(ledger, IRAN)
    ok, _ = gate.check(intent(size=100, limit=0.5, cond="0xc2", token="t2",
                              title="Iran ceasefire?"), market())
    assert ok   # 只算这笔自己的 $50，那 $500 不计


def test_exposure_group_disabled_by_default(ledger):
    seed_position(ledger, cost_size=1000, avg=0.5, token="t1", cond="0xc1",
                  title="Iran ceasefire?")
    gate = group_gate(ledger)   # 空数组 = 不启用
    ok, _ = gate.check(intent(size=1000, limit=0.5, cond="0xc2", token="t2",
                              title="Iran ceasefire?"), market())
    assert ok
