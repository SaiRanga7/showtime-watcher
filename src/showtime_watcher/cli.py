"""Command line: `showtime-watcher run | probe | test-alert | list | add | remove`."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from showtime_watcher import __version__
from showtime_watcher.bms import BlockedError, FetchError, HttpFetcher, Venue, load_venue_page
from showtime_watcher.bot import TelegramBot
from showtime_watcher.config import IST, Settings, load_settings
from showtime_watcher.notify import build_notifier
from showtime_watcher.targets import (
    date_code,
    load_watchlist,
    parse_dates,
    parse_watch_text,
    pretty_date,
    save_watchlist,
)
from showtime_watcher.watcher import RunReport, build_test_alert, run_once, write_job_summary

log = logging.getLogger("showtime_watcher")


def _github_output(name: str, value: str) -> None:
    """Expose a value to later workflow steps (no-op outside GitHub Actions)."""
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def _summary_path() -> Path | None:
    value = os.environ.get("GITHUB_STEP_SUMMARY")
    return Path(value) if value else None


# ---------------------------------------------------------------- commands
def _annotate(report: RunReport) -> None:
    """Show BookMyShow access on the run's page in GitHub (Annotations box)."""
    if report.access == "blocked":
        print(f"::warning title=BookMyShow refused this check::{report.access_note}")
    elif report.access == "error":
        print(f"::warning title=BookMyShow check failed::{report.access_note}")
    elif report.skipped:
        print("::notice title=Paused::BookMyShow refused recent checks; retrying every ~15 min")


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    with httpx.Client(timeout=20) as client:
        notifier = build_notifier(settings, client, dry_run=args.dry_run)
        bot = None
        if settings.telegram_token and settings.telegram_chat_id and not args.no_bot:
            bot = TelegramBot(settings.telegram_token, settings.telegram_chat_id, client)
        fetcher = HttpFetcher()
        try:
            while True:
                now = dt.datetime.now(IST)
                report = run_once(
                    settings,
                    fetcher=fetcher,
                    notifier=notifier,
                    bot=bot,
                    now=now,
                    dry_run=args.dry_run,
                    backoff=scheduled or bool(args.loop),
                )
                _github_output("changed", str(report.state_changed or report.watchlist_changed))
                if settings.github_actions:
                    _annotate(report)
                if summary := _summary_path():
                    write_job_summary(summary, report, now)
                if not args.loop:
                    return 0
                log.info("Sleeping %d s (Ctrl+C to stop)", args.loop)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            return 0
        finally:
            fetcher.close()


def cmd_probe(settings: Settings, args: argparse.Namespace) -> int:
    """Fetch one cinema page and print what's on: the quickest end-to-end sanity check."""
    watchlist = load_watchlist(settings.targets_path)
    try:
        venue = Venue.from_url(args.cinema) if args.cinema else watchlist.default_cinema
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if venue is None:
        log.error("Give a cinema link, or set defaults.cinema in targets.yaml")
        return 2
    today = dt.datetime.now(IST).date()
    try:
        parsed = parse_dates(args.date, today)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    day = parsed[0] if parsed else date_code(today)

    fetcher = HttpFetcher()
    try:
        page = load_venue_page(fetcher, venue, day)
    except BlockedError as exc:
        log.error("BLOCKED: %s", exc)
        if settings.github_actions:
            print(f"::error title=BookMyShow refused GitHub's server::{exc}")
        return 3
    except FetchError as exc:
        log.error("FAILED: %s", exc)
        return 1
    finally:
        fetcher.close()

    lines = [
        f"{page.venue_name} ({page.venue_code})",
        f"Asked for {pretty_date(day)}; page shows {pretty_date(page.page_date)}"
        + ("" if page.page_date == day else "  <- redirected: that date isn't on sale yet"),
        "Bookable: " + (", ".join(pretty_date(d) for d in page.bookable_dates) or "none"),
        "Not yet:  "
        + (", ".join(pretty_date(d.date_code) for d in page.dates if not d.bookable) or "-"),
        "",
    ]
    for movie in page.movies:
        variants: dict[str, list[str]] = {}
        for show in movie.shows:
            variants.setdefault(f"{show.language} {show.dimension}", []).append(
                f"{show.time} ({show.screen})"
            )
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in variants.items())
        lines.append(f"• {movie.title} — {detail}")
    text = "\n".join(lines)
    print(text)
    if summary := _summary_path():
        with summary.open("a", encoding="utf-8") as fh:
            fh.write("### Probe\n```\n" + text + "\n```\n")
    return 0


def cmd_test_alert(settings: Settings, _args: argparse.Namespace) -> int:
    with httpx.Client(timeout=20) as client:
        notifier = build_notifier(settings, client)
        if not notifier.channels:
            log.error("No channels configured. Set the secrets first (see README).")
            return 2
        delivered = notifier.send(build_test_alert())
    failed = sorted(set(notifier.names) - set(delivered))
    print(f"Delivered via: {', '.join(delivered) or 'nothing'}")
    if failed:
        print(f"FAILED: {', '.join(failed)} (see the error lines above)")
        return 1
    return 0


def cmd_list(settings: Settings, _args: argparse.Namespace) -> int:
    watchlist = load_watchlist(settings.targets_path)
    if watchlist.default_cinema:
        print(f"Default cinema: {watchlist.default_cinema.base_url}")
    if not watchlist.targets:
        print("Watch list is empty.")
    for t in watchlist.targets:
        print(f"#{t.id} {t.describe()}")
    return 0


def cmd_add(settings: Settings, args: argparse.Namespace) -> int:
    watchlist = load_watchlist(settings.targets_path)
    try:
        target = parse_watch_text(
            " ".join(args.text),
            today=dt.datetime.now(IST).date(),
            default_cinema=watchlist.default_cinema,
            next_id=watchlist.next_id(),
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    watchlist.targets.append(target)
    if watchlist.default_cinema is None:  # first cinema you use becomes the default
        watchlist.default_cinema = target.venue
    save_watchlist(settings.targets_path, watchlist)
    print(f"Added #{target.id}: {target.describe()}")
    return 0


def cmd_remove(settings: Settings, args: argparse.Namespace) -> int:
    watchlist = load_watchlist(settings.targets_path)
    before = len(watchlist.targets)
    watchlist.targets = [t for t in watchlist.targets if t.id not in set(args.ids)]
    save_watchlist(settings.targets_path, watchlist)
    print(f"Removed {before - len(watchlist.targets)} target(s).")
    return 0


# ---------------------------------------------------------------- entry point
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="showtime-watcher",
        description="Alert me when BookMyShow opens bookings for a movie at my cinema.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root", type=Path, help="folder with targets.yaml/state.json (default: .)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="one full pass (what GitHub Actions runs every 5 min)")
    run.add_argument("--dry-run", action="store_true", help="check and log, but send/save nothing")
    run.add_argument("--no-bot", action="store_true", help="skip reading Telegram commands")
    run.add_argument("--loop", type=int, metavar="SECONDS", help="repeat forever (local use)")
    run.set_defaults(func=cmd_run)

    probe = sub.add_parser("probe", help="fetch one cinema page and print what's on")
    probe.add_argument("cinema", nargs="?", default="", help="cinema link (default: defaults)")
    probe.add_argument("--date", default="", help="e.g. 2026-10-30, '30 Oct', tomorrow")
    probe.set_defaults(func=cmd_probe)

    test = sub.add_parser("test-alert", help="send a test alert to every configured channel")
    test.set_defaults(func=cmd_test_alert)

    lst = sub.add_parser("list", help="show the watch list")
    lst.set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="add a target, same words as Telegram /watch")
    add.add_argument("text", nargs="+", help="e.g. Jailer 2 30 Oct format=dolby <cinema link>")
    add.set_defaults(func=cmd_add)

    rm = sub.add_parser("remove", help="remove targets by id")
    rm.add_argument("ids", nargs="+", type=int)
    rm.set_defaults(func=cmd_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # httpx logs every request URL at INFO; Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = load_settings(args.root)
    return int(args.func(settings, args))


if __name__ == "__main__":
    sys.exit(main())
