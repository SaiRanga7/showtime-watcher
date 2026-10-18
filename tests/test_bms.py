"""Parsing a real BookMyShow cinema page (captured 18 Oct 2026) and classifying failures."""

from __future__ import annotations

import httpx
import pytest
from conftest import ALUC_URL, base_state, html_page

from showtime_watcher import bms
from showtime_watcher.bms import (
    BlockedError,
    HttpFetcher,
    ParseError,
    RawPage,
    Venue,
    extract_state,
    parse_page,
)


def _page(html: str, status: int = 200, url: str = ALUC_URL + "/20261018") -> RawPage:
    return RawPage(url, url, status, html)


def test_extract_state_stops_at_end_of_json() -> None:
    state = extract_state(html_page(base_state()))
    assert "venueShowtimesFunctionalApi" in state


def test_parse_real_page() -> None:
    page = parse_page(_page(html_page(base_state())), "20261018")
    assert page.venue_name == "ALLU Cinemas: Kokapet"
    assert page.venue_code == "ALUC"
    assert page.page_date == "20261018"
    # The date strip shows 7 days; only 18-20 Oct were bookable that day.
    assert page.bookable_dates == ["20261018", "20261019", "20261020"]
    assert len(page.dates) == 7
    assert [m.title for m in page.movies][:4] == [
        "Street Fighter",
        "Prahaar",
        "Ranabaali",
        "Jailer 2",
    ]


def test_show_details_and_deep_link() -> None:
    page = parse_page(_page(html_page(base_state())), "20261018")
    jailer = next(m for m in page.movies if m.title == "Jailer 2")
    assert [(s.time, s.language, s.dimension) for s in jailer.shows] == [
        ("07:55 PM", "Telugu", "DOLBY CINEMA 2D"),
        ("11:20 PM", "Telugu", "DOLBY CINEMA 2D"),
        ("08:10 PM", "Tamil", "2D"),
    ]
    show = jailer.shows[0]
    assert show.key == "20261018:6109"
    assert show.minutes == 19 * 60 + 55
    assert show.price_label == "₹295–350"
    # URL pattern confirmed by clicking a showtime on the live site.
    assert show.seat_layout_url("hyderabad", "ALUC") == (
        "https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00521930/ALUC/6109/20261018"
    )


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_status(status: int) -> None:
    with pytest.raises(BlockedError):
        parse_page(_page("<html>nope</html>", status), "20261018")


def test_challenge_page_is_blocked() -> None:
    html = "<html><head><title>Just a moment...</title></head><body>cf-chl-widget</body></html>"
    with pytest.raises(BlockedError):
        parse_page(_page(html), "20261018")


def test_real_page_with_cloudflare_beacon_is_not_blocked() -> None:
    # Normal pages include Cloudflare's challenge-platform beacon script; that's fine.
    html = html_page(base_state()).replace(
        "</body>", "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body>"
    )
    assert parse_page(_page(html), "20261018").venue_code == "ALUC"


def test_page_without_state_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_page(_page("<html><body>maintenance</body></html>"), "20261018")


@pytest.mark.parametrize(
    "url",
    [
        ALUC_URL,
        ALUC_URL + "/20261018",
        "in.bookmyshow.com/cinemas/Hyderabad/ALLU-Cinemas-Kokapet/buytickets/aluc/20261018?x=1",
    ],
)
def test_venue_from_url(url: str) -> None:
    assert Venue.from_url(url) == Venue("hyderabad", "allu-cinemas-kokapet", "ALUC")


def test_venue_from_bad_url() -> None:
    with pytest.raises(ValueError, match="cinema link"):
        Venue.from_url("https://in.bookmyshow.com/movies/hyderabad/jailer-2/ET00518674")


def test_http_fetcher_retries_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bms.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert "Chrome" in request.headers["User-Agent"]
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text=html_page(base_state()))

    client = httpx.Client(transport=httpx.MockTransport(handler), headers=bms.BROWSER_HEADERS)
    fetcher = HttpFetcher(client=client, pause=0)
    raw = fetcher.fetch(ALUC_URL + "/20261018")
    assert raw.status == 200 and calls["n"] == 2


def test_http_fetcher_does_not_retry_bot_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bms.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="<title>Just a moment...</title>")

    fetcher = HttpFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), pause=0)
    raw = fetcher.fetch(ALUC_URL + "/20261018")
    assert calls["n"] == 1
    with pytest.raises(BlockedError):
        parse_page(raw, "20261018")
