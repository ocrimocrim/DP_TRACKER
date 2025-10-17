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
    "User-Agent": "dpwt-marcel-bot/1.3 (+github-actions)",
    "Accept": "application/json,text/html"
})

# ------------------------------------------
# HTTP
# ------------------------------------------
def _get(url: str, as_json=False) -> Any:
    logging.debug(f"GET {url}")
    r = SESSION.get(url, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"http {r.status_code} for {url}")
    return r.json() if as_json else r.text

# ------------------------------------------
# Discovery „Playing this week“ ausschließlich per API
# ------------------------------------------
API_DISCOVERY = [
    # CMS Playerhub enthält Playing-this-week Blöcke
    "https://www.europeantour.com/api/cms/playerhub/{pid}?tour=dpworld-tour",
    # Profil-Overview
    "https://www.europeantour.com/api/cms/player/{pid}/overview?tour=dpworld-tour",
    # Individueller Spielplan des Spielers
    "https://www.europeantour.com/api/sportdata/Players/{pid}/Schedule?tour=dpworld-tour",
    # Alle Events dieser Woche
    "https://www.europeantour.com/api/sportdata/Events/ThisWeek?tour=dpworld-tour"
]

SLUG_RX = re.compile(r'/dpworld-tour/[^/]+-20\d{2}/?$', re.I)

def _scan_for_slug(obj: Any) -> Optional[str]:
    def walk(x) -> Optional[str]:
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str):
                    m = SLUG_RX.search(v)
                    if m:
                        return m.group(0).rstrip("/")
                got = walk(v)
                if got:
                    return got
        elif isinstance(x, list):
            for v in x:
                got = walk(v)
                if got:
                    return got
        elif isinstance(x, str):
            m = SLUG_RX.search(x)
            if m:
                return m.group(0).rstrip("/")
        return None
    return walk(obj)

def find_playing_this_week_url() -> Optional[str]:
    for tpl in API_DISCOVERY:
        url = tpl.format(pid=PLAYER_ID)
        try:
            data = _get(url, as_json=True)
        except Exception as e:
            logging.debug(f"Discovery miss {url} because {e}")
            continue
        slug = _scan_for_slug(data)
        if slug:
            full = BASE + slug
            logging.info(f"Playing this week Slug gefunden {slug}")
            return full
    logging.info("Kein Playing this week Slug per API gefunden")
    return None

# ------------------------------------------
# EventId nur per API auflösen
# ------------------------------------------
def resolve_event_id_via_player_schedule(event_url: str) -> Optional[int]:
    try:
        sched = _get(f"https://www.europeantour.com/api/sportdata/Players/{PLAYER_ID}/Schedule?tour=dpworld-tour", as_json=True)
    except Exception as e:
        logging.debug(f"Player schedule miss because {e}")
        return None
    target = event_url.replace(BASE, "")
    def match_item(it: Dict[str, Any]) -> bool:
        for k in ("Url", "URL", "Link", "link", "Path", "path", "EventUrl", "EventURL", "TournamentUrl"):
            v = it.get(k)
            if isinstance(v, str) and target in v:
                return True
        # manchmal liegt nur der Slug vor
        for k in ("Slug", "slug"):
            v = it.get(k)
            if isinstance(v, str) and v in target:
                return True
        return False
    if isinstance(sched, list):
        items = sched
    else:
        items = sched.get("Items") or sched.get("items") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        if match_item(it):
            for k in ("EventId", "eventId", "Id", "id"):
                if k in it and isinstance(it[k], int):
                    return int(it[k])
    return None

def resolve_event_id_via_this_week(event_url: str) -> Optional[int]:
    try:
        tw = _get("https://www.europeantour.com/api/sportdata/Events/ThisWeek?tour=dpworld-tour", as_json=True)
    except Exception as e:
        logging.debug(f"ThisWeek miss because {e}")
        return None
    target = event_url.replace(BASE, "")
    items = []
    if isinstance(tw, list):
        items = tw
    elif isinstance(tw, dict):
        items = tw.get("Events") or tw.get("events") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        # vergleiche alle Stringfelder auf den Slug
        found_url = False
        for v in it.values():
            if isinstance(v, str) and target in v:
                found_url = True
                break
        if found_url:
            for k in ("EventId", "eventId", "Id", "id"):
                if k in it and isinstance(it[k], int):
                    return int(it[k])
    return None

def extract_event_id(event_page_url: str) -> Optional[int]:
    # 1. Spieler-spezifischer Schedule
    eid = resolve_event_id_via_player_schedule(event_page_url)
    if eid:
        return eid
    # 2. Events dieser Woche
    eid = resolve_event_id_via_this_week(event_page_url)
    if eid:
        return eid
    # 3. Als letzte API-Option: Leaderboard-Config JSON, falls vorhanden
    try:
        lb_url = urljoin(event_page_url.rstrip("/") + "/", "leaderboard?round=4")
        # Einige Seiten liefern eingebettetes JSON in <script>__NEXT_DATA__
        txt = _get(lb_url, as_json=False)
        for m in re.finditer(r'<script[^>]*>\s*({.*?})\s*</script>', txt, re.S | re.I):
            block = m.group(1)
            cleaned = re.sub(r'(?://.*?$)|/\*.*?\*/', '', block, flags=re.M | re.S)
            try:
                j = json.loads(cleaned)
            except Exception:
                continue
            # Tiefensuche nach EventId
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
    except Exception as e:
        logging.debug(f"Leaderboard page check miss because {e}")
    logging.info("EventId wurde per API nicht gefunden")
    return None

# ------------------------------------------
# Sportdata API
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
    # 1. Spielerprofil per API auflösen
    event_url = find_playing_this_week_url()
    if not event_url:
        logging.info("Kein Turnier unter Playing this week gefunden. Abbruch.")
        return

    # 2. Leaderboard-Seite aus Slug ableiten für round=4
    leaderboard_page = urljoin(event_url.rstrip('/') + '/', "leaderboard?round=4")
    logging.info(f"Leaderboard Seite {leaderboard_page}")

    # 3. EventId strikt per API herleiten
    event_id = extract_event_id(event_url)
    if not event_id:
        logging.info("EventId wurde nicht gefunden. Abbruch.")
        return
    logging.info(f"EventId {event_id}")

    # 4. Leaderboard laden
    lb = fetch_leaderboard(event_id)
    players = lb.get("Players") or []
    me = find_player_row(players, PLAYER_ID)
    if not me:
        logging.info("Marcel Schneider ist nicht im Leaderboard vorhanden.")
        return

    # 5. State laden
    state = load_state(event_id)
    did_post = False

    # 6. Rundenabschlüsse posten
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

    # 7. Tagesabschluss posten, wenn alle Spieler fertig sind
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

    # 8. State speichern
    if did_post:
        save_state(event_id, state)
    else:
        logging.info("Kein neues Ereignis. Keine Discord Nachricht gesendet.")

if __name__ == "__main__":
    main()
