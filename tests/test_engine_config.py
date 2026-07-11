import json
from pathlib import Path

import pytest

from polycopycat.engine.config import ConfigError, EngineConfig, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"
ADDR = "0x" + "a" * 40


def minimal(**overrides):
    raw = {"targets": [{"address": ADDR}]}
    raw.update(overrides)
    return raw


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert config.mode == "paper"
    assert config.targets[0].ratio == 0.1
    assert config.sizing.max_per_trade_usdc == 100
    assert config.risk.kill_switch_file == "STOP"
    assert config.watch.stream is True


def test_defaults_from_minimal():
    config = EngineConfig.from_dict(minimal())
    assert config.mode == "paper"
    assert config.sizing.mode == "proportional"
    assert config.filters.follow_sells is True
    assert config.targets[0].address == ADDR


def test_requires_targets():
    with pytest.raises(ConfigError, match="至少要配置一个"):
        EngineConfig.from_dict({"targets": []})


def test_rejects_duplicate_targets():
    with pytest.raises(ConfigError, match="重复"):
        EngineConfig.from_dict({"targets": [{"address": ADDR}, {"address": ADDR.upper()}]})


def test_rejects_bad_mode_and_bad_numbers():
    with pytest.raises(ConfigError, match="mode"):
        EngineConfig.from_dict(minimal(mode="yolo"))
    with pytest.raises(ConfigError, match="slippage_cap"):
        EngineConfig.from_dict(minimal(execution={"slippage_cap": 0.9}))
    with pytest.raises(ConfigError, match="ratio"):
        EngineConfig.from_dict(minimal(sizing={"ratio": -1}))


def test_live_requires_explicit_risk_ack():
    with pytest.raises(ConfigError, match="i_understand_live_trading_risk"):
        EngineConfig.from_dict(minimal(mode="live"))
    config = EngineConfig.from_dict(
        minimal(mode="live", live={"i_understand_live_trading_risk": True})
    )
    assert config.mode == "live"


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.json")


def test_broken_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="读取配置"):
        load_config(path)


def test_unknown_keys_only_warn(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(minimal(unknown_top=1, risk={"whatever": 2})), encoding="utf-8")
    config = load_config(path)  # 不应抛错
    assert config.risk.max_total_exposure_usdc == 1000.0
