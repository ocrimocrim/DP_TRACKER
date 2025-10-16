import os, json, time, random
from datetime import datetime, timezone, date
import requests

# ===== Einstellungen =====
PLAYER_ID = 35703            # Marcel Schneider
TOUR_ID = 1                  # DP World Tour
SEASON = date.today().year   # Saison (bei Bedarf z.B. 2025 fix setzen)
BASE = "https://www.europeantour.com"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
UA = {"User-Agent": "github-action-dpwt-marcel-results"}

# Zustands-/Dokufiles im Repo
STATE_FILE = "et_state.json"
LOG_FILE   = "et_log.json"

# ===== Utils =====
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def jload(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def jsave(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def get_json(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()

def first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default

def discord(payload):
    if not DISCORD_WEBHOOK:
        # für Baseline/Tests keinen Hard-Fail
        return
    r = requests.post(DISCORD_WEBHOOK, json=payload, headers=UA, timeout=30)
    if r.status_code in (200, 204):
        return
    if r.status_code == 429:
        delay = float(r.headers.get("Retry-After", "5"))
        time.sleep(delay)
        r = requests.post(DISCORD_WEBHOOK, json=payload, headers=UA, timeout=30)
        if r.status_code in (200, 204):
            return
    r.raise_for_status()

def as_str(v):
    return "" if v is None else str(v)

# ===== API Pfade (aus window.et.config) =====
def url_player_results(player_id, season):
    return f"{BASE}/api/v1/players/{player_id}/results/{season}/"

def url_event_results_round(tour_id, event_id, round_no):
    return f"{BASE}/api/sportdata/Results/TourId/{tour_id}/Event/{event_id}/Round/{round_no}"

# ===== Formatierungen für Discord =====
def embed_round(tname, end_date, round_no, pos_text, strokes, to_par, total_after=None, link=None):
    lines = [
        f"**{tname}**",
        f"Runde: **R{round_no}**",
        f"End Date: {as_str(end_date)}",
        f"Pos.: {as_str(pos_text)}",
        f"Schläge R{round_no}: {as_str(strokes)}",
        f"To Par R{round_no}: {as_str(to_par)}",
    ]
    if total_after:
        lines.append(f"Total (bis R{round_no}): {as_str(total_after)}")

    return {
        "content": "Runden-Update",
        "embeds": [{
            "title": f"Update – {tname}",
            "url": link or "",
            "description": "\n".join(lines),
            "timestamp": now_iso(),
            "footer": {"text": "DP World Tour – Marcel Schneider"},
        }]
    }

def embed_finish(item, link):
    def g(k, *alts): return as_str(first(item, k, *alts, default="-"))
    tname = g("tournamentName","TournamentName","name")
    end_d = g("endDate","EndDate","date")
    pos   = g("positionText","PositionText","position")
    r2dr  = g("r2drPoints","R2DRPoints","r2dr")
    r2mr  = g("r2mrPoints","R2MRPoints","r2mr")
    prize = g("prizeMoney","PrizeMoney","prize")
    r1    = g("r1","R1"); r2 = g("r2","R2"); r3 = g("r3","R3"); r4 = g("r4","R4")
    total = g("total","Total"); topar = g("toPar","ToPar")

    desc = "\n".join([
        f"**{tname}**",
        f"End Date: {end_d}",
        f"Pos.: {pos}",
        f"R2DR Points: {r2dr}",
        f"R2MR Points: {r2mr}",
        f"Prize Money: {prize}",
        f"R1: {r1}",
        f"R2: {r2}",
        f"R3: {r3}",
        f"R4: {r4}",
        f"Total: {total}",
        f"To Par: {topar}",
    ])
    return {
        "content": "Turnier-Summary",
        "embeds": [{
            "title": f"Turnier beendet – {tname}",
            "url": link or "",
            "description": desc,
            "timestamp": now_iso(),
            "footer": {"text": "DP World Tour – Marcel Schneider"},
        }]
    }

# ===== Kern =====
def detect_max_round(item):
    played = 0
    for n in (1,2,3,4):
        v = first(item, f"r{n}", f"R{n}")
        if v not in (None, "", "-"):
            played = n
    return played

def is_finished(item):
    r4 = first(item, "r4","R4")
    if r4 not in (None, "", "-"):
        return True
    total = first(item, "total","Total")
    topar = first(item, "toPar","ToPar")
    r1 = first(item,"r1","R1"); r2=first(item,"r2","R2"); r3=first(item,"r3","R3")
    return all(x not in (None,"","-") for x in (r1,r2,r3,total,topar))

def main():
    # leichter Jitter
    time.sleep(random.randint(0, 120))

    state = jload(STATE_FILE, {"events": {}, "baseline_done": False})
    log   = jload(LOG_FILE,   {"season": SEASON, "playerId": PLAYER_ID, "events": {}})

    # aktuelle Saison-Resultate
    data = get_json(url_player_results(PLAYER_ID, SEASON))
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = data.get("items", [])

    # sortiere neueste zuerst nach EndDate
    def key_end(it): return first(it, "endDate","EndDate","date","Date","") or ""
    items.sort(key=lambda x: key_end(x), reverse=True)

    changed = False
    log_changed = False

    # === Baseline: lege vollständige Turnierliste ab, wenn noch nie geschehen ===
    if not state.get("baseline_done"):
        for it in items:
            event_id = str(first(it, "eventId","EventId","competitionId","CompetitionId"))
            if not event_id:
                continue
            tname = first(it, "tournamentName","TournamentName","name") or "Tournament"
            end_d = first(it, "endDate","EndDate","date") or ""
            link  = first(it, "url","Url","link")
            if link and link.startswith("/"):
                link = f"{BASE}{link}"

            # State-Grundlage
            state["events"][event_id] = {
                "last_round_posted": 0,
                "tournament_posted": False,
                "created_at": now_iso()
            }

            # Log-Grundlage
            log["events"][event_id] = {
                "tournamentName": tname,
                "endDate": end_d,
                "link": link,
                "baseline": {
                    "positionText": first(it, "positionText","PositionText","position"),
                    "r2drPoints": first(it, "r2drPoints","R2DRPoints","r2dr"),
                    "r2mrPoints": first(it, "r2mrPoints","R2MRPoints","r2mr"),
                    "prizeMoney": first(it, "prizeMoney","PrizeMoney","prize"),
                    "r1": first(it,"r1","R1"),
                    "r2": first(it,"r2","R2"),
                    "r3": first(it,"r3","R3"),
                    "r4": first(it,"r4","R4"),
                    "total": first(it,"total","Total"),
                    "toPar": first(it,"toPar","ToPar"),
                    "timestamp": now_iso()
                },
                "journal": []   # hier landen spätere Runden/Finish-Einträge
            }

        state["baseline_done"] = True
        changed = True
        log_changed = True

        # einmalige Info in Discord (optional)
        try:
            discord({"content": f"Monitor aktiv. Baseline gesetzt ({len(log['events'])} Turniere in {SEASON})."})
        except Exception:
            pass

    # === Normalbetrieb: neue Runden & Finish erkennen ===
    for it in items:
        event_id = str(first(it, "eventId","EventId","competitionId","CompetitionId"))
        if not event_id:
            continue

        # Stammdaten
        tname = first(it, "tournamentName","TournamentName","name") or "Tournament"
        end_d = first(it, "endDate","EndDate","date") or ""
        link  = first(it, "url","Url","link")
        if link and link.startswith("/"):
            link = f"{BASE}{link}"

        if event_id not in state["events"]:
            # neues Event (z.B. Jahreswechsel) – füge Baseline sofort hinzu
            state["events"][event_id] = {
                "last_round_posted": 0,
                "tournament_posted": False,
                "created_at": now_iso()
            }
            log["events"][event_id] = {
                "tournamentName": tname,
                "endDate": end_d,
                "link": link,
                "baseline": {
                    "positionText": first(it, "positionText","PositionText","position"),
                    "r2drPoints": first(it, "r2drPoints","R2DRPoints","r2dr"),
                    "r2mrPoints": first(it, "r2mrPoints","R2MRPoints","r2mr"),
                    "prizeMoney": first(it, "prizeMoney","PrizeMoney","prize"),
                    "r1": first(it,"r1","R1"),
                    "r2": first(it,"r2","R2"),
                    "r3": first(it,"r3","R3"),
                    "r4": first(it,"r4","R4"),
                    "total": first(it,"total","Total"),
                    "toPar": first(it,"toPar","ToPar"),
                    "timestamp": now_iso()
                },
                "journal": []
            }
            changed = True
            log_changed = True

        st = state["events"][event_id]
        last_round_posted = int(st.get("last_round_posted", 0))
        already_finished = bool(st.get("tournament_posted", False))

        # Welche Runden sind vorhanden?
        max_played = detect_max_round(it)

        # Runde(n) posten, die neu sind
        while last_round_posted < max_played:
            next_round = last_round_posted + 1

            # Hole Rundendaten (Pos/ToPar für genau diese Runde)
            pos_text = first(it, "positionText","PositionText","position") or "-"
            to_par_r = "-"
            try:
                rr = get_json(url_event_results_round(TOUR_ID, event_id, next_round))
                rows = rr.get("Players") or rr.get("players") or rr.get("items") or rr if isinstance(rr, list) else []
                for row in rows:
                    pid = str(first(row, "PlayerId","playerId","playerID"))
                    if pid == str(PLAYER_ID):
                        pos_text = first(row, "PositionText","positionText","position") or pos_text
                        to_par_r = first(row, "ToPar","toPar") or to_par_r
                        break
            except Exception:
                pass

            strokes = first(it, f"r{next_round}", f"R{next_round}") or "-"
            total_after = first(it, "total","Total")

            # Discord
            discord(embed_round(tname, end_d, next_round, pos_text, strokes, to_par_r, total_after, link))

            # Log-Eintrag
            log["events"][event_id]["journal"].append({
                "type": "round",
                "round": next_round,
                "positionText": pos_text,
                "strokes": strokes,
                "toPar": to_par_r,
                "totalAfter": total_after,
                "timestamp": now_iso()
            })

            # State aktualisieren
            last_round_posted = next_round
            st["last_round_posted"] = last_round_posted
            state["events"][event_id] = st
            changed = True
            log_changed = True
            time.sleep(1)

        # Turnier abgeschlossen?
        if (not already_finished) and is_finished(it):
            discord(embed_finish(it, link))
            st["tournament_posted"] = True
            state["events"][event_id] = st

            # vollständiger Snapshot ins Log
            log["events"][event_id]["journal"].append({
                "type": "finished",
                "positionText": first(it,"positionText","PositionText","position"),
                "r2drPoints": first(it,"r2drPoints","R2DRPoints","r2dr"),
                "r2mrPoints": first(it,"r2mrPoints","R2MRPoints","r2mr"),
                "prizeMoney": first(it,"prizeMoney","PrizeMoney","prize"),
                "r1": first(it,"r1","R1"),
                "r2": first(it,"r2","R2"),
                "r3": first(it,"r3","R3"),
                "r4": first(it,"r4","R4"),
                "total": first(it,"total","Total"),
                "toPar": first(it,"toPar","ToPar"),
                "timestamp": now_iso()
            })

            changed = True
            log_changed = True
            time.sleep(1)

    if changed:
        jsave(STATE_FILE, state)
    if log_changed:
        jsave(LOG_FILE, log)

if __name__ == "__main__":
    main()
