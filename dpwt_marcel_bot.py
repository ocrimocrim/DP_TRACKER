#!/usr/bin/env python3
import os, re, json, logging, pathlib, datetime as dt, html
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin
import requests

# ------------------------------------------
# Konstanten
# ------------------------------------------
PLAYER_ID = 35703  # Marcel Schneider
BASE = "https://www.europeantour.com"
JINA = "https://r.jina.ai/http://"
STATE_DIR = pathlib.Path(".state")
STATE_DIR.mkdir(parents=True, exist_ok=True)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_LIVE", "").strip()
DEBUG = os.environ.get("DEBUG", "0") == "1"
TZ = dt.timezone(dt.timedelta(hours=2))  # grob Europe/Berlin

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "dpwt-marcel-bot/1.4 (+github-actions)",
    "Accept": "text/html,application/json"
})

# ------------------------------------------
# HTTP
# ------------------------------------------
def _get(url: str, as_json=False, allow_jina=False) -> Any:
    try_urls = [url]
    if allow_jina:
        if url.startswith("https://"):
            try_urls.insert(0, JINA + url[len("https://"):])
        elif url.startswith("http://"):
            try_urls.insert(0, JINA + url[len("http://"):])
        else:
            try_urls.insert(0, JINA + url)
    last_err = None
    for u in try_urls:
        logging.debug(f"GET {u}")
        try:
            r = SESSION.get(u, timeout=25)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"fetch failed for {url} because {last_err}")

# ------------------------------------------
# Schritt 1 und 2
# Playing this week auf der Profilseite finden
# ------------------------------------------
def find_playing_this_week_url() -> Optional[str]:
    profile = f"{BASE}/players/marcel-schneider-{PLAYER_ID}/?tour=dpworld-tour"
    html_text = _get(profile, allow_jina=True)
    block = re.search(r"Playing this week(.+?)</section", html_text, re.I | re.S)
    hay = block.group(1) if block else html_text
    m = re.search(r'href="(/dpworld-tour/[^"/]+-20\d{2}/?)"', hay, re.I)
    if not m:
        # Fallback innerhalb der gleichen Seite, falls DOM etwas anders ist
        m = re.search(r'(/dpworld-tour/[^"/]+-20\d{2}/?)', hay, re.I)
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    url = BASE + slug
    logging.info(f"Playing this week Slug gefunden {slug}")
    return url

# ------------------------------------------
# Schritt 3 und 4
# Leaderboard-Seite zusammensetzen und EventId ermitteln
# ------------------------------------------
EVENT_ID_PATTERNS = [
    r'"EventId"\s*:\s*(\d+)',
    r'"eventId"\s*:\s*(\d+)',
    r'/api/sportdata/Leaderboard/Strokeplay/(\d+)/type/load',
    r'Leaderboard/Strokeplay/(\d+)/'
]

def _search_event_id_from_html(html_text: str) -> Optional[int]:
    # Direkte Patterns
    for pat in EVENT_ID_PATTERNS:
        m = re.search(pat, html_text, re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    # JSON-Blöcke aus <script> herauslösen
    for m in re.finditer(r'<script[^>]*>\s*({.*?})\s*</script>', html_text, re.S | re.I):
        block = m.group(1)
        if "EventId" in block or "eventId" in block:
            try:
                j = json.loads(block)
            except Exception:
                cleaned = re.sub(r'(?://.*?$)|/\*.*?\*/', '', block, flags=re.M | re.S)
                try:
                    j = json.loads(cleaned)
                except Exception:
                    continue
            def walk(x):
                if isinstance(x, dict):
                    for k, v in x.items():
                        if k in ("EventId", "eventId") and isinstance(v, int):
                            return v
                        got = walk(v)
                        if got:
                            return got
                elif isinstance(x, list):
                    for v in x:
                        got = walk(v)
                        if got:
                            return got
                return None
            got = walk(j)
            if got:
                return int(got)
    return None

def extract_event_id(event_page_url: str) -> Optional[int]:
    # Eventseite prüfen
    html1 = _get(event_page_url, allow_jina=True)
    found = _search_event_id_from_html(html1)
    if found:
        return found
    # Leaderboard-Seite mit round gleich vier prüfen
    lb_url = urljoin(event_page_url.rstrip("/") + "/", "leaderboard?round=4")
    html2 = _get(lb_url, allow_jina=True)
    found = _search_event_id_from_html(html2)
    if found:
        return found
    logging.info("EventId wurde nicht gefunden")
    return None

# ------------------------------------------
# Schritt 5
# Sportdata APIs
# ------------------------------------------
def fetch_leaderboard(event_id: int) -> Dict[str, Any]:
    url = f"{BASE}/api/sportdata/Leaderboard/Strokeplay/{event_id}/type/load"
    return _get(url, as_json=True)

def try_fetch_scorecard(event_id: int, pid: int) -> Optional[Dict[str, Any]]:
    candidates = [
        f"{BASE}/api/sportdata/Scorecard/Strokeplay/{event_id}/Player/{pid}",
        f"{BASE}/api/sportdata/Leaderboard/Strokeplay/{event_id}/Player/{pid}",
        f"{BASE}/api/sportdata/Scorecards/Strokeplay/{event_id}?playerId={pid}",
    ]
    for url in candidates:
        try:
            sc = _get(url, as_json=True)
            if isinstance(sc, dict) and sc:
                return sc
        except Exception as e:
            logging.debug(f"scorecard miss {url} because {e}")
    return None

# ------------------------------------------
# Utility
# ------------------------------------------
def find_player_row(players: List[Dict[str, Any]], pid: int) -> Optional[Dict[str, Any]]:
    for p in players:
        if p.get("PlayerId") == pid:
            return p
    return None

def round_completed_for(player: Dict[str, Any], rno: int) -> Optional[int]:
    rounds = player.get("Rounds", []) or []
    for r in rounds:
        if r.get("RoundNo") == rno and r.get("Strokes") is not None:
            return r.get("Strokes")
    return None

def all_players_finished_round(players: List[Dict[str, Any]], rno: int) -> bool:
    for p in players:
        has = False
        for r in p.get("Rounds", []) or []:
            if r.get("RoundNo") == rno and r.get("Strokes") is not None:
                has = True
                break
        if not has:
            return False
    return True

def build_par_and_strokes_text(scorecard: Optional[Dict[str, Any]], rno: int) -> List[str]:
    lines = []
    if not scorecard:
        lines.append("Scorecard nicht verfügbar. Ich liefere Runden Gesamtwert.")
        return lines
    rkey = str(rno)
    holes = scorecard.get("Holes") or scorecard.get("holes") or []
    per_round = scorecard.get("Rounds") or scorecard.get("rounds") or {}
    data = per_round.get(rkey) if isinstance(per_round, dict) else None
    if data and isinstance(data, dict):
        pars = data.get("Pars") or data.get("pars")
        strokes = data.get("StrokesPerHole") or data.get("strokes")
        if isinstance(pars, list) and isinstance(strokes, list) and len(pars) == len(strokes):
            lines.append("Par pro Loch")
            lines.append(" ".join(str(x) for x in pars))
            lines.append("Schläge pro Loch")
            lines.append(" ".join(str(x) for x in strokes))
            return lines
    if holes and isinstance(holes, list) and isinstance(holes[0], dict):
        pars = []
        strokes = []
        for h in holes:
            if h.get("RoundNo") == rno:
                pars.append(h.get("Par"))
                strokes.append(h.get("Strokes"))
        if pars and strokes and len(pars) == len(strokes):
            lines.append("Par pro Loch")
            lines.append(" ".join(str(x) for x in pars))
            lines.append("Schläge pro Loch")
            lines.append(" ".join(str(x) for x in strokes))
            return lines
    lines.append("Scorecard strukturiert, aber Feldnamen unbekannt. Debug aktivieren.")
    return lines

def fmt_discord_block(title: str, lines: List[str]) -> str:
    body = "\n".join(lines)
    return f"**{title}**\n{body}"

def post_discord(content: str):
    if not DISCORD_WEBHOOK:
        logging.warning("DISCORD_WEBHOOK_LIVE fehlt. Ausgabe nur im Log.")
        logging.info(content)
        return
    try:
        r = SESSION.post(DISCORD_WEBHOOK, json={"content": content}, timeout=20)
        if r.status_code >= 300:
            logging.error(f"Discord Webhook Fehler {r.status_code} {r.text[:200]}")
    except Exception as e:
        logging.error(f"Discord Webhook Exception {e}")

def state_path(event_id: int) -> pathlib.Path:
    return STATE_DIR / f"{event_id}_state.json"

def load_state(event_id: int) -> Dict[str, Any]:
    p = state_path(event_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"posted_rounds": [], "posted_all_finished": False}

def save_state(event_id: int, data: Dict[str, Any]):
    p = state_path(event_id)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ------------------------------------------
# Hauptlogik
# ------------------------------------------
def main():
    # Schritt 1 und 2
    event_url = find_playing_this_week_url()
    if not event_url:
        logging.info("Kein Turnier unter Playing this week gefunden. Abbruch.")
        return

    # Schritt 3 und 4
    leaderboard_page = urljoin(event_url.rstrip('/') + '/', "leaderboard?round=4")
    logging.info(f"Leaderboard Seite {leaderboard_page}")

    # Schritt 4 EventId ermitteln
    event_id = extract_event_id(event_url)
    if not event_id:
        logging.info("EventId wurde nicht gefunden. Abbruch.")
        return
    logging.info(f"EventId {event_id}")

    # Schritt 5 Daten ziehen
    lb = fetch_leaderboard(event_id)
    players = lb.get("Players") or []
    me = find_player_row(players, PLAYER_ID)
    if not me:
        logging.info("Marcel Schneider ist nicht im Leaderboard vorhanden.")
        return

    state = load_state(event_id)
    did_post = False

    for rno in [1, 2, 3, 4]:
        if rno in state["posted_rounds"]:
            continue
        strokes = round_completed_for(me, rno)
        if strokes is None:
            continue
        pos_desc = me.get("PositionDesc")
        score_to_par = me.get("ScoreToPar")
        round_lines = [
            "Turnier",
            f"{event_url}",
            f"Runde {rno}",
            f"Schläge gesamt {strokes}",
            f"Aktueller Rang {pos_desc}",
            f"Score gesamt gegen Par {score_to_par}",
            "Leaderboard Link",
            f"{leaderboard_page}"
        ]
        scorecard = try_fetch_scorecard(event_id, PLAYER_ID)
        round_lines.extend(build_par_and_strokes_text(scorecard, rno))
        post_discord(fmt_discord_block("Marcel Schneider Update", round_lines))
        state["posted_rounds"].append(rno)
        did_post = True

    if not state.get("posted_all_finished"):
        for rno in [1, 2, 3, 4]:
            if all_players_finished_round(players, rno):
                pos_desc = me.get("PositionDesc")
                lines = [
                    f"Alle Spieler haben Runde {rno} abgeschlossen",
                    "Tagesplatzierung von Marcel Schneider",
                    f"{pos_desc}",
                    "Leaderboard",
                    f"{leaderboard_page}"
                ]
                post_discord(fmt_discord_block("Tagesabschluss", lines))
                state["posted_all_finished"] = True
                did_post = True
                break

    if did_post:
        save_state(event_id, state)
    else:
        logging.info("Kein neues Ereignis. Keine Discord Nachricht gesendet.")

if __name__ == "__main__":
    main()
