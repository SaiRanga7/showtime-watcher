"""Persistent memory between runs (state.json, committed back to the repo by the workflow).

Only things that change *rarely* live here (a target opening, a show announced, the bot's
message offset, a daily keep-alive stamp). Per-run details go to the logs and the job
summary instead; otherwise every 5-minute run would create a git commit.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_VERSION = 2


@dataclass
class TargetState:
    status: str = "waiting"  # waiting | open | expired
    first_open_at: str | None = None  # ISO time bookings were first seen open
    notified: list[str] = field(default_factory=list)  # show keys already announced
    partial_alerted: bool = False  # told "on sale, but nothing matches your filters"
    note: str = ""  # last human-readable status (rewritten only when it changes)


@dataclass
class Health:
    failing_since: str | None = None  # ISO time checks started failing
    last_error: str = ""
    blind_alert_sent: bool = False
    last_blind_alert: str | None = None  # ISO time of the last "can't read BookMyShow" alert
    last_daily: str | None = None  # IST date of the last daily check-in / keep-alive
    # Reachability timeline: [ISO time, "ok" | "blocked" | "error"], one entry per *change*,
    # so it only costs a commit when access flips, not on every run.
    access_log: list[list[str]] = field(default_factory=list)

    def record_access(self, when: dt.datetime, status: str, keep: int = 200) -> bool:
        """Append a transition if ``status`` differs from the last one. Returns True if added."""
        if self.access_log and self.access_log[-1][1] == status:
            return False
        self.access_log.append([when.isoformat(timespec="minutes"), status])
        del self.access_log[:-keep]
        return True

    def availability(self, now: dt.datetime, hours: int = 24) -> float | None:
        """Share of the last ``hours`` in which checks reached BookMyShow (None = no data)."""
        start = now - dt.timedelta(hours=hours)
        current: str | None = None
        cursor = start
        ok = total = 0.0
        for stamp, status in self.access_log:
            when = dt.datetime.fromisoformat(stamp)
            if when <= start:
                current = status
                continue
            if current is not None:
                span = (when - cursor).total_seconds()
                total += span
                ok += span if current == "ok" else 0.0
            current, cursor = status, when
        if current is None:
            return None
        span = (now - cursor).total_seconds()
        total += span
        ok += span if current == "ok" else 0.0
        return ok / total if total > 0 else None


@dataclass
class State:
    version: int = STATE_VERSION
    targets: dict[str, TargetState] = field(default_factory=dict)
    health: Health = field(default_factory=Health)
    telegram_offset: int = 0

    def for_target(self, fingerprint: str) -> TargetState:
        return self.targets.setdefault(fingerprint, TargetState())

    def prune(self, keep: set[str], today_code: str) -> None:
        """Forget removed targets and shows whose date has passed."""
        for fp in list(self.targets):
            if fp not in keep:
                del self.targets[fp]
        for ts in self.targets.values():
            ts.notified = sorted(k for k in set(ts.notified) if k.split(":", 1)[0] >= today_code)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _from_dict(raw: dict[str, Any]) -> State:
    targets = {
        fp: TargetState(**{k: v for k, v in ts.items() if k in TargetState.__dataclass_fields__})
        for fp, ts in (raw.get("targets") or {}).items()
    }
    health_raw = raw.get("health") or {}
    health = Health(**{k: v for k, v in health_raw.items() if k in Health.__dataclass_fields__})
    return State(
        version=STATE_VERSION,
        targets=targets,
        health=health,
        telegram_offset=int(raw.get("telegram_offset", 0)),
    )


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        return _from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # A corrupt file must not stop the watcher; worst case some alerts repeat once.
        log.error("state.json unreadable (%s); starting fresh", exc)
        return State()


def save_state(path: Path, state: State) -> bool:
    """Write state atomically. Returns True if the file content changed."""
    new = state.to_json()
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if new == old:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    return True
