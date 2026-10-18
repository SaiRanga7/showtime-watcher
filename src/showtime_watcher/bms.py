"""BookMyShow access: fetch a cinema page and parse the showtimes JSON embedded in it.

How BookMyShow cinema pages work (checked on the live site, Oct 2026):

* A cinema page lives at ``/cinemas/<city>/<slug>/buytickets/<VENUECODE>/<YYYYMMDD>``.
* The server renders the page and embeds the data it used as
  ``window.__INITIAL_STATE__ = {...}`` in a <script> tag. That JSON holds the date strip
  (which dates are bookable) and every movie and showtime for the page's date. Parsing it
  is far more reliable than scraping the visible HTML, and needs no browser.
* Asking for a date that is NOT open yet does not fail: BookMyShow silently redirects to
  the nearest open date (usually today). So "page loaded and the movie is listed" does not
  mean *your* date is open. Always compare the date the page actually shows.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

BMS_BASE = "https://in.bookmyshow.com"
STATE_MARKER = "window.__INITIAL_STATE__"

# A normal desktop-browser identity. httpx's default User-Agent ("python-httpx/x.y")
# is commonly refused by bot walls even for public pages.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Phrases that only appear on bot-protection interstitials, never on a real cinema page.
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-chl-",
    "verify you are human",
    "access denied",
)

_VENUE_URL_RE = re.compile(
    r"bookmyshow\.com/cinemas/(?P<city>[\w-]+)/(?P<slug>[\w-]+)/buytickets/"
    r"(?P<code>[A-Za-z0-9]+)(?:/(?P<date>\d{8}))?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- errors
class FetchError(Exception):
    """Could not get usable data from BookMyShow (network, HTTP error, layout change)."""


class BlockedError(FetchError):
    """BookMyShow's bot protection refused the request (HTTP 403/429 or a challenge page)."""


class ParseError(FetchError):
    """The page arrived but its embedded data wasn't where or what we expected."""


# ---------------------------------------------------------------- data model
@dataclass(frozen=True)
class Venue:
    """A cinema, identified the way BookMyShow URLs identify it."""

    city: str  # "hyderabad"
    slug: str  # "allu-cinemas-kokapet"
    code: str  # "ALUC" (BookMyShow's venue code)

    @classmethod
    def from_url(cls, url: str) -> Venue:
        """Parse a cinema link, e.g. .../cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC."""
        match = _VENUE_URL_RE.search(url)
        if not match:
            raise ValueError(
                "That isn't a BookMyShow cinema link. Open the cinema on in.bookmyshow.com "
                "and copy a link like .../cinemas/<city>/<cinema>/buytickets/<CODE>/<date>"
            )
        return cls(match["city"].lower(), match["slug"].lower(), match["code"].upper())

    @property
    def base_url(self) -> str:
        return f"{BMS_BASE}/cinemas/{self.city}/{self.slug}/buytickets/{self.code}"

    def page_url(self, date_code: str) -> str:
        return f"{self.base_url}/{date_code}"


@dataclass(frozen=True)
class Show:
    """One screening, e.g. Jailer 2, Telugu, DOLBY CINEMA 2D, 07:55 PM, SCREEN 1."""

    date_code: str  # "20261030"
    time: str  # "07:55 PM", as BookMyShow prints it
    time_code: str  # "1955" (24 h HHMM), used by the after/before filters
    session_id: str  # BookMyShow's id for this screening; with the date it's unique
    screen: str  # "SCREEN 1"
    event_code: str  # ET-code of this language/format variant of the movie
    event_name: str  # "Jailer 2 (DOLBY CINEMA 2D)(Telugu) - Telugu"
    language: str  # "Telugu"
    dimension: str  # "2D", "DOLBY CINEMA 2D", "IMAX 3D", ...
    min_price: float | None
    max_price: float | None
    avail_status: str  # raw BookMyShow code; its meaning isn't documented, so it's only logged

    @property
    def key(self) -> str:
        """Identity used to remember which shows were already announced."""
        return f"{self.date_code}:{self.session_id}"

    @property
    def minutes(self) -> int:
        """Start time as minutes after midnight (from the 24 h time_code)."""
        digits = self.time_code.zfill(4)
        return int(digits[:2]) * 60 + int(digits[2:])

    @property
    def price_label(self) -> str:
        lo, hi = self.min_price, self.max_price
        if lo is None and hi is None:
            return ""
        if lo is None or hi is None or lo == hi:
            return f"₹{(lo if lo is not None else hi):.0f}"
        return f"₹{lo:.0f}–{hi:.0f}"

    def seat_layout_url(self, city: str, venue_code: str) -> str:
        """Deep link straight to seat selection (pattern confirmed on the live site)."""
        return (
            f"{BMS_BASE}/movies/{city}/seat-layout/"
            f"{self.event_code}/{venue_code}/{self.session_id}/{self.date_code}"
        )


@dataclass(frozen=True)
class Movie:
    """A movie on the page, with all its language/format variants' shows flattened."""

    title: str  # "Jailer 2"
    group_code: str  # "EG00515691", shared by all variants
    event_codes: tuple[str, ...]  # ET-codes of the variants
    shows: tuple[Show, ...]


@dataclass(frozen=True)
class ShowDate:
    """One chip of the date strip at the top of a cinema page."""

    date_code: str  # "20261030"
    label: str  # "Friday, 30 Oct"
    bookable: bool  # False while the chip is greyed out (no bookings for that date yet)


@dataclass(frozen=True)
class VenuePage:
    """Everything parsed from one cinema page."""

    venue_code: str
    venue_name: str
    page_date: str  # the date the page REALLY shows (may differ from the one requested)
    dates: tuple[ShowDate, ...]
    movies: tuple[Movie, ...]

    @property
    def bookable_dates(self) -> list[str]:
        return [d.date_code for d in self.dates if d.bookable]


@dataclass(frozen=True)
class RawPage:
    requested_url: str
    final_url: str  # after redirects
    status: int
    html: str


# ---------------------------------------------------------------- parsing
def extract_state(html: str) -> dict[str, Any]:
    """Pull the ``window.__INITIAL_STATE__ = {...}`` JSON object out of a page."""
    marker = html.find(STATE_MARKER)
    if marker == -1:
        raise ParseError("page has no embedded window.__INITIAL_STATE__ data")
    start = html.find("{", marker)
    if start == -1:
        raise ParseError("embedded state marker found but no JSON object follows it")
    try:
        # raw_decode stops at the end of the object, ignoring the </script> that follows.
        state, _ = json.JSONDecoder().raw_decode(html, start)
    except json.JSONDecodeError as exc:
        raise ParseError(f"embedded state is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ParseError("embedded state is not a JSON object")
    return state


def _deep_find(node: Any, pred: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """Depth-first search for the first dict satisfying ``pred`` (fallback for layout drift)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if pred(cur):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _venue_data(state: dict[str, Any], requested_date: str | None) -> dict[str, Any]:
    """Find the cached 'showtimes by venue' API response inside the Redux state."""
    queries = state.get("venueShowtimesFunctionalApi", {}).get("queries", {})
    candidates: list[dict[str, Any]] = []
    for key, query in queries.items():
        if not key.startswith("getShowtimesByVenue") or not isinstance(query, dict):
            continue
        if query.get("status") == "rejected":
            raise ParseError("BookMyShow's showtimes request failed on their side")
        data = query.get("data")
        if isinstance(data, dict) and "ShowDatesArray" in data:
            candidates.append(data)
    if not candidates:
        found = _deep_find(state, lambda d: "ShowDatesArray" in d and "showDetailsTransformed" in d)
        if found is None:
            raise ParseError("no venue showtimes found in the page data (layout changed?)")
        return found
    for data in candidates:
        if (data.get("showDetailsTransformed") or {}).get("Date") == requested_date:
            return data
    return candidates[0]


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_from_url(url: str) -> str | None:
    match = _VENUE_URL_RE.search(url)
    return match["date"] if match and match["date"] else None


def parse_venue_state(
    state: dict[str, Any], requested_date: str | None = None, final_url: str = ""
) -> VenuePage:
    """Turn the embedded state of a cinema page into a VenuePage."""
    data = _venue_data(state, requested_date)
    details = data.get("showDetailsTransformed") or {}
    venue = details.get("Venues") or {}

    page_date = str(details.get("Date") or _date_from_url(final_url) or "")
    if not page_date:
        raise ParseError("could not tell which date the page shows")

    dates = tuple(
        ShowDate(
            date_code=str(d.get("DateCode", "")),
            label=str(d.get("DispDate", "")),
            bookable=not d.get("isDisabled", False) and not data.get("AllShowDatesDisabled"),
        )
        for d in data.get("ShowDatesArray") or []
        if d.get("DateCode")
    )

    movies: list[Movie] = []
    for event in details.get("Event") or []:
        shows: list[Show] = []
        codes: list[str] = []
        for child in event.get("ChildEvents") or []:
            code = str(child.get("EventCode", ""))
            codes.append(code)
            for st in child.get("ShowTimes") or []:
                shows.append(
                    Show(
                        date_code=str(st.get("ShowDateCode") or page_date),
                        time=str(st.get("ShowTime", "")),
                        time_code=str(st.get("ShowTimeCode", "0000")),
                        session_id=str(st.get("SessionId", "")),
                        screen=str(st.get("ScreenName", "")),
                        event_code=code,
                        event_name=str(child.get("EventName", "")),
                        language=str(child.get("EventLanguage", "")),
                        dimension=str(child.get("EventDimension", "")),
                        min_price=_to_float(st.get("MinPrice")),
                        max_price=_to_float(st.get("MaxPrice")),
                        avail_status=str(st.get("AvailStatus", "")),
                    )
                )
        title = str(event.get("EventTitle") or "")
        if not title and event.get("ChildEvents"):
            title = str(event["ChildEvents"][0].get("EventName", ""))
        movies.append(
            Movie(
                title=title,
                group_code=str(event.get("EventGroup", "")),
                event_codes=tuple(codes),
                shows=tuple(shows),
            )
        )

    return VenuePage(
        venue_code=str(venue.get("VenueCode", "")),
        venue_name=str(venue.get("VenueName", "")),
        page_date=page_date,
        dates=dates,
        movies=tuple(movies),
    )


def looks_like_challenge(html: str) -> bool:
    """True for bot-protection interstitials (only meaningful when the state JSON is absent)."""
    head = html[:20000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def parse_page(raw: RawPage, requested_date: str) -> VenuePage:
    """Classify a raw response and parse it, raising the right FetchError subtype."""
    has_state = STATE_MARKER in raw.html
    if raw.status in (401, 403, 429) or (not has_state and looks_like_challenge(raw.html)):
        raise BlockedError(f"BookMyShow refused the request (HTTP {raw.status}, bot protection)")
    if raw.status >= 400:
        raise FetchError(f"HTTP {raw.status} from BookMyShow for {raw.requested_url}")
    return parse_venue_state(extract_state(raw.html), requested_date, raw.final_url)


# ---------------------------------------------------------------- fetchers
class Fetcher(Protocol):
    name: str

    def fetch(self, url: str) -> RawPage: ...

    def close(self) -> None: ...


class HttpFetcher:
    """Plain HTTPS GET, about 1 s per page.

    If BookMyShow refuses a request (403/429 or a bot-challenge page) we stop there and
    report it. We deliberately don't escalate to headless browsers, proxies or challenge
    solving: that's working around the site's protection, and it breaks without warning.
    """

    name = "http"

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        pause: float = 1.5,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout
        )
        self._retries = retries
        self._pause = pause  # polite gap between our own requests
        self._last_request = 0.0

    def fetch(self, url: str) -> RawPage:
        for attempt in range(self._retries + 1):
            wait = self._pause - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise FetchError(f"network error talking to BookMyShow: {exc}") from exc
                log.warning("Network error (%s), retrying", exc)
                time.sleep(2**attempt)
                continue
            # Retry real server hiccups, but never hammer a bot wall.
            transient = resp.status_code >= 500 and not looks_like_challenge(resp.text)
            if transient and attempt < self._retries:
                log.warning("HTTP %s from BookMyShow, retrying", resp.status_code)
                time.sleep(2**attempt)
                continue
            return RawPage(url, str(resp.url), resp.status_code, resp.text)
        raise FetchError("unreachable")  # pragma: no cover - loop always returns or raises

    def close(self) -> None:
        self._client.close()


def load_venue_page(fetcher: Fetcher, venue: Venue, date_code: str) -> VenuePage:
    """Fetch and parse one cinema page for one date."""
    raw = fetcher.fetch(venue.page_url(date_code))
    page = parse_page(raw, date_code)
    log.info(
        "%s %s: page shows %s, bookable %s, %d movies (via %s)",
        venue.code,
        date_code,
        page.page_date,
        ",".join(page.bookable_dates) or "-",
        len(page.movies),
        fetcher.name,
    )
    return page
