from polycopycat.engine.config import NotifyConfig
from polycopycat.engine.notify import (
    CompositeNotifier,
    DiscordNotifier,
    LogNotifier,
    Notifier,
    TelegramNotifier,
    build_notifier,
)


class FakeSession:
    def __init__(self, error=None, status_code=200):
        self.error = error
        self.status_code = status_code
        self.posts = []

    def post(self, url, json=None, timeout=None):
        if self.error:
            raise self.error
        self.posts.append((url, json))

        class Resp:
            status_code = self.status_code
            text = "ok"

        return Resp()


def test_telegram_posts_message():
    session = FakeSession()
    TelegramNotifier("TOKEN", "42", session=session).send("你好")
    url, payload = session.posts[0]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert payload == {"chat_id": "42", "text": "你好"}


def test_telegram_failure_is_swallowed():
    TelegramNotifier("TOKEN", "42", session=FakeSession(error=OSError("net down"))).send("x")
    TelegramNotifier("TOKEN", "42", session=FakeSession(status_code=500)).send("x")  # 不抛


DISCORD_URL = "https://discord.com/api/webhooks/123/abc"


def test_discord_posts_content():
    session = FakeSession(status_code=204)  # webhook 成功返回 204
    DiscordNotifier(DISCORD_URL, session=session).send("跟单成交 $12.30")
    url, payload = session.posts[0]
    assert url == DISCORD_URL
    assert payload == {"content": "跟单成交 $12.30"}


def test_discord_truncates_long_content():
    session = FakeSession(status_code=204)
    DiscordNotifier(DISCORD_URL, session=session).send("x" * 5000)
    _, payload = session.posts[0]
    assert len(payload["content"]) <= 1900 and payload["content"].endswith("…")


def test_discord_failure_is_swallowed():
    DiscordNotifier(DISCORD_URL, session=FakeSession(error=OSError("net down"))).send("x")
    DiscordNotifier(DISCORD_URL, session=FakeSession(status_code=500)).send("x")  # 不抛


def test_composite_fans_out():
    class Collect(Notifier):
        def __init__(self):
            self.got = []

        def send(self, text):
            self.got.append(text)

    a, b = Collect(), Collect()
    CompositeNotifier([a, b]).send("msg")
    assert a.got == ["msg"] and b.got == ["msg"]


def test_build_notifier_log_only_by_default():
    assert isinstance(build_notifier(NotifyConfig()), LogNotifier)


def test_build_notifier_with_telegram(monkeypatch):
    monkeypatch.setenv("TG_TOKEN_TEST", "abc")
    notifier = build_notifier(
        NotifyConfig(telegram_bot_token_env="TG_TOKEN_TEST", telegram_chat_id="42")
    )
    assert isinstance(notifier, CompositeNotifier)


def test_build_notifier_missing_env_falls_back(monkeypatch):
    monkeypatch.delenv("TG_TOKEN_MISSING", raising=False)
    notifier = build_notifier(
        NotifyConfig(telegram_bot_token_env="TG_TOKEN_MISSING", telegram_chat_id="42")
    )
    assert isinstance(notifier, LogNotifier)


def test_build_notifier_with_discord(monkeypatch):
    monkeypatch.setenv("DISCORD_HOOK_TEST", DISCORD_URL)
    notifier = build_notifier(NotifyConfig(discord_webhook_url_env="DISCORD_HOOK_TEST"))
    assert isinstance(notifier, CompositeNotifier)


def test_build_notifier_discord_missing_env_falls_back(monkeypatch):
    monkeypatch.delenv("DISCORD_HOOK_MISSING", raising=False)
    notifier = build_notifier(NotifyConfig(discord_webhook_url_env="DISCORD_HOOK_MISSING"))
    assert isinstance(notifier, LogNotifier)
