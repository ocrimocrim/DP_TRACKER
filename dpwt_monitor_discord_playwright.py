#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, pathlib, datetime
from typing import Dict, Any, List, Optional

from playwright.sync_api import sync_playwright

# -------- Config ----------
PLAYER_ID = int(os.getenv("DPWT_PLAYER_ID", "35703"))   # Marcel Schneider
TOUR_ID   = int(os.getenv("DPWT_TOUR_ID", "1"))         # DP World Tour
# Saison automatisch (läuft weiter in 2026), manuell via DPWT_SEASON übersteuerbar
BERLIN_TZ_OFFSET = datetime.timezone(datetime.timedelta(hours=2))  # CEST grob; für UTC-ISO egal
now_utc = datetime.datetime.utcnow()
auto_season = int(os.getenv("DPWT_SEASON", str(now_utc.year)))
SEASON  = auto_season

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()   # muss gesetzt sein
DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)

BASELINE_PATH = DATA_DIR / f"baseline-{SEASON}.json"
EVENTS_DUMP_PATH = DATA_DIR / f"events-{SEASON}.json"
LAST_NONLIVE_TS = DATA_DIR / ".last_nonlive_ok.ts"

DEBUG_RESP_HTML = DATA_DIR / "_debug_last_response.html"
DEBUG_RESP_URL  = DATA_DIR / "_debug_last_url.txt"
DEBUG_ERROR     = DATA_DIR / "_debug_error.json"

# Frequenz-Steuerung:
# - Workflow läuft alle 30 Min tagsüber.
# - Wenn KEIN Live-Event: intern nur alle 120 Min wirklich arbeiten (sonst sauber beenden).
NONLIVE_MIN_INTERVAL_MIN = 120


# -------- Utils ----------
def iso_utc(dt: Optional[datetime.datetime] = None) -> str:
    if dt is None:
        dt = datetime.datetime.utcnow()
    return dt.replace(tzinfo=datetime.timezone.utc).isoformat().replace("+00:00", "Z")

def write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    tmp.replace(path)

def read_json(path: pathlib.Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def sha(sig: str) -> str:
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]

def event_key(ev: Dict[str, Any]) -> str:
    # Bevorzugt stabile IDs, sonst Fallback auf Name+Datum
    for k in ("EventId", "EventID", "Id", "ID", "EventIDCode"):
        if k in ev and ev[k]:
            return str(ev[k])
    name = str(ev.get("Tournament") or ev.get("EventName") or ev.get("Name") or "unknown").strip()
    endd = str(ev.get("EndDate") or ev.get("End") or ev.get("Date") or "").strip()
    return f"{name}::{endd}"

def event_signature(ev: Dict[str, Any]) -> str:
    # Runde für Runde + Zusammenfassung, Position, Par, Geld etc.
    fields = [
        "Pos", "ToPar", "Total", "R1", "R2", "R3", "R4",
        "PrizeMoney", "R2DR", "R2DRPoints", "R2MR", "R2MRPoints"
    ]
    parts = []
    for f in fields:
        v = ev.get(f, "")
        # Einige Felder kommen als int/float -> zu String normalisieren
        if v is None:
            v = ""
        v = str(v)
        parts.append(f"{f}={v.strip()}")
    return "|".join(parts)

def parse_live_banner_events(html: str) -> List[Dict[str, Any]]:
    # In der Seite steht: <live-event-banner :events="[{&#34;...&#34;}]">
    m = re.search(r'<live-event-banner[^>]*?:events="([^"]+)"', html)
    if not m:
        return []
    raw = m.group(1)
    # HTML Entities zurückkonvertieren
    raw = raw.replace("&#34;", '"').replace("&quot;", '"')
    try:
        return json.loads(raw)
    except Exception:
        return []

def detect_is_live(html: str) -> bool:
    evs = parse_live_banner_events(html)
    # RoundStatus: 2 == live (aus deiner HTML-Probe)
    for ev in evs:
        rs = ev.get("RoundStatus")
        st = ev.get("Status")
        if str(rs) in ("1","2") or str(st) in ("1","2"):  # etwas großzügig
            return True
    return False

def throttle_when_not_live(is_live: bool) -> bool:
    """True = weiterlaufen; False = sauber beenden (zu früh)."""
    if is_live:
        return True
    # Nicht live -> nur alle NONLIVE_MIN_INTERVAL_MIN verarbeiten
    now = time.time()
    if LAST_NONLIVE_TS.exists():
        try:
            last = float(LAST_NONLIVE_TS.read_text().strip())
            if (now - last) < NONLIVE_MIN_INTERVAL_MIN * 60:
                print(f"[dpwt] not live; throttled ({int((NONLIVE_MIN_INTERVAL_MIN*60 - (now-last))//60)} min left)")
                return False
        except Exception:
            pass
    LAST_NONLIVE_TS.write_text(str(now))
    return True

def post_discord(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        print("[dpwt] no DISCORD_WEBHOOK_URL set; skip post")
        return
    try:
        import requests
        resp = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=20)
        print(f"[dpwt] discord status: {resp.status_code}")
    except Exception as e:
        print(f"[dpwt] discord error: {e}")

def format_baseline_msg(count: int) -> str:
    return f"**Turniertracker**\nMonitor aktiv. Baseline {SEASON} gesetzt (**{count}** Turniere)."

def format_update_msg(kind: str, ev: Dict[str, Any]) -> str:
    # kind: NEW or UPDATE
    title = str(ev.get("Tournament") or ev.get("EventName") or ev.get("Name") or "Unbekannt").strip()
    pos   = str(ev.get("Pos") or "—")
    topar = str(ev.get("ToPar") or "—")
    total = str(ev.get("Total") or "—")
    r1 = str(ev.get("R1") or "—"); r2 = str(ev.get("R2") or "—")
    r3 = str(ev.get("R3") or "—"); r4 = str(ev.get("R4") or "—")
    prize = str(ev.get("PrizeMoney") or "—")
    r2dr  = str(ev.get("R2DR") or ev.get("R2DRPoints") or "—")
    r2mr  = str(ev.get("R2MR") or ev.get("R2MRPoints") or "—")
    endd  = str(ev.get("EndDate") or "—")

    head = "🆕 **Neues Turnier**" if kind == "NEW" else "🔄 **Update**"
    lines = [
        f"{head}: {title}",
        f"Enddatum: {endd}",
        f"Pos.: **{pos}**   To Par: **{topar}**   Total: **{total}**",
        f"Runden: R1 {r1} | R2 {r2} | R3 {r3} | R4 {r4}",
        f"R2DR: {r2dr}   R2MR: {r2mr}   Preisgeld: {prize}",
        f"Stand: {iso_utc()}"
    ]
    return "\n".join(lines)

# -------- Core: fetch results via network sniff ----------
def fetch_results_via_page(player_id: int, tour_id: int, season: int) -> Dict[str, Any]:
    """
    Öffnet die Seite, klickt Consent, lauscht auf den Netzwerk-Call:
      /api/v1/players/<id>/results/<season>/
    und liefert dessen JSON.
    """
    url = f"https://www.europeantour.com/players/{player_id}/results?tour=dpworld-tour"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
            timezone_id="Europe/Berlin",
            viewport={"width": 1368, "height": 900},
        )
        page = context.new_page()

        # Response-Sniffer
        captured: List[Any] = []
        pat = re.compile(r"/api/v1/players/\d+/results/\d{4}/")

        def on_response(resp):
            try:
                if pat.search(resp.url):
                    captured.append(resp)
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded")

        # Consent klicken (OneTrust)
        try:
            page.wait_for_timeout(800)  # kurz verschnaufen
            # Häufige IDs/Buttons probieren:
            for sel in [
                "#onetrust-accept-btn-handler",
                "button#onetrust-accept-btn-handler",
                "button[aria-label='Agree']",
                "button:has-text('ACCEPT ALL')",
                "button:has-text('Allow all')",
            ]:
                if page.locator(sel).first.is_visible():
                    page.locator(sel).first.click()
                    print("[dpwt] consent clicked")
                    break
        except Exception:
            pass

        # Sicherstellen, dass Player-Results gerendert werden
        try:
            page.wait_for_selector("player-results", timeout=15000)
        except Exception:
            pass

        # Manche Seiten laden die API erst nach vollständiger Idle-Phase
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        # Wenn nichts gefangen: einmal neu laden
        if not captured:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)

        # Live-Status ermitteln (nur fürs Throttling)
        html = page.content()
        live = detect_is_live(html)

        # Auswerten, was wir gefangen haben
        last_json: Optional[Dict[str, Any]] = None
        last_nonjson_txt: Optional[str] = None
        last_url: Optional[str] = None

        for resp in captured:
            last_url = resp.url
            try:
                if "application/json" in (resp.headers.get("content-type") or "") and resp.status == 200:
                    last_json = resp.json()
            except Exception:
                try:
                    last_nonjson_txt = resp.text()
                except Exception:
                    last_nonjson_txt = "<no text>"

        if last_json is None:
            # Debug sichern
            if last_nonjson_txt:
                write_json(DEBUG_ERROR, {
                    "ts": iso_utc(), "step": "fetch",
                    "error": "results fetch produced no JSON (non-json capture)"
                })
                DEBUG_RESP_HTML.write_text(last_nonjson_txt)
                if last_url:
                    DEBUG_RESP_URL.write_text(last_url)
            else:
                write_json(DEBUG_ERROR, {
                    "ts": iso_utc(), "step": "fetch",
                    "error": "results fetch produced no JSON (no capture)"
                })
            raise RuntimeError("results fetch produced no JSON (see data/_debug_last_response.html)")

        # Kleines Paket mit Zusatzinfo (live)
        return {"_is_live": live, "payload": last_json}


# -------- Main ----------
def main():
    print("[dpwt] starting")
    # Daten holen
    fetched = fetch_results_via_page(PLAYER_ID, TOUR_ID, SEASON)
    is_live = bool(fetched.get("_is_live"))
    if not throttle_when_not_live(is_live):
        # sauber beenden (kein Fehler)
        return

    payload = fetched["payload"]
    # Struktur tolerant verarbeiten:
    # Häufig: {"Results":[{...}, {...}] } oder direkt Liste
    if isinstance(payload, dict):
        results = payload.get("Results") or payload.get("results") or payload.get("Data") or payload.get("data") or []
    elif isinstance(payload, list):
        results = payload
    else:
        results = []

    # Dump der kompletten Saison speichern
    write_json(EVENTS_DUMP_PATH, {"ts": iso_utc(), "season": SEASON, "results": results})

    # Baseline laden/erstellen
    baseline = read_json(BASELINE_PATH, default={"season": SEASON, "index": {}, "ts": iso_utc()})
    index: Dict[str, str] = dict(baseline.get("index", {}))

    new_index: Dict[str, str] = {}
    new_events: List[Dict[str, Any]] = []
    updated_events: List[Dict[str, Any]] = []

    for ev in results:
        k = event_key(ev)
        sig = sha(event_signature(ev))
        new_index[k] = sig
        old = index.get(k)
        if old is None:
            new_events.append(ev)
        elif old != sig:
            updated_events.append(ev)

    # Baseline aktualisieren
    baseline_out = {"season": SEASON, "index": new_index, "ts": iso_utc()}
    write_json(BASELINE_PATH, baseline_out)
    print(f"[dpwt] baseline written: {BASELINE_PATH}")

    # Discord: beim allerersten Lauf (leere index zuvor) kurze Baseline-Meldung
    first_run = (len(index) == 0)
    if first_run:
        post_discord(format_baseline_msg(len(results)))
        return

    # Neue oder geänderte Events posten
    for ev in new_events:
        post_discord(format_update_msg("NEW", ev))
        time.sleep(1)
    for ev in updated_events:
        post_discord(format_update_msg("UPDATE", ev))
        time.sleep(1)

    if not new_events and not updated_events:
        print("[dpwt] no changes")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Debug-Fehlerdatei schreiben
        write_json(DEBUG_ERROR, {"ts": iso_utc(), "step": "main", "error": repr(e)})
        raise
