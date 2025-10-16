import os, json, time, hashlib, datetime as dt
import requests

BASE = "https://www.europeantour.com"
PLAYER_ID = 35703          # Marcel Schneider
TOUR_ID = 1                # DP World Tour
UA = {
    "User-Agent": "gh-actions-dpwt-marcel/1.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour",
}

DATA_DIR = "data"
STATE_DIR = "state"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

def url_player_results(player_id: int, season: int) -> str:
    # tourId=1 ist wichtig, sonst kommen manchmal leere Listen zurück
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

def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

def get(season_url):
    r = requests.get(season_url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()

def discord(msg):
    if not WEBHOOK:
        return
    try:
        requests.post(WEBHOOK, json=msg, timeout=20)
    except Exception:
        pass

def current_season():
    # Wenn heute im Dez/Jan? – API liefert per season klar; hier simple Heuristik:
    return dt.datetime.utcnow().year

def has_live_event():
    # Prüfe globale DPWT-Statusliste (einfach, schnell)
    try:
        r = requests.get(url_event_status(), headers=UA, timeout=20)
        r.raise_for_status()
        arr = r.json() or []
        # Live wenn irgendein Event Status 2 (running) oder RoundStatus 2 etc.
        for ev in arr:
            if ev.get("TourId") == TOUR_ID and (ev.get("Status") in (1,2) or ev.get("RoundStatus") in (1,2)):
                return True
    except Exception:
        return False
    return False

def throttle_ok():
    # Wenn live: NIE drosseln (wir laufen ja eh 30-Minuten-Cron)
    if has_live_event():
        return True, "live"
    # Sonst: nur alle 2h
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    last_ts = state.get("last_check_ts")
    if not last_ts:
        return True, "first"
    last = dt.datetime.fromisoformat(last_ts.replace("Z",""))
    if (dt.datetime.utcnow() - last) >= dt.timedelta(hours=2):
        return True, "2h-pass"
    return False, "throttled"

def save_last_check():
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    state["last_check_ts"] = now_utc()
    with open(f"{STATE_DIR}/last_seen.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def ensure_baseline(season, items):
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
    with open(base_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    # Info in Discord
    discord({"content": f"Monitor aktiv. Baseline {season} gesetzt ({len(items)} Turniere)."})
    return True

def load_state():
    path = f"{STATE_DIR}/events.json"
    return load_json(path, {"events": {}})

def save_state(state):
    with open(f"{STATE_DIR}/events.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def ev_key(ev):
    # Die season-API liefert "CompetitionId" (eventId) – nutzbar als Schlüssel.
    # Fallback: Name+EndDate Hash.
    cid = ev.get("CompetitionId") or ev.get("EventId")
    if cid:
        return str(cid)
    return sha([ev.get("Tournament"), ev.get("EndDate")])

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
        "color": 0x2ecc71,  # grün
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
        "color": 0x3498db,  # blau
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

def main():
    # Saison automatisch bestimmen, aber auch Übergang handlen
    season = current_season()
    url = url_player_results(PLAYER_ID, season)
    ok, reason = throttle_ok()
    if not ok:
        # leise exit
        return

    try:
        items = get(url)
    except Exception as e:
        # Fallback: ohne tourId (sollte kaum nötig sein)
        try:
            fallback = f"{BASE}/api/v1/players/{PLAYER_ID}/results/{season}/"
            items = get(fallback)
        except Exception:
            return

    # Baseline schreiben, falls fehlt
    ensure_baseline(season, items)

    # State laden
    state = load_state()
    events = state["events"]

    # Journal-Datei für die Saison
    jpath = f"{DATA_DIR}/history-{season}.jsonl"

    # Iteriere durch Turniere
    for ev in items or []:
        key = ev_key(ev)
        t_name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
        # Init
        if key not in events:
            events[key] = {"rounds": {}, "finished": False, "name": t_name}

        # Runde(n) erkennen (neue Werte)
        rdat = round_fields_from_item(ev)
        # Welche Runden existieren?
        for rno in (1,2,3,4):
            col = f"R{rno}"
            val = (rdat.get(col) or "").strip() if rdat.get(col) else ""
            if val and events[key]["rounds"].get(str(rno)) != val:
                # Neue Runde oder aktualisiert -> Discord posten
                post_round_update(
                    t_name=t_name,
                    round_no=rno,
                    pos=rdat.get("Pos"),
                    strokes=val,
                    to_par=None,  # DPWT-Ergebnisliste hat kein explizites ToPar je Runde; könnte per Round-API ergänzt werden
                    total=rdat.get("Total"),
                    url=ev.get("Link") or ev.get("TournamentUrl")
                )
                events[key]["rounds"][str(rno)] = val
                # Journal
                append_jsonl(jpath, {
                    "ts": now_utc(), "type": "round", "eventKey": key, "round": rno,
                    "position": rdat.get("Pos"), "strokes": val, "total": rdat.get("Total"),
                    "tournament": t_name
                })

        # Turnier fertig? Indikator: Total + ToPar vorhanden und keine fehlende R4 mehr
        finished = bool((ev.get("Total") or "").strip()) and ((ev.get("R4") or ev.get("R3") or "").strip())
        if finished and not events[key]["finished"]:
            # Finalpost
            post_final_summary(ev)
            events[key]["finished"] = True
            append_jsonl(jpath, {
                "ts": now_utc(), "type": "finished", "eventKey": key, "tournament": t_name,
                "snapshot": ev
            })

    save_state(state)
    save_last_check()

if __name__ == "__main__":
    main()
