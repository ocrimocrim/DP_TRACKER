import os
import json
import logging
import datetime
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------
# Failsafe-Logging-System: erstellt IMMER eine Logdatei
# ----------------------------------------------------------
timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
log_filename = f"dpwt_debug_failsafe_{timestamp}.log"

logging.basicConfig(
    filename=log_filename,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logging.info("=== DPWT Leaderboard Watcher gestartet ===")

# Discord Webhook (zweiter Kanal)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_2", "").strip()
if not DISCORD_WEBHOOK:
    logging.error("Kein DISCORD_WEBHOOK_2 gefunden – bitte im GitHub Secret setzen.")
else:
    logging.info("Webhook erkannt.")

PLAYER_URL = "https://www.europeantour.com/players/marcel-schneider-35703/?tour=dpworld-tour"

# ----------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------

def send_discord_message(content):
    """Sendet Text an Discord, mit Logging."""
    try:
        if not DISCORD_WEBHOOK:
            logging.warning("Kein Discord-Webhook verfügbar. Nachricht wird übersprungen.")
            return
        resp = requests.post(DISCORD_WEBHOOK, json={"content": content})
        if resp.status_code == 204:
            logging.info("Discord-Post erfolgreich.")
        else:
            logging.warning(f"Discord-Post fehlgeschlagen: {resp.status_code}")
    except Exception as e:
        logging.exception(f"Fehler beim Discord-Post: {e}")

def fetch_html_with_playwright(url):
    """Lädt HTML über echten Browser (Bypass Cloudflare)."""
    try:
        logging.info(f"Starte Playwright-Browser für URL: {url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=60000)
            html = page.content()
            browser.close()
            logging.info("Playwright-HTML erfolgreich geladen.")
            return html
    except Exception as e:
        logging.exception(f"Playwright-Fehler bei {url}: {e}")
        return ""

def find_playing_this_week_event(html):
    """Parst die aktuelle Turnier-Info aus der Profilseite."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        section = soup.find("h2", string=lambda s: s and "Playing this week" in s)
        if not section:
            logging.warning("Kein Abschnitt 'Playing this week' gefunden.")
            return None
        container = section.find_next("div", class_="table__row")
        if not container:
            logging.warning("Kein Turniercontainer unter 'Playing this week' gefunden.")
            return None

        date_div = container.find("div", class_="table__cell-inner")
        event_link = container.find("a", href=True)
        event_name = event_link.text.strip() if event_link else "Unbekannt"
        event_url = "https://www.europeantour.com" + event_link["href"] if event_link else None
        event_date = date_div.text.strip() if date_div else "?"
        logging.info(f"Aktives Turnier erkannt: {event_name} ({event_date}) {event_url}")
        return {"name": event_name, "date": event_date, "url": event_url}
    except Exception as e:
        logging.exception(f"Fehler beim Parsen des aktiven Events: {e}")
        return None

# ----------------------------------------------------------
# Hauptablauf
# ----------------------------------------------------------

def main():
    logging.info("Lade Profilseite mit Playwright...")
    html = fetch_html_with_playwright(PLAYER_URL)
    if not html:
        logging.error("Profilseite konnte nicht geladen werden.")
        send_discord_message("❌ Fehler: Profilseite konnte nicht geladen werden.")
        return

    event = find_playing_this_week_event(html)
    if not event:
        logging.warning("Kein aktives Event gefunden.")
        send_discord_message("ℹ️ Kein aktives Event gefunden.")
        return

    # Testweise Discord-Nachricht, wenn Turnier erkannt wurde
    msg = (
        f"🏌️‍♂️ Marcel Schneider spielt diese Woche:\n"
        f"**{event['name']}**\n"
        f"Datum: {event['date']}\n"
        f"Link: {event['url']}"
    )
    send_discord_message(msg)
    logging.info("Turniernachricht erfolgreich gesendet.")

    # Logfile sichern
    logging.info("=== Lauf erfolgreich abgeschlossen ===")

    # Letzten Zustand speichern (JSON)
    with open("last_run_state.json", "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2, ensure_ascii=False)
    logging.info("Zustand gespeichert: last_run_state.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(f"Hauptfehler: {e}")
        send_discord_message(f"❌ Bot-Abbruch wegen Fehler: {e}")
    finally:
        logging.info("Beende Scriptlauf.")
