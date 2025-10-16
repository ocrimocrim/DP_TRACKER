# ms_leaderboard_bot.py
# DP World Tour – Marcel Schneider: "Playing this week" + Live-Leaderboard
# - 1x täglich: Spieler-Seite scannen -> nächstes/aktuelles Turnier + 2-Tage-Reminder
# - während Event: alle 30 Min Leaderboard prüfen:
#     * Zwischenpost: sobald Marcel eine Runde fertig hat
#     * Round-Final: sobald ALLE die Runde fertig haben
# - State wird in derselben Issue wie beim ersten Bot abgelegt (eigene Keys)
#
# ENV:
#   DISCORD_WEBHOOK_LIVE   -> zweiter Discord Webhook (für diesen Bot)
#   GITHUB_TOKEN, GH_REPO, STATE_ISSUE_NUMBER -> wie beim ersten Bot
#   RELAY_BASE (optional)  -> z.B. https://dein-worker.workers.dev  (der Worker nimmt ?url=...)
#
#   PLAYER_ID=35703 (default)
#   PLAYER_PAGE=https://www.europeantour.com/players/marcel-schneider-35703/?tour=dpworld-tour
#   RESULTS_URL=https://www.europeantour.com/api/v1/players/35703/results/2025/

import os, re, json, time, random, base64, sys, subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests

PLAYER_ID = int(os.getenv("PLAYER_ID", "35703"))
PLAYER_PAGE = os.getenv(
    "PLAYER_PAGE",
    "https://www.europeantour.com/players/marcel-schneider-35703/?tour=dpworld-tour"
)
RESULTS_URL = os.getenv(
    "RESULTS_URL",
    "https://www.europeantour.com/api/v1/players/35703/results/2025/"
)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_LIVE")  # <- eigener Webhook

# GitHub State (wir verwenden die gleiche Issue wie der erste Bot, aber eigene Keys)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_REPO = os.getenv("GH_REPO")
env_val = os.getenv("STATE_ISSUE_NUMBER", "").strip()
STATE_ISSUE_NUMBER = int(env_val) if env_val.isdigit() else 0

RELAY_BASE = os.getenv("RELAY_BASE", "").rstrip("/")

# ---- HTTP Basics -------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
]

session = requests.Session()
session.timeout = 30

def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": "https://www.europeantour.com/",
        "Origin": "https://www.europeantour.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }

def _relay(url: str) -> str:
    if RELAY_BASE:
        return f"{RELAY_BASE}?url={quote_plus(url)}"
    return url

def get_text(url: str) -> str:
    last = None
    for _ in range(4):
        try:
            r = session.get(_relay(url), headers=_headers(), timeout=30)
            if r.status_code in (403, 429, 503):
                raise requests.HTTPError(f"http {r.status_code}")
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.5 + random.random())
    raise last

def get_json(url: str):
    last = None
    for _ in range(4):
        try:
            r = session.get(_relay(url), headers=_headers(), timeout=30)
            if r.status_code in (403, 429, 503):
                raise requests.HTTPError(f"http {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 + random.random())
    raise last

def ensure_bs4():
    try:
        import bs4  # noqa
        return
    except Exception:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "beautifulsoup4==4.12.3"])

def ensure_playwright():
    try:
        import playwright  # noqa
        return
    except Exception:
        # fallback nur wenn wirklich nötig
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright==1.55.0"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])

def fetch_html(url: str) -> str:
    try:
        return get_text(url)
    except Exception:
        # sehr selten nötig
        ensure_playwright()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=random.choice(USER_AGENTS), locale="de-DE")
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=45000)
            html = page.content()
            browser.close()
        return html

# ---- Helpers ----------------------------------------------------------------

def iso_dt(s):
    if not s: return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def eur_date_to_iso(dmy: str):
    # "15/10/2025" -> date
    try:
        return datetime.strptime(dmy.strip(), "%d/%m/%Y").date()
    except Exception:
        return None

def send_discord(msg: str):
    if not DISCORD_WEBHOOK:
        print("[DRY] ", msg)
        return
    r = session.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=20)
    r.raise_for_status()

# ---- State in Issue ----------------------------------------------------------

def issue_state_enabled():
    return bool(GITHUB_TOKEN and GH_REPO and STATE_ISSUE_NUMBER > 0)

def gh_issue_get_state():
    if not issue_state_enabled():
        return {}
    h = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r = session.get(url, headers=h, timeout=20)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    body = r.json().get("body") or ""
    a, b = "<!--STATE_JSON_START-->", "<!--STATE_JSON_END-->"
    if a in body and b in body:
        blob = body.split(a)[1].split(b)[0].strip()
        try:
            return json.loads(blob)
        except Exception:
            return {}
    return {}

def gh_issue_set_state(state, title="DPWT State"):
    if not issue_state_enabled():
        return
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
    a, b = "<!--STATE_JSON_START-->", "<!--STATE_JSON_END-->"
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    if a in body_old and b in body_old:
        new_body = body_old.split(a)[0] + a + "\n" + payload + "\n" + b + body_old.split(b)[1]
    else:
        new_body = f"{body_old}\n\n{a}\n{payload}\n{b}"
    patch_url = f"https://api.github.com/repos/{GH_REPO}/issues/{STATE_ISSUE_NUMBER}"
    r3 = session.patch(patch_url, headers=h, json={"body": new_body}, timeout=20)
    r3.raise_for_status()

def state_load():
    st = gh_issue_get_state() if issue_state_enabled() else {}
    st.setdefault("leaderboard_bot", {
        "pre_alert_event_id": None,
        "active_event_url": None,
        "active_event_id": None,
        "round_done": {},        # {event_id: {round_no: True}}
        "round_final": {},       # {event_id: {round_no: True}}
        "round_cum_to_par": {},  # {event_id: {round_no: value}}
    })
    return st

def state_save(st):
    gh_issue_set_state(st)

# ---- Parsing "Playing this week" --------------------------------------------

def parse_playing_this_week(html: str):
    """
    Liefert (start_date: date, event_name, event_url_abs) oder None.
    Robust: sucht Anker '/dpworld-tour/.../' in Nähe von 'Playing this week'.
    """
    ensure_bs4()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Heuristik: Section mit Titel "Playing this week"
    header = None
    for h in soup.find_all(["h2","h3","div","span"]):
        if h.get_text(strip=True).lower() == "playing this week":
            header = h
            break
    root = header.parent if header else soup

    # Finde ersten Link auf ein DP-World-Tour Event
    a = None
    for link in root.find_all("a", href=True):
        if link["href"].startswith("/dpworld-tour/") and link["href"].count("/") >= 3:
            a = link
            break
    if not a:
        return None

    # Datum steht in Nachbarzellen mit class 'table__cell-inner' – nimm die erste dd/mm/yyyy
    date_text = None
    cells = a.find_parent()
    if cells:
        txt = cells.get_text(" ", strip=True)
        m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", txt)
        if m:
            date_text = m.group(1)

    start_date = eur_date_to_iso(date_text) if date_text else None
    event_url_abs = "https://www.europeantour.com" + a["href"]
    event_name = a.get_text(strip=True)
    return start_date, event_name, event_url_abs

# ---- EventId via Results-API mappen -----------------------------------------

def map_event_id_from_results(event_url_path: str):
    """
    Nimmt '/dpworld-tour/xxx-2025/' und sucht in der Results-API nach EventId.
    """
    data = get_json(RESULTS_URL)
    for e in data.get("Results", []):
        if e.get("EventUrl") == event_url_path:
            return e.get("EventId"), data.get("Season")
    return None, data.get("Season")

# ---- Leaderboard JSON -------------------------------------------------------

def fetch_leaderboard(event_id: int):
    url = f"https://www.europeantour.com/api/sportdata/Leaderboard/Strokeplay/{event_id}/type/load"
    return get_json(url)

def find_player(players, player_id):
    for p in players or []:
        if int(p.get("PlayerId", 0)) == player_id:
            return p
    return None

def round_strokes_map(p):
    d = {}
    for r in (p.get("Rounds") or []):
        d[int(r.get("RoundNo"))] = r.get("Strokes")
    return d

def everyone_finished_round(players, rnd: int) -> bool:
    for p in players or []:
        m = round_strokes_map(p)
        if m.get(rnd) is None:
            return False
    return True

# ---- Messaging --------------------------------------------------------------

def fmt_eur(num):
    # Tausenderpunkte / Komma – hier nur für Scores nicht nötig, behalten für evtl. Erweiterung
    try:
        s = f"{num:,.2f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(num)

def post_pre_alert(event_name, start_date, event_url):
    ds = start_date.strftime("%d.%m.%Y") if start_date else "bald"
    msg = (
        f"**{event_name}** startet in 2 Tagen.\n"
        f"Startdatum: {ds}\n"
        f"Leaderboard: {event_url}leaderboard"
    )
    send_discord(msg)

def post_round_done(event_name, event_url, rnd, strokes, delta_rnd, cum_to_par, pos):
    delta_txt = f"{delta_rnd:+d}" if isinstance(delta_rnd, int) else str(delta_rnd)
    cum_txt = f"{cum_to_par:+d}" if isinstance(cum_to_par, int) else str(cum_to_par)
    msg = (
        f"**{event_name}** – **Runde {rnd}** beendet (Marcel):\n"
        f"Strokes R{rnd}: **{strokes}** (ΔRunde: {delta_txt})\n"
        f"Kumuliert To-Par: **{cum_txt}**\n"
        f"Tagesplatzierung: **{pos}**\n"
        f"Link: {event_url}leaderboard"
    )
    send_discord(msg)

def post_round_final(event_name, event_url, rnd, pos, cum_to_par):
    cum_txt = f"{cum_to_par:+d}" if isinstance(cum_to_par, int) else str(cum_to_par)
    msg = (
        f"**{event_name}** – **Runde {rnd}** komplett (alle Spieler durch):\n"
        f"Marcel nach R{rnd}: Platz **{pos}**, kumuliert **{cum_txt}**\n"
        f"Link: {event_url}leaderboard"
    )
    send_discord(msg)

# ---- Orchestrierung ---------------------------------------------------------

def main():
    st = state_load()
    L = st["leaderboard_bot"]

    # 1) Spieler-Seite scannen (für Reminder + Event-URL)
    html = fetch_html(PLAYER_PAGE)
    got = parse_playing_this_week(html)
    start_date, event_name, event_url = (None, None, None)
    if got:
        start_date, event_name, event_url = got
    else:
        print("Playing-this-week nicht gefunden – fahre fort (ggf. nur Live-Teil).")

    # 2) 2-Tage-Reminder
    if start_date and event_name and event_url:
        if (start_date - datetime.utcnow().date()).days == 2:
            # EventId für deduplizieren bestimmen
            path = event_url.replace("https://www.europeantour.com", "")
            eid, _season = map_event_id_from_results(path)
            key = str(eid) if eid else event_url
            if L.get("pre_alert_event_id") != key:
                post_pre_alert(event_name, start_date, event_url)
                L["pre_alert_event_id"] = key

        # Merke aktive Event-URL (für Live)
        L["active_event_url"] = event_url

    # 3) Live-Leaderboard nur, wenn Event-Fenster ungefähr passt (EndDate-3d..EndDate+12h)
    #    Wir holen EventId aus der Results-API (zuverlässig).
    active_url = L.get("active_event_url")
    if not active_url:
        state_save(st)
        print("Kein aktives Event bekannt – Ende.")
        return

    event_path = active_url.replace("https://www.europeantour.com", "")
    event_id, season = map_event_id_from_results(event_path)
    if not event_id:
        print("EventId nicht via Results-API gefunden – Ende (noch nicht im Katalog?).")
        state_save(st)
        return

    L["active_event_id"] = event_id

    # Rough activity check über EndDate-3 Tage
    res_all = get_json(RESULTS_URL)
    end_dt = None
    for e in res_all.get("Results", []):
        if e.get("EventId") == event_id:
            end_dt = iso_dt(e.get("EndDate"))
            break
    now = datetime.now(timezone.utc)
    active_window = False
    if end_dt:
        active_window = (end_dt - timedelta(days=3) <= now <= end_dt + timedelta(hours=12))

    if not active_window:
        state_save(st)
        print("Event nicht im aktiven Fenster – Ende.")
        return

    # 4) Leaderboard ziehen
    lb = fetch_leaderboard(event_id)
    players = lb.get("Players") or []
    me = find_player(players, PLAYER_ID)
    if not me:
        print("Marcel im Leaderboard nicht gefunden – Ende.")
        state_save(st)
        return

    # Position / kumulierter To-Par
    pos = me.get("PositionDesc") or str(me.get("Position"))
    cum_to_par = me.get("ScoreToPar")
    rmap = round_strokes_map(me)

    # State-Strukturen
    L["round_done"].setdefault(str(event_id), {})
    L["round_final"].setdefault(str(event_id), {})
    L["round_cum_to_par"].setdefault(str(event_id), {})

    # Welche Runde ist "neu fertig"?
    for rnd in (1,2,3,4):
        strokes = rmap.get(rnd)
        if strokes is None:
            continue
        if not L["round_done"][str(event_id)].get(str(rnd)):
            # ΔRunde berechnen: cum_to_par(Rn) - cum_to_par(Rn-1)
            # Wir kennen kumulierten Wert JETZT; den vorherigen aus State ziehen
            prev_cum = L["round_cum_to_par"][str(event_id)].get(str(rnd-1))
            delta_rnd = cum_to_par - prev_cum if (prev_cum is not None and cum_to_par is not None) else None
            post_round_done(event_name, active_url, rnd, strokes, delta_rnd, cum_to_par, pos)
            L["round_done"][str(event_id)][str(rnd)] = True
            L["round_cum_to_par"][str(event_id)][str(rnd)] = cum_to_par

    # Round-Final (alle fertig)?
    for rnd in (1,2,3,4):
        if not L["round_done"][str(event_id)].get(str(rnd)):
            # Marcel hat die Runde noch nicht fertig -> Round-Final für Rn sowieso nicht
            continue
        if L["round_final"][str(event_id)].get(str(rnd)):
            continue
        if everyone_finished_round(players, rnd):
            post_round_final(event_name, active_url, rnd, pos, cum_to_par)
            L["round_final"][str(event_id)][str(rnd)] = True

    state_save(st)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FEHLER:", e)
        # kein Re-Raise, damit der Workflow nicht rot wird, wenn mal ein 403/Timeout kommt
