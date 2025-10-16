import os, json, time, hashlib, datetime as dt
import requests
from typing import Any, List

DEBUG = os.environ.get("DEBUG") == "1"

BASE = "https://www.europeantour.com"
PLAYER_ID = 35703          # Marcel Schneider
TOUR_ID = 1                # DP World Tour

UA = {
    "User-Agent": "gh-actions-dpwt-marcel/1.1",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour",
    "Origin": BASE,
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

DATA_DIR = "data"
STATE_DIR = "state"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

def log(*args):
    if DEBUG:
        print("[dpwt]", *args, flush=True)

def url_player_results(player_id: int, season: int) -> str:
    return f"{BASE}/api/v1/players/{player_id}/results/{season}/?tourId={TOUR_ID}"

def url_event_round_results(event_id: int, round_no: int) -> str:
    return f"{BASE}/api/sportdata/Results/TourId/{TOUR_ID}/Event/{event_id}/Round/{round_no}"

def url_event_status() -> str:
    return f"{BASE}/api/sportdata/Event/Status"

def now_utc() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()+"Z"

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

def http_json(url):
    log("GET", url)
    r = requests.get(url, headers=UA, timeout=40)
    log("status", r.status_code)
    r.raise_for_status()
    return r.json()

def discord(msg):
    if not WEBHOOK:
        log("no DISCORD_WEBHOOK – skipping send")
        return
    try:
        requests.post(WEBHOOK, json=msg, timeout=20)
        log("discord post sent")
    except Exception as e:
        log("discord error:", repr(e))

def current_season():
    return dt.datetime.utcnow().year

def has_live_event():
    try:
        arr = http_json(url_event_status()) or []
        for ev in arr:
            if ev.get("TourId") == TOUR_ID and (ev.get("Status") in (1,2) or ev.get("RoundStatus") in (1,2)):
                return True
    except Exception as e:
        log("live-check failed:", repr(e))
        # lieber nicht blockieren, wir machen dann normalen 2h-Takt
    return False

def throttle_ok():
    # Baseline/erster Lauf soll IMMER laufen -> wenn es noch keinen Zeitstempel gibt: True
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    last_ts = state.get("last_check_ts")
    if not last_ts:
        return True, "first"

    if has_live_event():
        return True, "live"

    last = dt.datetime.fromisoformat(last_ts.replace("Z",""))
    if (dt.datetime.utcnow() - last) >= dt.timedelta(hours=2):
        return True, "2h-pass"
    return False, "throttled"

def save_last_check():
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    state["last_check_ts"] = now_utc()
    write_json(f"{STATE_DIR}/last_seen.json", state)

def ensure_baseline(season, items: List[dict]):
    base_path = f"{DATA_DIR}/baseline-{season}.json"
    if os.path.exists(base_path):
        return False
    baseline = {
        "season": season,
        "created": now_utc(),
        "count": len(items or []),
        "hash": sha(items),
        "items": items
    }
    write_json(base_path, baseline)
    discord({"content": f"Monitor aktiv. Baseline {season} gesetzt ({len(items)} Turniere)."})
    log("baseline written:", base_path)
    return True

def load_state():
    path = f"{STATE_DIR}/events.json"
    return load_json(path, {"events": {}})

def save_state(state):
    write_json(f"{STATE_DIR}/events.json", state)

def ev_key(ev):
    cid = ev.get("CompetitionId") or ev.get("EventId")
    if cid:
        return str(cid)
    return sha([ev.get("Tournament") or ev.get("TournamentName"), ev.get("EndDate")])

def round_fields_from_item(ev):
    return {
        "R1": ev.get("R1"), "R2": ev.get("R2"), "R3": ev.get("R3"), "R4": ev.get("R4"),
        "Total": ev.get("Total"), "ToPar": ev.get("ToPar"),
        "Pos": ev.get("PositionText") or ev.get("Position") or ev.get("Pos")
    }

def post_round_update(t_name, round_no, pos, strokes, to_par, total, url=None):
    embed = {
        "title": f"Runden-Update – {t_name}",
        "url": url,
        "color": 0x2ecc71,
        "fields": [
            {"name": "Runde", "value": f"R{round_no}", "inline": True},
            {"name": "Pos.", "value": pos or "–", "inline": True},
            {"name": f"Schläge R{round_no}", "value": strokes or "–", "inline": True},
            {"name": f"To Par R{round_no}", "value": to_par or "–", "inline": True},
            {"name": "Total (bis jetzt)", "value": total or "–", "inline": True},
        ],
        "footer": {"text": "DP World Tour – Marcel Schneider"},
    }
    discord({"embeds": [embed]})

def post_final_summary(ev):
    t_name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
    embed = {
        "title": f"Turnier beendet – {t_name}",
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

def normalize_items(api_obj: Any) -> List[dict]:
    # Akzeptiere Liste oder Objekt mit z. B. "Results"/"Items"
    if isinstance(api_obj, list):
        return api_obj
    if isinstance(api_obj, dict):
        for key in ("Results", "results", "Items", "items", "Data", "data"):
            if isinstance(api_obj.get(key), list):
                return api_obj[key]
    # Wenn wir hier sind, ist das Format anders -> speichere Rohdaten zur Analyse
    return []

def main():
    season = current_season()  # 2025 jetzt; läuft in 2026 automatisch weiter
    url = url_player_results(PLAYER_ID, season)

    # API holen (Fehler robust behandeln, Rohdump schreiben)
    try:
        raw = http_json(url)
    except Exception as e:
        # Fallback ohne tourId
        try:
            log("primary failed, retrying without tourId")
            raw = http_json(f"{BASE}/api/v1/players/{PLAYER_ID}/results/{season}/")
        except Exception as e2:
            err = {"ts": now_utc(), "step": "fetch", "error": repr(e2)}
            write_json(f"{DATA_DIR}/_debug_error.json", err)
            log("fetch failed -> wrote data/_debug_error.json")
            return

    # Rohdaten IMMER sichern
    write_json(f"{DATA_DIR}/raw-{season}.json", raw)

    # Items normalisieren
    items = normalize_items(raw)
    log("items found:", len(items))

    # Sicherheit: wenn 0 Items -> abbrechen, aber Fehler anzeigen
    if not items:
        warn = {
            "ts": now_utc(),
            "step": "normalize",
            "note": "0 items after normalization – check raw file",
        }
        write_json(f"{DATA_DIR}/_debug_warning.json", warn)
        log("0 items – wrote data/_debug_warning.json")
        # Baseline trotzdem leer schreiben, damit wir beim nächsten Lauf sehen, dass es gelaufen ist
        ensure_baseline(season, [])
        save_last_check()
        return

    # Baseline zuerst (kein Throttle davor!)
    ensure_baseline(season, items)

    # Ab hier kann gedrosselt werden
    ok, reason = throttle_ok()
    log("throttle:", reason)
    if not ok:
        return

    state = load_state()
    events = state["events"]
    jpath = f"{DATA_DIR}/history-{season}.jsonl"

    for ev in items:
        key = ev_key(ev)
        t_name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
        events.setdefault(key, {"rounds": {}, "finished": False, "name": t_name})

        rdat = round_fields_from_item(ev)
        for rno in (1,2,3,4):
            col = f"R{rno}"
            val = (rdat.get(col) or "").strip() if rdat.get(col) else ""
            if val and events[key]["rounds"].get(str(rno)) != val:
                post_round_update(
                    t_name=t_name, round_no=rno, pos=rdat.get("Pos"),
                    strokes=val, to_par=None, total=rdat.get("Total"),
                    url=ev.get("Link") or ev.get("TournamentUrl")
                )
                events[key]["rounds"][str(rno)] = val
                append_jsonl(jpath, {
                    "ts": now_utc(), "type": "round", "eventKey": key, "round": rno,
                    "position": rdat.get("Pos"), "strokes": val, "total": rdat.get("Total"),
                    "tournament": t_name
                })

        finished = bool((ev.get("Total") or "").strip()) and ((ev.get("R4") or ev.get("R3") or "").strip())
        if finished and not events[key]["finished"]:
            post_final_summary(ev)
            events[key]["finished"] = True
            append_jsonl(jpath, {
                "ts": now_utc(), "type": "finished", "eventKey": key,
                "tournament": t_name, "snapshot": ev
            })

    save_state(state)
    save_last_check()
    log("done")

if __name__ == "__main__":
    main()
