import pytest

pytest.importorskip("py_clob_client")

from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions  # noqa: E402

from polycopycat.engine.config import EngineConfig  # noqa: E402
from polycopycat.engine.live import LiveExecutor  # noqa: E402
from polycopycat.engine.signals import OrderIntent  # noqa: E402

ADDR = "0x" + "a" * 40


def make_config(**live_overrides):
    live = {"i_understand_live_trading_risk": True}
    live.update(live_overrides)
    return EngineConfig.from_dict(
        {"mode": "live", "targets": [{"address": ADDR}], "live": live}
    )


def intent(**overrides):
    kwargs = dict(
        token_id="tok1", condition_id="0xcond", side="BUY", limit_price=0.52,
        size=48.07, ref_price=0.50, neg_risk=True, tick_size=0.01,
        title="T", outcome="Yes",
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


class FakeClobClient:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else {
            "success": True, "orderID": "0xoid", "status": "matched",
        }
        self.error = error
        self.created = []
        self.posted = []

    def create_order(self, order_args, options=None):
        if self.error:
            raise self.error
        self.created.append((order_args, options))
        return {"signed": True}

    def post_order(self, order, order_type=OrderType.GTC, post_only=False):
        self.posted.append((order, order_type))
        return self.response


def test_execute_maps_intent_to_fak_order():
    client = FakeClobClient()
    executor = LiveExecutor(make_config(), client=client)
    result = executor.execute(intent())

    order_args, options = client.created[0]
    assert isinstance(order_args, OrderArgs)
    assert order_args.token_id == "tok1"
    assert order_args.price == 0.52
    assert order_args.size == 48.07
    assert order_args.side == "BUY"
    assert isinstance(options, PartialCreateOrderOptions)
    assert options.neg_risk is True
    assert options.tick_size == "0.01"
    assert client.posted[0][1] == OrderType.FAK
    assert result.status == "submitted"
    assert "0xoid" in result.detail
    assert executor.applies_fills is False  # 实盘成交以对账为准


def test_unusual_tick_size_left_to_client():
    client = FakeClobClient()
    LiveExecutor(make_config(), client=client).execute(intent(tick_size=0.005))
    _, options = client.created[0]
    assert options.tick_size is None


def test_rejection_becomes_error_result():
    client = FakeClobClient(response={"success": False, "errorMsg": "not enough balance"})
    result = LiveExecutor(make_config(), client=client).execute(intent())
    assert result.status == "error"
    assert "not enough balance" in result.detail


def test_exception_becomes_error_result():
    client = FakeClobClient(error=RuntimeError("boom"))
    result = LiveExecutor(make_config(), client=client).execute(intent())
    assert result.status == "error"
    assert "boom" in result.detail


def test_build_refuses_without_risk_ack(monkeypatch):
    monkeypatch.setenv("POLYCOPYCAT_PRIVATE_KEY", "0x" + "11" * 32)
    with pytest.raises(RuntimeError, match="i_understand_live_trading_risk"):
        LiveExecutor(make_config(i_understand_live_trading_risk=False))


def test_build_requires_private_key_env(monkeypatch):
    monkeypatch.delenv("POLYCOPYCAT_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="私钥"):
        LiveExecutor(make_config())


def test_proxy_signature_requires_funder(monkeypatch):
    monkeypatch.setenv("POLYCOPYCAT_PRIVATE_KEY", "0x" + "11" * 32)
    with pytest.raises(RuntimeError, match="funder"):
        LiveExecutor(make_config(signature_type=2, funder=""))
