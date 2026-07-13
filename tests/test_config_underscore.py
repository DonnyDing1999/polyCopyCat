import logging
from polycopycat.engine.config import EngineConfig

ADDR = "0x" + "a" * 40

def test_underscore_keys_silent(caplog):
    raw = {
        "_comment": "顶层注释",
        "targets": [{"address": ADDR, "_note": "战绩注脚", "ratio": 0.1}],
        "sizing": {"_why": "说明", "ratio": 0.2},
    }
    with caplog.at_level(logging.WARNING):
        c = EngineConfig.from_dict(raw)
    assert c.targets[0].ratio == 0.1 and c.sizing.ratio == 0.2
    assert "_note" not in caplog.text and "_comment" not in caplog.text and "_why" not in caplog.text

def test_real_typo_still_warns(caplog):
    raw = {"targets": [{"address": ADDR, "ratioo": 0.1}]}  # 手滑多打个 o
    with caplog.at_level(logging.WARNING):
        EngineConfig.from_dict(raw)
    assert "ratioo" in caplog.text
