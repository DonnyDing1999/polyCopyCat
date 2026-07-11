"""通知：跟单动作与风控事件的出口。

M0 先提供日志通知；Telegram 等外部渠道在 M1 接入。
通知失败绝不能影响交易主流程，所以 send 内部吞掉一切异常。
"""

from __future__ import annotations

import logging

from .config import NotifyConfig

logger = logging.getLogger(__name__)


class Notifier:
    def send(self, text: str) -> None:  # pragma: no cover - 接口定义
        raise NotImplementedError


class LogNotifier(Notifier):
    def send(self, text: str) -> None:
        logger.info("[通知] %s", text)


def build_notifier(config: NotifyConfig) -> Notifier:
    return LogNotifier()
