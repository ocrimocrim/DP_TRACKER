#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DPWT Leaderboard Watcher – robuste Event-Erkennung + Debug-Logging

Was macht das Skript?
- Lädt die Profilseite des Spielers (Overview).
- Extrahiert das aktuelle Event (Link + EventId) robust:
  1) JSON aus <live-event-banner :events="[...]">
  2) Fallback: Link in der "Playing this week"-Tabelle (a.event_tournament-link)
- Schreibt immer Log-File + HTML-Snapshot.
- Meldet per Discord:
  - 2 Tage vor Turnierstart (zweiter Webhook).
  - Optional: „Aktives Event erkannt“ (erster Webhook), damit man die Erkennung sieht.

Hinweis:
- Reines HTML-Scraping – kein JS nötig.
- Zeitzonen sauber: StartDate ist UTC (ISO-8601); Vergleich in UTC.

ENV-Variablen (siehe Workflow unten):
- PLAYER_SLUG (z.B. "marcel-schneider-35703")
- TOUR_SLUG (z.B. "dpworld-tour")
- DISCORD_WEBHOOK_ANNOUNCE  (dein „normaler“ Kanal)
- DISCORD_WEBHOOK_SECOND    (zweiter Hook für Vorankündigungen)
- USER_AGENT (optional, sonst Browser-ähnlich)
"""

import os
import re
import json
import time
import pathlib
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry


# ---------------------------
# Konfiguration / Pfade
# ---------------------------

RUN_ID = time.strftime("%Y%m%d-%H%M%S")
BASE_URL = "https://www.europeantour.com"

ROOT = pathlib.Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SNAP_DIR = ROOT / "snapshots"
STATE_DIR = ROOT / "state"     # (optional) für spätere Deduplizierung
REPORT_DIR = ROOT / "reports"

for d in (LOG_DIR, SNAP_DIR, STATE_DIR, REPORT_DIR):
    d.mkdir(exist_ok=True)

LOG_PATH = LOG_DIR / f"dpwt_watcher_{RUN_ID}.log"
REPORT_PATH = REPORT_DIR / "last_run.txt"


# ---------------------------
# Logging
# ---------------------------

handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[handler]
)
log = logging.getLogger("dpwt-watcher")


# ---------------------------
# HTTP Session (anti-block)
# ---------------------------

def make_session() -> requests.Session:
    ua = os.getenv("USER_AGENT") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": BASE_URL + "/dpworld-tour/",
        "Connection": "keep-alive",
        # Ganz simpel: ein Cookie-Key, damit wir nicht „leer“ kommen
        "Cookie": "et_platform=europeantour-web",
    })
    retries = Retry(
        total=5, backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"])
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------------------------
# Hilfen
# ---------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def save_snapshot(html: str, name: str):
    path = SNAP_DIR / f"{RUN_ID}_{name}.html"
    path.write_text(html, encoding="utf-8")
    log.info("HTML snapshot: %s", path)
    return path


def post_discord(webhook: str | None, content: str, embed: dict | None = None) -> bool:
    if not webhook:
        log.warning("Kein Webhook gesetzt – skip.")
        return False
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        ok = 200 <= r.status_code < 300
        log.info("Discord POST %s → %s", webhook[:60] + "...", r.status_code)
        if not ok:
            log.warning("Discord response: %s", r.text[:300])
        return ok
    except Exception as e:
        log.exception("Discord-Fehler: %s", e)
        return False


# ---------------------------
# Event-Erkennung
# ---------------------------

def extract_event_from_live_banner(soup: BeautifulSoup) -> dict | None:
    """
    <live-event-banner :events="[...]"> enthält JSON mit EventId, EventUrl, StartDate, RoundStatus u.a.
    """
    leb = soup.find("live-event-banner")
    if not leb or not leb.has_attr(":events"):
        return None
    try:
        arr = json.loads(leb[":events"])
        if not arr:
            return None
        evt = arr[0]
        # AbsUrl ergänzen
        if "EventUrl" in evt and evt["EventUrl"]:
            evt["EventAbsUrl"] = urljoin(BASE_URL, evt["EventUrl"])
        return evt
    except Exception as e:
        log.exception("Parsing live-event-banner failed: %s", e)
        return None


def extract_event_from_table(soup: BeautifulSoup) -> dict | None:
    """
    Fallback: Link aus Tabelle („Playing this week“) – <a class="event_tournament-link" href="...">
    """
    a = soup.select_one('a[class*="event_tournament-link"]')
    if not a or not a.get("href"):
        return None
    href = a["href"].strip()
    return {
        "EventUrl": href,
        "EventAbsUrl": urljoin(BASE_URL, href)
    }


def parse_iso_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Die Seite liefert z.B. "2025-10-15T18:30:00+00:00"
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_event_live_or_imminent(evt: dict) -> tuple[bool, str]:
    """
    Robust:
    - live: RoundStatus in {"1","2"} (1=live, 2=suspended)
    - ansonsten Zeitfenster (Start/Ende) als Fallback.
    """
    rs = str(evt.get("RoundStatus", ""))
    if rs in {"1", "2"}:
        return True, f"RoundStatus={rs}"

    start = parse_iso_utc(evt.get("StartDate"))
    end = parse_iso_utc(evt.get("EndDate"))
    now = utcnow()

    if start and start - now <= timedelta(days=0) and (not end or now <= end):
        return True, "within start/end window"
    return False, "not live by status/time"


def days_to_start(evt: dict) -> int | None:
    start = parse_iso_utc(evt.get("StartDate"))
    if not start:
        return None
    delta = (start - utcnow()).days
    # bei z.B. 1.8 Tagen wollen wir „1“ – runde ab
    return int((start - utcnow()).total_seconds() // 86400)


def extract_event(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    evt = extract_event_from_live_banner(soup)
    if evt:
        log.info("Live-banner Event gefunden: %s", {k: evt.get(k) for k in ("EventId","EventUrl","RoundStatus","StartDate")})
        return evt

    evt = extract_event_from_table(soup)
    if evt:
        log.info("Tabellen-Event-Link gefunden: %s", evt["EventUrl"])
        return evt

    log.info("Kein Event im HTML gefunden (weder banner noch Tabelle).")
    return None


# ---------------------------
# Leaderboard-Hilfen (optional / vorbereitet)
# ---------------------------

def fetch_leaderboard_json(session: requests.Session, event_id: int) -> dict | None:
    """
    Strokeplay ist bei DPWT Standard. Der 'type/load'-Endpoint liefert alles Wichtige.
    """
    url = f"{BASE_URL}/api/sportdata/Leaderboard/Strokeplay/{event_id}/type/load"
    log.info("GET %s", url)
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        log.warning("LB %s → %s", url, r.status_code)
        return None
    try:
        return r.json()
    except Exception:
        log.exception("JSON parse failed for leaderboard")
        return None


def find_player_round_done(lb_json: dict, player_id: int, round_no: int) -> tuple[bool, int | None, int | None, int | None]:
    """
    Prüft, ob ein Spieler (player_id) die Runde round_no beendet hat.
    Rückgabe: (is_done, strokes_of_round, course_par_for_round, position)
    """
    if not lb_json or "Players" not in lb_json:
        return (False, None, None, None)

    for p in lb_json["Players"]:
        if int(p.get("PlayerId", -1)) != int(player_id):
            continue
        # Runden-Array (siehe Screenshots: Rounds: [{RoundNo:1, Strokes:64, ...}, ...])
        rounds = p.get("Rounds") or []
        pos = p.get("Position") or None
        for r in rounds:
            if int(r.get("RoundNo", -1)) == int(round_no):
                strokes = r.get("Strokes")
                course_no = r.get("CourseNo")  # Par kann man ggf. über HoleAverages/Scorecard ziehen
                is_done = strokes is not None
                return (bool(is_done), int(strokes) if strokes is not None else None, None if course_no is None else int(course_no), int(pos) if pos else None)
        # Falls keine Runde mit round_no existiert → nicht fertig
        return (False, None, None, int(pos) if pos else None)

    return (False, None, None, None)


# ---------------------------
# Main
# ---------------------------

def main():
    # ENV
    player_slug = os.getenv("PLAYER_SLUG", "marcel-schneider-35703").strip("/")
    tour_slug = os.getenv("TOUR_SLUG", "dpworld-tour").strip("/")

    discord_announce = os.getenv("DISCORD_WEBHOOK_ANNOUNCE", "").strip()
    discord_second = os.getenv("DISCORD_WEBHOOK_SECOND", "").strip()

    session = make_session()

    player_url = f"{BASE_URL}/players/{player_slug}/?tour={tour_slug}"
    log.info("Lese Profilseite: %s", player_url)

    try:
        resp = session.get(player_url, timeout=40)
        resp.raise_for_status()
    except Exception as e:
        log.exception("Fehler beim Laden der Profilseite: %s", e)
        REPORT_PATH.write_text(f"❌ Fehler beim Laden: {e}\nURL: {player_url}\n", encoding="utf-8")
        return

    save_snapshot(resp.text, "player_overview")

    # Event aus HTML ziehen
    evt = extract_event(resp.text)
    if not evt:
        msg = "Kein aktives Event erkannt – (weder live-banner noch Tabellenlink gefunden)."
        log.warning(msg)
        REPORT_PATH.write_text("Kein aktives Event gefunden.\n", encoding="utf-8")
        print("Kein aktives Event bekannt – Ende.")
        return

    # Felder normalisieren:
    evt_url = evt.get("EventAbsUrl") or urljoin(BASE_URL, evt.get("EventUrl", "/"))
    evt_id = evt.get("EventId")
    round_status = str(evt.get("RoundStatus", ""))
    round_no = evt.get("RoundNo")

    # Start/Countdown
    dts = days_to_start(evt)
    if dts is not None:
        log.info("Days until start (UTC): %s", dts)

    # --- 2 Tage vorher Info an zweiten Hook
    if dts == 2:
        text = f"⏳ In **2 Tagen** beginnt das Turnier: {evt.get('Name','(unbekannt)')} – {evt_url}"
        post_discord(discord_second, text)

    # Live/im Fenster?
    live, reason = is_event_live_or_imminent(evt)
    log.info("Event live/imminent? %s (%s)", live, reason)

    # Optional: Info in Hauptkanal, damit sichtbar ist, dass Erkennung klappt
    embed = {
        "title": evt.get("Name", "Unbekanntes Event"),
        "url": evt_url,
        "fields": [
            {"name": "EventId", "value": str(evt_id), "inline": True},
            {"name": "RoundNo", "value": str(round_no), "inline": True},
            {"name": "RoundStatus", "value": str(round_status), "inline": True},
            {"name": "Start (UTC)", "value": str(evt.get("StartDate")), "inline": False},
        ],
        "timestamp": utcnow().isoformat()
    }
    post_discord(discord_announce, f"🔎 Event erkannt ({'LIVE' if live else 'n. live'}): {evt_url}", embed)

    # Report schreiben
    REPORT_PATH.write_text(
        json.dumps({
            "when": utcnow().isoformat(),
            "player_url": player_url,
            "event": {
                "name": evt.get("Name"),
                "event_id": evt_id,
                "event_url": evt_url,
                "round_no": round_no,
                "round_status": round_status,
                "start_utc": evt.get("StartDate"),
                "detected_via": "live-event-banner" if ":events" in (BeautifulSoup(resp.text, "html.parser").find("live-event-banner") or {}).attrs else "table-link"
            },
            "live_or_imminent": {"live": live, "reason": reason},
            "days_to_start": dts
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print("Run OK – Details siehe logs/ & reports/.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Unbehandelter Fehler: %s", e)
        REPORT_PATH.write_text(f"❌ Unbehandelter Fehler: {e}\n", encoding="utf-8")
        raise
