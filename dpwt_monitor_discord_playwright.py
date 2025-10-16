import os, json, hashlib, datetime as dt
from typing import Any, List, Tuple
import requests
from playwright.sync_api import sync_playwright, APIResponse, TimeoutError as PWTimeout, Page

DEBUG = os.environ.get("DEBUG") == "1"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

BASE = "https://www.europeantour.com"
PLAYER_ID = 35703   # Marcel Schneider
TOUR_ID = 1         # DP World Tour

DATA_DIR = "data"
STATE_DIR = "state"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

def log(*a):
    if DEBUG:
        print("[dpwt]", *a, flush=True)

def now_utc() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def current_season() -> int:
    return dt.datetime.utcnow().year

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def write_text(path, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

def url_player_results(player_id: int, season: int) -> str:
    return f"{BASE}/api/v1/players/{player_id}/results/{season}/?tourId={TOUR_ID}"

def url_event_status() -> str:
    return f"{BASE}/api/sportdata/Event/Status"

def discord(payload: dict):
    if not WEBHOOK:
        log("no DISCORD_WEBHOOK configured – skipping send")
        return
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=20)
        log("discord post sent:", r.status_code)
    except Exception as e:
        log("discord error:", repr(e))

def ensure_baseline(season: int, items: List[dict]):
    path = f"{DATA_DIR}/baseline-{season}.json"
    if os.path.exists(path):
        return False
    baseline = {
        "season": season,
        "created": now_utc(),
        "count": len(items),
        "hash": sha(items),
        "items": items
    }
    write_json(path, baseline)
    discord({"content": f"Monitor aktiv. Baseline {season} gesetzt ({len(items)} Turniere)."})
    log("baseline written:", path)
    return True

def save_last_check():
    state = load_json(f"{STATE_DIR}/last_seen.json", {})
    state["last_check_ts"] = now_utc()
    write_json(f"{STATE_DIR}/last_seen.json", state)

def throttle_ok(is_live: bool):
    st = load_json(f"{STATE_DIR}/last_seen.json", {})
    last = st.get("last_check_ts")
    if not last:
        return True, "first"
    if is_live:
        # alle 30 Min bei Live-Event – erlaubt
        return True, "live"
    last_dt = dt.datetime.fromisoformat(last.replace("Z", ""))
    if (dt.datetime.utcnow() - last_dt) >= dt.timedelta(hours=2):
        return True, "2h-pass"
    return False, "throttled"

def normalize_items(api_obj: Any) -> List[dict]:
    if isinstance(api_obj, list):
        return api_obj
    if isinstance(api_obj, dict):
        for k in ("Results", "results", "Items", "items", "Data", "data"):
            v = api_obj.get(k)
            if isinstance(v, list):
                return v
    return []

def ev_key(ev: dict) -> str:
    cid = ev.get("CompetitionId") or ev.get("EventId")
    if cid:
        return str(cid)
    return sha([ev.get("Tournament") or ev.get("TournamentName"), ev.get("EndDate")])

def round_fields(ev: dict):
    return {
        "R1": ev.get("R1"), "R2": ev.get("R2"), "R3": ev.get("R3"), "R4": ev.get("R4"),
        "Total": ev.get("Total"), "ToPar": ev.get("ToPar"),
        "Pos": ev.get("PositionText") or ev.get("Position") or ev.get("Pos")
    }

def post_round_update(name, rno, pos, strokes, total, url=None):
    embed = {
        "title": f"Runden-Update – {name}",
        "url": url, "color": 0x2ecc71,
        "fields": [
            {"name": "Runde", "value": f"R{rno}", "inline": True},
            {"name": "Pos.", "value": (pos or "–"), "inline": True},
            {"name": f"Schläge R{rno}", "value": (strokes or "–"), "inline": True},
            {"name": "Total (bis jetzt)", "value": (total or "–"), "inline": True},
        ],
        "footer": {"text": "DP World Tour – Marcel Schneider"},
    }
    discord({"embeds": [embed]})

def post_final(ev):
    name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
    embed = {
        "title": f"Turnier beendet – {name}",
        "url": ev.get("TournamentUrl") or ev.get("Link"),
        "color": 0x3498db,
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

def accept_consent_if_present(page: Page):
    # OneTrust Variationen abdecken
    for sel in ("#onetrust-accept-btn-handler", "button#onetrust-accept-btn-handler", "button[aria-label='Accept Cookies']"):
        try:
            page.wait_for_selector(sel, timeout=4000)
            page.click(sel)
            log("consent clicked")
            break
        except:
            pass

def has_live_event(context) -> bool:
    try:
        res: APIResponse = context.request.get(
            url_event_status(),
            headers={
                "Accept": "application/json",
                "Referer": f"{BASE}/dpworld-tour/",
                "Origin": BASE,
            },
            max_redirects=5, timeout=45000
        )
        if not res.ok:
            return False
        data = res.json()
        for ev in data or []:
            if ev.get("TourId") == TOUR_ID and (ev.get("Status") in (1,2) or ev.get("RoundStatus") in (1,2)):
                return True
    except Exception as e:
        log("live-check failed:", repr(e))
    return False

def parse_json_maybe(text: str) -> Tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except:
        return False, None

def fetch_results_via_browser(context, season: int) -> Any:
    api = url_player_results(PLAYER_ID, season)
    page = context.new_page()
    page.set_default_timeout(60000)
    try:
        page.goto(f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour", wait_until="domcontentloaded")
        accept_consent_if_present(page)
    except PWTimeout:
        log("page goto timeout (ok)")
    # Primär: context.request (nimmt Cookies/Proxy mit)
    res: APIResponse = context.request.get(
        api,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE}/players/marcel-schneider-{PLAYER_ID}/results/?tour=dpworld-tour",
            "Origin": BASE,
        },
        max_redirects=5, timeout=60000
    )
    if res.ok:
        ct = (res.headers.get("content-type") or "").lower()
        body = res.text()
        if "application/json" in ct:
            page.close()
            return res.json()
        write_text(f"{DATA_DIR}/_debug_last_url.txt", api)
        write_text(f"{DATA_DIR}/_debug_last_response.html", body)
        log("context.request returned non-json; wrote _debug_last_response.html")
    # Fallback: im Page-Kontext als Text
    try:
        js = f"""async () => {{
          const r = await fetch("{api}", {{ credentials: "include" }});
          const ct = r.headers.get("content-type") || "";
          const t = await r.text();
          return {{ status: r.status, ct, body: t }};
        }}"""
        obj = page.evaluate(js)
        if obj and isinstance(obj, dict):
            if "application/json" in (obj.get("ct","").lower()):
                ok, val = parse_json_maybe(obj.get("body",""))
                if ok:
                    return val
            write_text(f"{DATA_DIR}/_debug_last_url.txt", api)
            write_text(f"{DATA_DIR}/_debug_last_response.html", obj.get("body",""))
            log("page.fetch returned non-json; wrote _debug_last_response.html")
    finally:
        page.close()
    raise RuntimeError("results fetch produced no JSON (see data/_debug_last_response.html)")

def main():
    season = current_season()
    raw_path = f"{DATA_DIR}/raw-{season}.json"

    # Proxy aus ENV (HTTP(S)_PROXY oder DPWT_PROXY)
    proxy_url = os.environ.get("DPWT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    headless = os.environ.get("HEADLESS", "1") != "0"  # HEADLESS=0 → headful

    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless, "args": ["--no-sandbox"]}
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}
            log(f"using proxy: {proxy_url.split('@')[-1]}")

        browser = pw.chromium.launch(**launch_kwargs)
        context_kwargs = {
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
            "locale": "de-DE",
            "viewport": {"width": 1366, "height": 768},
        }
        if proxy_url:
            context_kwargs["proxy"] = {"server": proxy_url}

        context = browser.new_context(**context_kwargs)

        # JSON holen
        try:
            raw = fetch_results_via_browser(context, season)
        except Exception as e:
            write_json(f"{DATA_DIR}/_debug_error.json", {"ts": now_utc(), "step": "fetch", "error": repr(e)})
            log("fetch failed, wrote _debug_error.json", repr(e))
            context.close(); browser.close()
            return

        # Rohdump speichern
        write_json(raw_path, raw)

        items = normalize_items(raw)
        if not items:
            write_json(f"{DATA_DIR}/_debug_warning.json", {"ts": now_utc(), "step": "normalize", "note": "0 items – check raw & debug html"})
            ensure_baseline(season, [])
            save_last_check()
            context.close(); browser.close()
            return

        ensure_baseline(season, items)

        live = has_live_event(context)
        ok, _ = throttle_ok(live)
        if not ok:
            context.close(); browser.close()
            return

        state = load_json(f"{STATE_DIR}/events.json", {"events": {}})
        events = state["events"]
        hist = f"{DATA_DIR}/history-{season}.jsonl"

        for ev in items:
            key = ev_key(ev)
            name = ev.get("Tournament") or ev.get("TournamentName") or "Turnier"
            events.setdefault(key, {"rounds": {}, "finished": False, "name": name})

            rdat = round_fields(ev)
            for rno in (1,2,3,4):
                col = f"R{rno}"
                val = (rdat.get(col) or "").strip() if rdat.get(col) else ""
                if val and events[key]["rounds"].get(str(rno)) != val:
                    post_round_update(name, rno, rdat.get("Pos"), val, rdat.get("Total"), ev.get("Link") or ev.get("TournamentUrl"))
                    events[key]["rounds"][str(rno)] = val
                    append_jsonl(hist, {"ts": now_utc(), "type": "round", "eventKey": key, "round": rno,
                                        "position": rdat.get("Pos"), "strokes": val, "total": rdat.get("Total"),
                                        "tournament": name})

            finished = bool((ev.get("Total") or "").strip()) and ((ev.get("R4") or ev.get("R3") or "").strip())
            if finished and not events[key]["finished"]:
                post_final(ev)
                events[key]["finished"] = True
                append_jsonl(hist, {"ts": now_utc(), "type": "finished", "eventKey": key,
                                    "tournament": name, "snapshot": ev})

        write_json(f"{STATE_DIR}/events.json", state)
        save_last_check()
        context.close(); browser.close()

if __name__ == "__main__":
    main()
