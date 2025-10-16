#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright

# --------------------
# Konstante IDs / URLs
# --------------------
PLAYER_ID = 35703  # Marcel Schneider
PLAYER_PROFILE_URL = "https://www.europeantour.com/players/marcel-schneider-35703/?tour=dpworld-tour"

# Verzeichnisse im Repo
LOG_DIR = Path("logs")
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "state_dpwt.json"
LAST_RUN_LOG = LOG_DIR / "last_run.log"

# Discord
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_2", "").strip()

# --------------------
# Logging
# --------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LAST_RUN_LOG, mode="w", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Konnte state-Datei nicht lesen – starte neu.")
    return {
        "last_event_id": None,
        "last_event_url": None,
        "prealert_sent_for_event": {},          # {event_id: true}
        "marcel_round_done": {},                # {f"{event_id}|R{round_no}": true}
        "round_all_done": {},                   # {f"{event_id}|R{round_no}": true}
        "last_checked": None
    }

def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def post_discord(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        logging.warning("Kein Discord-Webhook gesetzt – überspringe Post.")
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=20)
        if resp.status_code >= 300:
            logging.error(f"Discord-Fehler {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logging.exception(f"Discord-Post fehlgeschlagen: {e}")

def utcnow():
    return datetime.now(timezone.utc)

# --------------------
# Playwright Hilfen (nur fetch, kein HTML-Scrape)
# --------------------
def with_page_json(fetch_js: str) -> Any:
    """
    Hilfsfunktion: Startet Playwright, lädt die Profilseite (gleiche Origin),
    führt den übergebenen JS-Code aus (der fetch() macht) und gibt JSON zurück.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # headless=True reicht hier
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        logging.info(f"Starte Playwright-Browser für URL: {PLAYER_PROFILE_URL}")
        page.goto(PLAYER_PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)

        # Sicherstellen, dass window.et.config existiert (die Seite initialisiert das schnell;
        # wir warten einmal kurz und versuchen mehrfach)
        for _ in range(10):
            has_et = page.evaluate("() => !!(window.et && window.et.config)")
            if has_et:
                break
            time.sleep(0.5)

        data = page.evaluate(fetch_js)  # fetch in Page-Context
        context.close()
        browser.close()
        return data

# --------------------
# API Wrapper (im Page-Kontext)
# --------------------
def get_player_upcoming() -> Optional[Dict[str, Any]]:
    """
    Ruft /api/v1/players/{PLAYER_ID}/upcoming/ im Browserkontext ab.
    Liefert dict mit anstehendem Event (oder None).
    """
    logging.info("Hole Upcoming-Event (PLAYER_UPCOMING JSON)...")
    js = f"""
        async () => {{
            const url = `/api/v1/players/{PLAYER_ID}/upcoming/`;
            const r = await fetch(url, {{ credentials: 'include' }});
            if (!r.ok) return null;
            return await r.json();
        }}
    """
    return with_page_json(js)

def get_strokeplay_leaderboard(event_id: int) -> Optional[Dict[str, Any]]:
    """
    Ruft Leaderboard JSON. Erst ohne /type/load, dann Fallback mit /type/load.
    """
    logging.info(f"Hole Leaderboard Strokeplay für Event {event_id}...")
    js_primary = f"""
        async () => {{
            const url = `/api/sportdata/Leaderboard/Strokeplay/{event_id}`;
            const r = await fetch(url, {{ credentials: 'include' }});
            if (!r.ok) return null;
            return await r.json();
        }}
    """
    data = with_page_json(js_primary)
    if data is not None:
        return data

    logging.info("Primary Leaderboard-Call war leer – versuche /type/load ...")
    js_fallback = f"""
        async () => {{
            const url = `/api/sportdata/Leaderboard/Strokeplay/{event_id}/type/load`;
            const r = await fetch(url, {{ credentials: 'include' }});
            if (!r.ok) return null;
            return await r.json();
        }}
    """
    return with_page_json(js_fallback)

def get_scorecard(event_id: int, player_id: int) -> Optional[Dict[str, Any]]:
    """
    Ruft Marcels Scorecard JSON.
    """
    logging.info(f"Hole Scorecard für Event {event_id}, Player {player_id}...")
    js = f"""
        async () => {{
            const url = `/api/sportdata/Scorecard/Strokeplay/Event/{event_id}/Player/{player_id}`;
            const r = await fetch(url, {{ credentials: 'include' }});
            if (!r.ok) return null;
            return await r.json();
        }}
    """
    return with_page_json(js)

# --------------------
# Auswertelogik
# --------------------
def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def days_until(start_dt: datetime) -> int:
    now = utcnow()
    delta = start_dt - now
    return math.ceil(delta.total_seconds() / 86400)

def format_score_to_par(n: Optional[int]) -> str:
    if n is None:
        return "E"
    if n == 0:
        return "E"
    return f"{'+' if n > 0 else ''}{n}"

def find_player(players: list, pid: int) -> Optional[Dict[str, Any]]:
    for p in players or []:
        if (p.get("PlayerId") == pid) or (p.get("Id") == pid):
            return p
    return None

def holes_finished_in_round(scorecard: Dict[str, Any], round_no: int) -> bool:
    """
    Prüft, ob Marcel Runde `round_no` komplett (18 Löcher) hat.
    Scorecard-Struktur variiert je nach API-Version; robust prüfen.
    """
    if not scorecard:
        return False
    # Mögliche Felder: "Rounds": [{ "RoundNo": 1, "Holes": [{...} x18], ...}, ...]
    rounds = scorecard.get("Rounds") or scorecard.get("Round") or []
    for r in rounds:
        rn = r.get("RoundNo") or r.get("Round") or r.get("Number")
        if rn == round_no:
            holes = r.get("Holes") or r.get("HoleScores") or []
            return len(holes) >= 18
    # Fallback: wenn nur eine flache Liste existiert, die ein RoundNo enthält
    holes = scorecard.get("Holes") or []
    if holes and all(h.get("RoundNo") == round_no for h in holes):
        return len(holes) >= 18
    return False

def round_all_players_finished(ldb: Dict[str, Any], round_no: int) -> bool:
    players = ldb.get("Players") or ldb.get("PlayerList") or []
    if not players:
        return False
    all_done = True
    for p in players:
        # robuste Felder:
        holes_played = p.get("HolesPlayed")
        # manche Payloads haben pro Runde Strukturen – hier einfach: >=18 in der Runde
        if holes_played is None:
            # wenn nicht vorhanden, versuche über Rounds-Array des Players
            rounds = p.get("Rounds") or []
            finished = False
            for r in rounds:
                rn = r.get("RoundNo") or r.get("Round") or r.get("Number")
                if rn == round_no:
                    hp = r.get("HolesPlayed") or r.get("Holes") or []
                    finished = (hp == 18) or (isinstance(hp, list) and len(hp) >= 18)
                    break
            if not finished:
                all_done = False
                break
        else:
            if holes_played < 18:
                all_done = False
                break
    return all_done

def main():
    logging.info("=== DPWT Leaderboard Watcher gestartet ===")

    state = load_state()
    if not DISCORD_WEBHOOK:
        logging.error("Kein DISCORD_WEBHOOK_2 gefunden – bitte im GitHub Secret setzen.")

    # 1) Upcoming-Ereignis laden (JSON via Browser fetch)
    upcoming = get_player_upcoming()
    if not upcoming:
        logging.warning("Keine Upcoming-Daten erhalten.")
        state["last_checked"] = utcnow().isoformat()
        save_state(state)
        return

    # Das Upcoming-JSON kann je nach Saison/Status leer oder Liste/Objekt sein.
    # Ziel: ein aktives oder nächstes Event extrahieren.
    # In deinen Screens war es oft ein Objekt mit Feldern wie EventId, EventUrl, StartDate.
    event_obj = None
    if isinstance(upcoming, dict) and upcoming.get("EventId"):
        event_obj = upcoming
    elif isinstance(upcoming, list) and upcoming:
        # nimm das erste sinnvolle
        for item in upcoming:
            if item and item.get("EventId"):
                event_obj = item
                break

    if not event_obj:
        logging.warning("Kein Event im Upcoming-JSON gefunden.")
        state["last_checked"] = utcnow().isoformat()
        save_state(state)
        return

    event_id = int(event_obj["EventId"])
    event_url = event_obj.get("EventUrl") or event_obj.get("Url")  # z.B. "/dpworld-tour/dp-world-india-championship-2025/"
    start_str = event_obj.get("StartDate")
    round_no_str = (event_obj.get("RoundNo") or "").strip()
    round_status = event_obj.get("RoundStatus")

    state["last_event_id"] = event_id
    state["last_event_url"] = event_url

    # 2) Vorab-Benachrichtigung 2 Tage vorher
    if start_str:
        try:
            start_dt = parse_iso(start_str)
            dleft = days_until(start_dt)
            key = str(event_id)
            if dleft <= 2 and dleft >= 0 and not state["prealert_sent_for_event"].get(key):
                msg = f"⛳️ *{event_obj.get('Name', 'Event')}* startet in **{dleft} Tagen**\n{('https://www.europeantour.com' + event_url) if event_url else ''}"
                post_discord(msg)
                state["prealert_sent_for_event"][key] = True
        except Exception as e:
            logging.warning(f"StartDate konnte nicht geparst werden: {e}")

    # 3) Während des Turniers: Leaderboard + Scorecard
    ldb = get_strokeplay_leaderboard(event_id)
    if not ldb:
        logging.warning("Leaderboard-Daten leer – evtl. noch kein Scoring aktiv.")
        state["last_checked"] = utcnow().isoformat()
        save_state(state)
        return

    players = ldb.get("Players") or ldb.get("PlayerList") or []
    marcel = find_player(players, PLAYER_ID)

    # Welche Runde beobachten?
    try:
        current_round = int(round_no_str) if round_no_str.isdigit() else int(ldb.get("Round", 0)) or int(ldb.get("CurrentRound", 0))
    except Exception:
        current_round = int(ldb.get("CurrentRound", 0)) if ldb.get("CurrentRound") else 0

    if current_round <= 0:
        # Wenn keine Runde im JSON steht, aber Turnier schon läuft, nimm 1 als Fallback
        current_round = 1

    # 3a) Marcel-Runde fertig?
    marcel_done_key = f"{event_id}|R{current_round}"
    if marcel and not state["marcel_round_done"].get(marcel_done_key):
        sc = get_scorecard(event_id, PLAYER_ID)
        if holes_finished_in_round(sc, current_round):
            # Werte fürs Posting
            marcel_pos = marcel.get("Position") or marcel.get("Pos")
            marcel_to_par = marcel.get("TotalToPar") or marcel.get("ScoreToPar") or marcel.get("Score")
            msg = (
                f"✅ *Marcel Schneider* hat **R{current_round}** beendet.\n"
                f"• Platzierung: **{marcel_pos}**\n"
                f"• Gesamt: **{format_score_to_par(marcel_to_par)}**\n"
                f"• Leaderboard: https://www.europeantour.com{event_url}leaderboard?round={current_round if current_round else 1}"
            )
            post_discord(msg)
            state["marcel_round_done"][marcel_done_key] = True

    # 3b) Ganze Runde fertig?
    all_done_key = f"{event_id}|R{current_round}"
    if not state["round_all_done"].get(all_done_key):
        if round_all_players_finished(ldb, current_round):
            # Hole ggf. Top-Infos
            msg = (
                f"🏁 **Runde {current_round} ist komplett**.\n"
                f"https://www.europeantour.com{event_url}leaderboard?round={current_round}"
            )
            post_discord(msg)
            state["round_all_done"][all_done_key] = True

    state["last_checked"] = utcnow().isoformat()
    save_state(state)
    logging.info("Lauf beendet.")

if __name__ == "__main__":
    main()
