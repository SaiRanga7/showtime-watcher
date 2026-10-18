"""Settings, read from environment variables (GitHub secrets) or a local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

IST = ZoneInfo("Asia/Kolkata")


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    root: Path
    telegram_token: str | None
    telegram_chat_id: str | None
    gmail_address: str | None
    gmail_app_password: str | None
    email_to: str | None
    ntfy_topic: str | None
    ntfy_server: str
    ntfy_token: str | None
    daily_hour: int  # IST hour after which the once-a-day check-in is sent
    daily_message: bool  # send the check-in to Telegram (the keep-alive happens anyway)
    blind_after_minutes: int  # alert if every check has failed for this long
    github_actions: bool

    @property
    def targets_path(self) -> Path:
        return self.root / "targets.yaml"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs or error messages."""
        values = (self.telegram_token, self.gmail_app_password, self.ntfy_topic, self.ntfy_token)
        return [v for v in values if v]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text


def load_settings(root: Path | None = None) -> Settings:
    root = (root or Path.cwd()).resolve()
    # Real environment (GitHub secrets) wins; .env only fills gaps on your own machine.
    load_dotenv(root / ".env", override=False)
    return Settings(
        root=root,
        telegram_token=_str("TELEGRAM_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        gmail_address=_str("GMAIL_ADDRESS"),
        gmail_app_password=_str("GMAIL_APP_PASSWORD"),
        email_to=_str("EMAIL_TO"),
        ntfy_topic=_str("NTFY_TOPIC"),
        ntfy_server=_str("NTFY_SERVER") or "https://ntfy.sh",
        ntfy_token=_str("NTFY_TOKEN"),
        daily_hour=_int("DAILY_HOUR", 9),
        daily_message=_flag("DAILY_MESSAGE", True),
        blind_after_minutes=_int("BLIND_AFTER_MINUTES", 45),
        github_actions=_flag("GITHUB_ACTIONS", False),
    )
