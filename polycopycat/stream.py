"""Polymarket 实时成交推送（WebSocket），把发现新下单的时延压到秒内。

数据源是 Polymarket 的实时数据服务（官网活动流同款）：

    wss://ws-live-data.polymarket.com

订阅 ``activity/trades`` 主题后，全站每笔成交都会实时推过来，字段与
Data API ``/trades`` 一致，本模块在客户端按监控地址过滤。协议入口若有
变动，可用参数或环境变量 ``POLYCOPYCAT_WS_URL`` 覆盖。

警告：不要给订阅带 ``filters`` 字段做服务端过滤。2026-07 实测（camelCase/
snake_case、JSON 字符串/对象四种形态）：一旦携带 filters，服务端会静默
不推任何消息——实时通道整个失效且无报错。全站流 + 客户端过滤是唯一
可靠契约。

推送链路可能断线丢事件，所以设计上永远搭配轮询兜底使用（见 CLI 的
``watch --stream`` 混合模式）：断线重连成功后回调 ``on_gap``，由外部
立即触发一次对账轮询补漏。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Callable, Iterable

import websocket

from .data_api import normalize_address
from .models import Trade

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://ws-live-data.polymarket.com"
ENV_WS_URL = "POLYCOPYCAT_WS_URL"

_INITIAL_BACKOFF = 1.0


class TradeStream:
    """订阅实时成交流，命中监控地址时回调 ``on_trade``（在后台线程执行）。"""

    def __init__(
        self,
        addresses: Iterable[str],
        *,
        on_trade: Callable[[Trade], None],
        on_gap: Callable[[], None] | None = None,
        ws_url: str | None = None,
        topic: str = "activity",
        msg_type: str = "trades",
        filters: str | None = None,
        ping_interval: float = 5.0,
        max_backoff: float = 30.0,
    ) -> None:
        self._addresses = {normalize_address(a) for a in addresses}
        if not self._addresses:
            raise ValueError("至少需要一个监控地址")
        self._on_trade = on_trade
        self._on_gap = on_gap
        self.ws_url = ws_url or os.environ.get(ENV_WS_URL) or DEFAULT_WS_URL
        self.topic = topic
        self.msg_type = msg_type
        self.filters = filters
        self.ping_interval = float(ping_interval)
        self.max_backoff = float(max_backoff)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app: websocket.WebSocketApp | None = None
        self._backoff = _INITIAL_BACKOFF
        self._connected_once = False

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("stream 已经启动过")
        self._thread = threading.Thread(
            target=self._run, name="polycopycat-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不影响退出
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---- 连接与重连 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            app = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=lambda _app, exc: logger.debug("实时连接报错: %s", exc),
            )
            self._app = app
            try:
                # 协议层 ping 探测死链；应用层 "ping" 在 _ping_loop 里按服务端要求发
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 —— 任何异常都走重连
                logger.debug("实时连接异常退出: %s", exc)
            if self._stop.is_set():
                break
            delay = self._next_backoff()
            logger.warning("实时连接断开，%.1f 秒后重连", delay)
            if self._stop.wait(delay):
                break

    def _next_backoff(self) -> float:
        delay = self._backoff
        self._backoff = min(self._backoff * 2, self.max_backoff)
        return delay

    def _handle_open(self, app: websocket.WebSocketApp) -> None:
        subscription: dict = {"topic": self.topic, "type": self.msg_type}
        if self.filters:
            subscription["filters"] = self.filters
        app.send(json.dumps({"action": "subscribe", "subscriptions": [subscription]}))
        threading.Thread(
            target=self._ping_loop, args=(app,),
            name="polycopycat-stream-ping", daemon=True,
        ).start()
        self._backoff = _INITIAL_BACKOFF
        if self._connected_once:
            logger.info("实时连接已恢复，触发一次对账轮询补漏")
            if self._on_gap is not None:
                self._on_gap()
        else:
            self._connected_once = True
            logger.info("实时成交流已连接: %s", self.ws_url)

    def _ping_loop(self, app: websocket.WebSocketApp) -> None:
        # 服务端要求客户端周期性发应用层 ping，长时间不发会被断开
        while not self._stop.is_set() and app.keep_running:
            try:
                app.send("ping")
            except Exception:  # noqa: BLE001 —— 连接已死，交给重连逻辑
                return
            if self._stop.wait(self.ping_interval):
                return

    # ---- 消息解析 ----

    def _handle_message(self, _app: websocket.WebSocketApp, raw) -> None:
        for trade in self._extract_trades(raw):
            try:
                self._on_trade(trade)
            except Exception:  # noqa: BLE001 —— 回调炸了不能拖垮接收循环
                logger.exception("处理实时成交回调失败: %s", trade)

    def _extract_trades(self, raw) -> list[Trade]:
        """把一条 WS 消息解析成命中监控地址的成交列表，其余消息忽略。"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        text = (raw or "").strip()
        if not text or text.lower() in ("ping", "pong"):
            return []
        try:
            message = json.loads(text)
        except ValueError:
            logger.debug("忽略无法解析的实时消息: %.120s", text)
            return []
        items = message if isinstance(message, list) else [message]
        trades: list[Trade] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic")
            msg_type = item.get("type")
            if topic is not None and topic != self.topic:
                continue
            if msg_type is not None and msg_type != self.msg_type:
                continue
            # 标准消息带 payload 包壳；也兼容直接推裸成交对象的形态
            payload = item.get("payload", item)
            for entry in payload if isinstance(payload, list) else [payload]:
                if not isinstance(entry, dict) or "proxyWallet" not in entry:
                    continue
                trade = Trade.from_api(entry)
                if trade.proxy_wallet in self._addresses:
                    trades.append(trade)
        return trades
