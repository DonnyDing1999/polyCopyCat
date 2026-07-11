import json

from polycopycat.models import Trade

SAMPLE_RAW = {
    "proxyWallet": "0x9D84CE0306F8551E02EFEF1680475FC0F1DC1344",
    "side": "buy",
    "asset": "71639258321432544966257054027683589342857315291706293935537123123456789",
    "conditionId": "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
    "size": "100.5",
    "price": 0.45,
    "timestamp": "1719400000",
    "title": "Will X happen by June 30?",
    "slug": "will-x-happen",
    "icon": "https://example.com/icon.png",
    "eventSlug": "x-event",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "name": "some-trader",
    "pseudonym": "Quick-Fox",
    "transactionHash": "0xabc123",
    "someFutureField": {"nested": True},
}


def test_from_api_parses_and_normalizes():
    trade = Trade.from_api(SAMPLE_RAW)
    assert trade.proxy_wallet == SAMPLE_RAW["proxyWallet"].lower()
    assert trade.side == "BUY"
    assert trade.size == 100.5
    assert trade.price == 0.45
    assert trade.timestamp == 1719400000
    assert trade.outcome == "Yes"
    assert trade.outcome_index == 0
    assert trade.transaction_hash == "0xabc123"
    assert trade.trader_name == "some-trader"
    assert trade.notional == 100.5 * 0.45
    assert trade.time_utc == "2024-06-26T11:06:40Z"


def test_from_api_tolerates_missing_fields():
    trade = Trade.from_api({})
    assert trade.side == ""
    assert trade.size == 0.0
    assert trade.price == 0.0
    assert trade.timestamp == 0
    assert trade.outcome_index == -1
    assert trade.notional == 0.0


def test_key_distinguishes_fills_in_same_tx():
    base = Trade.from_api(SAMPLE_RAW)
    other = Trade.from_api({**SAMPLE_RAW, "price": 0.46})
    assert base.key != other.key
    assert base.key == Trade.from_api(dict(SAMPLE_RAW)).key


def test_to_dict_is_json_serializable():
    payload = Trade.from_api(SAMPLE_RAW).to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert "notional" in payload and "time_utc" in payload
    assert "Will X happen" in text
