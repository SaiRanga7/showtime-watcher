"""Shared fixtures: a fake BookMyShow built from a real page captured on 18 Oct 2026."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from showtime_watcher.bms import RawPage, Venue
from showtime_watcher.config import IST, Settings
from showtime_watcher.notify import Alert, Notifier

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 10, 18)  # a Sunday; the day the fixture was captured
NOW = dt.datetime(2026, 10, 18, 10, 0, tzinfo=IST)
ALUC_URL = "https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC"
ALUC = Venue.from_url(ALUC_URL)
QUERY_PREFIX = "getShowtimesByVenue-ALUC-"


def base_state() -> dict:
    return json.loads((FIXTURES / "aluc_20261018_state.json").read_text(encoding="utf-8"))


def state_for(day: str, *, bookable: list[str], drop: tuple[str, ...] = ()) -> dict:
    """The real page, re-dated to ``day``; ``drop`` removes movies by title."""
    state = copy.deepcopy(base_state())
    queries = state["venueShowtimesFunctionalApi"]["queries"]
    query = queries.pop(QUERY_PREFIX + "20261018")
    query["originalArgs"]["dateCode"] = day
    data = query["data"]
    details = data["showDetailsTransformed"]
    details["Date"] = day
    details["Event"] = [e for e in details["Event"] if e["EventTitle"] not in drop]
    for event in details["Event"]:
        for child in event["ChildEvents"]:
            for show in child["ShowTimes"]:
                show["ShowDateCode"] = day
    strip = sorted({(TODAY + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(7)} | {day})
    data["ShowDatesArray"] = [
        {"DateCode": code, "DispDate": code, "isDisabled": code not in bookable} for code in strip
    ]
    queries[QUERY_PREFIX + day] = query
    return state


def add_show(state: dict, title: str, session_id: str, time_code: str, time_label: str) -> None:
    details = next(iter(state["venueShowtimesFunctionalApi"]["queries"].values()))["data"][
        "showDetailsTransformed"
    ]
    event = next(e for e in details["Event"] if e["EventTitle"] == title)
    template = copy.deepcopy(event["ChildEvents"][0]["ShowTimes"][0])
    template.update(SessionId=session_id, ShowTimeCode=time_code, ShowTime=time_label)
    event["ChildEvents"][0]["ShowTimes"].append(template)


def html_page(state: dict) -> str:
    """Wrap state exactly like the live site: JSON immediately followed by </script>."""
    return (
        "<!DOCTYPE html><html><head><title>ALLU Cinemas</title></head><body>"
        '<div id="app">server-rendered markup</div>'
        '<script type="text/javascript">window.__INITIAL_STATE__ = '
        + json.dumps(state)
        + "</script><script>var later = {not: 'state'};</script></body></html>"
    )


class FakeBMS:
    """Behaves like BookMyShow: a date without a page redirects to the earliest page."""

    name = "http"

    def __init__(self, pages: dict[str, dict], *, blocked: bool = False) -> None:
        self.pages = pages
        self.blocked = blocked
        self.requests: list[str] = []

    def fetch(self, url: str) -> RawPage:
        self.requests.append(url)
        if self.blocked:
            return RawPage(url, url, 403, "<html><title>Just a moment...</title></html>")
        wanted = re.search(r"/(\d{8})$", url)[1]
        if wanted in self.pages:
            return RawPage(url, url, 200, html_page(self.pages[wanted]))
        first = min(self.pages)
        return RawPage(url, url[:-8] + first, 200, html_page(self.pages[first]))

    def close(self) -> None:
        pass


class RecordingChannel:
    def __init__(self, name: str = "telegram", *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append(alert)


class FakeBot:
    chat_id = "42"

    def __init__(self, messages: list[tuple[int, str]]) -> None:
        self.updates = [
            {"update_id": 500 + i, "message": {"chat": {"id": chat}, "text": text}}
            for i, (chat, text) in enumerate(messages)
        ]
        self.replies: list[str] = []

    def get_updates(self, offset: int) -> list[dict]:
        return [u for u in self.updates if u["update_id"] >= offset]

    def reply(self, text: str) -> None:
        self.replies.append(text)


def make_settings(root: Path, **overrides) -> Settings:
    values = dict(
        root=root,
        telegram_token=None,
        telegram_chat_id=None,
        gmail_address=None,
        gmail_app_password=None,
        email_to=None,
        ntfy_topic=None,
        ntfy_server="https://ntfy.sh",
        ntfy_token=None,
        daily_hour=9,
        daily_message=False,
        blind_after_minutes=45,
        github_actions=False,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def channel() -> RecordingChannel:
    return RecordingChannel()


@pytest.fixture
def notifier(channel: RecordingChannel, settings: Settings) -> Notifier:
    return Notifier([channel], settings)
