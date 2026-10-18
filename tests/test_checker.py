"""When is a target 'open'? Including the redirect trap that fooled v1."""

from __future__ import annotations

import pytest
from conftest import ALUC, FakeBMS, base_state, state_for

from showtime_watcher.bms import BlockedError, Movie, Show
from showtime_watcher.checker import PageCache, evaluate, movie_matches
from showtime_watcher.targets import Target

TODAY_CODE = "20261018"
OPEN_NOW = ["20261018", "20261019", "20261020"]


def _target(movie: str = "Jailer 2", dates: list[str] | None = None, **filters) -> Target:
    return Target(id=1, movie=movie, venue=ALUC, dates=dates or [], **filters)


def _check(target: Target, bms: FakeBMS):
    return evaluate(target, PageCache(bms), TODAY_CODE)


def test_redirected_date_is_not_open() -> None:
    """THE v1 bug: 30 Oct isn't on sale, BMS shows 18 Oct instead, where Jailer 2 plays."""
    bms = FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW)})
    result = _check(_target(dates=["20261030"]), bms)
    assert result.status == "waiting"
    assert result.shows == []
    assert "not on sale yet" in result.note
    assert "Sun 18 Oct – Tue 20 Oct" in result.note


def test_dated_target_open() -> None:
    bookable = [*OPEN_NOW, "20261030"]
    bms = FakeBMS(
        {
            TODAY_CODE: state_for(TODAY_CODE, bookable=bookable),
            "20261030": state_for("20261030", bookable=bookable),
        }
    )
    result = _check(_target(dates=["20261030"]), bms)
    assert result.status == "open"
    assert [s.time for s in result.shows] == ["07:55 PM", "08:10 PM", "11:20 PM"]  # by time
    assert {s.date_code for s in result.shows} == {"20261030"}
    assert result.venue_name == "ALLU Cinemas: Kokapet"


def test_date_open_but_movie_missing() -> None:
    bms = FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW, drop=("Jailer 2",))})
    result = _check(_target(dates=[TODAY_CODE]), bms)
    assert result.status == "waiting"
    assert "isn't listed yet" in result.note


@pytest.mark.parametrize(
    ("filters", "times"),
    [
        ({"format": "dolby"}, ["07:55 PM", "11:20 PM"]),
        ({"language": "tamil"}, ["08:10 PM"]),
        ({"after": "21:00"}, ["11:20 PM"]),
        ({"before": "20:00"}, ["07:55 PM"]),
        ({"screen": "screen 4"}, ["08:10 PM"]),
        ({"format": "dolby", "after": "20:00"}, ["11:20 PM"]),
    ],
)
def test_filters(filters: dict, times: list[str]) -> None:
    bms = FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW)})
    result = _check(_target(dates=[TODAY_CODE], **filters), bms)
    assert [s.time for s in result.shows] == times


def test_on_sale_but_filtered_out() -> None:
    bms = FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW)})
    result = _check(_target(dates=[TODAY_CODE], format="imax"), bms)
    assert result.status == "waiting"
    assert len(result.unmatched) == 3
    assert "none of 3 shows match format=imax" in result.note


def test_any_date_scans_every_bookable_date() -> None:
    bms = FakeBMS(
        {
            TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW, drop=("Whalefall",)),
            "20261019": state_for("20261019", bookable=OPEN_NOW),
            "20261020": state_for("20261020", bookable=OPEN_NOW, drop=("Whalefall",)),
        }
    )
    result = _check(_target(movie="whalefall"), bms)
    assert result.status == "open"
    assert [s.key for s in result.shows] == ["20261019:6144"]
    assert len(bms.requests) == 3  # one page per date, no duplicates


def test_pages_are_shared_between_targets() -> None:
    bms = FakeBMS({TODAY_CODE: state_for(TODAY_CODE, bookable=OPEN_NOW)})
    cache = PageCache(bms)
    for movie in ("Jailer 2", "Whalefall", "Prahaar"):
        evaluate(_target(movie=movie, dates=[TODAY_CODE]), cache, TODAY_CODE)
    assert len(bms.requests) == 1


def test_expired_target_is_not_fetched() -> None:
    bms = FakeBMS({TODAY_CODE: base_state()})
    result = _check(_target(dates=["20261001"]), bms)
    assert result.status == "expired" and bms.requests == []


def test_blocked_is_an_error_result() -> None:
    result = _check(_target(dates=[TODAY_CODE]), FakeBMS({}, blocked=True))
    assert result.status == "error"
    assert isinstance(result.error, BlockedError)


def _movie(title: str, codes: tuple[str, ...] = ("ET00000001",)) -> Movie:
    show = Show(
        "20261018",
        "07:00 PM",
        "1900",
        "1",
        "S1",
        codes[0],
        f"{title} - Telugu",
        "Telugu",
        "2D",
        None,
        None,
        "1",
    )
    return Movie(title, "EG00000001", codes, (show,))


@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("jailer 2", "Jailer 2", True),
        ("JAILER-2", "Jailer 2", True),
        ("Kara", "Karate Kid", False),  # whole words only
        ("kara", "Kara", True),
        ("ET00000001", "Anything", True),  # exact ET code
        ("EG00000001", "Anything", True),  # group code covers all variants
    ],
)
def test_movie_matches(query: str, title: str, expected: bool) -> None:
    assert movie_matches(query, _movie(title)) is expected
