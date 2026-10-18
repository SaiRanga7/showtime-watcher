"""Telegram commands, so the watch list can be managed from your phone.

GitHub Actions can't host an always-on bot, so each run *polls* Telegram (getUpdates) for
messages sent since the last run, applies them, and replies after the check -- which
means a /watch reply already contains the first check result. Expect replies within
one run interval (about 5 minutes).

Only messages from TELEGRAM_CHAT_ID are obeyed; anyone else who finds the bot is ignored.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from showtime_watcher.bms import Venue
from showtime_watcher.checker import TargetResult
from showtime_watcher.notify import send_telegram
from showtime_watcher.targets import WatchList, parse_watch_text

log = logging.getLogger(__name__)

HELP = """<b>Showtime Watcher</b> — I check BookMyShow every ~5 min.

<b>/watch</b> movie [dates] [filters] [cinema link]
  /watch Jailer 2 30 Oct
  /watch Rising Sun any format=dolby after=6pm
  /watch Jailer 2 on fri https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC
  Dates: 30 Oct · 30/10 · 2026-10-30 · 30 Oct, 31 Oct · on fri · on tomorrow · (none = any date)
  Filters: lang= format= screen= after= before= notify=first

<b>/default</b> cinema-link — cinema used when /watch has no link
<b>/list</b> — what I'm watching
<b>/stop</b> 2 (or /stop all)
<b>/status</b> — health of the watcher
<b>/test</b> — send a test alert to every channel"""

# A reply is rendered after this run's checks so it can include fresh results.
ReplyRenderer = Callable[[dict[int, TargetResult]], str]


@dataclass
class BotContext:
    watchlist: WatchList
    today: dt.date
    status_renderer: ReplyRenderer
    watchlist_changed: bool = False
    test_requested: bool = False


class TelegramBot:
    def __init__(self, token: str, chat_id: str, client: httpx.Client) -> None:
        self._api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self._client = client

    def get_updates(self, offset: int) -> list[dict]:
        try:
            resp = self._client.get(
                f"{self._api}/getUpdates",
                params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])},
                timeout=20,
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Telegram getUpdates failed: {type(exc).__name__}") from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates refused: {data.get('description')}")
        return list(data.get("result") or [])

    def reply(self, text: str) -> None:
        send_telegram(
            self._client,
            f"{self._api}/sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _status_line(result: TargetResult | None, fallback: str) -> str:
    if result is None:
        return fallback
    icon = {"open": "✅", "waiting": "⏳", "expired": "⌛", "error": "⚠️", "skipped": "⏸"}
    return f"{icon.get(result.status, '•')} {_esc(result.note)}"


def handle_command(text: str, ctx: BotContext) -> ReplyRenderer:
    """Apply one command to the watch list; return how to render the reply later."""
    command, _, args = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()  # "/watch@MyBot" in groups
    args = args.strip()
    wl = ctx.watchlist

    if command in ("/start", "/help"):
        return lambda _results: HELP

    if command == "/watch":
        if not args:
            return lambda _results: "Usage: /watch Jailer 2 30 Oct\n\n" + HELP
        try:
            target = parse_watch_text(
                args, today=ctx.today, default_cinema=wl.default_cinema, next_id=wl.next_id()
            )
        except ValueError as exc:
            message = str(exc)
            return lambda _results: f"❌ {_esc(message)}"
        duplicate = next((t for t in wl.targets if t.fingerprint() == target.fingerprint()), None)
        if duplicate:
            return lambda results: (
                f"Already watching #{duplicate.id}: {_esc(duplicate.describe())}\n"
                + _status_line(results.get(duplicate.id), "")
            )
        wl.targets.append(target)
        ctx.watchlist_changed = True
        tip = ""
        if wl.default_cinema is None:  # first cinema you use becomes the default
            wl.default_cinema = target.venue
            tip = (
                "\n🏠 Saved as your default cinema: "
                "next time just /watch &lt;movie&gt; &lt;date&gt;"
            )
        return lambda results: (
            f"👀 Watching #{target.id}: "
            f"<b>{_esc(target.describe(getattr(results.get(target.id), 'venue_name', None)))}</b>\n"
            + _status_line(results.get(target.id), "First check runs now.")
            + tip
        )

    if command == "/list":

        def render_list(results: dict[int, TargetResult]) -> str:
            if not wl.targets:
                return "Nothing on the watch list. Add one with /watch Jailer 2 30 Oct"
            lines = []
            for t in wl.targets:
                res = results.get(t.id)
                lines.append(f"<b>#{t.id}</b> {_esc(t.describe(res.venue_name if res else None))}")
                lines.append("    " + _status_line(res, "not checked this run"))
            return "\n".join(lines)

        return render_list

    if command == "/stop":
        if args.lower() == "all":
            count = len(wl.targets)
            wl.targets.clear()
            ctx.watchlist_changed = True
            return lambda _results: f"🗑 Stopped all {count} watches."
        ids = {int(a) for a in args.replace(",", " ").replace("#", "").split() if a.isdigit()}
        removed = [t for t in wl.targets if t.id in ids]
        if not removed:
            return lambda _results: "Which one? Send /list, then e.g. /stop 2"
        wl.targets[:] = [t for t in wl.targets if t.id not in ids]
        ctx.watchlist_changed = True
        summary = "\n".join(f"🗑 #{t.id} {_esc(t.describe())}" for t in removed)
        return lambda _results: summary

    if command == "/default":
        try:
            wl.default_cinema = Venue.from_url(args)
        except ValueError as exc:
            message = str(exc)
            return lambda _results: f"❌ {_esc(message)}"
        ctx.watchlist_changed = True
        venue = wl.default_cinema
        return lambda _results: f"🏠 Default cinema set: {_esc(venue.slug)} ({venue.code})"

    if command == "/test":
        ctx.test_requested = True
        return lambda _results: "🔔 Test alert sent to every configured channel."

    if command == "/status":
        return ctx.status_renderer

    link = next((t for t in text.split() if "bookmyshow.com" in t.lower()), None)
    if link:
        return lambda _results: (
            "To watch a movie at that cinema, send:\n/watch &lt;movie&gt; [date] &lt;link&gt;\n"
            "e.g. /watch Jailer 2 30 Oct " + _esc(link)
        )
    return lambda _results: "I didn't get that.\n\n" + HELP
