import os
import re
import sqlite3
import time
import logging
from pathlib import Path
import requests
import yaml
from playwright.sync_api import sync_playwright
from dotenv import dotenv_values

ROOT = Path(__file__).parent

# Prefer env vars (CI), fall back to .env file (local dev)
SECRETS = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN") or SECRETS.get("TELEGRAM_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID") or SECRETS.get("TELEGRAM_CHAT_ID")
if not TG_TOKEN or not TG_CHAT:
    raise SystemExit("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("watcher.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ---------- state ----------
def db():
    conn = sqlite3.connect(ROOT / "state.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notified (
            key TEXT PRIMARY KEY,
            notified_at TEXT
        )
    """)
    return conn

def already_notified(conn, key):
    return conn.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone() is not None

def mark_notified(conn, key):
    conn.execute("INSERT OR REPLACE INTO notified VALUES (?, datetime('now'))", (key,))
    conn.commit()


# ---------- notify ----------
def notify(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        r.raise_for_status()
        log.info("Telegram sent")
    except Exception as e:
        log.error("Telegram failed: %s", e)


# ---------- core check ----------
def build_url(target):
    """Cinema URL pattern: /cinemas/<city>/<slug>/buytickets/<code>/<YYYYMMDD>"""
    date_compact = target["date"].replace("-", "")
    return (
        f"https://in.bookmyshow.com/cinemas/{target['city']}/"
        f"{target['cinema_slug']}/buytickets/{target['cinema_code']}/{date_compact}"
    )

def check_target(page, target):
    """
    Returns dict: {open: bool, movie_found: bool, showtimes: [str], reason: str}
    """
    url = build_url(target)
    log.info("Fetching %s", url)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        return {"open": False, "movie_found": False, "showtimes": [], "reason": f"goto failed: {e}"}

    page.wait_for_timeout(4000)

    body_text = page.locator("body").inner_text()

    closed_markers = [
        "Oops", "no shows", "No shows", "could not find",
        "not available", "Coming Soon"
    ]
    is_closed = any(m.lower() in body_text.lower() for m in closed_markers)

    movie_query = target["movie"].lower()
    movie_links = page.locator(f"a[href*='/movies/{target['city']}/']").all()

    matched_showtimes = []
    movie_found = False
    for link in movie_links:
        try:
            title = link.inner_text().strip()
        except Exception:
            continue
        if movie_query not in title.lower():
            continue
        movie_found = True
        row = link.locator("xpath=ancestor::div[@role='gridcell'][1]")
        if row.count() == 0:
            row = link.locator("xpath=ancestor::div[3]")
        try:
            row_text = row.inner_text()
        except Exception:
            row_text = ""
        times = re.findall(r"\b\d{1,2}:\d{2}\s?[AP]M\b", row_text)
        matched_showtimes.extend(times)

    is_open = (not is_closed) and movie_found and len(matched_showtimes) > 0

    return {
        "open": is_open,
        "movie_found": movie_found,
        "showtimes": list(dict.fromkeys(matched_showtimes)),
        "reason": "ok" if is_open else (
            "page closed/empty" if is_closed else
            "movie not listed" if not movie_found else
            "no showtimes parsed"
        ),
    }


# ---------- main ----------
def run_once():
    with open(ROOT / "targets.yaml") as f:
        targets = yaml.safe_load(f)["targets"]

    conn = db()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
        )
        page = ctx.new_page()

        for t in targets:
            key = f"{t['movie']}|{t['cinema_code']}|{t['date']}"
            if already_notified(conn, key):
                log.info("Skip (already notified): %s", key)
                continue
            try:
                result = check_target(page, t)
                log.info("%s -> %s", key, result)
                if result["open"]:
                    times = ", ".join(result["showtimes"][:6]) or "see page"
                    notify(
                        f"🎬 Booking OPEN!\n"
                        f"Movie: {t['movie']}\n"
                        f"Cinema: {t['cinema_name']}\n"
                        f"Date:   {t['date']}\n"
                        f"Times:  {times}\n"
                        f"{build_url(t)}"
                    )
                    mark_notified(conn, key)
            except Exception:
                log.exception("Check failed for %s", key)

        browser.close()
    conn.close()


if __name__ == "__main__":
    if os.environ.get("ONESHOT") == "1":
        log.info("One-shot mode")
        run_once()
    else:
        interval = int(os.environ.get("POLL_SECONDS", "600"))
        log.info("Loop mode, interval=%ds", interval)
        while True:
            try:
                run_once()
            except Exception:
                log.exception("run_once crashed")
            time.sleep(interval)