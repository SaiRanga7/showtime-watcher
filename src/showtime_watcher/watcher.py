"""One watcher pass: read Telegram commands, check every target, send alerts, remember."""

from __future__ import annotations

import contextlib
import datetime as dt
import html
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from showtime_watcher.bms import BMS_BASE, Fetcher, FetchError, Show
from showtime_watcher.bot import BotContext, ReplyRenderer, TelegramBot, handle_command
from showtime_watcher.checker import PageCache, TargetResult, evaluate
from showtime_watcher.config import Settings
from showtime_watcher.notify import Alert, Notifier
from showtime_watcher.state import State, load_state, save_state
from showtime_watcher.targets import (
    Target,
    date_code,
    load_watchlist,
    pretty_date,
    save_watchlist,
)

log = logging.getLogger(__name__)


BACKOFF_EVERY = 3  # while blocked, scheduled runs try BookMyShow on 1 in 3 slots (~15 min)
BLIND_REPEAT_HOURS = 6  # don't repeat the "can't read BookMyShow" alert more often


@dataclass
class RunReport:
    results: list[TargetResult] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    access: str | None = None  # ok | blocked | error | None (BookMyShow not contacted)
    skipped: bool = False  # backing off this run
    access_note: str = ""  # why it was blocked/failed, for logs and annotations
    state_changed: bool = False
    watchlist_changed: bool = False


# ---------------------------------------------------------------- alert builders
def build_show_alert(result: TargetResult, new_shows: list[Show], first: bool) -> Alert:
    target = result.target
    venue = result.venue_name or target.venue.code
    if first:
        kind, title = "open", f"🚨 Bookings OPEN: {target.movie} @ {venue}"
    else:
        plural = "s" if len(new_shows) > 1 else ""
        kind, title = "more", f"➕ {len(new_shows)} new show{plural}: {target.movie} @ {venue}"

    lines: list[str] = []
    by_date: dict[str, list[Show]] = defaultdict(list)
    for show in new_shows:
        by_date[show.date_code].append(show)
    for day in sorted(by_date):
        lines.append(f"📅 {pretty_date(day)}")
        by_variant: dict[tuple[str, str], list[Show]] = defaultdict(list)
        for show in by_date[day]:
            by_variant[(show.language, show.dimension)].append(show)
        for (language, dimension), shows in by_variant.items():
            lines.append(f"  {language} · {dimension}")
            for show in shows:
                price = f" · {show.price_label}" if show.price_label else ""
                lines.append(f"   • {show.time} · {show.screen}{price}")
    lines.append("Tap a show to jump straight to seat selection.")

    links = [
        (
            f"{pretty_date(s.date_code)} · {s.time} · {s.screen}",
            s.seat_layout_url(target.venue.city, target.venue.code),
        )
        for s in new_shows[:8]
    ]
    first_day = min(by_date)
    return Alert(kind, title, "\n".join(lines), url=target.venue.page_url(first_day), links=links)


def build_partial_alert(result: TargetResult) -> Alert:
    target = result.target
    venue = result.venue_name or target.venue.code
    sample = "\n".join(
        f" • {pretty_date(s.date_code)} {s.time} · {s.language} {s.dimension} · {s.screen}"
        for s in result.unmatched[:8]
    )
    body = (
        f"{target.movie} is on sale at {venue}, but no show matches {target.filters_label()}.\n"
        f"On sale now:\n{sample}\n"
        "I'll keep watching and alert you when a matching show appears."
    )
    url = target.venue.page_url(result.unmatched[0].date_code)
    return Alert("partial", f"ℹ️ {target.movie} is on sale (not as filtered)", body, url=url)


def build_test_alert() -> Alert:
    return Alert(
        "test",
        "🔔 Test alert from Showtime Watcher",
        "If you can read this, this channel works.\n"
        "A real alert lists the shows, and each button opens seat selection.",
        url=BMS_BASE,
        links=[("Open BookMyShow", BMS_BASE)],
    )


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _short_time(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat(iso).strftime("%d %b %H:%M")
    except ValueError:
        return iso


# ---------------------------------------------------------------- the pass
def run_once(
    settings: Settings,
    *,
    fetcher: Fetcher,
    notifier: Notifier,
    bot: TelegramBot | None,
    now: dt.datetime,
    dry_run: bool = False,
    backoff: bool = False,
) -> RunReport:
    """Do one complete pass. ``now`` must be timezone-aware (IST).

    ``backoff=True`` (scheduled runs) slows down to ~15 min while BookMyShow is refusing us.
    """
    report = RunReport()
    watchlist = load_watchlist(settings.targets_path)
    state = load_state(settings.state_path)
    today = now.date()
    today_code = date_code(today)

    def render_status(results: dict[int, TargetResult]) -> str:
        health = state.health
        lines = ["🩺 <b>Watcher status</b>"]
        if health.failing_since:
            lines.append(
                f"⚠️ Can't read BookMyShow since {_short_time(health.failing_since)}: "
                f"{_esc(health.last_error)}"
            )
        else:
            lines.append("✅ Checks are reaching BookMyShow")
        if (share := health.availability(now)) is not None:
            lines.append(f"Reachable {share:.0%} of the last 24 h")
        channels = ", ".join(notifier.names) or "NONE configured!"
        lines.append(f"Watching {len(watchlist.targets)} · alerts via {channels}")
        for t in watchlist.targets:
            res = results.get(t.id)
            note = res.note if res else "not checked"
            lines.append(f"#{t.id} {_esc(t.movie)}: {_esc(note)}")
        return "\n".join(lines)

    # 1) Telegram commands (applied before checking, replied to after).
    ctx = BotContext(watchlist=watchlist, today=today, status_renderer=render_status)
    replies: list[ReplyRenderer] = []
    if bot:
        try:
            updates = bot.get_updates(state.telegram_offset)
        except RuntimeError as exc:
            log.warning("%s", settings.redact(str(exc)))
            updates = []
        for update in updates:
            state.telegram_offset = max(state.telegram_offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = (message.get("text") or "").strip()
            if chat_id != bot.chat_id:
                log.warning("Ignoring a Telegram message from another chat (%s)", chat_id)
                continue
            if text:
                log.info("Telegram command: %s", text[:100])
                replies.append(handle_command(text, ctx))
    if ctx.watchlist_changed and not dry_run:
        save_watchlist(settings.targets_path, watchlist)
        report.watchlist_changed = True
    if ctx.test_requested:
        notifier.send(build_test_alert())

    # 2) Check every target (pages are shared between targets at the same cinema).
    cache = PageCache(fetcher)
    report.skipped = backoff and _backing_off(state, now)
    results: list[TargetResult] = []
    for t in watchlist.targets:
        if report.skipped and not t.is_expired(today_code):
            note = "paused: BookMyShow refused recent checks, retrying every ~15 min"
            results.append(TargetResult(t, "skipped", note=note))
        else:
            results.append(evaluate(t, cache, today_code))
    if not report.skipped and cache.fetched == 0 and watchlist.default_cinema:
        # Nothing to watch: still look once, so reachability and health stay measured.
        with contextlib.suppress(FetchError):
            cache.get(watchlist.default_cinema, today_code)
    report.results = results
    report.access = cache.access
    if cache.blocked or cache.failed:
        report.access_note = settings.redact(str(cache.blocked or cache.failed))[:300]
    for r in results:
        log.info("#%d %s -> %s: %s", r.target.id, r.target.movie, r.status, r.note)
    log.info("BookMyShow access this run: %s %s", report.access or "-", report.access_note)

    # 3) Alerts for new shows (state is updated only for alerts that were delivered).
    for r in results:
        ts = state.for_target(r.target.fingerprint())
        if r.status in ("error", "skipped"):
            continue
        ts.note = r.note
        if r.status == "expired":
            ts.status = "expired"
            continue
        new_shows = [s for s in r.shows if s.key not in ts.notified]
        if new_shows:
            first = ts.status != "open"
            if first or r.target.notify == "new":
                alert = build_show_alert(r, new_shows, first)
                if not notifier.send(alert) and notifier.channels:
                    log.error("No channel delivered the alert; will retry next run")
                    continue
                report.alerts.append(alert.title)
            ts.notified.extend(s.key for s in new_shows)
            if first:
                ts.first_open_at = now.isoformat(timespec="minutes")
            ts.status = "open"
        elif (
            r.status == "waiting"
            and r.unmatched
            and r.target.has_filters()
            and not ts.partial_alerted
        ):
            alert = build_partial_alert(r)
            if notifier.send(alert) or not notifier.channels:
                ts.partial_alerted = True
                report.alerts.append(alert.title)

    # 4) Health: log reachability changes, shout if blind for a while, and on recovery.
    if report.access:
        state.health.record_access(now, report.access)
        _update_health(settings, state, notifier, report.access, report.access_note, now)

    # 5) Daily check-in. Its state change also gives the repo a commit every day, which
    #    stops GitHub from disabling the schedule after 60 days without activity.
    if state.health.last_daily != today.isoformat() and now.hour >= settings.daily_hour:
        state.health.last_daily = today.isoformat()
        if settings.daily_message:
            notifier.send(_daily_alert(watchlist.targets, results, state, now))

    # 6) Replies to this run's Telegram commands, now that results are known.
    if bot and replies:
        by_id = {r.target.id: r for r in results}
        for render in replies:
            text = render(by_id)
            if dry_run:
                log.info("[dry-run] would reply: %s", text[:200])
                continue
            try:
                bot.reply(text)
            except RuntimeError as exc:
                log.error("Reply failed: %s", settings.redact(str(exc)))

    # 7) Remember.
    state.prune({t.fingerprint() for t in watchlist.targets}, today_code)
    if not dry_run:
        report.state_changed = save_state(settings.state_path, state)
    return report


def _backing_off(state: State, now: dt.datetime) -> bool:
    """While failing, only every BACKOFF_EVERY-th 5-minute slot contacts BookMyShow."""
    since = state.health.failing_since
    if not since:
        return False
    slot = int((now - dt.datetime.fromisoformat(since)).total_seconds() // 300)
    return slot % BACKOFF_EVERY != 0


def _update_health(
    settings: Settings,
    state: State,
    notifier: Notifier,
    access: str,
    error_text: str,
    now: dt.datetime,
) -> None:
    health = state.health
    if access != "ok":
        if not health.failing_since:
            health.failing_since = now.isoformat(timespec="minutes")
            health.last_error = error_text
        failing_for = now - dt.datetime.fromisoformat(health.failing_since)
        recently_alerted = health.last_blind_alert and (
            now - dt.datetime.fromisoformat(health.last_blind_alert)
            < dt.timedelta(hours=BLIND_REPEAT_HOURS)
        )
        if (
            failing_for >= dt.timedelta(minutes=settings.blind_after_minutes)
            and not health.blind_alert_sent
            and not recently_alerted
        ):
            why = (
                "BookMyShow's bot protection is refusing GitHub's servers"
                if access == "blocked"
                else f"Checks are failing: {health.last_error}"
            )
            alert = Alert(
                "blind",
                "⚠️ Showtime Watcher can't read BookMyShow",
                f"{why} (since {_short_time(health.failing_since)}).\n"
                "Ticket alerts can't fire meanwhile. I keep trying every ~15 min and "
                "will message you when it gets through.",
                url=f"{BMS_BASE}/",
            )
            if notifier.send(alert) or not notifier.channels:
                health.blind_alert_sent = True
                health.last_blind_alert = now.isoformat(timespec="minutes")
    elif health.failing_since:
        if health.blind_alert_sent:
            notifier.send(
                Alert(
                    "recovered",
                    "✅ Showtime Watcher is back",
                    f"Checks work again (they were failing since "
                    f"{_short_time(health.failing_since)}).",
                )
            )
        health.failing_since = None
        health.last_error = ""
        health.blind_alert_sent = False


def _daily_alert(
    targets: list[Target], results: list[TargetResult], state: State, now: dt.datetime
) -> Alert:
    by_id = {r.target.id: r for r in results}
    if not targets:
        body = "Nothing on the watch list. Send /watch <movie> <date> to add one."
    else:
        rows = []
        for t in targets:
            res = by_id.get(t.id)
            rows.append(f"#{t.id} {t.describe(res.venue_name if res else None)}")
            rows.append(f"    {res.note if res else '-'}")
        body = "\n".join(rows)
    if (share := state.health.availability(now)) is not None:
        body += f"\nBookMyShow reachable from GitHub: {share:.0%} of the last 24 h"
    if state.health.failing_since:
        body += f"\n⚠️ Can't read BookMyShow since {_short_time(state.health.failing_since)}"
    return Alert("daily", "👀 Daily check-in: watcher is running", body, silent=True)


# ---------------------------------------------------------------- GitHub integration
def write_job_summary(path: Path, report: RunReport, now: dt.datetime) -> None:
    access = {
        "ok": "✅ reached BookMyShow",
        "blocked": "⛔ BookMyShow refused this check (bot protection)",
        "error": f"⚠️ check failed: {report.access_note}",
    }.get(report.access or "", "⏸ paused (backing off)" if report.skipped else "– not contacted")
    lines = [
        f"### Showtime Watcher · {now:%d %b %Y %H:%M} IST · {access}",
        "",
        "| # | Movie | Cinema | Dates | Status |",
        "|---|---|---|---|---|",
    ]
    for r in report.results:
        t = r.target
        cinema = r.venue_name or t.venue.code
        lines.append(f"| {t.id} | {t.movie} | {cinema} | {t.dates_label()} | {r.note} |")
    if not report.results:
        lines.append("| – | nothing on the watch list | | | |")
    if report.alerts:
        lines += ["", "**Alerts sent:** " + "; ".join(report.alerts)]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
