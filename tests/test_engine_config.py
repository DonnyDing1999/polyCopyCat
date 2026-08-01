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


def test_live_config_loads_without_ack():
    # 风险确认在启动实盘执行器时校验（这样 --paper 保险丝仍可用），配置阶段只解析
    config = EngineConfig.from_dict(minimal(mode="live"))
    assert config.mode == "live"
    assert config.live.i_understand_live_trading_risk is False


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.json")


def test_broken_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="读取配置"):
        load_config(path)


def test_recruit_blocklist_normalized():
    config = EngineConfig.from_dict(minimal(health={"recruit_blocklist": ["0x" + "B" * 40]}))
    assert config.health.recruit_blocklist == ["0x" + "b" * 40]


def test_recruit_blocklist_rejects_bare_string():
    with pytest.raises(ConfigError, match="地址数组"):
        EngineConfig.from_dict(minimal(health={"recruit_blocklist": ADDR}))


def test_recruit_gates_defaults_and_validation():
    health = EngineConfig.from_dict(minimal()).health
    assert health.recruit_min_score == 70.0 and health.recruit_max_per_round == 3
    # ≤0 各有含义：门槛 0 = 不设门槛，每轮上限 ≤0 = 不限量，都不该被夹成正数
    health = EngineConfig.from_dict(
        minimal(health={"recruit_min_score": 0, "recruit_max_per_round": -1})
    ).health
    assert health.recruit_min_score == 0.0 and health.recruit_max_per_round == -1
    with pytest.raises(ConfigError, match="recruit_min_score"):
        EngineConfig.from_dict(minimal(health={"recruit_min_score": "高一点"}))
    with pytest.raises(ConfigError, match="recruit_max_per_round"):
        EngineConfig.from_dict(minimal(health={"recruit_max_per_round": None}))


def test_unknown_keys_only_warn(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(minimal(unknown_top=1, risk={"whatever": 2})), encoding="utf-8")
    config = load_config(path)  # 不应抛错
    assert config.risk.max_total_exposure_usdc == 1000.0


# ---- 事件族敞口闸的配置校验 ----

def test_exposure_groups_normalized():
    config = EngineConfig.from_dict(minimal(risk={"exposure_groups": [
        {"name": "伊朗", "patterns": ["Iran", "HORMUZ", "  "], "max_usdc": 80},
    ]}))
    assert config.risk.exposure_groups == [
        {"name": "伊朗", "patterns": ["iran", "hormuz"], "max_usdc": 80.0}
    ]
    assert EngineConfig.from_dict(minimal()).risk.exposure_groups == []   # 默认不启用


@pytest.mark.parametrize("group, match", [
    ({"patterns": ["iran"], "max_usdc": 80}, "name"),                     # 缺 name
    ({"name": "  ", "patterns": ["iran"], "max_usdc": 80}, "name"),       # name 全空白
    ({"name": "伊朗", "max_usdc": 80}, "patterns"),                       # 缺 patterns
    ({"name": "伊朗", "patterns": [], "max_usdc": 80}, "patterns"),       # 空数组
    ({"name": "伊朗", "patterns": "iran", "max_usdc": 80}, "patterns"),   # 写成裸字符串
    ({"name": "伊朗", "patterns": ["iran"]}, "max_usdc"),                 # 缺上限
    ({"name": "伊朗", "patterns": ["iran"], "max_usdc": None}, "max_usdc"),
    ({"name": "伊朗", "patterns": ["iran"], "max_usdc": 0}, "max_usdc"),  # 非正数
])
def test_exposure_groups_reject_bad_structure(group, match):
    # 写错的敞口闸等于没有闸，必须报错而不是静默忽略
    with pytest.raises(ConfigError, match=match):
        EngineConfig.from_dict(minimal(risk={"exposure_groups": [group]}))


def test_exposure_groups_reject_non_object_entries():
    with pytest.raises(ConfigError, match="应为对象"):
        EngineConfig.from_dict(minimal(risk={"exposure_groups": ["iran"]}))
    with pytest.raises(ConfigError, match="应为数组"):
        EngineConfig.from_dict(minimal(risk={"exposure_groups": {"name": "伊朗"}}))


# ---- 零跟单率解聘的配置校验 ----

def test_recruit_dismiss_defaults_and_validation():
    health = EngineConfig.from_dict(minimal()).health
    assert health.recruit_dismiss_min_signals == 50
    assert health.recruit_dismiss_min_hours == 24.0
    assert health.recruit_dismiss_cooldown_days == 14.0
    # ≤0 = 关闭本机制，不该被夹成正数
    assert EngineConfig.from_dict(
        minimal(health={"recruit_dismiss_min_signals": 0})
    ).health.recruit_dismiss_min_signals == 0
    with pytest.raises(ConfigError, match="recruit_dismiss_min_signals"):
        EngineConfig.from_dict(minimal(health={"recruit_dismiss_min_signals": "很多"}))
    with pytest.raises(ConfigError, match="recruit_dismiss_min_hours"):
        EngineConfig.from_dict(minimal(health={"recruit_dismiss_min_hours": 0}))
    with pytest.raises(ConfigError, match="recruit_dismiss_cooldown_days"):
        EngineConfig.from_dict(minimal(health={"recruit_dismiss_cooldown_days": None}))
