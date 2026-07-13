"""use_os_trust_store：公司 TLS 中间人环境下改用系统信任库。"""

import sys
import types

from polycopycat.cli import use_os_trust_store


def test_noop_when_truststore_absent(monkeypatch):
    # 模拟未安装 truststore：import 抛 ImportError
    monkeypatch.delenv("POLYCOPYCAT_NO_TRUSTSTORE", raising=False)
    monkeypatch.setitem(sys.modules, "truststore", None)  # import 得到 None → 触发 ImportError 分支
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **k):
        if name == "truststore":
            raise ImportError("no truststore")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert use_os_trust_store() is False


def test_injects_when_truststore_present(monkeypatch):
    monkeypatch.delenv("POLYCOPYCAT_NO_TRUSTSTORE", raising=False)
    called = {"n": 0}
    fake = types.ModuleType("truststore")
    fake.inject_into_ssl = lambda: called.__setitem__("n", called["n"] + 1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert use_os_trust_store() is True
    assert called["n"] == 1


def test_env_var_disables(monkeypatch):
    monkeypatch.setenv("POLYCOPYCAT_NO_TRUSTSTORE", "1")
    # 即便装了 truststore，显式关闭也不注入
    fake = types.ModuleType("truststore")
    fake.inject_into_ssl = lambda: (_ for _ in ()).throw(AssertionError("不该被调用"))
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert use_os_trust_store() is False
