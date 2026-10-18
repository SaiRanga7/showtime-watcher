"""Alert channels: Telegram, Gmail (SMTP) and ntfy. Each is used only if its secrets exist."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

import httpx

from showtime_watcher.config import Settings

log = logging.getLogger(__name__)

# ntfy priority per alert kind: 5 = max (long buzz, pop-over, can ring through DND on
# Android if you allow it for that priority), 3 = default, 2 = low.
NTFY_PRIORITY = {"open": 5, "more": 4, "blind": 4, "test": 4, "partial": 3, "recovered": 3}
# Which channels get which kind. Anything not listed goes everywhere.
ROUTES = {"daily": {"telegram"}}


@dataclass
class Alert:
    kind: str  # open | more | partial | blind | recovered | daily | test
    title: str  # one line, e.g. "🚨 Bookings OPEN · Jailer 2"
    body: str  # plain text, may span lines
    url: str | None = None  # main link (cinema page for the date)
    links: list[tuple[str, str]] = field(default_factory=list)  # (label, url) quick-book buttons
    silent: bool = False  # Telegram: deliver without sound


class Channel(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = client

    def send(self, alert: Alert) -> None:
        text = f"<b>{html.escape(alert.title)}</b>\n{html.escape(alert.body)}"
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": alert.silent,
        }
        buttons = [{"text": label[:60], "url": url} for label, url in alert.links[:10]]
        if alert.url:
            buttons.append({"text": "🎟 All shows on BookMyShow", "url": alert.url})
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[b] for b in buttons]}
        send_telegram(self._client, self._url, payload)


def send_telegram(client: httpx.Client, url: str, payload: dict[str, object]) -> None:
    """POST to the Bot API; raise RuntimeError with Telegram's reason (never the URL/token)."""
    try:
        resp = client.post(url, json=payload, timeout=20)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Telegram unreachable: {type(exc).__name__}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram refused: {data.get('description', resp.status_code)}")


class EmailChannel:
    name = "email"

    def __init__(self, address: str, app_password: str, to: str) -> None:
        self._address = address
        self._password = app_password.replace(" ", "")  # Google shows it as "abcd efgh ..."
        self._to = to

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = alert.title
        msg["From"] = f"Showtime Watcher <{self._address}>"
        msg["To"] = self._to
        links = "\n".join(f"{label}: {url}" for label, url in alert.links)
        main = f"\nAll shows: {alert.url}" if alert.url else ""
        msg.set_content(f"{alert.body}\n\n{links}{main}\n")
        msg.add_alternative(_email_html(alert), subtype="html")
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)


def _email_html(alert: Alert) -> str:
    body = html.escape(alert.body).replace("\n", "<br>")
    buttons = "".join(
        f'<p style="margin:6px 0"><a href="{html.escape(url)}" style="background:#f84464;'
        f'color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none">'
        f"{html.escape(label)}</a></p>"
        for label, url in alert.links
    )
    main = (
        f'<p><a href="{html.escape(alert.url)}">All shows on BookMyShow</a></p>'
        if alert.url
        else ""
    )
    return (
        '<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5">'
        f"<h2 style='margin:0 0 8px'>{html.escape(alert.title)}</h2>"
        f"<p>{body}</p>{buttons}{main}"
        "<p style='color:#888;font-size:12px'>Sent by your showtime-watcher on GitHub Actions</p>"
        "</div>"
    )


class NtfyChannel:
    name = "ntfy"

    def __init__(self, server: str, topic: str, token: str | None, client: httpx.Client) -> None:
        self._server = server.rstrip("/")
        self._topic = topic
        self._token = token
        self._client = client

    def send(self, alert: Alert) -> None:
        # JSON publishing (POST to the server root) keeps emoji/UTF-8 safe in the title.
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": alert.title,
            "message": alert.body[:3500],
            "priority": NTFY_PRIORITY.get(alert.kind, 3),
        }
        if alert.url:
            payload["click"] = alert.url
        actions = [
            {"action": "view", "label": label[:40], "url": url} for label, url in alert.links
        ]
        if actions:
            payload["actions"] = actions[:3]  # ntfy allows at most 3 buttons
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = self._client.post(self._server, json=payload, headers=headers, timeout=20)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"ntfy unreachable: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"ntfy refused: HTTP {resp.status_code} {resp.text[:200]}")


class Notifier:
    """Fans an alert out to every configured channel; one failing channel never blocks others."""

    def __init__(self, channels: list[Channel], settings: Settings, *, dry_run: bool = False):
        self.channels = channels
        self._settings = settings
        self._dry_run = dry_run

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.channels]

    def send(self, alert: Alert) -> list[str]:
        """Returns the names of channels that delivered it."""
        allowed = ROUTES.get(alert.kind)
        chosen = [c for c in self.channels if allowed is None or c.name in allowed]
        if self._dry_run:
            log.info("[dry-run] %s via %s: %s", alert.kind, [c.name for c in chosen], alert.title)
            return [c.name for c in chosen]
        delivered: list[str] = []
        for channel in chosen:
            try:
                channel.send(alert)
                delivered.append(channel.name)
            except Exception as exc:
                log.error("%s alert failed: %s", channel.name, self._settings.redact(str(exc)))
        log.info("Alert '%s' delivered via %s", alert.title, delivered or "nothing")
        return delivered


def build_notifier(settings: Settings, client: httpx.Client, *, dry_run: bool = False) -> Notifier:
    channels: list[Channel] = []
    if settings.telegram_token and settings.telegram_chat_id:
        channels.append(TelegramChannel(settings.telegram_token, settings.telegram_chat_id, client))
    if settings.gmail_address and settings.gmail_app_password:
        to = settings.email_to or settings.gmail_address
        channels.append(EmailChannel(settings.gmail_address, settings.gmail_app_password, to))
    if settings.ntfy_topic:
        channels.append(
            NtfyChannel(settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token, client)
        )
    if not channels:
        log.warning("No alert channel configured: set TELEGRAM_*, GMAIL_* or NTFY_TOPIC secrets")
    return Notifier(channels, settings, dry_run=dry_run)
