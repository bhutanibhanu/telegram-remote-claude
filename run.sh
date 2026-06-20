#!/usr/bin/env bash
# Start the Claude Telegram bot. Edit .env first (see .env.example).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating venv..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt
exec python main.py
