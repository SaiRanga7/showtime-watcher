"""Whole passes: alert once, alert again only for new shows, health alerts, Telegram commands."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from conftest import (
    ALUC,
    NOW,
    TODAY,
    FakeBMS,
    FakeBot,
    RecordingChannel,
    add_show,
    make_settings,
    state_for,
)

from showtime_watcher.config import Settings
from showtime_watcher.notify import Notifier
from showtime_watcher.state import Health
from showtime_watcher.targets import Target, WatchList, load_watchlist, save_watchlist
from showtime_watcher.watcher import run_once

TODAY_CODE = "20261018"
OPEN_NOW = ["20261018", "20261019", "20261020"]


def _bms(**kwargs) -> FakeBMS:
    return FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW)}, **kwargs)


def _watch(settings: Settings, *targets: Target) -> None:
    save_watchlist(settings.targets_path, WatchList(targets=list(targets), default_cinema=ALUC))


def _jailer(**extra) -> Target:
    return Target(id=1, movie="Jailer 2", venue=ALUC, dates=[TODAY_CODE], **extra)


def _run(settings: Settings, notifier: Notifier, bms: FakeBMS, now=NOW, bot=None, **kwargs):
    return run_once(settings, fetcher=bms, notifier=notifier, bot=bot, now=now, **kwargs)


def test_first_open_then_only_new_shows(settings, notifier, channel) -> None:
    _watch(settings, _jailer())
    report = _run(settings, notifier, _bms())
    assert [a.kind for a in channel.sent] == ["open"]
    alert = channel.sent[0]
    assert alert.title == "🚨 Bookings OPEN: Jailer 2 @ ALLU Cinemas: Kokapet"
    assert "07:55 PM · SCREEN 1 · ₹295–350" in alert.body
    assert alert.links[0][1].endswith("/seat-layout/ET00521930/ALUC/6109/20261018")
    assert report.state_changed

    _run(settings, notifier, _bms())  # same shows again: silence
    assert len(channel.sent) == 1

    bms = _bms()
    add_show(bms.pages[TODAY_CODE], "Jailer 2", "7001", "2345", "11:45 PM")
    _run(settings, notifier, bms)
    assert [a.kind for a in channel.sent] == ["open", "more"]
    assert "11:45 PM" in channel.sent[1].body and "07:55 PM" not in channel.sent[1].body


def test_notify_first_only(settings, notifier, channel) -> None:
    _watch(settings, _jailer(notify="first"))
    _run(settings, notifier, _bms())
    bms = _bms()
    add_show(bms.pages[TODAY_CODE], "Jailer 2", "7001", "2345", "11:45 PM")
    _run(settings, notifier, bms)
    assert [a.kind for a in channel.sent] == ["open"]


def test_undelivered_alert_is_retried(settings) -> None:
    _watch(settings, _jailer())
    down = RecordingChannel(fail=True)
    _run(settings, Notifier([down], settings), _bms())
    up = RecordingChannel()
    _run(settings, Notifier([up], settings), _bms())
    assert [a.kind for a in up.sent] == ["open"]  # still the *first* alert


def test_dry_run_changes_nothing(settings, channel) -> None:
    _watch(settings, _jailer())
    dry = Notifier([channel], settings, dry_run=True)
    report = _run(settings, dry, _bms(), dry_run=True)
    assert channel.sent == [] and not report.state_changed
    assert not settings.state_path.exists()


def test_partial_alert_once(settings, notifier, channel) -> None:
    _watch(settings, _jailer(format="imax"))
    _run(settings, notifier, _bms())
    _run(settings, notifier, _bms())
    assert [a.kind for a in channel.sent] == ["partial"]


def test_blind_and_recovered_alerts(settings, notifier, channel) -> None:
    _watch(settings, _jailer())
    blocked = _bms(blocked=True)
    _run(settings, notifier, blocked, now=NOW)
    assert channel.sent == []  # one failure isn't worth waking you up
    _run(settings, notifier, blocked, now=NOW + dt.timedelta(minutes=50))
    _run(settings, notifier, blocked, now=NOW + dt.timedelta(minutes=55))
    assert [a.kind for a in channel.sent] == ["blind"]
    _run(settings, notifier, _bms(), now=NOW + dt.timedelta(minutes=60))
    assert [a.kind for a in channel.sent] == ["blind", "open", "recovered"]
    health = json.loads(settings.state_path.read_text())["health"]
    assert health["failing_since"] is None and health["blind_alert_sent"] is False


def test_blind_alert_not_repeated_within_six_hours(settings, notifier, channel) -> None:
    _watch(settings, _jailer())
    blocked = _bms(blocked=True)
    for minutes in (0, 50):  # blind alert
        _run(settings, notifier, blocked, now=NOW + dt.timedelta(minutes=minutes))
    _run(settings, notifier, _bms(), now=NOW + dt.timedelta(minutes=60))  # recovered
    for minutes in (70, 130):  # blocked again for an hour, but within 6 h of the last alert
        _run(settings, notifier, blocked, now=NOW + dt.timedelta(minutes=minutes))
    assert [a.kind for a in channel.sent] == ["blind", "open", "recovered"]


def test_first_refusal_stops_the_run(settings, notifier) -> None:
    _watch(settings, _jailer(), Target(id=2, movie="Whalefall", venue=ALUC, dates=["20261019"]))
    blocked = _bms(blocked=True)
    report = _run(settings, notifier, blocked)
    assert report.access == "blocked"
    assert len(blocked.requests) == 1  # no second knock on a door that said no


def test_backoff_while_blocked(settings, notifier) -> None:
    _watch(settings, _jailer())
    blocked = _bms(blocked=True)
    _run(settings, notifier, blocked, now=NOW, backoff=True)  # failing starts: slot 0
    statuses = []
    for minutes in (5, 10, 15):
        report = _run(
            settings, notifier, blocked, now=NOW + dt.timedelta(minutes=minutes), backoff=True
        )
        statuses.append(report.results[0].status)
    assert statuses == ["skipped", "skipped", "error"]  # only every 3rd slot knocks
    assert len(blocked.requests) == 2


def test_empty_watchlist_still_measures_access(settings, notifier) -> None:
    _watch(settings)
    bms = _bms()
    report = _run(settings, notifier, bms)
    assert report.access == "ok" and len(bms.requests) == 1


def test_access_log_records_changes_only(settings, notifier) -> None:
    _watch(settings, _jailer())
    plan = [False, False, True, True, False]  # blocked?
    for i, is_blocked in enumerate(plan):
        _run(settings, notifier, _bms(blocked=is_blocked), now=NOW + dt.timedelta(minutes=5 * i))
    log = json.loads(settings.state_path.read_text())["health"]["access_log"]
    assert [status for _when, status in log] == ["ok", "blocked", "ok"]


def test_availability_from_transitions() -> None:
    health = Health(
        access_log=[
            [(NOW - dt.timedelta(hours=30)).isoformat(), "ok"],
            [(NOW - dt.timedelta(hours=12)).isoformat(), "blocked"],
            [(NOW - dt.timedelta(hours=6)).isoformat(), "ok"],
        ]
    )
    # Last 24 h: ok for 12 h, blocked 6 h, ok 6 h -> 75 %.
    assert health.availability(NOW) == 0.75
    assert Health().availability(NOW) is None


def test_daily_check_in_once_per_day(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, daily_message=True)
    telegram, email = RecordingChannel("telegram"), RecordingChannel("email")
    notifier = Notifier([telegram, email], settings)
    _watch(settings, Target(id=1, movie="Not Out Yet", venue=ALUC, dates=["20261030"]))
    _run(settings, notifier, _bms(), now=NOW.replace(hour=8))
    assert telegram.sent == []  # before 9 AM
    _run(settings, notifier, _bms(), now=NOW.replace(hour=9, minute=2))
    _run(settings, notifier, _bms(), now=NOW.replace(hour=9, minute=7))
    assert [a.kind for a in telegram.sent] == ["daily"] and telegram.sent[0].silent
    assert email.sent == []  # check-ins are Telegram-only
    assert json.loads(settings.state_path.read_text())["health"]["last_daily"] == "2026-10-18"


def test_telegram_watch_list_stop(settings, notifier, channel) -> None:
    _watch(settings)
    bot = FakeBot(
        [
            (42, "/watch Jailer 2 18 Oct format=dolby"),
            (999, "/stop all"),  # a stranger: ignored
            (42, "/list"),
        ]
    )
    _run(settings, notifier, _bms(), bot=bot)
    (target,) = load_watchlist(settings.targets_path).targets
    assert (target.movie, target.dates, target.format) == ("Jailer 2", ["20261018"], "dolby")
    assert bot.replies[0].startswith("👀 Watching #1")
    assert "OPEN: 2 matching shows" in bot.replies[0]  # reply carries the first check
    assert "#1" in bot.replies[1]
    assert [a.kind for a in channel.sent] == ["open"]
    assert json.loads(settings.state_path.read_text())["telegram_offset"] == 503

    bot2 = FakeBot([(42, "/stop 1")])
    bot2.updates[0]["update_id"] = 503
    _run(settings, notifier, _bms(), bot=bot2)
    assert load_watchlist(settings.targets_path).targets == []
    assert bot2.replies[0].startswith("🗑 #1")


def test_first_cinema_becomes_default(settings, notifier) -> None:
    save_watchlist(settings.targets_path, WatchList())
    link = "https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC"
    bot = FakeBot([(42, f"/watch Whalefall 18 Oct {link}"), (42, "/watch Prahaar 18 Oct")])
    _run(settings, notifier, _bms(), bot=bot)
    wl = load_watchlist(settings.targets_path)
    assert wl.default_cinema == ALUC
    assert [t.movie for t in wl.targets] == ["Whalefall", "Prahaar"]
    assert "Saved as your default cinema" in bot.replies[0]


def test_telegram_duplicate_watch_and_bad_input(settings, notifier) -> None:
    _watch(settings, _jailer())
    bot = FakeBot([(42, "/watch jailer 2 18/10"), (42, "/watch 30 Oct"), (42, "hello")])
    _run(settings, notifier, _bms(), bot=bot)
    assert bot.replies[0].startswith("Already watching #1")
    assert bot.replies[1].startswith("❌ Which movie?")
    assert "I didn't get that" in bot.replies[2]
    assert len(load_watchlist(settings.targets_path).targets) == 1


def test_today_is_ist(settings) -> None:
    # 20:00 UTC on 17 Oct is already 01:30 on 18 Oct in India.
    assert NOW.date() == TODAY
    late = dt.datetime(2026, 10, 17, 20, 0, tzinfo=dt.UTC).astimezone(NOW.tzinfo)
    assert late.date() == TODAY
