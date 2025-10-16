import os, json, hashlib, datetime as dt
from typing import Any, List
import requests
from playwright.sync_api import sync_playwright, APIResponse, TimeoutError as PWTimeout

DEBUG = os.environ.get("DEBUG") == "1"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

BASE = "https://www.europeantour.com"
PLAYER_ID = 35703   # Marcel Schneider
TOUR_ID = 1         # DP World Tour

DATA_DIR = "data"
STATE_DIR = "state"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

def log(*a): 
    if DEBUG: 
        print("[dpwt]", *a, flush=True)

def now_utc() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()+"Z"

def current_season() -> int:
    return dt.datetime.utcnow().year

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

def url_player_results(player_id: int, season: int) -> str:
    return f"{BASE}/api/v1/players/{player_id}/results/{season}/?tourId={TOUR_ID}"

def url_event_status() -> str:
    return f"{BASE}/api/sportdata/Event/Status"

def discord(payload: dict):
    if not WEBHOOK:
        log("no DISCORD_WEBHOOK configured – skipping send")
        return
    try:
        requests.post(WEBHOOK, json=payload, timeout=20)
        log("discord post sent")
    except Exception as e:
        log("discord error:", repr(e))

def ensure_baseline(season: int, items: List[dict]):
    path = f"{DATA_DIR}/baseline-{season}.json"
    if os.path.exists(path):
        return False
    baseline = {
        "season": season,
        "created": now_utc(),
        "count": len(items),
        "hash": sha(items),
        "items": items
    }
    write_json(path, baseline)
    discord({"content": f"Monitor aktiv. Baseline {season} gesetzt ({len(items)} Turniere)."})
    log("baseline written:", path)
    return True

def save_last_check():
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    state["last_check_ts"] = now_utc()
    write_json(f"{STATE_DIR}/last_seen.json", state)

def throttle_ok(is_live: bool):
    st = load_json(f"{STATE_DIR}/last_seen.json", {})
    last = st.get("last_check_ts")
    if not last:
        return True, "first"
    if is_live:
        # alle 30 Min (Runner läuft ohnehin alle 30 Min)
        return True, "live"
    last_dt = dt.datetime.fromisoformat(last.replace("Z",""))
    if (dt.datetime.utcnow() - last_dt) >= dt.timedelta(hours=2):
        return True, "2h-pass"
    return False, "throttled"

def normalize_items(api_obj: Any) -> List[dict]:
    if isinstance(api_obj, list): 
        return api_obj
    if isinstance(api_obj, dict):
        for k in ("Results","results","Items","items","Data","data"):
            v = api_obj.get(k)
            if isinstance(v, list):
                return v
    return []

def ev_key(ev: dict) -> str:
    cid = ev.get("CompetitionId") or ev.get("EventId")
    if cid: 
        return str(cid)
    return sha([ev.get("Tournament") or ev.get("TournamentName"), ev.get("EndDate")])

def round_fields(ev: dict):
    return {
        "R1": ev.get("R1"), "R2": ev.get("R2"), "R3": ev.get("R3"), "R4": ev.get("R4"),
        "Total": ev.get("Total"), "ToPar": ev.get("ToPar"),
        "Pos": ev.get("PositionText") or ev.get("Position") or ev.get("Pos")
    }

def post_round_update(name, rno, pos, strokes, total, url=None):
    embed = {
        "title": f"Runden-Update – {name}",
        "url": url, "color": 0x2ecc71,
        "fields": [
            {"name": "Runde", "value": f"R{rno}", "inline": True},
            {"name": "Pos.", "value": pos or "–", "inline": True},
            {"name": f"Schläge R{rno}", "value": strokes or "–", "inline": True},
            {"name": "Total (bis jetzt)", "value": total or "–", "inline": True},
        ],
        "footer": {"text": "DP World Tour – Marcel Schneider"},
    }
    discord({"embeds": [embed]})

def post_final(ev):
    name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
    embed = {
        "title": f"Turnier beendet – {name}",
        "url": ev.get("TournamentUrl") or ev.get("Link"),
        "color": 0x3498db,
        "fields": [
            {"name": "End Date", "value": ev.get("EndDate") or "–", "inline": True},
            {"name": "Pos.", "value": ev.get("PositionText") or "–", "inline": True},
            {"name": "R2DR Points", "value": ev.get("R2DRPoints") or ev.get("R2DR") or "–", "inline": True},
            {"name": "R2MR Points", "value": ev.get("R2MRPoints") or ev.get("R2MR") or "–", "inline": True},
            {"name": "Prize Money", "value": ev.get("PrizeMoney") or "–", "inline": True},
            {"name": "R1", "value": ev.get("R1") or "–", "inline": True},
            {"name": "R2", "value": ev.get("R2") or "–", "inline": True},
            {"name": "R3", "value": ev.get("R3") or "–", "inline": True},
            {"name": "R4", "value": ev.get("R4") or "–", "inline": True},
            {"name": "Total", "value": ev.get("Total") or "–", "inline": True},
            {"name": "To Par", "value": ev.get("ToPar") or "–", "inline": True},
        ],
        "footer": {"text": "DP World Tour – Marcel Schneider"},
    }
    discord({"embeds": [embed]})

def has_live_event(context) -> bool:
    try:
        res: APIResponse = context.request.get(url_event_status(), max_redirects=5, timeout=30000)
        if not res.ok:
            return False
        data = res.json()
        for ev in data or []:
            if ev.get("TourId") == TOUR_ID and (ev.get("Status") in (1,2) or ev.get("RoundStatus") in (1,2)):
                return True
    except Exception as e:
        log("live-check failed:", repr(e))
    return False

def fetch_results_via_browser(context, season: int) -> Any:
    api = url_player_results(PLAYER_ID, season)
    # 1) Versuch: über context.request (nimmt Cookies/Headers aus Browser-Kontext)
    res: APIResponse = context.request.get(api, headers={"Accept": "application/json"}, max_redirects=5, timeout=45000)
    if res.ok:
        return res.json()
    # 2) Fallback: innerhalb der Seite via fetch() (same-origin, nutzt JS-Stack)
    page = context.new_page()
    try:
        page.goto(f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour", timeout=60000)
        page.wait_for_load_state("domcontentloaded")
        js = f"""() => fetch("{api}", {{credentials:"include"}}).then(r => r.json())"""
        data = page.evaluate(js)
        return data
    finally:
        page.close()

def main():
    season = current_season()
    raw_path = f"{DATA_DIR}/raw-{season}.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
            locale="de-DE",
        )
        page = context.new_page()
        try:
            # Seite aufrufen, damit Consent/Cookies/Headers gesetzt werden
            page.goto(f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour", timeout=60000)
            page.wait_for_load_state("domcontentloaded")
        except PWTimeout:
            pass  # selbst wenn Werbung/CMP blockiert – das reicht für Cookies

        # JSON holen (mit Cookies)
        try:
            raw = fetch_results_via_browser(context, season)
        except Exception as e:
            write_json(f"{DATA_DIR}/_debug_error.json", {"ts": now_utc(), "step": "fetch", "error": repr(e)})
            log("fetch failed, wrote _debug_error.json", repr(e))
            context.close(); browser.close()
            return

        # Rohdump immer speichern
        write_json(raw_path, raw)

        items = normalize_items(raw)
        log("items:", len(items))
        if not items:
            write_json(f"{DATA_DIR}/_debug_warning.json", {"ts": now_utc(), "step": "normalize", "note": "0 items – check raw"})
            context.close(); browser.close()
            # baseline trotzdem schreiben (leer), damit du siehst, dass es lief
            ensure_baseline(season, [])
            save_last_check()
            return

        # Baseline zuerst
        ensure_baseline(season, items)

        # Throttle
        live = has_live_event(context)
        ok, reason = throttle_ok(live)
        log("throttle:", reason, "live" if live else "not-live")
        if not ok:
            context.close(); browser.close()
            return

        # State laden
        state = load_json(f"{STATE_DIR}/events.json", {"events": {}})
        events = state["events"]
        hist = f"{DATA_DIR}/history-{season}.jsonl"

        for ev in items:
            key = ev_key(ev)
            name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
            events.setdefault(key, {"rounds": {}, "finished": False, "name": name})

            rdat = round_fields(ev)
            for rno in (1,2,3,4):
                col = f"R{rno}"
                val = (rdat.get(col) or "").strip() if rdat.get(col) else ""
                if val and events[key]["rounds"].get(str(rno)) != val:
                    post_round_update(name, rno, rdat.get("Pos"), val, rdat.get("Total"), ev.get("Link") or ev.get("TournamentUrl"))
                    events[key]["rounds"][str(rno)] = val
                    append_jsonl(hist, {"ts": now_utc(), "type": "round", "eventKey": key, "round": rno,
                                        "position": rdat.get("Pos"), "strokes": val, "total": rdat.get("Total"),
                                        "tournament": name})

            finished = bool((ev.get("Total") or "").strip()) and ((ev.get("R4") or ev.get("R3") or "").strip())
            if finished and not events[key]["finished"]:
                post_final(ev)
                events[key]["finished"] = True
                append_jsonl(hist, {"ts": now_utc(), "type": "finished", "eventKey": key,
                                    "tournament": name, "snapshot": ev})

        write_json(f"{STATE_DIR}/events.json", state)
        save_last_check()
        context.close(); browser.close()
        log("done")

if __name__ == "__main__":
    main()
