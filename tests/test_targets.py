"""Date parsing, the /watch syntax and targets.yaml round-trips."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from conftest import ALUC, ALUC_URL, TODAY

from showtime_watcher.targets import (
    Target,
    WatchList,
    load_watchlist,
    normalize_hhmm,
    parse_dates,
    parse_one_date,
    parse_watch_text,
    save_watchlist,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30 Oct", dt.date(2026, 10, 30)),
        ("Oct 30", dt.date(2026, 10, 30)),
        ("30oct", dt.date(2026, 10, 30)),
        ("30th October", dt.date(2026, 10, 30)),
        ("30/10", dt.date(2026, 10, 30)),
        ("30-10-2026", dt.date(2026, 10, 30)),
        ("2026-10-30", dt.date(2026, 10, 30)),
        ("Oct 30, 2026", dt.date(2026, 10, 30)),
        ("1 Jan", dt.date(2027, 1, 1)),  # already past this year -> next year
        ("tomorrow", dt.date(2026, 10, 19)),
        ("fri", dt.date(2026, 10, 23)),  # TODAY is a Sunday
        ("sunday", dt.date(2026, 10, 18)),  # today counts
    ],
)
def test_parse_one_date(text: str, expected: dt.date) -> None:
    assert parse_one_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["Jailer", "2", "31 Feb", "13/13", ""])
def test_not_a_date(text: str) -> None:
    assert parse_one_date(text, TODAY) is None


def test_relative_words_can_be_refused() -> None:
    assert parse_one_date("sun", TODAY, allow_relative=False) is None


def test_parse_dates_list_and_any() -> None:
    assert parse_dates("30 Oct, 31 Oct", TODAY) == ["20261030", "20261031"]
    assert parse_dates("any", TODAY) == []
    with pytest.raises(ValueError, match="date"):
        parse_dates("someday", TODAY)


def _watch(text: str, default=ALUC) -> Target:
    return parse_watch_text(text, today=TODAY, default_cinema=default, next_id=7)


@pytest.mark.parametrize(
    ("text", "movie", "dates"),
    [
        ("Jailer 2 30 Oct", "Jailer 2", ["20261030"]),
        ("Jailer 2 on 30 Oct, 31 Oct", "Jailer 2", ["20261030", "20261031"]),
        ("Rising Sun", "Rising Sun", []),  # 'Sun' is part of the title...
        ("Rising Sun on sun", "Rising Sun", ["20261018"]),  # ...unless after "on"
        ("Edge of Tomorrow", "Edge of Tomorrow", []),
        ("Jailer 2 on tomorrow", "Jailer 2", ["20261019"]),
        ("12 Gaam 30/10", "12 Gaam", ["20261030"]),
        ("Friday", "Friday", []),  # never eat the whole title
    ],
)
def test_watch_movie_and_dates(text: str, movie: str, dates: list[str]) -> None:
    target = _watch(text)
    assert (target.movie, target.dates, target.id) == (movie, dates, 7)


def test_watch_filters_and_link_anywhere() -> None:
    target = _watch(f"{ALUC_URL}/20261018 Jailer 2 30 Oct format=dolby lang=Telugu after=6pm")
    assert target.venue == ALUC
    assert target.dates == ["20261030"]
    assert (target.format, target.language, target.after) == ("dolby", "Telugu", "18:00")


def test_watch_needs_movie_and_cinema() -> None:
    with pytest.raises(ValueError, match="Which movie"):
        _watch("30 Oct")
    with pytest.raises(ValueError, match="Which cinema"):
        _watch("Jailer 2", default=None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6pm", "18:00"),
        ("6:30 pm", "18:30"),
        ("18:00", "18:00"),
        ("1800", "18:00"),
        ("12am", "00:00"),
    ],
)
def test_normalize_hhmm(text: str, expected: str) -> None:
    assert normalize_hhmm(text) == expected


def test_normalize_hhmm_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        normalize_hhmm("25:00")


def test_yaml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    wl = WatchList(
        targets=[_watch("Jailer 2 30 Oct format=dolby"), _watch("Rising Sun")], default_cinema=ALUC
    )
    wl.targets[1].id = 8
    save_watchlist(path, wl)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Movies to watch")
    assert "'2026-10-30'" in text  # ISO dates, quoted so YAML keeps them as text
    loaded = load_watchlist(path)
    assert loaded.default_cinema == ALUC
    assert [t.fingerprint() for t in loaded.targets] == [t.fingerprint() for t in wl.targets]
    assert [t.id for t in loaded.targets] == [7, 8]


def test_loads_v1_format(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(
        "targets:\n"
        '  - movie: "Michael"\n'
        '    city: "hyderabad"\n'
        '    cinema_slug: "allu-cinemas-kokapet"\n'
        '    cinema_code: "ALUC"\n'
        '    cinema_name: "ALLU Cinemas Kokapet"\n'
        '    date: "2026-04-28"\n',
        encoding="utf-8",
    )
    (target,) = load_watchlist(path).targets
    assert (target.movie, target.venue, target.dates) == ("Michael", ALUC, ["20260428"])
    assert target.is_expired("20261018")


def test_fingerprint_tracks_what_not_id() -> None:
    a, b = _watch("Jailer 2 30 Oct"), _watch("jailer 2  30/10")
    b.id = 99
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != _watch("Jailer 2 31 Oct").fingerprint()
