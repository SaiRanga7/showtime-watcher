"""What each channel actually sends (HTTP calls are intercepted, nothing leaves the machine)."""

from __future__ import annotations

import json
import logging
from typing import ClassVar

import httpx
import pytest
from conftest import RecordingChannel, make_settings

from showtime_watcher import notify
from showtime_watcher.notify import Alert, EmailChannel, Notifier, NtfyChannel, TelegramChannel

ALERT = Alert(
    "open",
    "🚨 Bookings OPEN: A <B> & C",
    "📅 Fri 30 Oct\n • 07:55 PM · SCREEN 1",
    url="https://in.bookmyshow.com/x",
    links=[(f"show {i}", f"https://in.bookmyshow.com/s/{i}") for i in range(5)],
)


def _client(responses: list[dict], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_telegram_payload() -> None:
    seen: list[httpx.Request] = []
    TelegramChannel("TOKEN123", "42", _client([{"ok": True}], seen)).send(ALERT)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/botTOKEN123/sendMessage"
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert body["text"].startswith("<b>🚨 Bookings OPEN: A &lt;B&gt; &amp; C</b>")
    buttons = [row[0] for row in body["reply_markup"]["inline_keyboard"]]
    assert buttons[0] == {"text": "show 0", "url": "https://in.bookmyshow.com/s/0"}
    assert buttons[-1]["url"] == "https://in.bookmyshow.com/x"


def test_telegram_error_hides_token() -> None:
    seen: list[httpx.Request] = []
    channel = TelegramChannel(
        "TOKEN123", "42", _client([{"ok": False, "description": "Bad"}], seen)
    )
    with pytest.raises(RuntimeError) as info:
        channel.send(ALERT)
    assert "Bad" in str(info.value) and "TOKEN123" not in str(info.value)


def test_ntfy_payload() -> None:
    seen: list[httpx.Request] = []
    NtfyChannel("https://ntfy.sh/", "my-topic", None, _client([{}], seen)).send(ALERT)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://ntfy.sh"
    assert body["topic"] == "my-topic" and body["priority"] == 5
    assert body["title"] == ALERT.title and body["click"] == ALERT.url
    assert len(body["actions"]) == 3  # ntfy's limit


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, **_kwargs) -> None:
        self.host, self.port = host, port
        self.login_args: tuple[str, str] | None = None
        self.messages: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_exc) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg) -> None:
        self.messages.append(msg)


def test_email_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    EmailChannel("me@gmail.com", "abcd efgh ijkl mnop", "me@gmail.com").send(ALERT)
    smtp = FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
    assert smtp.login_args == ("me@gmail.com", "abcdefghijklmnop")
    msg = smtp.messages[0]
    assert msg["Subject"] == ALERT.title
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    assert "https://in.bookmyshow.com/s/0" in html_part and "&lt;B&gt;" in html_part


def test_routing_and_isolation(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(tmp_path, telegram_token="SECRET-TOKEN")
    broken = RecordingChannel("email", fail=True)
    telegram = RecordingChannel("telegram")
    notifier = Notifier([broken, telegram], settings)

    class Leaky:
        name = "ntfy"

        def send(self, _alert: Alert) -> None:
            raise RuntimeError("failed calling https://api/botSECRET-TOKEN/x")

    notifier.channels.append(Leaky())
    with caplog.at_level(logging.ERROR):
        assert notifier.send(ALERT) == ["telegram"]  # one broken channel doesn't stop others
    assert "SECRET-TOKEN" not in caplog.text and "***" in caplog.text

    notifier.send(Alert("daily", "check-in", "all good", silent=True))
    assert [a.kind for a in telegram.sent] == ["open", "daily"]
