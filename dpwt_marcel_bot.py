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
TZ = dt.timezone(dt.timedelta(hours=2))  # Europe Berlin grob

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "dpwt-marcel-bot/1.2 (+github-actions)",
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
# Discovery für Playing this week über API
# ------------------------------------------
API_CANDIDATES = [
    "https://www.europeantour.com/api/cms/playerhub/{pid}?tour=dpworld-tour",
    "https://www.europeantour.com/api/cms/player/{pid}/overview?tour=dpworld-tour",
    "https://www.europeantour.com/api/sportdata/Players/{pid}/Schedule?tour=dpworld-tour",
    "https://www.europeantour.com/api/sportdata/Events/ThisWeek?tour=dpworld-tour",
    "https://www.europeantour.com/players/marcel-schneider-{pid}/?tour=dpworld-tour"
]

def _maybe_json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        for m in re.finditer(r'<script[^>]*>\s*({.*?})\s*</script>', text, re.S | re.I):
            block = m.group(1)
            cleaned = re.sub(r'(?://.*?$)|/\*.*?\*/', '', block, flags=re.M | re.S)
            try:
                return json.loads(cleaned)
            except Exception:
                continue
    return None

def _find_slug_in_json_blob(blob: Any) -> Optional[str]:
    def scan(x):
        if isinstance(x, dict):
            for k in ("playingThisWeek", "playing_this_week", "upNext", "tournament", "event", "link", "url", "href", "path", "Slug", "slug"):
                v = x.get(k)
                if isinstance(v, str):
                    m = re.search(r'/dpworld-tour/[^"/]+-20\d{2}/?', v, re.I)
                    if m:
                        return m.group(0).rstrip("/")
            for v in x.values():
                got = scan(v)
                if got:
                    return got
        elif isinstance(x, list):
            for v in x:
                got = scan(v)
                if got:
                    return got
        elif isinstance(x, str):
            m = re.search(r'/dpworld-tour/[^"/]+-20\d{2}/?', x, re.I)
            if m:
                return m.group(0).rstrip("/")
        return None
    return scan(blob)

def find_playing_this_week_url() -> Optional[str]:
    pid = PLAYER_ID
    last_err = None
    for raw in API_CANDIDATES:
        url = raw.format(pid=pid)
        try:
            txt = _get(url, as_json=False, allow_jina=True)
            data = _maybe_json(txt)
            if data is not None:
                slug = _find_slug_in_json_blob(data)
                if slug:
                    logging.info(f"Playing this week Slug gefunden {slug}")
                    return BASE + slug
            m = re.search(r'href="(/dpworld-tour/[^"/]+-20\d{2}/?)"', txt, re.I)
            if m:
                slug = m.group(1).rstrip("/")
                logging.info(f"Playing this week Slug via HTML gefunden {slug}")
                return BASE + slug
        except Exception as e:
            last_err = str(e)
            logging.debug(f"candidate miss {url} because {e}")
            continue
    logging.info(f"Kein Playing this week Slug per API gefunden. Letzter Fehler {last_err}")
    return None

# ------------------------------------------
# EventId finden
# ------------------------------------------
EVENT_ID_PATTERNS = [
    r'"EventId"\s*:\s*(\d+)',
    r'"eventId"\s*:\s*(\d+)',
    r'Leaderboard/Strokeplay/(\d+)/'
]

def _search_event_id_from_html(html_text: str) -> Optional[int]:
    for pat in EVENT_ID_PATTERNS:
        m = re.search(pat, html_text, re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    for m in re.finditer(r'<script[^>]*>({.*?})</script>', html_text, re.S | re.I):
        block = m.group(1)
        if "EventId" in block or "eventId" in block:
            try:
                j = json.loads(block)
            except Exception:
                block_clean = re.sub(r'(?://.*?$)|/\*.*?\*/', '', block, flags=re.M | re.S)
                try:
                    j = json.loads(block_clean)
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
            found = walk(j)
            if found:
                return int(found)
    return None

def extract_event_id(event_page_url: str) -> Optional[int]:
    html1 = _get(event_page_url, allow_jina=True)
    found = _search_event_id_from_html(html1)
    if found:
        return found
    lb_url = urljoin(event_page_url + "/", "leaderboard")
    html2 = _get(lb_url, allow_jina=True)
    found = _search_event_id_from_html(html2)
    if found:
        return found
    logging.debug("EventId weder auf Eventseite noch auf Leaderboardseite gefunden")
    return None

# ------------------------------------------
# API und Fallbacks
# ------------------------------------------
def fetch_leaderboard(event_id: int) -> Dict[str, Any]:
    url = f"{BASE}/api/sportdata/Leaderboard/Strokeplay/{event_id}/type/load"
    return _get(url, as_json=True)

def find_player_row(players: List[Dict[str, Any]], pid: int) -> Optional[Dict[str, Any]]:
    for p in players:
        if p.get("PlayerId") == pid:
            return p
    return None

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

def _scrape_fallback_player_row(leaderboard_html: str, player_name: str) -> Optional[Dict[str, Any]]:
    text = html.unescape(re.sub(r'\s+', ' ', leaderboard_html))
    m = re.search(r'([T]?\d{1,3})[^<]{0,40}%s[^<]{0,80}?([+\-]?\d{1,2}|E)' % re.escape(player_name), text, re.I)
    if not m:
        return None
    pos = m.group(1)
    score = m.group(2)
    return {"PositionDesc": pos, "ScoreToPar": 0 if score.upper() == "E" else int(score)}

# ------------------------------------------
# Discord und State
# ------------------------------------------
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

# ------------------------------------------
# Hauptlogik
# ------------------------------------------
def main():
    event_url = find_playing_this_week_url()
    if not event_url:
        logging.info("Kein Turnier unter Playing this week gefunden. Abbruch.")
        return

    event_id = extract_event_id(event_url)
    if not event_id:
        logging.info("EventId wurde nicht gefunden. Versuche HTML Fallback.")
        leaderboard_html = _get(f"{event_url}/leaderboard", allow_jina=True)
        scraped = _scrape_fallback_player_row(leaderboard_html, "Marcel Schneider")
        if scraped:
            lines = [
                "Turnier",
                f"{event_url}",
                "Schnappschuss ohne API",
                f"Aktueller Rang {scraped['PositionDesc']}",
                f"Gesamt gegen Par {scraped['ScoreToPar']}",
                "Leaderboard Link",
                f"{event_url}/leaderboard"
            ]
            post_discord(fmt_discord_block("Marcel Schneider Update", lines))
        else:
            logging.info("Weder API noch HTML Fallback lieferte Daten für Marcel Schneider.")
        return

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
            f"{event_url}/leaderboard?round=4"
        ]
        scorecard = try_fetch_scorecard(event_id, PLAYER_ID)
        round_lines.extend(build_par_and_strokes_text(scorecard, rno))

        content = fmt_discord_block("Marcel Schneider Update", round_lines)
        post_discord(content)
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
                    f"{event_url}/leaderboard?round=4"
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
