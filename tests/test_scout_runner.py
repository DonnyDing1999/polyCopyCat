import pytest
import requests

from polycopycat.data_api import DataApiError
from polycopycat.models import Position, Trade
from polycopycat.scout.runner import (
    ScoutError,
    candidates_from_leaderboard,
    candidates_from_recent_trades,
    scout_addresses,
    targets_snippet,
)
from polycopycat.scout.score import ScoutConfig

A1 = "0x" + "1" * 40
A2 = "0x" + "2" * 40
A3 = "0x" + "3" * 40
NOW = 1_800_000_000
DAY = 86400


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        return self.responses.pop(0)


def test_leaderboard_parses_and_skips_garbage():
    payload = [
        {"proxyWallet": A1, "amount": 1000},
        {"wallet": A2},
        {"proxyWallet": "not-an-address"},
        "garbage",
    ]
    session = FakeSession([FakeResponse(payload=payload)])
    out = candidates_from_leaderboard("https://lb.test", session=session, window="7d")
    assert out == [A1, A2]
    url, params = session.requests[0]
    assert url == "https://lb.test/leaderboard"
    assert params["window"] == "7d" and params["rankType"] == "pnl"


def test_leaderboard_error_raises_scout_error():
    session = FakeSession([FakeResponse(status_code=404, payload={})])
    with pytest.raises(ScoutError):
        candidates_from_leaderboard("https://lb.test", session=session)


class FakeDataClient:
    def __init__(self, global_trades=None, tapes=None, positions=None, errors=None):
        self.global_trades = global_trades or []
        self.tapes = {k.lower(): v for k, v in (tapes or {}).items()}
        self.positions = {k.lower(): v for k, v in (positions or {}).items()}
        self.errors = {k.lower() for k in (errors or [])}

    def get_recent_trades(self, *, limit=100, offset=0, taker_only=True):
        return self.global_trades[offset:offset + limit]

    def get_trades(self, user, *, limit=500, offset=0, **kwargs):
        if user.lower() in self.errors:
            raise DataApiError("boom")
        return self.tapes.get(user.lower(), [])[offset:offset + limit]

    def get_positions(self, user, **kwargs):
        return self.positions.get(user.lower(), [])


def make_trade(wallet, side, size, price, ts, asset="tok1"):
    return Trade(proxy_wallet=wallet, side=side, asset=asset,
                 condition_id=f"0xc-{asset}", size=size, price=price,
                 timestamp=ts, transaction_hash=f"0x{wallet[-4:]}{ts}")


def winner_tape(wallet):
    tape = []
    for i in range(12):
        base = NOW - (12 - i) * DAY
        tape.append(make_trade(wallet, "BUY", 500, 0.40, base, asset=f"tok{i}"))
        tape.append(make_trade(wallet, "SELL", 500, 0.55, min(base + DAY, NOW - 3600),
                               asset=f"tok{i}"))
    return tape


def test_candidates_from_recent_trades_ranked_by_notional():
    global_trades = [
        make_trade(A1, "BUY", 10, 0.5, NOW),        # $5
        make_trade(A2, "BUY", 1000, 0.5, NOW - 1),  # $500
        make_trade(A1, "BUY", 20, 0.5, NOW - 2),    # A1 共 $15
        make_trade(A3, "BUY", 100, 0.5, NOW - 3),   # $50
    ]
    client = FakeDataClient(global_trades=global_trades)
    assert candidates_from_recent_trades(client, pages=1, top=2) == [A2, A3]


def test_scout_addresses_ranks_and_reports_failures():
    client = FakeDataClient(
        tapes={A1: winner_tape(A1), A2: []},
        positions={A1: [Position(proxy_wallet=A1, asset="tokX", condition_id="0xc",
                                 size=100, avg_price=0.4, cur_price=0.5)]},
        errors=[A3],
    )
    config = ScoutConfig(request_delay_s=0.0)
    verdicts = scout_addresses(client, [A2, A3, A1], config=config, now=NOW)
    assert [v.address for v in verdicts][0] == A1  # 合格者排最前
    assert verdicts[0].eligible and verdicts[0].exposure_usdc == 40.0
    by_addr = {v.address: v for v in verdicts}
    assert any("样本不足" in r for r in by_addr[A2].reasons)
    assert any("数据拉取失败" in r for r in by_addr[A3].reasons)


def test_targets_snippet_only_eligible_top_n():
    client = FakeDataClient(tapes={A1: winner_tape(A1), A2: []})
    verdicts = scout_addresses(client, [A1, A2],
                               config=ScoutConfig(request_delay_s=0.0), now=NOW)
    snippet = targets_snippet(verdicts, top=5)
    assert A1 in snippet and A2 not in snippet
    import json
    parsed = json.loads(snippet)
    assert parsed["targets"][0]["address"] == A1
    assert parsed["targets"][0]["ratio"] == 0.1
