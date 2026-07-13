"""启动自检：ClobReadClient.ping / DataApiClient.ping 与自检文案。"""

import requests

from polycopycat.cli import _preflight_lines
from polycopycat.data_api import DataApiClient
from polycopycat.engine.clob import ClobReadClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

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
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---- CLOB ping ----

def test_clob_ping_ok_200():
    client = ClobReadClient("https://clob.test", session=FakeSession([FakeResponse(200)]))
    ok, msg = client.ping()
    assert ok and "可达" in msg


def test_clob_ping_reachable_on_404_json():
    # 健康 CLOB 但 /ok 不存在 → 404，非拦截页，仍算可达（不误报）
    client = ClobReadClient("https://clob.test", session=FakeSession([FakeResponse(404)]))
    ok, _ = client.ping()
    assert ok


def test_clob_ping_detects_gateway_block():
    resp = FakeResponse(403, headers={"content-type": "text/html; charset=utf-8", "Server": "feilian-agw"})
    client = ClobReadClient("https://clob.test", session=FakeSession([resp]))
    ok, msg = client.ping()
    assert not ok
    assert "被拦截" in msg and "feilian-agw" in msg


def test_clob_ping_connection_error():
    client = ClobReadClient(
        "https://clob.test",
        session=FakeSession([requests.ConnectionError("boom")]),
    )
    ok, msg = client.ping()
    assert not ok and "连接失败" in msg


def test_clob_ping_server_error():
    client = ClobReadClient("https://clob.test", session=FakeSession([FakeResponse(502)]))
    ok, msg = client.ping()
    assert not ok and "502" in msg


# ---- Data API ping ----

def test_data_ping_ok():
    client = DataApiClient("https://data.test", session=FakeSession([FakeResponse(200, payload=[])]))
    ok, msg = client.ping()
    assert ok and msg == "可达"


def test_data_ping_failure():
    client = DataApiClient(
        "https://data.test",
        session=FakeSession([FakeResponse(403, headers={"content-type": "text/html"})] * 3),
        backoff=0.0,
    )
    ok, _ = client.ping()
    assert not ok


# ---- 自检文案 ----

def test_preflight_lines_all_ok():
    lines = _preflight_lines(True, "可达", True, "可达（HTTP 200）")
    text = "\n".join(lines)
    assert "就绪" in text
    assert "⚠️" not in text


def test_preflight_lines_clob_blocked_warns_loudly():
    lines = _preflight_lines(True, "可达", False, "被拦截 HTTP 403（网关 feilian-agw）")
    text = "\n".join(lines)
    assert "CLOB 不可达" in text
    assert "无法执行" in text
    assert "云服务器" in text  # 给出解法


def test_preflight_lines_data_down():
    lines = _preflight_lines(False, "连接失败", True, "可达")
    text = "\n".join(lines)
    assert "Data API 不可达" in text
