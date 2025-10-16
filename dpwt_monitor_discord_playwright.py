#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import datetime
import requests
from pathlib import Path
from typing import Any, Dict, List
from playwright.sync_api import sync_playwright

# -----------------------------------------------------------
# KONSTANTEN
# -----------------------------------------------------------

BASE_URL = "https://www.europeantour.com"
PLAYER_ID = "35703"  # Marcel Schneider
TOUR_ID = "1"
SEASON = 2025

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BASELINE_FILE = DATA_DIR / f"baseline-{SEASON}.json"
DEBUG_ERROR_FILE = DATA_DIR / "_debug_error.json"
DEBUG_LAST_HTML = DATA_DIR / "_debug_last_response.html"
DEBUG_LAST_URL = DATA_DIR / "_debug_last_url.txt"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

# -----------------------------------------------------------
# HILFSFUNKTIONEN
# -----------------------------------------------------------

def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def write_debug(step: str, err: str) -> None:
    write_json(DEBUG_ERROR_FILE, {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "step": step,
        "error": err,
    })


def discord_post(content: str) -> None:
    if not DISCORD_WEBHOOK:
        print("[dpwt] no webhook set, skipping discord")
        return
    r = requests.post(DISCORD_WEBHOOK, json={"content": content})
    print(f"[dpwt] discord post sent: {r.status_code}")


# -----------------------------------------------------------
# FETCH MIT PLAYWRIGHT
# -----------------------------------------------------------

def fetch_results_via_page(page, player_id: str, tour_id: str, season: int) -> Dict[str, Any]:
    """
    Lädt die Spieler-Seite, klickt Cookie-Consent und ruft die JSON
    über window.fetch() im Browserkontext (Same-Origin) ab.
    """
    url = f"{BASE_URL}/players/{player_id}/results?tour=dpworld-tour"
    page.set_default_timeout(30000)
    page.goto(url, wait_until="domcontentloaded")

    # Cookie Banner wegklicken (OneTrust – mehrere Fallbacks)
    for sel in [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Alle akzeptieren')",
    ]:
        try:
            page.locator(sel).click(timeout=3000)
            print("[dpwt] consent clicked")
            break
        except Exception:
            pass

    time.sleep(1.2)

    api_url = f"/api/v1/players/{player_id}/results/{season}/?tourId={tour_id}"

    try:
        js = """
        async (u) => {
          const res = await fetch(u, { credentials: 'same-origin' });
          const ct = (res.headers.get('content-type') || '').toLowerCase();
          const txt = await res.text();
          if (!ct.includes('application/json')) {
            return { ok: false, url: u, contentType: ct, text: txt };
          }
          try {
            const data = JSON.parse(txt);
            return { ok: true, url: u, data };
          } catch (e) {
            return { ok: false, url: u, contentType: ct, text: txt };
          }
        }
        """
        result = page.evaluate(js, api_url)

        if not result or not result.get("ok"):
            DEBUG_LAST_URL.write_text(result.get("url", api_url), encoding="utf-8")
            DEBUG_LAST_HTML.write_text(result.get("text", ""), encoding="utf-8")
            write_debug("fetch", "results fetch produced no JSON (see data/_debug_last_response.html)")
            raise RuntimeError("results fetch produced no JSON (see data/_debug_last_response.html)")

        return result["data"]

    except Exception as e:
        DEBUG_LAST_URL.write_text(api_url, encoding="utf-8")
        try:
            DEBUG_LAST_HTML.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        write_debug("fetch", repr(e))
        raise


# -----------------------------------------------------------
# DATENVERGLEICH & FORMATIERUNG
# -----------------------------------------------------------

def summarize_event(ev: Dict[str, Any]) -> str:
    name = ev.get("Tournament", "")
    pos = ev.get("Pos", "")
    total = ev.get("Total", "")
    finish = ev.get("Finish", "")
    return f"{name} {pos} ({total}) {finish}".strip()


def diff_events(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[str]:
    changes = []
    old_map = {e.get("Tournament"): e for e in old or []}
    for ev in new or []:
        name = ev.get("Tournament")
        if not name:
            continue
        old_ev = old_map.get(name)
        if not old_ev:
            changes.append(f"Neues Event: {summarize_event(ev)}")
            continue
        if json.dumps(old_ev, sort_keys=True) != json.dumps(ev, sort_keys=True):
            changes.append(f"Aktualisiert: {summarize_event(ev)}")
    return changes


# -----------------------------------------------------------
# HAUPTLOGIK
# -----------------------------------------------------------

def main():
    print("[dpwt] starting")
    baseline = read_json(BASELINE_FILE) or []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            data = fetch_results_via_page(page, PLAYER_ID, TOUR_ID, SEASON)
        finally:
            browser.close()

    if not data or not isinstance(data, list):
        write_debug("parse", "no event list in data")
        raise RuntimeError("no valid event list in data")

    write_json(BASELINE_FILE, data)
    print(f"[dpwt] baseline written: {BASELINE_FILE}")

    # Änderungen erkennen
    changes = diff_events(baseline, data)

    if not baseline:
        discord_post(f"Monitor aktiv. Baseline {SEASON} gesetzt ({len(data)} Turniere).")
        return

    if changes:
        msg = f"Update DPWT {SEASON}:\n" + "\n".join(changes)
        discord_post(msg)
    else:
        print("[dpwt] no changes detected")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_debug("main", repr(e))
        raise
