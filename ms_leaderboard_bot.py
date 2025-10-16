import os
import json
import time
import logging
import random
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

# Playwright-Fallback
from playwright.sync_api import sync_playwright

# Discord Webhook (zweiter Channel)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_MS")

# Logfile im Repo
LOG_FILE = f"dpwt_watcher_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.log"
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

PLAYER_URL = "https://www.europeantour.com/players/marcel-schneider-35703/?tour=dpworld-tour"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/127.0.0.1 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.europeantour.com/",
}

def send_discord_message(msg: str):
    if not DISCORD_WEBHOOK:
        logging.error("Kein Discord Webhook gesetzt")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg})
    except Exception as e:
        logging.error(f"Discord-Fehler: {e}")

def fetch_html_requests(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 403:
        raise requests.exceptions.HTTPError("403 Forbidden")
    resp.raise_for_status()
    return resp.text

def fetch_html_playwright(url):
    """Playwright-Browser-Request mit echten TLS-Fingerprints"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="de-DE",
                                  user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        page.goto(url, timeout=60000)
        html = page.content()
        browser.close()
        return html

def get_html(url):
    """Versuche Requests → Fallback auf Playwright"""
    try:
        logging.info(f"Lese Profilseite: {url}")
        html = fetch_html_requests(url)
        return html
    except Exception as e:
        logging.warning(f"Requests fehlgeschlagen ({e}), versuche Playwright...")
        try:
            html = fetch_html_playwright(url)
            return html
        except Exception as e2:
            logging.error(f"Playwright ebenfalls fehlgeschlagen: {e2}")
            return None

def parse_next_event(html):
    soup = BeautifulSoup(html, "html.parser")
    playing = soup.find("div", string=lambda t: t and "Playing this week" in t)
    if not playing:
        return None
    section = playing.find_next("a", href=True)
    if not section:
        return None
    name = section.get_text(strip=True)
    link = "https://www.europeantour.com" + section["href"]
    date_elem = section.find_previous("div", class_="table__cell-inner")
    date_str = date_elem.get_text(strip=True) if date_elem else None
    return {"name": name, "link": link, "date": date_str}

def main():
    try:
        html = get_html(PLAYER_URL)
        if not html:
            send_discord_message("❌ Konnte Profilseite nicht laden (403/Timeout).")
            return
        event = parse_next_event(html)
        if not event:
            send_discord_message("Kein aktives Event gefunden.")
            logging.info("Kein Event in 'Playing this week'")
            return

        msg = (f"🏌️‍♂️ Nächstes Event erkannt!\n\n"
               f"**{event['name']}**\n"
               f"Datum: {event['date']}\n"
               f"Link: {event['link']}")
        send_discord_message(msg)
        logging.info(f"Aktives Event: {event}")
    except Exception as e:
        logging.exception("Fehler im Main:")
        send_discord_message(f"❌ Fehler im Run: {e}")

if __name__ == "__main__":
    main()
    # Log-Datei persistieren
    try:
        with open("last_run.json", "w", encoding="utf-8") as f:
            json.dump({"last_run": datetime.utcnow().isoformat()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Konnte last_run.json nicht schreiben: {e}")
