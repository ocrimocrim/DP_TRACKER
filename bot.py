# bot.py
import os, json, time, base64, hashlib, random
from datetime import datetime, timedelta, timezone
import requests

API_URL = os.getenv("API_URL", "https://www.europeantour.com/api/v1/players/35703/results/2025/")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_REPO = os.getenv("GH_REPO")            # owner/repo
STATE_ISSUE_NUMBER = int(os.getenv("STATE_ISSUE_NUMBER", "0"))
CF_WORKER_URL = os.getenv("CF_WORKER_URL")  # optional fetch relay, siehe worker unten

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
]

SESSION = requests.Session()

def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": "https://www.europeantour.com/",
        "Origin": "https://www.europeantour.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }

def fetch_json_with_workarounds(url, tries=5):
    backoff = 2
    last_exc = None
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=30, headers=_headers())
            # gelegentliche 5xx oder 403 abfangen
            if r.status_code == 403 and CF_WORKER_URL:
                # Cloudflare Worker Relay
                relay = CF_WORKER_URL.rstrip("/") + "/fetch?url=" + requests.utils.quote(url, safe="")
                rr = SESSION.get(relay, timeout=30, headers=_headers())
                rr.raise_for_status()
                return rr.json()
            if r.status_code in (403, 429, 503):
                raise requests.HTTPError(f"upstream {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            time.sleep(backoff + random.uniform(0, 1.5))
            backoff = min(backoff * 2, 20)
    raise last_exc

def iso_to_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def send_discord(text):
    if not DISCORD_WEBHOOK:
        print(text)
        return
    r = SESSION.post(DISCORD_WEBHOOK, json={"content": text}, timeout=20)
    r.raise_for_status()

def gh_get_issue_state():
    if not (GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER):
        return {}
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = SESSION.get(url, headers=h, timeout=20)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    body = r.json().get("body") or ""
    a = "<!--STATE_JSON_START-->"
    b = "<!--STATE_JSON_END-->"
    if a in body and b in body:
        blob = body.split(a)[1].split(b)[0].strip()
        try:
            return json.loads(blob)
        except Exception:
            return {}
    return {}

def gh_set_issue_state(state, title="DPWT State"):
    if not (GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER):
        return
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    get_url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = SESSION.get(get_url, headers=h, timeout=20)
    if r.status_code == 404:
        post_url = f"https://api.github.com/repos/{GH_REPO}/issues"
        body = f"{title}\n\n<!--STATE_JSON_START-->\n{json.dumps(state, ensure_ascii=False, indent=2)}\n<!--STATE_JSON_END-->"
        r2 = SESSION.post(post_url, headers=h, json={"title": title, "body": body}, timeout=20)
        r2.raise_for_status()
        return
    r.raise_for_status()
    issue = r.json()
    body_old = issue.get("body") or ""
    a = "<!--STATE_JSON_START-->"
    b = "<!--STATE_JSON_END-->"
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    if a in body_old and b in body_old:
        new_body = body_old.split(a)[0] + a + "\n" + payload + "\n" + b + body_old.split(b)[1]
    else:
        new_body = f"{body_old}\n\n{a}\n{payload}\n{b}"
    patch_url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r3 = SESSION.patch(patch_url, headers=h, json={"body": new_body}, timeout=20)
    r3.raise_for_status()

def gh_read_file(path):
    if not (GITHUB_TOKEN and GH_REPO):
        return None, None
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    r = SESSION.get(url, headers=h, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = data["sha"]
    return content, sha

def gh_write_file(path, content, message):
    if not (GITHUB_TOKEN and GH_REPO):
        return
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    old_content, sha = gh_read_file(path)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": os.getenv("GITHUB_REF_NAME", None) or None,
    }
    if sha:
        payload["sha"] = sha
    r = SESSION.put(url, headers=h, json=payload, timeout=20)
    r.raise_for_status()

def rounds_map(rounds):
    d = {}
    for it in rounds or []:
        d[it.get("RoundNo")] = it.get("Strokes")
    return d

def event_active(e):
    end_dt = iso_to_dt(e.get("EndDate"))
    if not end_dt:
        return False
    start_dt = end_dt - timedelta(days=3)
    now = datetime.now(timezone.utc)
    in_window = start_dt <= now <= end_dt + timedelta(hours=12)
    if not in_window:
        return False
    r = rounds_map(e.get("Rounds"))
    complete = all(r.get(i) is not None for i in [1,2,3,4])
    finished = e.get("Total") is not None and e.get("ScoreToPar") is not None and complete
    return not finished

def choose_current(results):
    now = datetime.now(timezone.utc)
    items = sorted(results, key=lambda e: iso_to_dt(e.get("EndDate")) or datetime.min.replace(tzinfo=timezone.utc))
    best, best_delta = None, timedelta(days=9999)
    for e in items:
        end_dt = iso_to_dt(e.get("EndDate"))
        if not end_dt:
            continue
        delta = abs(end_dt - now)
        if delta < best_delta:
            best, best_delta = e, delta
    return best

def build_round_msgs(e):
    name = e.get("EventName")
    url = "https://www.europeantour.com" + e.get("EventUrl", "")
    pos = e.get("PositionDesc") or str(e.get("Position"))
    r = rounds_map(e.get("Rounds"))
    out = []
    for i in [1,2,3,4]:
        if r.get(i) is not None:
            out.append(f"{name}  Runde {i}  Marcel Schneider Score {r[i]}  Platz {pos}  {url}")
    return out

def build_final_msg(e, season):
    name = e.get("EventName")
    url = "https://www.europeantour.com" + e.get("EventUrl", "")
    end_dt = iso_to_dt(e.get("EndDate"))
    ds = end_dt.strftime("%d.%m.%Y") if end_dt else ""
    pos = e.get("PositionDesc") or str(e.get("Position"))
    r = rounds_map(e.get("Rounds"))
    total = e.get("Total"); to_par = e.get("ScoreToPar")
    pts = e.get("Points"); earn = e.get("Earnings")
    return (
        f"{name} Saison {season} abgeschlossen. "
        f"Enddatum {ds}. Platz {pos}. "
        f"R1 {r.get(1)}  R2 {r.get(2)}  R3 {r.get(3)}  R4 {r.get(4)}. "
        f"Gesamt {total}  To Par {to_par}. "
        f"Punkte {pts}  Preisgeld {earn}. "
        f"https://www.europeantour.com{e.get('EventUrl','')}"
    )

def archive_append(season, event):
    # JSONL zeilenweise
    os.makedirs("archive", exist_ok=True)
    path_jsonl = f"archive/{season}.jsonl"
    path_csv = f"archive/summary.csv"
    # lokaler Merge, danach per API zurückschreiben
    # JSONL
    old_jsonl, _ = gh_read_file(path_jsonl)
    lines = []
    if old_jsonl:
        lines = [ln for ln in old_jsonl.splitlines() if ln.strip()]
    # doppelte vermeiden über EventId Hash
    key = str(event.get("EventId"))
    present = any((json.loads(ln).get("EventId") == event.get("EventId")) for ln in lines) if lines else False
    if not present:
        lines.append(json.dumps(event, ensure_ascii=False))
        gh_write_file(path_jsonl, "\n".join(lines) + "\n", f"archive add season {season} event {key}")

    # CSV Zusammenfassung
    csv_old, _ = gh_read_file(path_csv)
    header = "season,event_id,event_name,end_date,position,total,score_to_par,points,earnings,url\n"
    if not csv_old:
        csv = header
    else:
        csv = csv_old
    stamp = f"{season},{event.get('EventId')},{event.get('EventName').replace(',', ' ')},{event.get('EndDate')},{event.get('PositionDesc')},{event.get('Total')},{event.get('ScoreToPar')},{event.get('Points')},{event.get('Earnings')},https://www.europeantour.com{event.get('EventUrl','')}\n"
    if not csv_old or stamp not in csv_old:
        gh_write_file(path_csv, csv + stamp, f"archive summary add {event.get('EventId')}")

def main():
    # State laden
    state = gh_get_issue_state() or {"last_full_check":"1970-01-01T00:00:00Z","last_round_hash":{},"posted_final_for":[]}
    data = fetch_json_with_workarounds(API_URL)
    season = data.get("Season")
    results = data.get("Results", [])
    if not results:
        print("keine Ergebnisse")
        return

    current = choose_current(results)
    active = event_active(current) if current else False

    now = datetime.now(timezone.utc)
    last_full = datetime.fromisoformat(state["last_full_check"].replace("Z","+00:00"))

    if not active and now - last_full < timedelta(hours=4):
        print("inaktiv Zeitfenster 4h noch offen")
        return

    # aktive Runden posten bei Änderungen
    if active and current:
        eid = str(current.get("EventId"))
        seen = state["last_round_hash"].get(eid, {})
        r = rounds_map(current.get("Rounds"))
        to_post_idx = []
        for i in [1,2,3,4]:
            s = r.get(i)
            if s is None:
                continue
            key = f"R{i}"
            if seen.get(key) != s:
                seen[key] = s
                to_post_idx.append(i)
        if to_post_idx:
            msgs = build_round_msgs(current)
            for i, msg in zip([1,2,3,4], msgs):
                if i in to_post_idx:
                    send_discord(msg)
            state["last_round_hash"][eid] = seen
        else:
            print("aktiv keine neuen Rundenscores")

    # Abschluss melden und archivieren
    if current and not event_active(current):
        eid = str(current.get("EventId"))
        if eid not in state["posted_final_for"]:
            send_discord(build_final_msg(current, season))
            # Archiv ergänzen
            archive_append(season, current)
            state["posted_final_for"].append(eid)

    state["last_full_check"] = now.isoformat().replace("+00:00","Z")
    gh_set_issue_state(state)

if __name__ == "__main__":
    for i in range(3):
        try:
            main()
            break
        except Exception as e:
            print(f"retry {i+1} wegen {e}")
            time.sleep(2 + i*3)
