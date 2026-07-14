"""通知：跟单动作与风控事件的出口。

默认打日志；配置了 Telegram（bot token 环境变量 + chat_id）或 Discord
（频道 webhook URL 环境变量）则同时推送。通知失败绝不能影响交易主流程，
所以 send 内部吞掉一切异常。
"""

from __future__ import annotations

import logging
import os

import requests

from .config import NotifyConfig

logger = logging.getLogger(__name__)


class Notifier:
    def send(self, text: str) -> None:  # pragma: no cover - 接口定义
        raise NotImplementedError


class LogNotifier(Notifier):
    def send(self, text: str) -> None:
        logger.info("[通知] %s", text)


class TelegramNotifier(Notifier):
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._session = session or requests.Session()
        self._timeout = timeout

    def send(self, text: str) -> None:
        try:
            response = self._session.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                logger.warning(
                    "Telegram 通知失败 HTTP %d: %.200s",
                    response.status_code, getattr(response, "text", ""),
                )
        except Exception as exc:  # noqa: BLE001 —— 通知失败不能影响交易
            logger.warning("Telegram 通知失败: %s", exc)


class DiscordNotifier(Notifier):
    """推到 Discord 频道 webhook（无需 bot / token，频道设置里建一个即可）。

    webhook URL 形如 https://discord.com/api/webhooks/<id>/<token>，本身是
    半机密（拿到就能往频道发消息），所以配置里存的是环境变量名，不是 URL。
    """

    # Discord content 上限 2000 字符，留些余量并加省略号
    _MAX = 1900

    def __init__(
        self,
        webhook_url: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = webhook_url
        self._session = session or requests.Session()
        self._timeout = timeout

    def send(self, text: str) -> None:
        content = text if len(text) <= self._MAX else text[: self._MAX - 1] + "…"
        try:
            response = self._session.post(
                self._url, json={"content": content}, timeout=self._timeout
            )
            # webhook 成功返回 204 No Content；带 ?wait=true 时返回 200
            if response.status_code not in (200, 204):
                logger.warning(
                    "Discord 通知失败 HTTP %d: %.200s",
                    response.status_code, getattr(response, "text", ""),
                )
        except Exception as exc:  # noqa: BLE001 —— 通知失败不能影响交易
            logger.warning("Discord 通知失败: %s", exc)


class CompositeNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def send(self, text: str) -> None:
        for notifier in self._notifiers:
            notifier.send(text)


def build_notifier(config: NotifyConfig) -> Notifier:
    notifiers: list[Notifier] = [LogNotifier()]
    if config.telegram_bot_token_env and config.telegram_chat_id:
        token = os.environ.get(config.telegram_bot_token_env, "").strip()
        if token:
            notifiers.append(TelegramNotifier(token, config.telegram_chat_id))
            logger.info("Telegram 通知已启用")
        else:
            logger.warning(
                "配置了 telegram_bot_token_env=%s 但该环境变量为空，只用日志通知",
                config.telegram_bot_token_env,
            )
    if config.discord_webhook_url_env:
        url = os.environ.get(config.discord_webhook_url_env, "").strip()
        if url:
            notifiers.append(DiscordNotifier(url))
            logger.info("Discord 通知已启用")
        else:
            logger.warning(
                "配置了 discord_webhook_url_env=%s 但该环境变量为空，跳过 Discord 通知",
                config.discord_webhook_url_env,
            )
    return notifiers[0] if len(notifiers) == 1 else CompositeNotifier(notifiers)
