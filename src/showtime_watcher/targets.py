"""The watch list (targets.yaml): which movie, at which cinema, on which dates.

Also parses the human-friendly /watch syntax used by the Telegram bot and the CLI, e.g.
``Jailer 2 30 Oct format=dolby https://in.bookmyshow.com/cinemas/.../buytickets/ALUC``.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from showtime_watcher.bms import Venue

FILTER_KEYS = {
    "lang": "language",
    "language": "language",
    "format": "format",
    "fmt": "format",
    "screen": "screen",
    "after": "after",
    "before": "before",
    "notify": "notify",
}
NOTIFY_MODES = ("new", "first")

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_name) if name})
_MONTHS["sept"] = 9
_WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_abbr)}
_WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_name)})
_FILLER_WORDS = {"on", "at", "for", "@", "-", "|", ","}

YAML_HEADER = """\
# Movies to watch on BookMyShow. Add/remove from Telegram (/watch, /stop) or edit here.
# Note: the bot rewrites this file, so comments other than this header are not kept.
#
# targets:
#   - id: 1
#     movie: Jailer 2                 # name (case-insensitive) or ET-code
#     cinema: https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC
#     dates: ['2026-10-30']           # leave out for "any date"
#     language: Telugu                # optional filters...
#     format: dolby                   # matches "DOLBY CINEMA 2D"; also imax, 3d, 4dx ...
#     after: '18:00'                  # earliest show start (24 h)
#     before: '23:59'                 # latest show start
#     notify: new                     # new = every new matching show, first = only once
"""


# ---------------------------------------------------------------- dates
def date_code(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def code_to_date(code: str) -> dt.date:
    return dt.datetime.strptime(code, "%Y%m%d").date()


def pretty_date(code: str) -> str:
    """'20261030' -> 'Fri 30 Oct'."""
    return code_to_date(code).strftime("%a %d %b").replace(" 0", " ")


def _upcoming(day: int, month: int, today: dt.date) -> dt.date:
    """Day/month without a year means the next such date (this year or next)."""
    candidate = dt.date(today.year, month, day)
    return candidate if candidate >= today else dt.date(today.year + 1, month, day)


def _relative_date(s: str, today: dt.date) -> dt.date | None:
    """today / tomorrow / weekday names ('fri' = the coming Friday, today included)."""
    if s == "today":
        return today
    if s in ("tomorrow", "tmrw", "tmr"):
        return today + dt.timedelta(days=1)
    if s in _WEEKDAYS:
        return today + dt.timedelta(days=(_WEEKDAYS[s] - today.weekday()) % 7)
    return None


def parse_one_date(text: str, today: dt.date, *, allow_relative: bool = True) -> dt.date | None:
    """Parse one human date. Returns None if ``text`` isn't a date (never guesses).

    ``allow_relative=False`` rejects words like 'sun' or 'tomorrow', which are also the
    last word of real titles ("Rising Sun", "Edge of Tomorrow").
    """
    s = text.strip().lower().rstrip(",.;")
    if not s:
        return None
    relative = _relative_date(s, today)
    if relative is not None:
        return relative if allow_relative else None
    try:
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s):  # 2026-10-30
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s):  # 30-10-2026
            return dt.date(int(m[3]), int(m[2]), int(m[1]))
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", s):  # 30/10 (Indian order)
            return _upcoming(int(m[1]), int(m[2]), today)
        # 30 Oct / 30oct / 30th October 2026
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]+)\.?(?:\s+(\d{4}))?", s)
        if m and m[2] in _MONTHS:
            day, month, year = int(m[1]), _MONTHS[m[2]], m[3]
            return dt.date(int(year), month, day) if year else _upcoming(day, month, today)
        # Oct 30 / October 30, 2026
        m = re.fullmatch(r"([a-z]+)\.?\s*(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
        if m and m[1] in _MONTHS:
            day, month, year = int(m[2]), _MONTHS[m[1]], m[3]
            return dt.date(int(year), month, day) if year else _upcoming(day, month, today)
    except ValueError:  # e.g. 31 Feb
        return None
    return None


def parse_dates(text: str, today: dt.date) -> list[str]:
    """'30 Oct, 31 Oct' / 'any' / '' -> sorted YYYYMMDD codes ([] means any date)."""
    if text.strip().lower() in ("", "any", "anyday", "any date"):
        return []
    out: set[str] = set()
    for part in re.split(r",|\band\b|&", text):
        if not part.strip():
            continue
        day = parse_one_date(part, today)
        if day is None:
            raise ValueError(f"I can't read the date '{part.strip()}'. Try 30 Oct or 2026-10-30.")
        out.add(date_code(day))
    return sorted(out)


def _split_trailing_dates(tokens: list[str], today: dt.date) -> tuple[list[str], list[str]]:
    """Peel dates off the end of the words: 'Jailer 2 30 Oct' -> (['Jailer','2'], [20261030]).

    Tries the longest date phrase first (3 words, then 2, then 1), repeatedly, and never
    eats the last remaining word. Relative words (today, tomorrow, fri, sun...) only count
    after "on" -- 'Rising Sun' stays a movie, 'Rising Sun on sun' means this Sunday.
    """
    words = list(tokens)
    found: list[str] = []
    progress = True
    while progress and len(words) > 1:
        progress = False
        for size in (3, 2, 1):
            if size >= len(words):
                continue
            phrase = " ".join(words[-size:])
            day = parse_one_date(phrase, today, allow_relative=False)
            if day is None and size == 1 and len(words) > 2 and words[-2].lower() == "on":
                day = parse_one_date(phrase, today)  # "on fri" is unambiguous
            if day is not None:
                found.append(date_code(day))
                del words[-size:]
                while len(words) > 1 and words[-1].lower().strip(",") in _FILLER_WORDS:
                    words.pop()
                progress = True
                break
    return words, sorted(set(found))


def normalize_hhmm(value: str) -> str:
    """'6pm' / '18:00' / '1800' / '6:30 pm' -> '18:00'."""
    s = value.strip().lower().replace(".", ":").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", s)
    if not m:
        raise ValueError(f"I can't read the time '{value}'. Try 18:00 or 6pm.")
    hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"'{value}' isn't a valid time of day.")
    return f"{hour:02d}:{minute:02d}"


# ---------------------------------------------------------------- targets
def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


@dataclass
class Target:
    """One thing to watch: a movie at a cinema, optionally on given dates and with filters."""

    id: int
    movie: str
    venue: Venue
    dates: list[str] = field(default_factory=list)  # YYYYMMDD; empty = any bookable date
    language: str | None = None
    format: str | None = None
    screen: str | None = None
    after: str | None = None  # "HH:MM"
    before: str | None = None  # "HH:MM"
    notify: str = "new"

    def fingerprint(self) -> str:
        """Stable id for state: changes whenever *what* is watched changes."""
        key = {
            "movie": _norm(self.movie),
            "venue": self.venue.code,
            "dates": sorted(self.dates),
            "language": _norm(self.language),
            "format": _norm(self.format),
            "screen": _norm(self.screen),
            "after": self.after,
            "before": self.before,
        }
        return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]

    def has_filters(self) -> bool:
        return any((self.language, self.format, self.screen, self.after, self.before))

    def is_expired(self, today_code: str) -> bool:
        return bool(self.dates) and max(self.dates) < today_code

    def dates_label(self) -> str:
        return ", ".join(pretty_date(d) for d in self.dates) if self.dates else "any date"

    def filters_label(self) -> str:
        parts = [
            f"{name}={value}"
            for name, value in (
                ("lang", self.language),
                ("format", self.format),
                ("screen", self.screen),
                ("after", self.after),
                ("before", self.before),
            )
            if value
        ]
        if self.notify != "new":
            parts.append(f"notify={self.notify}")
        return " ".join(parts)

    def describe(self, venue_name: str | None = None) -> str:
        where = venue_name or self.venue.slug.replace("-", " ").title()
        text = f"{self.movie} · {where} · {self.dates_label()}"
        flt = self.filters_label()
        return f"{text} · {flt}" if flt else text

    def to_yaml(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "movie": self.movie, "cinema": self.venue.base_url}
        if self.dates:
            out["dates"] = [code_to_date(d).isoformat() for d in self.dates]
        for name in ("language", "format", "screen", "after", "before"):
            if value := getattr(self, name):
                out[name] = value
        if self.notify != "new":
            out["notify"] = self.notify
        return out


@dataclass
class WatchList:
    targets: list[Target] = field(default_factory=list)
    default_cinema: Venue | None = None

    def next_id(self) -> int:
        return max((t.id for t in self.targets), default=0) + 1

    def get(self, target_id: int) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)


def _as_date_codes(raw: Any) -> list[str]:
    """YAML gives datetime.date for unquoted 2026-10-30 and str for quoted; accept both."""
    if raw in (None, "", "any"):
        return []
    items = raw if isinstance(raw, list) else [raw]
    codes = []
    for item in items:
        if isinstance(item, dt.date):
            codes.append(date_code(item))
        else:
            s = str(item).strip()
            codes.append(s if re.fullmatch(r"\d{8}", s) else date_code(dt.date.fromisoformat(s)))
    return sorted(set(codes))


def _venue_from_entry(entry: dict[str, Any], default: Venue | None) -> Venue:
    if cinema := entry.get("cinema"):
        return Venue.from_url(str(cinema))
    if entry.get("cinema_code"):  # v1 format: city / cinema_slug / cinema_code
        return Venue(
            str(entry.get("city", "hyderabad")).lower(),
            str(entry["cinema_slug"]).lower(),
            str(entry["cinema_code"]).upper(),
        )
    if default:
        return default
    raise ValueError(f"target {entry.get('movie')!r} has no cinema and there's no default")


def target_from_entry(entry: dict[str, Any], fallback_id: int, default: Venue | None) -> Target:
    notify = str(entry.get("notify", "new")).lower()
    if notify not in NOTIFY_MODES:
        raise ValueError(f"notify must be one of {NOTIFY_MODES}, got {notify!r}")
    return Target(
        id=int(entry.get("id", fallback_id)),
        movie=str(entry["movie"]).strip(),
        venue=_venue_from_entry(entry, default),
        dates=_as_date_codes(entry.get("dates", entry.get("date"))),
        language=entry.get("language") or None,
        format=entry.get("format") or None,
        screen=entry.get("screen") or None,
        after=normalize_hhmm(str(entry["after"])) if entry.get("after") else None,
        before=normalize_hhmm(str(entry["before"])) if entry.get("before") else None,
        notify=notify,
    )


def load_watchlist(path: Path) -> WatchList:
    if not path.exists():
        return WatchList()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    default_cinema = Venue.from_url(defaults["cinema"]) if defaults.get("cinema") else None
    targets: list[Target] = []
    used: set[int] = set()
    for i, entry in enumerate(raw.get("targets") or [], start=1):
        target = target_from_entry(entry, i, default_cinema)
        if target.id in used:  # repair duplicate ids from hand edits
            target.id = max(used) + 1
        used.add(target.id)
        targets.append(target)
    return WatchList(targets=targets, default_cinema=default_cinema)


def save_watchlist(path: Path, watchlist: WatchList) -> None:
    data: dict[str, Any] = {}
    if watchlist.default_cinema:
        data["defaults"] = {"cinema": watchlist.default_cinema.base_url}
    data["targets"] = [t.to_yaml() for t in watchlist.targets]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(YAML_HEADER + "\n" + body, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- /watch syntax
def parse_watch_text(
    text: str,
    *,
    today: dt.date,
    default_cinema: Venue | None,
    next_id: int,
) -> Target:
    """Parse '/watch' arguments: movie words, optional dates, key=value filters, cinema link.

    Order doesn't matter except that dates must come right after the movie name, e.g.
    ``Jailer 2 30 Oct format=dolby <link>`` or ``<link> Rising Sun fri after=6pm``.
    """
    venue: Venue | None = None
    filters: dict[str, str] = {}
    words: list[str] = []
    for token in text.split():
        if token.lower().startswith(("http://", "https://")) or "bookmyshow.com" in token.lower():
            venue = Venue.from_url(token)
        elif "=" in token and token.split("=", 1)[0].lower() in FILTER_KEYS:
            key, value = token.split("=", 1)
            filters[FILTER_KEYS[key.lower()]] = value.strip()
        else:
            words.append(token)

    words, dates = _split_trailing_dates(words, today)
    movie = " ".join(words).strip(" ,;|-@")
    if not movie or parse_one_date(movie, today, allow_relative=False):
        raise ValueError("Which movie? Example: /watch Jailer 2 30 Oct")
    venue = venue or default_cinema
    if venue is None:
        raise ValueError(
            "Which cinema? Add the BookMyShow cinema link to the message, "
            "or set a default once with /default <cinema link>."
        )
    entry: dict[str, Any] = {"id": next_id, "movie": movie, "cinema": venue.base_url}
    if dates:
        entry["dates"] = dates
    entry.update(filters)
    return target_from_entry(entry, next_id, default_cinema)
