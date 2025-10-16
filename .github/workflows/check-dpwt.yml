name: DPWT Marcel Schneider → Discord

on:
  schedule:
    - cron: "*/30 * * * *"  # alle 30 Min (live-Fälle throtteln wir im Script)
  workflow_dispatch:

jobs:
  check-dpwt:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install requests playwright
          sudo apt-get update
          sudo apt-get install -y xvfb fonts-liberation fonts-noto-color-emoji \
              fonts-freefont-ttf fonts-ipafont-gothic fonts-unifont fonts-wqy-zenhei \
              xfonts-cyrillic xfonts-encodings xfonts-utils xfonts-scalable
          python -m playwright install --with-deps chromium

      - name: Run monitor (headful via Xvfb)
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
          DEBUG: "1"
          HEADLESS: "0"           # 0 = headful (unter Xvfb)
          DPWT_PROXY: ${{ secrets.DPWT_PROXY }} # optional, z.B. http://user:pass@host:port
          HTTPS_PROXY: ${{ secrets.DPWT_PROXY }} # ebenfalls setzen, falls vorhanden
          HTTP_PROXY: ${{ secrets.DPWT_PROXY }}
        run: |
          xvfb-run -a python dpwt_monitor_discord_playwright.py

      - name: Commit results (if any)
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -A
          if ! git diff --cached --quiet; then
            git commit -m "update(dpwt): state/log/baseline"
            git push
          fi
