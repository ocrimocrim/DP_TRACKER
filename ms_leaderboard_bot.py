#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import math
import pathlib
import logging
import datetime as dt
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

# =========================
# Konfiguration via ENV
# =========================
PLAYER_ID = int(os.getenv("DPWT_PLAYER_ID", "35703"))  # Marcel Schneider
TOUR_SLUG = os.getenv("DPWT_TOUR_SLUG", "dpworld-tour")
BASE = "https://www.europeantour.com"

# Proxy-Relay (Cloudflare Worker o.ä.), z.B.:
# https://your-worker.example.workers.dev/fetch?url=<ENCODED_TARGET>
# oder allgemein: <PROXY_URL>?url=<ENCODED_TARGET>
PROXY_URL = os.getenv("PROXY_URL", "").rstrip("/")

DISCORD_WEBHOOK_1 = os.getenv("DISCORD_WEBHOOK")         # Info-Kanal
DISCORD_WEBHOOK_2 = os.getenv("DISCORD_WEBHOOK_2")       # „zweiter“ Kanal, wie gewünscht

# Dateiausgaben ins Repo
DATA_DIR = pathlib.Path("reports")
SNAP_DIR = DATA_DIR / "snapshots"
STATE_PATH = DATA_DIR / "state.json"
LAST_RUN_JSON = DATA_DIR / "last_run.json"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAP_DIR.mkdir(parents=True, exist_ok=True)

# Logging
ts = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
log_path = LOG_DIR / f"dpwt_watcher_{ts}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler()
    ],
)
log = logging.getLogger("dpwt")

# HTTP Session
session = requests.Session()
session.headers.update({
    "User-Agent": os.getenv("DPWT_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
})
TIMEOUT = 30

def now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def write_json(path: pathlib.Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def read_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    write_json(STATE_PATH, state)

def discord_post(message: str):
    ok = True
    for hook in [DISCORD_WEBHOOK_1, DISCORD_WEBHOOK_2]:
        if not hook:
            continue
        try:
            r = session.post(hook, json={"content": message}, timeout=TIMEOUT)
            if r.status_code >= 400:
                log.error("Discord Webhook Fehler %s: %s", r.status_code, r.text[:500])
                ok = False
        except Exception as e:
            log.exception("Discord Post Exception: %s", e)
            ok = False
    return ok

def via_proxy(url: str) -> str:
    if not PROXY_URL:
        # harte Abbruchbedingung, damit wir nicht wieder 403 laufen
        raise RuntimeError("PROXY_URL ist nicht gesetzt – der Runner wird geblockt (403).")
    return f"{PROXY_URL}?url={quote(url, safe='')}"

def http_get(url: str, expect_html=True, try_count=3, sleep_base=1.5):
    """
    Lädt URL zwingend über Proxy-Relay. Mit Retry/Backoff.
    """
    prox = via_proxy(url)
    last_err = None
    for i in range(try_count):
        try:
            log.debug("GET %s", url)
            r = session.get(prox, timeout=TIMEOUT)
            # Einige Worker geben Status durch – wenn der Origin 403 liefert, kommt hier 403
            if r.status_code == 403:
                raise requests.HTTPError(f"403 from origin for {url}", response=r)
            r.raise_for_status()
            if expect_html and "text/html" not in r.headers.get("Content-Type", ""):
                log.warning("Content-Type unerwartet: %s", r.headers.get("Content-Type"))
            return r.text
        except Exception as e:
            last_err = e
            sleep = sleep_base * (2 ** i) + (0.1 * i)
            time.sleep(sleep)
    # alle Versuche fehlgeschlagen
    raise last_err

def http_get_json(url: str, try_count=3):
    text = http_get(url, expect_html=False, try_count=try_count)
    try:
        return json.loads(text)
    except Exception:
        # Manche Worker umbrechen Header – letzte Chance: direkte JSON-Interpretation durch BeautifulSoup cleanup
        try:
            cleaned = BeautifulSoup(text, "html.parser").text
            return json.loads(cleaned)
        except Exception:
            # Speichere Rohantwort
            snap = SNAP_DIR / f"raw_{int(time.time())}.json.txt"
            snap.write_text(text, encoding="utf-8")
            raise

# --------------------------------------------------------
# Parsing Helfer
# --------------------------------------------------------
LIVE_BANNER_RE = re.compile(
    r"<live-event-banner[^>]*:events\s*=\s*\"(?P<json>\[.*?\])\"",
    re.DOTALL | re.IGNORECASE
)

def extract_live_events(html: str):
    """
    Sucht das :events= JSON im <live-event-banner ...> wie in deinem HTML.
    Gibt Liste von Events zurück.
    """
    m = LIVE_BANNER_RE.search(html)
    if not m:
        return []
    raw = m.group("json")
    # HTML-Entities &quot; etc. ersetzen
    raw = raw.replace("&quot;", "\"").replace("&#34;", "\"")
    try:
        return json.loads(raw)
    except Exception as e:
        # Fallback: Quotes/Whitespace bereinigen
        cleaned = raw.strip()
        return json.loads(cleaned)

def load_profile_and_find_next_event():
    url = f"{BASE}/players/marcel-schneider-{PLAYER_ID}/?tour={TOUR_SLUG}"
    log.info("Lese Profilseite: %s", url)
    html = http_get(url, expect_html=True)
    # Snapshot
    (SNAP_DIR / f"profile_{int(time.time())}.html").write_text(html, encoding="utf-8")

    # 1) Primärquelle: live-event-banner :events=[{...}]
    events = extract_live_events(html)
    if events:
        # nimm das erste
        ev = events[0]
        # Erwartete Felder laut HTML: EventUrl, EventId, Name, StartDate, RoundNo, Status
        ev["EventUrlFull"] = urljoin(BASE, ev.get("EventUrl") or "")
        return ev

    # 2) Fallback: "Playing this week" Abschnitt im DOM – hole den ersten Link darunter
    soup = BeautifulSoup(html, "html.parser")
    playing_header = soup.find(lambda tag: tag.name in ("h4", "h3") and "Playing this week" in tag.get_text(strip=True))
    if playing_header:
        # Suche in den nächsten Geschwistern nach einem <a>
        container = playing_header.find_parent()
        if container:
            a = container.find("a", href=True)
            if a:
                return {
                    "EventUrlFull": urljoin(BASE, a["href"]),
                    "EventId": None,
                    "Name": a.get_text(" ", strip=True),
                    "FromFallback": True,
                }

    return None

# --------------------------------------------------------
# Leaderboard / Score
# --------------------------------------------------------
def eventid_from_event_page(html: str):
    """
    Versuche EventId aus dem globalen window.et.config zu ziehen, oder aus Daten-Attributen.
    """
    # 1) live-event-banner existiert auch auf Eventseite -> probieren
    events = extract_live_events(html)
    if events:
        return events[0].get("EventId")

    # 2) window.et.config.sportsDataApi.STROKEPLAY Pfad hilft beim Konstruieren,
    # aber EventId brauchen wir trotzdem. Manchmal steht sie in data-event-id
    m = re.search(r'"EventId"\s*:\s*(\d+)', html)
    if m:
        return int(m.group(1))

    return None

def build_leaderboard_api_url(event_id: int):
    # Laut Seite: /api/sportdata/Leaderboard/Strokeplay/{eventId}
    return f"{BASE}/api/sportdata/Leaderboard/Strokeplay/{event_id}"

def fetch_leaderboard_json(event_id: int):
    url = build_leaderboard_api_url(event_id)
    return http_get_json(url)

def round_finished_for_player(player_obj, round_no: int):
    # Spielerobjekt hat "Rounds": [{RoundNo, Strokes, CourseNo}]
    rounds = player_obj.get("Rounds") or []
    for r in rounds:
        if r.get("RoundNo") == round_no and (r.get("Strokes") is not None):
            return True, r.get("Strokes")
    return False, None

def all_players_finished(lb_json: dict, round_no: int):
    players = lb_json.get("Players") or []
    if not players:
        return False
    for p in players:
        done, _ = round_finished_for_player(p, round_no)
        if not done:
            return False
    return True

def find_player(lb_json: dict, player_id: int):
    for p in (lb_json.get("Players") or []):
        if p.get("PlayerId") == player_id:
            return p
    return None

def leaderboard_link_from_event_url(event_url: str, round_no: int | None):
    # laut Anforderung: einfach /leaderboard?round=X hinten dran
    if not event_url.endswith("/"):
        event_url += "/"
    if round_no is None:
        return event_url + "leaderboard"
    return f"{event_url}leaderboard?round={round_no}"

# --------------------------------------------------------
# Benachrichtigungslogik
# --------------------------------------------------------
def maybe_pre_event_ping(event_info: dict, state: dict):
    """
    2 Tage vor Start -> Discord Hinweis.
    """
    start_iso = event_info.get("StartDate")  # z.B. "2025-10-15T18:30:00+00:00"
    name = event_info.get("Name") or "Unknown Event"
    event_url = event_info.get("EventUrlFull")

    if not start_iso:
        return state

    try:
        start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except Exception:
        return state

    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    delta_days = (start - now).total_seconds() / 86400.0

    key = f"pre_ping_{event_info.get('EventId') or event_url}"
    already = state.get(key)

    if 1.0 <= delta_days <= 2.1 and not already:
        msg = f"⛳️ In **{math.ceil(delta_days)} Tagen** beginnt **{name}**.\n{event_url}"
        discord_post(msg)
        state[key] = now_iso()
        save_state(state)

    return state

def post_round_done(event_info: dict, lb_json: dict, round_no: int, player_done: bool, player_strokes: int | None):
    """
    Zwei Meldungen je Runde:
    1) Wenn Marcel fertig ist (sofort)
    2) Wenn alle fertig sind (abschließend)
    Format: Stroke + Platz + Par (ScoreToPar)
    """
    name = event_info.get("Name") or "Event"
    event_id = event_info.get("EventId")
    lb_link = leaderboard_link_from_event_url(event_info.get("EventUrlFull", ""), round_no)

    p = find_player(lb_json, PLAYER_ID)
    pos_desc = p.get("PositionDesc") if p else "—"
    score_to_par = p.get("ScoreToPar") if p else None
    stp = f"{score_to_par:+d}" if isinstance(score_to_par, int) else "—"

    # 1) Wenn Marcel fertig wurde – sofort
    if player_done:
        msg = f"✅ **R{round_no} fertig (Marcel)** @ **{name}**\n" \
              f"Strokes (R{round_no}): **{player_strokes}**, Gesamt: **{stp}**, Platz: **{pos_desc}**\n{lb_link}"
        discord_post(msg)

    # 2) Wenn ALLE fertig sind – abschließend
    if all_players_finished(lb_json, round_no):
        # Versuche Rundenstrokes für Marcel erneut zu setzen, falls oben None
        if player_strokes is None and p:
            for r in (p.get("Rounds") or []):
                if r.get("RoundNo") == round_no and r.get("Strokes") is not None:
                    player_strokes = r["Strokes"]
                    break
        msg = f"🏁 **R{round_no} abgeschlossen (ALLE)** @ **{name}**\n" \
              f"Marcel (R{round_no}): **{player_strokes if player_strokes is not None else '—'}**, Gesamt: **{stp}**, Platz: **{pos_desc}**\n{lb_link}"
        discord_post(msg)

# --------------------------------------------------------
# MAIN
# --------------------------------------------------------
def main():
    last_run = {"ok": False, "error": None, "steps": [], "ts": now_iso()}
    try:
        if not PROXY_URL:
            raise RuntimeError("PROXY_URL nicht gesetzt – bitte als Secret konfigurieren (siehe README).")

        # 1) Profilseite lesen -> nächstes/aktuelles Event
        profile_ev = load_profile_and_find_next_event()
        if not profile_ev:
            msg = "Kein aktives/kommendes Event im Profil gefunden."
            log.info(msg)
            last_run["steps"].append({"profile": "none"})
            write_json(LAST_RUN_JSON, last_run | {"ok": True})
            return

        last_run["steps"].append({"profile_event": profile_ev})
        state = read_state()

        # 2) Pre-Event Ping (2 Tage vor Start)
        state = maybe_pre_event_ping(profile_ev, state)

        # 3) Eventseite + EventId extrahieren (falls nicht schon vorhanden)
        ev_url = profile_ev.get("EventUrlFull")
        if not ev_url:
            log.info("EventUrl fehlt, breche ab.")
            write_json(LAST_RUN_JSON, last_run | {"ok": True})
            return

        ev_html = http_get(ev_url, expect_html=True)
        (SNAP_DIR / f"event_{int(time.time())}.html").write_text(ev_html, encoding="utf-8")

        event_id = profile_ev.get("EventId") or eventid_from_event_page(ev_html)
        if not event_id:
            raise RuntimeError("EventId konnte nicht ermittelt werden.")

        # 4) Leaderboard JSON laden
        lb = fetch_leaderboard_json(event_id)
        last_run["steps"].append({"leaderboard_summary": {
            "EventId": lb.get("EventId"),
            "RoundCountPlayers": len(lb.get("Players") or []),
            "LastUpdated": lb.get("LastUpdated"),
        }})

        # Aktuelle Runde einschätzen:
        # Falls live-banner eine RoundNo hat, nutze die. Sonst max RoundNo aus Daten von Spielern.
        round_no = None
        if "RoundNo" in profile_ev and str(profile_ev["RoundNo"]).isdigit():
            round_no = int(profile_ev["RoundNo"])
        else:
            # Heuristik: höchste RoundNo, die vorkommt
            max_r = 0
            for p in (lb.get("Players") or []):
                for r in (p.get("Rounds") or []):
                    if isinstance(r.get("RoundNo"), int):
                        max_r = max(max_r, r["RoundNo"])
            round_no = max_r if max_r > 0 else 1

        # 5) Trigger/Meldungen pro Runde
        #    - Wenn Marcel für Runde X fertig -> sofort posten (einmalig)
        #    - Wenn ALLE fertig -> einmalig
        p = find_player(lb, PLAYER_ID)
        if not p:
            log.info("Marcel nicht im Leaderboard – evtl. noch nicht gestartet.")
        else:
            player_done, player_strokes = round_finished_for_player(p, round_no)

            # State keys
            ev_key = f"event_{event_id}"
            state.setdefault(ev_key, {"posted_player_rounds": [], "posted_all_rounds": []})

            # 1) Meldung wenn Marcel fertig und noch nicht gepostet
            if player_done and round_no not in state[ev_key]["posted_player_rounds"]:
                post_round_done(profile_ev | {"EventId": event_id}, lb, round_no, True, player_strokes)
                state[ev_key]["posted_player_rounds"].append(round_no)
                save_state(state)

            # 2) Meldung wenn alle fertig und noch nicht gepostet
            if all_players_finished(lb, round_no) and round_no not in state[ev_key]["posted_all_rounds"]:
                post_round_done(profile_ev | {"EventId": event_id}, lb, round_no, False, player_strokes)
                state[ev_key]["posted_all_rounds"].append(round_no)
                save_state(state)

        # 6) Snapshots & last_run Info
        write_json(SNAP_DIR / f"leaderboard_{event_id}_{int(time.time())}.json", lb)
        last_run["ok"] = True
        write_json(LAST_RUN_JSON, last_run)

    except Exception as e:
        log.error("Fehler: %s", e, exc_info=True)
        last_run["ok"] = False
        last_run["error"] = f"{type(e).__name__}: {e}"
        write_json(LAST_RUN_JSON, last_run)
        # Zusätzlich verständliche Fehlermeldung für Discord (nur Kanal 2, um Spam zu vermeiden)
        if DISCORD_WEBHOOK_2:
            try:
                discord_post(f"❌ Fehler beim Run: {last_run['error']}")
            except Exception:
                pass

if __name__ == "__main__":
    main()
