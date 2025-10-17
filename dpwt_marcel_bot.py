#!/usr/bin/env python3
import os, re, json, logging, pathlib, datetime as dt, html
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin, urlencode, urlparse, urlunparse
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
TZ = dt.timezone(dt.timedelta(hours=2))

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "dpwt-marcel-bot/1.6 (+github-actions)",
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
        m = re.search(r'(/dpworld-tour/[^"/]+-20\d{2}/?)', hay, re.I)
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    url = BASE + slug
    logging.info(f"Playing this week Slug gefunden {slug}")
    return url

# ------------------------------------------
# Schritt 3 und 4
# Leaderboard-Seite aufbauen
# ------------------------------------------
def build_leaderboard_page(event_page_url: str) -> str:
    return urljoin(event_page_url.rstrip("/") + "/", "leaderboard?round=4")

# ------------------------------------------
# Schritt 5
# EventId nur über die Leaderboard-Seite ermitteln
# ------------------------------------------
EVENT_LOAD_URL_RX = re.compile(r'/api/sportdata/Leaderboard/Strokeplay/(\d+)/type/load', re.I)
EVENT_ID_KEY_RX  = re.compile(r'"(?:EventId|eventId)"\s*:\s*(\d+)', re.I)
LEADERBOARD_DOC_ID_RX = re.compile(r'"id"\s*:\s*"leaderboard-strokeplay-(\d+)"', re.I)

def _event_id_from_text(html_text: str) -> Optional[int]:
    m = EVENT_LOAD_URL_RX.search(html_text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    m = LEADERBOARD_DOC_ID_RX.search(html_text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    m = EVENT_ID_KEY_RX.search(html_text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # Eingebettete JSON-Blöcke durchsuchen
    for m in re.finditer(r'<script[^>]*>\s*({.*?})\s*</script>', html_text, re.S | re.I):
        block = m.group(1)
        # Direkt versuchen
        try:
            j = json.loads(block)
        except Exception:
            # Kommentare entfernen und erneut versuchen
            cleaned = re.sub(r'(?://.*?$)|/\*.*?\*/', '', block, flags=re.M | re.S)
            try:
                j = json.loads(cleaned)
            except Exception:
                continue
        # Tiefensuche
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

def _resolver_try(path: str) -> Optional[int]:
    """
    Manche Seiten liefern Metadaten über Resolver-APIs für genau diesen Pfad.
    Ich teste mehrere übliche Resolver auf derselben Domain und lese eine zahlige EventId.
    """
    candidates = [
        f"{BASE}/api/seo/resolve?{urlencode({'path': path})}",
        f"{BASE}/api/cms/resolve?{urlencode({'path': path})}",
        f"{BASE}/api/cms/page-resolver?{urlencode({'path': path})}",
    ]
    for url in candidates:
        try:
            txt = _get(url, as_json=False)
        except Exception as e:
            logging.debug(f"resolver miss {url} because {e}")
            continue
        # EventId direkt
        m = EVENT_ID_KEY_RX.search(txt)
        if m:
            return int(m.group(1))
        # oder Leaderboard-Doc-Id
        m = LEADERBOARD_DOC_ID_RX.search(txt)
        if m:
            return int(m.group(1))
        # oder Sportdata-URL
        m = EVENT_LOAD_URL_RX.search(txt)
        if m:
            return int(m.group(1))
        # Notfalls JSON parsen und tief suchen
        try:
            data = json.loads(txt)
        except Exception:
            for js in re.finditer(r'({.*?})', txt, re.S):
                try:
                    data = json.loads(js.group(1))
                    break
                except Exception:
                    data = None
                    continue
        if data is None:
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
        got = walk(data)
        if got:
            return int(got)
    return None

def extract_event_id(event_page_url: str) -> Optional[int]:
    lb_url = build_leaderboard_page(event_page_url)

    # 1) Leaderboard HTML via Proxy
    try:
        html1 = _get(lb_url, allow_jina=True)
        eid = _event_id_from_text(html1)
        if eid:
            logging.info(f"EventId Quelle Leaderboard Jina {eid}")
            return eid
    except Exception as e:
        logging.debug(f"LeaderBoard Jina miss because {e}")

    # 2) Leaderboard HTML direkt
    try:
        html2 = _get(lb_url, allow_jina=False)
        eid = _event_id_from_text(html2)
        if eid:
            logging.info(f"EventId Quelle Leaderboard direkt {eid}")
            return eid
    except Exception as e:
        logging.debug(f"LeaderBoard direct miss because {e}")

    # 3) Resolver für genau diesen Pfad (gleiche Seite, kein anderer Flow)
    path = urlparse(lb_url).path  # nur Pfad ohne Domain und Query
    eid = _resolver_try(path)
    if eid:
        logging.info(f"EventId Quelle Resolver {eid}")
        return eid

    # 4) Resolver zusätzlich ohne '?round=4' am Event-Wurzelpfad
    root_path = urlparse(event_page_url).path.rstrip("/")
    eid = _resolver_try(root_path)
    if eid:
        logging.info(f"EventId Quelle Resolver root {eid}")
        return eid

    logging.info("EventId wurde nicht gefunden")
    return None

# ------------------------------------------
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
    event_url = find_playing_this_week_url()
    if not event_url:
        logging.info("Kein Turnier unter Playing this week gefunden. Abbruch.")
        return

    leaderboard_page = build_leaderboard_page(event_url)
    logging.info(f"Leaderboard Seite {leaderboard_page}")

    event_id = extract_event_id(event_url)
    if not event_id:
        logging.info("EventId wurde nicht gefunden. Abbruch.")
        return
    logging.info(f"EventId {event_id}")

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
