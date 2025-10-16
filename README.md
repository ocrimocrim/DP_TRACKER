# DPWT Turniertracker (Marcel Schneider → Discord)

- Scrape über **Playwright** im Browser-Kontext (um 403/Edge zu vermeiden)
- Baseline pro Saison: `data/baseline-<Jahr>.json`
- Änderungs-Logs mit Zeitstempel: `data/logs/<ISO>.json`
- Discord-Posts:
  - Neues Turnier
  - Rundendaten (R1–R4, Pos., To Par, Total)
  - Turnier abgeschlossen inkl. Preisgeld/Punkte

## Zeitplan
- Workflow triggert alle **30 Minuten** (07–23 Berlin).
- Script entscheidet:
  - **Live-Event** → posten **alle 30 Minuten**.
  - **Nicht live** → posten nur **alle 2 Stunden** (07, 09, 11, 13, 15, 17, 19, 21).

## Setup
1. Repo-Secret `DISCORD_WEBHOOK_URL` setzen.
2. Actions aktivieren.
3. Workflow läuft — beim ersten Lauf wird die Baseline `<Jahr>` geschrieben.

## Debug
- `data/_debug_last_response.html` / `_debug_last_url.txt`: Rohantwort, falls das API HTML liefert (Access Denied).
- `data/_debug_error.json`: letzte Fehlermeldung.
