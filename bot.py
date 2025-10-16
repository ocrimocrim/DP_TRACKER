import os
import json
import time
from datetime import datetime, timedelta, timezone
import requests

API_URL = "https://www.europeantour.com/api/v1/players/35703/results/2025/"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")            # von Actions automatisch gesetzt
GH_REPO = os.getenv("GH_REPO")                      # owner/repo, z. B. "deinuser/DP_World_Tour_Marcel_Newsfeed"
STATE_ISSUE_NUMBER = int(os.getenv("STATE_ISSUE_NUMBER", "0"))  # eine existierende Issue als KV-Speicher

USER_AGENT = "dpworld-results-bot/1.0 (+github actions)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

def iso_to_date(s):
    if not s:
        return None
    # Beispiel 2025-10-12T00:00:00+00:00
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def send_discord(text):
    if not DISCORD_WEBHOOK:
        print("Kein DISCORD_WEBHOOK gesetzt. Output:")
        print(text)
        return
    r = SESSION.post(DISCORD_WEBHOOK, json={"content": text}, timeout=20)
    r.raise_for_status()

def gh_get_state():
    if not (GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER):
        return {}
    url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = SESSION.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=20)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    body = r.json().get("body") or ""
    marker_start = "<!--STATE_JSON_START-->"
    marker_end = "<!--STATE_JSON_END-->"
    if marker_start in body and marker_end in body:
        blob = body.split(marker_start)[1].split(marker_end)[0].strip()
        try:
            return json.loads(blob)
        except Exception:
            return {}
    return {}

def gh_set_state(state: dict, title_fallback="DPWT Newsfeed State"):
    if not (GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER):
        return
    url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = SESSION.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=20)
    if r.status_code == 404:
        # Issue anlegen
        url_new = f"https://api.github.com/repos/{GH_REPO}/issues"
        body = f"{title_fallback}\n\n<!--STATE_JSON_START-->\n{json.dumps(state, ensure_ascii=False, indent=2)}\n<!--STATE_JSON_END-->"
        r2 = SESSION.post(url_new, headers={"Authorization": f"token {GITHUB_TOKEN}"}, json={"title": title_fallback, "body": body}, timeout=20)
        r2.raise_for_status()
        return
    r.raise_for_status()
    issue = r.json()
    body_old = issue.get("body") or ""
    marker_start = "<!--STATE_JSON_START-->"
    marker_end = "<!--STATE_JSON_END-->"
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    if marker_start in body_old and marker_end in body_old:
        new_body = body_old.split(marker_start)[0] + marker_start + "\n" + payload + "\n" + marker_end + body_old.split(marker_end)[1]
    else:
        new_body = f"{body_old}\n\n<!--STATE_JSON_START-->\n{payload}\n<!--STATE_JSON_END-->"
    url_edit = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r3 = SESSION.patch(url_edit, headers={"Authorization": f"token {GITHUB_TOKEN}"}, json={"body": new_body}, timeout=20)
    r3.raise_for_status()

def fetch_results():
    r = SESSION.get(API_URL, timeout=30)
    r.raise_for_status()
    return r.json()

def rounds_to_dict(rounds):
    d = {}
    for itm in rounds or []:
        no = itm.get("RoundNo")
        d[no] = itm.get("Strokes")
    return d

def event_is_active(event):
    # Heuristik
    end_dt = iso_to_date(event.get("EndDate"))
    if not end_dt:
        return False
    start_dt = end_dt - timedelta(days=3)
    now = datetime.now(timezone.utc)
    # aktiv im Zeitfenster Do bis So
    if not (start_dt <= now <= end_dt + timedelta(hours=12)):
        return False
    # aktiv, solange Total oder ScoreToPar noch None ist oder noch nicht alle 4 Runden Zahlen tragen
    rounds = rounds_to_dict(event.get("Rounds"))
    complete = all(rounds.get(i) is not None for i in [1,2,3,4])
    finished = event.get("Total") is not None and event.get("ScoreToPar") is not None and complete
    return not finished

def build_round_update_messages(event):
    name = event.get("EventName")
    url = "https://www.europeantour.com" + event.get("EventUrl", "")
    pos = event.get("PositionDesc") or str(event.get("Position"))
    rounds = rounds_to_dict(event.get("Rounds"))
    msgs = []
    for i in [1,2,3,4]:
        strokes = rounds.get(i)
        if strokes is None:
            continue
        msgs.append(f"{name}  Runde {i}  Marcel Schneider Score {strokes}  Platz {pos}  {url}")
    return msgs

def build_final_message(event, season):
    name = event.get("EventName")
    url = "https://www.europeantour.com" + event.get("EventUrl", "")
    end_date = iso_to_date(event.get("EndDate"))
    ds = end_date.strftime("%d.%m.%Y") if end_date else ""
    pos = event.get("PositionDesc") or str(event.get("Position"))
    total = event.get("Total")
    to_par = event.get("ScoreToPar")
    pts = event.get("Points")
    earn = event.get("Earnings")
    rounds = rounds_to_dict(event.get("Rounds"))
    r1 = rounds.get(1); r2 = rounds.get(2); r3 = rounds.get(3); r4 = rounds.get(4)
    return (
        f"{name} Saison {season} abgeschlossen. "
        f"Enddatum {ds}. Platz {pos}. "
        f"R1 {r1}  R2 {r2}  R3 {r3}  R4 {r4}. "
        f"Gesamt {total}  To Par {to_par}. "
        f"Punkte {pts}  Preisgeld {earn}. "
        f"{url}"
    )

def choose_current_event(results):
    # Nimm das Event mit EndDate am nächsten in der Zukunft oder gerade vorbei
    items = sorted(results, key=lambda e: iso_to_date(e.get("EndDate")) or datetime.min.replace(tzinfo=timezone.utc))
    now = datetime.now(timezone.utc)
    # best match
    best = None
    best_delta = timedelta(days=9999)
    for e in items:
        end_dt = iso_to_date(e.get("EndDate"))
        if not end_dt:
            continue
        delta = abs(end_dt - now)
        if delta < best_delta:
            best_delta = delta
            best = e
    return best

def main():
    state = gh_get_state()
    if not state:
        state = {
            "last_full_check": "1970-01-01T00:00:00Z",
            "last_round_hash": {},
            "posted_final_for": []
        }

    data = fetch_results()
    season = data.get("Season")
    results = data.get("Results", [])
    if not results:
        print("Keine Ergebnisse gefunden.")
        return

    current = choose_current_event(results)
    active = event_is_active(current) if current else False

    now = datetime.now(timezone.utc)
    last_full_check = datetime.fromisoformat(state["last_full_check"].replace("Z", "+00:00"))

    # 4-Stunden-Takt, wenn nicht aktiv
    if not active and now - last_full_check < timedelta(hours=4):
        print("Kein aktives Turnier. 4-Stunden-Fenster noch nicht abgelaufen.")
        return

    # Round-Updates bei aktivem Event
    if active:
        eid = str(current.get("EventId"))
        rounds = rounds_to_dict(current.get("Rounds"))
        # baue Hash der bereits gemeldeten Runden
        seen = state["last_round_hash"].get(eid, {})
        to_post = []
        for i, strokes in rounds.items():
            if strokes is None:
                continue
            key = f"R{i}"
            prev = seen.get(key)
            if prev != strokes:
                to_post.append(i)
                seen[key] = strokes
        if to_post:
            # baue Nachrichten nur für geänderte Runden
            messages = build_round_update_messages(current)
            filtered = [m for i, m in zip([1,2,3,4], messages) if i in to_post]
            for text in filtered:
                send_discord(text)
            state["last_round_hash"][eid] = seen
        else:
            print("Aktiv, aber keine neuen Rundenscores.")
    else:
        print("Kein aktives Turnier. Führe groben Check aus.")

    # Abschluss-Post, falls Turnier jetzt fertig
    finished = not event_is_active(current)
    if finished and current:
        eid = str(current.get("EventId"))
        if eid not in state["posted_final_for"]:
            final_msg = build_final_message(current, season)
            send_discord(final_msg)
            state["posted_final_for"].append(eid)

    # State speichern
    state["last_full_check"] = now.isoformat().replace("+00:00", "Z")
    gh_set_state(state)

if __name__ == "__main__":
    # simple retry gegen sporadische Fehler
    tries = 3
    for i in range(tries):
        try:
            main()
            break
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))
