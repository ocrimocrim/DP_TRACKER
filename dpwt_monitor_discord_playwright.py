#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, sys, re, pathlib, datetime
from typing import Dict, Any, List, Tuple
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# -------- Konfiguration (ENV mit Fallbacks) --------
PLAYER_ID = os.getenv("DPWT_PLAYER_ID", "35703")  # Marcel Schneider
TOUR_ID   = os.getenv("DPWT_TOUR_ID", "1")       # DP World Tour
SEASON    = os.getenv("DPWT_SEASON")             # leer => aktuelles Jahr
BASE_URL  = "https://www.europeantour.com"
WEBHOOK   = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DATA_DIR  = pathlib.Path("data")
LOG_DIR   = DATA_DIR / "logs"
DEBUG_LAST_HTML = DATA_DIR / "_debug_last_response.html"
DEBUG_LAST_URL  = DATA_DIR / "_debug_last_url.txt"
DEBUG_ERROR     = DATA_DIR / "_debug_error.json"

USER_AGENT = os.getenv("DPWT_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/120.0.0.0 Safari/537.36")

TZ = "Europe/Berlin"


# -------- Helpers: Files, JSON, Time --------
def now_utc_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def now_local() -> datetime.datetime:
    # “Pseudo”-Lokalzeit, reicht für Gate-Logic
    return datetime.datetime.now()


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: pathlib.Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def write_json(path: pathlib.Path, obj: Any):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_debug(step: str, err: str):
    write_json(DEBUG_ERROR, {"ts": now_utc_iso(), "step": step, "error": err})


def post_discord(text: str):
    if not WEBHOOK:
        print("[dpwt] WARN: Kein DISCORD_WEBHOOK_URL gesetzt -> skip post")
        return None
    payload = {"content": text}
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    print(f"[dpwt] discord post sent: {r.status_code}")
    return r.status_code


# -------- Domain-Logik: DPWT erkennen, Taktik (30min live / 2h sonst) --------
def should_run_this_tick(live: bool) -> bool:
    """
    Actions-Schedule setzt *alle 30 Minuten* (siehe Workflow).
    - Wenn live Event -> immer weiterlaufen (jede 30min)
    - Wenn nicht live -> nur zu bestimmten Stunden (2h-Takt tagsüber)
    """
    t = now_local()
    hour = t.hour
    minute = t.minute

    # Tagesfenster (Berlin-Zeit): 07–21 Uhr
    daytime_hours = {7, 9, 11, 13, 15, 17, 19, 21}

    if live:
        return True  # alle 30 Minuten durchlaufen

    # nicht live: nur 2h-Korridor und exakt Minute == 0
    if hour in daytime_hours and minute == 0:
        return True
    return False


# -------- Scrape: über Playwright + JS fetch (Same-Origin) --------
def fetch_results_via_page(page, player_id: str, tour_id: str, season: int) -> Dict[str, Any]:
    """
    Lädt die Spieler-Seite, klickt Cookie-Consent, liest über window.fetch (im Browser-Kontext)
    die JSON vom internen API-Endpunkt. Das vermeidet 403 von Edge/Akamai gegen “Bot-Clients”.
    """
    url = f"{BASE_URL}/players/{player_id}/results?tour=dpworld-tour"
    page.set_default_timeout(30000)
    page.goto(url, wait_until="domcontentloaded")
    # Cookie Banner wegklicken (OneTrust)
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=5000)
        print("[dpwt] consent clicked")
    except PWTimeoutError:
        pass
    except Exception:
        pass

    # Warte kurz, dann im Page-Kontext “fetch”
    time.sleep(1.5)
    api_url = f"/api/v1/players/{player_id}/results/{season}/?tourId={tour_id}"

    try:
        js = """
        (async () => {
          const u = arguments[0];
          const res = await fetch(u, { credentials: 'same-origin' });
          const ct = res.headers.get('content-type') || '';
          const text = await res.text();
          if (!ct.includes('application/json')) {
            return { ok: false, url: u, contentType: ct, text: text };
          }
          try {
            const data = JSON.parse(text);
            return { ok: true, url: u, data: data };
          } catch (e) {
            return { ok: false, url: u, contentType: ct, text: text };
          }
        })();
        """
        result = page.evaluate(js, api_url)

        if not result or not result.get("ok"):
            # Debug dump, falls HTML (Access Denied / Captcha)
            DEBUG_LAST_URL.write_text(result.get("url", api_url), encoding="utf-8")
            DEBUG_LAST_HTML.write_text(result.get("text", ""), encoding="utf-8")
            write_debug("fetch", "results fetch produced no JSON (see data/_debug_last_response.html)")
            raise RuntimeError("results fetch produced no JSON (see data/_debug_last_response.html)")

        return result["data"]
    except Exception as e:
        # Fallback: HTML dump der aktuellen Seite
        try:
            DEBUG_LAST_URL.write_text(api_url, encoding="utf-8")
            DEBUG_LAST_HTML.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        write_debug("fetch", repr(e))
        raise


def extract_live_status(page) -> bool:
    """
    Prüft, ob im DOM ein live Event-Banner mit Status aktiv ist.
    Grobe Heuristik: live-event-banner vorhanden und enthält RoundStatus/Status != 0.
    """
    try:
        # Der Banner hängt als Custom Element im DOM; wir lesen dessen innerHTML
        html = page.locator("live-event-banner").first.inner_html(timeout=3000)
        # Quick heuristics
        return "RoundNo" in html or "RoundStatus" in html or "Status" in html
    except Exception:
        return False


def normalize_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mapped API-Felder auf dein gewünschtes Schema.
    Der API-Shape enthält je nach Saison leicht andere Keys; wir sind defensiv.
    """
    # Kandidatenschlüssel (verschiedene Schreibweisen)
    name = ev.get("EventName") or ev.get("Event") or ev.get("Tournament") or ev.get("Name") or ""
    end_date = ev.get("EndDate") or ev.get("EventEndDate") or ev.get("Date") or ev.get("EventDate") or ""

    def g(key_list, default=""):
        for k in key_list:
            if k in ev and ev[k] is not None:
                return ev[k]
        return default

    out = {
        "End Date": end_date,
        "Tournament": name,
        "Pos.": g(["Position", "Pos", "Pos."]),
        "R2DR Points": g(["R2DRPoints", "R2DR Points"]),
        "R2MR Points": g(["R2MRPoints", "R2MR Points"]),
        "Prize Money": g(["PrizeMoney", "Prize Money", "Earnings"]),
        "R1": g(["R1"]),
        "R2": g(["R2"]),
        "R3": g(["R3"]),
        "R4": g(["R4"]),
        "Total": g(["Total"]),
        "To Par": g(["ToPar", "To Par"]),
        # Für matching/ID:
        "_EventId": g(["EventId", "EventID", "Id"]),
    }

    # “finished” Heuristik: Total mit Zahl + (R4 vorhanden ODER R3 vorhanden aber R4 fehlt (Cut))
    total_raw = out["Total"]
    total_ok = isinstance(total_raw, (str, int)) and str(total_raw).strip() not in ("", "-")
    r4 = str(out["R4"] or "").strip()
    r3 = str(out["R3"] or "").strip()
    finished = False
    if total_ok and (r4 or (r3 and not r4)):
        finished = True
    out["_finished"] = finished

    return out


def to_key(ev: Dict[str, Any]) -> str:
    """
    Stabiler Schlüssel pro Event: bevorzugt EventId, sonst Name+EndDate
    """
    eid = ev.get("_EventId")
    if eid:
        return f"id:{eid}"
    return f"name:{ev.get('Tournament','')}|end:{ev.get('End Date','')}"


def compare_baseline(old_list: List[Dict[str, Any]], new_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Liefert Diffs: new_tournaments, round_updates, finished_now (mit Vorher/Nachher).
    “Round-Updates”: neue Werte in R1..R4 oder Positions-/ToPar-Änderungen.
    """
    old_map = {to_key(e): e for e in old_list}
    new_map = {to_key(e): e for e in new_list}

    changes = {
        "new_tournaments": [],
        "round_updates": [],
        "finished_now": [],
    }

    # neue Turniere
    for k, ev in new_map.items():
        if k not in old_map:
            changes["new_tournaments"].append(ev)

    # Updates / “finished now”
    for k, new_ev in new_map.items():
        if k not in old_map:
            continue
        old_ev = old_map[k]

        # Runden-Änderungen + Pos./To Par-Änderungen
        fields = ["R1", "R2", "R3", "R4", "Pos.", "To Par", "Total", "Prize Money", "R2DR Points", "R2MR Points"]
        diffs = {}
        for f in fields:
            if str(old_ev.get(f) or "") != str(new_ev.get(f) or ""):
                diffs[f] = {"old": old_ev.get(f), "new": new_ev.get(f)}

        if diffs:
            changes["round_updates"].append({"event": new_ev, "diffs": diffs})

        # Finished?
        if not old_ev.get("_finished") and new_ev.get("_finished"):
            changes["finished_now"].append({"event": new_ev, "before": old_ev})

    return changes


def format_round_post(ev: Dict[str, Any]) -> str:
    # Kompakt, gut lesbar für Discord
    lines = []
    lines.append(f"**{ev.get('Tournament','(unbekannt)')}** – Zwischenstand")
    if ev.get("Pos."):   lines.append(f"Platzierung: **{ev['Pos.']}**")
    if ev.get("To Par"): lines.append(f"To Par: **{ev['To Par']}**")
    # Runden, nur die gefüllten
    for r in ["R1", "R2", "R3", "R4"]:
        v = str(ev.get(r) or "").strip()
        if v and v != "-":
            lines.append(f"{r}: **{v}**")
    if ev.get("Total"):  lines.append(f"Total: **{ev['Total']}**")
    return "\n".join(lines)


def format_finished_post(ev: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"**{ev.get('Tournament','(unbekannt)')}** – beendet ✅")
    if ev.get("Pos."):         lines.append(f"Endplatzierung: **{ev['Pos.']}**")
    if ev.get("To Par"):       lines.append(f"To Par: **{ev['To Par']}**")
    if ev.get("Total"):        lines.append(f"Total: **{ev['Total']}**")
    if ev.get("Prize Money"):  lines.append(f"Preisgeld: **{ev['Prize Money']}**")
    if ev.get("R2DR Points"):  lines.append(f"R2DR Punkte: **{ev['R2DR Points']}**")
    if ev.get("R2MR Points"):  lines.append(f"R2MR Punkte: **{ev['R2MR Points']}**")
    # Rundenübersicht immer anhängen:
    rounds = " • ".join([f"{r}:{str(ev.get(r) or '-').strip()}" for r in ["R1","R2","R3","R4"]])
    lines.append(rounds)
    return "\n".join(lines)


def main():
    ensure_dirs()

    # Jahr ermitteln (lokal, Berlin-Zeit), damit 2026 automatisch weiterläuft
    season = int(SEASON) if SEASON else now_local().year
    baseline_path = DATA_DIR / f"baseline-{season}.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/Berlin"
        )
        page = context.new_page()

        try:
            # 1) Daten holen
            data = fetch_results_via_page(page, PLAYER_ID, TOUR_ID, season)
            # Live-Status prüfen (steuert 30min vs. 2h)
            live = extract_live_status(page)

            # Gate: falls nicht “dran”, beenden (aber Baseline sicherstellen)
            if not should_run_this_tick(live):
                # Baseline ggf. initial setzen
                if not baseline_path.exists():
                    events = [normalize_event(e) for e in (data or [])]
                    write_json(baseline_path, {"ts": now_utc_iso(), "season": season, "tournaments": events})
                    if WEBHOOK:
                        post_discord(f"Turniertracker\nMonitor aktiv. Baseline {season} gesetzt ({len(events)} Turniere).")
                print("[dpwt] skip tick (nicht live & nicht im 2h-Fenster)")
                return

            # 2) Normalisieren
            events = [normalize_event(e) for e in (data or [])]

            # 3) Baseline lesen/setzen
            baseline = read_json(baseline_path, default=None)
            first_run = baseline is None
            if first_run:
                write_json(baseline_path, {"ts": now_utc_iso(), "season": season, "tournaments": events})
                if WEBHOOK:
                    post_discord(f"Turniertracker\nMonitor aktiv. Baseline {season} gesetzt ({len(events)} Turniere).")
                print(f"[dpwt] baseline written: {baseline_path}")
                return

            old_events = baseline.get("tournaments", [])
            # 4) Diff
            changes = compare_baseline(old_events, events)

            if not (changes["new_tournaments"] or changes["round_updates"] or changes["finished_now"]):
                print("[dpwt] no changes")
                return

            # 5) Log schreiben
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
            log_path = LOG_DIR / f"{ts}.json"
            log_obj = {
                "ts": now_utc_iso(),
                "season": season,
                "live": live,
                "changes": changes
            }
            write_json(log_path, log_obj)

            # 6) Discord Posts
            posts: List[str] = []

            for ev in changes["new_tournaments"]:
                name = ev.get("Tournament", "(unbekannt)")
                posts.append(f"**Neues Turnier im Kalender:** {name}\nEnddatum: {ev.get('End Date','-')}")

            # Round-Updates: poste kompakt (ein Post pro Event)
            for item in changes["round_updates"]:
                ev = item["event"]
                posts.append(format_round_post(ev))

            for item in changes["finished_now"]:
                ev = item["event"]
                posts.append(format_finished_post(ev))

            for content in posts:
                post_discord(content)
                time.sleep(0.5)  # minimaler Puffer

            # 7) Baseline aktualisieren
            write_json(baseline_path, {"ts": now_utc_iso(), "season": season, "tournaments": events})
            print(f"[dpwt] baseline updated ({baseline_path})")

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_debug("main", repr(e))
        print(f"[dpwt] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
