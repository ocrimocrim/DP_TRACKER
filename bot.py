# bot.py
import os, json, time, random, subprocess, sys, base64
from datetime import datetime, timedelta, timezone
import requests

API_URL = os.getenv("API_URL", "https://www.europeantour.com/api/v1/players/35703/results/2025/")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

# Arbeitsverzeichnis des Skripts für persistente Dateien ermitteln.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# GitHub Settings für optionalen State und Archiv über Contents API
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_REPO = os.getenv("GH_REPO")  # owner/repo
env_val = os.getenv("STATE_ISSUE_NUMBER", "").strip()
STATE_ISSUE_NUMBER = int(env_val) if env_val.isdigit() else 0

STATE_FILE = os.path.join(SCRIPT_DIR, ".state.json")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
]

session = requests.Session()
_PW_READY = False  # Playwright-Install-Flag

# ---------- Helpers: Header, Fetch ----------
def headers():
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

def fetch_json_requests(url, retries=4):
    backoff = 2
    last = None
    for _ in range(retries):
        try:
            r = session.get(url, headers=headers(), timeout=30)
            if r.status_code in (403, 429, 503):
                raise requests.HTTPError(f"status {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(backoff + random.uniform(0, 1.5))
            backoff = min(backoff * 2, 20)
    raise last

def ensure_playwright():
    """Installiert eine verfügbare Playwright-Version, ohne harten Pin."""
    global _PW_READY
    if _PW_READY:
        return
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        for pkg in ("playwright==1.55.0", "playwright==1.54.0", "playwright"):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
                break
            except Exception:
                continue
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
        from playwright.sync_api import sync_playwright  # noqa: F401
        _PW_READY = True
    except Exception as e:
        _PW_READY = False
        print(f"Playwright-Setup nicht verfügbar: {e}")

def fetch_json_playwright(url):
    ensure_playwright()
    if not _PW_READY:
        raise RuntimeError("Playwright nicht verfügbar")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=random.choice(USER_AGENTS), locale="de-DE")
        req = ctx.request
        resp = req.get(url, headers={"Referer": "https://www.europeantour.com/", "Origin": "https://www.europeantour.com"})
        if resp.status >= 400:
            browser.close()
            raise RuntimeError(f"playwright status {resp.status}")
        data = resp.json()
        browser.close()
        return data

def fetch_results():
    try:
        return fetch_json_requests(API_URL)
    except Exception:
        # nur wenn requests scheitert (z. B. 403), Playwright benutzen
        return fetch_json_playwright(API_URL)

def iso_to_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def send_discord(text):
    if not DISCORD_WEBHOOK:
        print(text)
        return
    r = session.post(DISCORD_WEBHOOK, json={"content": text}, timeout=20)
    r.raise_for_status()

def de_money(n):
    # 17133.76 -> "17.133,76"
    try:
        return f"{float(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(n)

def de_decimal(n):
    try:
        return f"{n}".replace(".", ",")
    except Exception:
        return str(n)

# ---------- Domain-Logik ----------
def rounds_maps(rounds):
    """Liefert zwei Maps: strokes[round] und par[round]."""
    strokes, pars = {}, {}
    for it in rounds or []:
        rn = it.get("RoundNo")
        strokes[rn] = it.get("Strokes")
        pars[rn] = it.get("Par")
    return strokes, pars

def event_active(e):
    # Aktiv, wenn im typischen Turnierfenster und noch nicht komplett abgeschlossen
    end_dt = iso_to_dt(e.get("EndDate"))
    if not end_dt:
        return False
    start_dt = end_dt - timedelta(days=3)
    now = datetime.now(timezone.utc)
    window = start_dt <= now <= end_dt + timedelta(hours=12)
    if not window:
        return False
    strokes, _ = rounds_maps(e.get("Rounds"))
    complete = all(strokes.get(i) is not None for i in [1, 2, 3, 4])
    finished = e.get("Total") is not None and e.get("ScoreToPar") is not None and complete
    return not finished

def choose_current(results):
    now = datetime.now(timezone.utc)
    items = sorted(results, key=lambda e: iso_to_dt(e.get("EndDate")) or datetime.min.replace(tzinfo=timezone.utc))
    best, best_delta = None, timedelta(days=9999)
    for e in items:
        dt = iso_to_dt(e.get("EndDate"))
        if not dt:
            continue
        d = abs(dt - now)
        if d < best_delta:
            best, best_delta = e, d
    return best

# ---------- Nachrichten-Formatierung ----------
def build_round_msgs(e):
    name = e.get("EventName")
    url = "https://www.europeantour.com" + e.get("EventUrl", "")
    pos = e.get("PositionDesc") or str(e.get("Position"))
    strokes, pars = rounds_maps(e.get("Rounds"))
    msgs = []
    for i in [1, 2, 3, 4]:
        s = strokes.get(i)
        p = pars.get(i)
        if s is None:
            continue
        msg = (
            f"**{name}** – Zwischenstand\n"
            f"**Runde:** {i}\n"
            f"**Spieler:** Marcel Schneider\n"
            f"**Score:** {s} (Par: {p})\n"
            f"**Platz:** {pos}\n"
            f"**Link:** {url}"
        )
        msgs.append(msg)
    return msgs

def build_final_msg(e, season):
    name = e.get("EventName")
    url = "https://www.europeantour.com" + e.get("EventUrl", "")
    end_dt = iso_to_dt(e.get("EndDate"))
    ds = end_dt.strftime("%d.%m.%Y") if end_dt else ""
    pos = e.get("PositionDesc") or str(e.get("Position"))
    strokes, pars = rounds_maps(e.get("Rounds"))
    total = e.get("Total")
    to_par = e.get("ScoreToPar")
    pts = e.get("Points")
    earn = e.get("Earnings")
    msg = (
        f"**Marcel Schneider** hat die **{name}** **Saison {season}** abgeschlossen.\n\n"
        f"**Enddatum:** {ds}\n"
        f"**Platz:** {pos}\n"
        f"**R1:** {strokes.get(1)} (Par: {pars.get(1)})\n"
        f"**R2:** {strokes.get(2)} (Par: {pars.get(2)})\n"
        f"**R3:** {strokes.get(3)} (Par: {pars.get(3)})\n"
        f"**R4:** {strokes.get(4)} (Par: {pars.get(4)})\n"
        f"**Gesamtschläge:** {total}\n"
        f"**To-Par:** {to_par}\n"
        f"**Punkte:** {de_decimal(pts)}\n"
        f"**Preisgeld:** {de_money(earn)} €\n"
        f"**Link:** {url}"
    )
    return msg

# ---------- State Verwaltung ----------
def issue_state_enabled():
    return bool(GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER > 0)

def gh_issue_get_state():
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = session.get(url, headers=h, timeout=20)
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

def gh_issue_set_state(state, title="DPWT State"):
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    get_url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = session.get(get_url, headers=h, timeout=20)
    if r.status_code == 404:
        post_url = f"https://api.github.com/repos/{GH_REPO}/issues"
        body = f"{title}\n\n<!--STATE_JSON_START-->\n{json.dumps(state, ensure_ascii=False, indent=2)}\n<!--STATE_JSON_END-->"
        r2 = session.post(post_url, headers=h, json={"title": title, "body": body}, timeout=20)
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
    r3 = session.patch(patch_url, headers=h, json={"body": new_body}, timeout=20)
    r3.raise_for_status()

def state_load():
    if issue_state_enabled():
        s = gh_issue_get_state()
        if s:
            return s
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_full_check":"1970-01-01T00:00:00Z","last_round_hash":{},"posted_final_for":[]}

def state_save(state):
    if issue_state_enabled():
        gh_issue_set_state(state)
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------- Archiv ----------
def gh_read_file(path):
    if not (GITHUB_TOKEN and GH_REPO):
        return None, None
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    r = session.get(url, headers=h, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = data["sha"]
    return content, sha

def gh_write_file(path, content, message):
    if not (GITHUB_TOKEN and GH_REPO):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"lokales Archiv geschrieben {path}")
        return
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    old_content, sha = gh_read_file(path)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    r = session.put(url, headers=h, json=payload, timeout=20)
    r.raise_for_status()

def archive_update(season, event):
    os.makedirs("archive", exist_ok=True)
    path_jsonl = f"archive/{season}.jsonl"
    path_csv = "archive/summary.csv"

    # JSONL
    existing = set()
    prev_jsonl, _ = gh_read_file(path_jsonl)
    if prev_jsonl:
        for ln in prev_jsonl.splitlines():
            if ln.strip():
                try:
                    existing.add(json.loads(ln).get("EventId"))
                except Exception:
                    pass
    if event.get("EventId") not in existing:
        lines = prev_jsonl.splitlines() if prev_jsonl else []
        lines.append(json.dumps(event, ensure_ascii=False))
        gh_write_file(path_jsonl, "\n".join(lines) + "\n", f"archive season {season} add {event.get('EventId')}")

    # CSV
    prev_csv, _ = gh_read_file(path_csv)
    header = "season,event_id,event_name,end_date,position,total,score_to_par,points,earnings,url\n"
    if not prev_csv:
        csv_data = header
    else:
        csv_data = prev_csv
    line = f"{season},{event.get('EventId')},{str(event.get('EventName')).replace(',', ' ')},{event.get('EndDate')},{event.get('PositionDesc')},{event.get('Total')},{event.get('ScoreToPar')},{event.get('Points')},{event.get('Earnings')},https://www.europeantour.com{event.get('EventUrl','')}\n"
    if not prev_csv or line not in prev_csv:
        gh_write_file(path_csv, csv_data + line, f"archive summary add {event.get('EventId')}")

# ---------- Einmaliger Durchlauf (gibt True zurück, wenn weiterhin aktiv) ----------
def run_once_and_post():
    state = state_load()
    data = fetch_results()
    season = data.get("Season")
    results = data.get("Results", [])
    if not results:
        print("keine Ergebnisse")
        return False  # kein aktives Event

    current = choose_current(results)
    active = event_active(current) if current else False

    now = datetime.now(timezone.utc)
    last_full = datetime.fromisoformat(state["last_full_check"].replace("Z","+00:00"))

    # außerhalb aktiver Turniere: nur alle 4h wirklich arbeiten
    if not active and now - last_full < timedelta(hours=4):
        print("inaktiv: 4h-Fenster noch offen")
        state["last_full_check"] = now.isoformat().replace("+00:00","Z")
        state_save(state)
        return False

    # aktive Runden posten (nur Änderungen)
    if active and current:
        eid = str(current.get("EventId"))
        seen = state["last_round_hash"].get(eid, {})
        strokes, _pars = rounds_maps(current.get("Rounds"))
        to_post_idx = []
        for i in [1,2,3,4]:
            s = strokes.get(i)
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
            print("aktiv: keine neuen Rundenscores")

    # Abschluss melden + archivieren
    if current and not event_active(current):
        eid = str(current.get("EventId"))
        if eid not in state["posted_final_for"]:
            send_discord(build_final_msg(current, season))
            archive_update(season, current)
            state["posted_final_for"].append(eid)

    state["last_full_check"] = now.isoformat().replace("+00:00","Z")
    state_save(state)
    return active

# ---------- Hauptprogramm: 4h-Run, bei aktivem Event 30-Min-Watch im selben Job ----------
if __name__ == "__main__":
    # Erster Lauf (mit Retries)
    active = False
    for i in range(3):
        try:
            active = run_once_and_post()
            break
        except Exception as e:
            print(f"retry {i+1} wegen {e}")
            time.sleep(2 + i*3)

    # Wenn aktiv, dann im selben Job alle 30 Minuten weiterprüfen (max. 72h)
    if active:
        print("Aktives Turnier erkannt → 30-Minuten-Watch gestartet.")
        end_watch = time.time() + 72 * 3600  # maximal 72 Stunden beobachten
        while time.time() < end_watch:
            time.sleep(30 * 60)  # 30 Minuten warten
            try:
                still_active = run_once_and_post()
                if not still_active:
                    print("Turnier nicht mehr aktiv → Watch beendet.")
                    break
            except Exception as e:
                print(f"Watch-Fehler: {e} (weiter)")
                continue
