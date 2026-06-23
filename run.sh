#!/usr/bin/env bash
# Quick-start the Claude Telegram bot. Idempotent: safe to re-run — it creates the venv if
# missing, (re)installs/refreshes deps, then starts the bot in the FOREGROUND. Edit .env
# first (cp .env.example .env). To keep it running across reboots/crashes, see deploy/README.md.
#
# One token == one poller: if you've installed the launchd keep-alive, `launchctl unload` it
# before running this, or the two pollers will hit a Telegram `Conflict`.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "Creating venv (.venv)..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Refresh deps every run (idempotent; fast when already satisfied). requirements.txt
# installs this project (-e .), which pulls the runtime deps from pyproject.toml and the
# `claude-telegram-bot` console script — so re-running picks up dependency/entry changes.
echo "Installing/refreshing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env  (then fill in your token + chat id)." >&2
  exit 1
fi

# Start via the package module so this matches the installed console script + the
# keep-alive (deploy/) entry. `exec` so signals (Ctrl-C / launchd stop) reach the bot.
echo "Starting the bot (Ctrl-C to stop)..."
exec python -m claude_tg
