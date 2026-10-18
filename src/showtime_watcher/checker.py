"""Decide, for each target, whether bookings are open and which shows match.

The core rule (the v1 bug fix): a date counts as open only if BookMyShow serves a page
whose *own* date equals the date we asked for. Unopened dates redirect to today, so
without this check a movie that is already running would look "open" for every date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from showtime_watcher.bms import (
    BlockedError,
    Fetcher,
    FetchError,
    Movie,
    Show,
    Venue,
    VenuePage,
    load_venue_page,
)
from showtime_watcher.targets import Target, pretty_date

log = logging.getLogger(__name__)


@dataclass
class TargetResult:
    target: Target
    status: str  # open | waiting | expired | error | skipped
    shows: list[Show] = field(default_factory=list)  # matching shows (all dates checked)
    unmatched: list[Show] = field(default_factory=list)  # movie on sale, but filtered out
    note: str = ""  # one-line human summary
    venue_name: str = ""
    error: FetchError | None = None


class PageCache:
    """One fetch per (cinema, date) per run, shared by all targets.

    Also the run's view of BookMyShow access: once a request is refused as blocked, no
    further requests are made in that run (knocking again won't help and isn't polite).
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._pages: dict[tuple[str, str], VenuePage | FetchError] = {}
        self.fetched = 0
        self.blocked: BlockedError | None = None
        self.failed: FetchError | None = None

    @property
    def access(self) -> str | None:
        """'ok' / 'blocked' / 'error' for this run, or None if nothing was fetched."""
        if self.blocked:
            return "blocked"
        if self.failed:
            return "error"
        return "ok" if self.fetched else None

    def get(self, venue: Venue, date_code: str) -> VenuePage:
        key = (venue.code, date_code)
        if key not in self._pages:
            if self.blocked:
                raise self.blocked
            self.fetched += 1
            try:
                page = load_venue_page(self.fetcher, venue, date_code)
                self._pages[key] = page
                # A redirect still tells us the page for its real date; remember that too.
                self._pages.setdefault((venue.code, page.page_date), page)
            except BlockedError as exc:
                self.blocked = exc
                self._pages[key] = exc
            except FetchError as exc:
                self.failed = self.failed or exc
                self._pages[key] = exc
        cached = self._pages[key]
        if isinstance(cached, FetchError):
            raise cached
        return cached


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def movie_matches(query: str, movie: Movie) -> bool:
    """Whole-word, case/punctuation-insensitive title match, or an exact ET/EG code."""
    q = query.strip()
    if re.fullmatch(r"(ET|EG)\d{8}", q, re.IGNORECASE):
        return q.upper() == movie.group_code.upper() or q.upper() in movie.event_codes
    needle = _norm(q)
    if not needle:
        return False
    pattern = rf"\b{re.escape(needle)}\b"
    names = [movie.title, *{s.event_name for s in movie.shows}]
    return any(re.search(pattern, _norm(name)) for name in names)


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def show_passes(target: Target, show: Show) -> bool:
    if target.language and _norm(target.language) not in _norm(show.language):
        return False
    if target.format and _norm(target.format) not in _norm(show.dimension):
        return False
    if target.screen and _norm(target.screen) not in _norm(show.screen):
        return False
    if target.after and show.minutes < _hhmm_to_minutes(target.after):
        return False
    return not (target.before and show.minutes > _hhmm_to_minutes(target.before))


def _range_label(codes: list[str]) -> str:
    if not codes:
        return "no dates"
    if len(codes) == 1:
        return pretty_date(codes[0])
    return f"{pretty_date(codes[0])} – {pretty_date(codes[-1])}"


def evaluate(target: Target, cache: PageCache, today_code: str) -> TargetResult:
    """Check one target against BookMyShow (via the shared page cache)."""
    if target.is_expired(today_code):
        return TargetResult(target, "expired", note="all watched dates are in the past")

    try:
        if target.dates:
            wanted = [d for d in target.dates if d >= today_code]
            pages = {d: cache.get(target.venue, d) for d in wanted}
        else:
            # Any date: the first page's date strip says which dates are bookable,
            # then look at each of those dates.
            anchor = cache.get(target.venue, today_code)
            wanted = anchor.bookable_dates or [anchor.page_date]
            pages = {d: cache.get(target.venue, d) for d in wanted}
    except FetchError as exc:
        return TargetResult(target, "error", note=f"check failed: {exc}", error=exc)

    any_page = next(iter(pages.values()))
    venue_name = any_page.venue_name
    bookable = any_page.bookable_dates

    matched: list[Show] = []
    unmatched: list[Show] = []
    open_dates: list[str] = []
    for day, page in sorted(pages.items()):
        if page.page_date != day:  # redirected: this date isn't on sale yet
            continue
        open_dates.append(day)
        for movie in page.movies:
            if not movie_matches(target.movie, movie):
                continue
            for show in movie.shows:
                if show.date_code != day:
                    continue
                (matched if show_passes(target, show) else unmatched).append(show)

    matched.sort(key=lambda s: (s.date_code, s.minutes))
    unmatched.sort(key=lambda s: (s.date_code, s.minutes))
    if matched:
        dates = sorted({s.date_code for s in matched})
        note = f"OPEN: {len(matched)} matching shows on {', '.join(map(pretty_date, dates))}"
        return TargetResult(target, "open", matched, unmatched, note, venue_name)

    if unmatched:
        note = f"on sale, but none of {len(unmatched)} shows match {target.filters_label()}"
    elif target.dates and not open_dates:
        note = f"not on sale yet (cinema is selling {_range_label(bookable)})"
    elif open_dates:
        note = f"date on sale, but {target.movie} isn't listed yet ({_range_label(open_dates)})"
    else:
        note = f"not listed yet (cinema is selling {_range_label(bookable)})"
    return TargetResult(target, "waiting", [], unmatched, note, venue_name)
