name: DPWT to Discord

on:
  schedule:
    - cron: "0 */4 * * *"   # alle 4 Stunden (UTC)
  workflow_dispatch:

permissions:
  contents: write
  issues: write

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Run bot
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
          STATE_ISSUE_NUMBER: ${{ vars.STATE_ISSUE_NUMBER }}
          API_URL: https://www.europeantour.com/api/v1/players/35703/results/2025/
          PYTHONUNBUFFERED: "1"
        run: python bot.py
