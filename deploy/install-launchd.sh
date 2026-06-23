#!/usr/bin/env bash
# Generate + install the macOS LaunchAgent that keeps the Claude Telegram bot running
# (P7/T3). Idempotent: re-running regenerates the plist and reloads the agent.
#
# What it does:
#   * Resolves the bot start command (prefers a `claude-telegram-bot` console script in
#     this repo's .venv, then on PATH; else falls back to "<venv-python> -m claude_tg").
#   * Uses this repo checkout as the WorkingDirectory (it holds .env — the bot loads
#     .env from its CWD). Override with --workdir DIR.
#   * Renders deploy/com.claude-telegram-bot.plist into ~/Library/LaunchAgents/ with a
#     PATH that includes your `claude` binary's directory so the subprocess resolves.
#   * Loads the agent (RunAtLoad + KeepAlive: starts now, on login, and on crash).
#
# The bot TOKEN is never written into the plist — it stays in .env in the WorkingDirectory.
#
# IMPORTANT (one instance per token): this agent runs the only poller. Before you start
# the bot by hand (./run.sh), `launchctl unload` it first (see deploy/README.md), or two
# pollers will hit a Telegram `Conflict`.
#
# Usage:
#   deploy/install-launchd.sh                 # install + load from this checkout
#   deploy/install-launchd.sh --workdir DIR   # use DIR (must contain .env) as the CWD
#   deploy/install-launchd.sh --print         # print the rendered plist, don't install
#   deploy/install-launchd.sh --uninstall     # unload + remove the installed agent
set -euo pipefail

LABEL="com.claude-telegram-bot"
# Repo root = parent of this script's dir (deploy/..).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist"

LA_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LA_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
STDOUT_LOG="$LOG_DIR/$LABEL.out.log"
STDERR_LOG="$LOG_DIR/$LABEL.err.log"

WORKDIR="$REPO_ROOT"
MODE="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --print) MODE="print"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

uninstall() {
  if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Removed $PLIST_DEST (agent unloaded)."
  else
    echo "Nothing to remove ($PLIST_DEST not present)."
  fi
}

if [ "$MODE" = "uninstall" ]; then
  uninstall
  exit 0
fi

# --- resolve the bot start command (absolute argv tokens, one per line) ---
# bash 3.2-compatible (macOS system bash): no `mapfile`/`readarray`.
resolve_bot_command() {
  # 1) console script in this repo's venv
  if [ -x "$REPO_ROOT/.venv/bin/claude-telegram-bot" ]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/claude-telegram-bot"
    return
  fi
  # 2) console script on PATH
  local on_path
  if on_path="$(command -v claude-telegram-bot 2>/dev/null)"; then
    printf '%s\n' "$on_path"
    return
  fi
  # 3) fall back to "<python> -m claude_tg"
  local py
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    py="$REPO_ROOT/.venv/bin/python"
  else
    py="$(command -v python3 || command -v python)"
  fi
  printf '%s\n-m\nclaude_tg\n' "$py"
}

# Build a PATH that includes the venv bin + the dir holding `claude` so the agent
# (which gets a minimal launchd PATH) can find both. Falls back to a sane default.
resolve_path() {
  local parts=()
  [ -d "$REPO_ROOT/.venv/bin" ] && parts+=("$REPO_ROOT/.venv/bin")
  local claude_bin
  if claude_bin="$(command -v claude 2>/dev/null)"; then
    parts+=("$(dirname "$claude_bin")")
  fi
  parts+=("/opt/homebrew/bin" "/usr/local/bin" "/usr/bin" "/bin")
  # de-dup while preserving order
  printf '%s\n' "${parts[@]}" | awk '!seen[$0]++' | paste -sd: -
}

# Populate BOT_ARGV (array) + ARGV_XML (<string> lines) from the resolver, without
# `mapfile` (absent in macOS bash 3.2). One token per line; tokens here are paths we
# generate, never user free-text, so word-splitting on newlines is safe.
BOT_ARGV=()
ARGV_XML=""
while IFS= read -r tok; do
  [ -n "$tok" ] || continue
  BOT_ARGV+=("$tok")
  ARGV_XML+="        <string>${tok}</string>"$'\n'
done < <(resolve_bot_command)
ARGV_XML="${ARGV_XML%$'\n'}"

RESOLVED_PATH="$(resolve_path)"

render() {
  # Stream the template line-by-line; when we hit the single-token __BOT_COMMAND__
  # <string> line, emit the full (possibly multi-line) argv block instead, then
  # substitute the scalar placeholders. Done in bash (no awk -v: the argv block has
  # newlines, which awk's -v can't carry). sed handles the one-line scalar swaps.
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *'<string>__BOT_COMMAND__</string>'*)
        printf '%s\n' "$ARGV_XML"
        ;;
      *)
        printf '%s\n' "$line" | sed \
          -e "s|__WORKING_DIR__|$WORKDIR|g" \
          -e "s|__PATH__|$RESOLVED_PATH|g" \
          -e "s|__STDOUT_LOG__|$STDOUT_LOG|g" \
          -e "s|__STDERR_LOG__|$STDERR_LOG|g"
        ;;
    esac
  done < "$TEMPLATE"
}

if [ ! -f "$TEMPLATE" ]; then
  echo "template not found: $TEMPLATE" >&2; exit 1
fi

if [ "$MODE" = "print" ]; then
  render
  exit 0
fi

# --- install + load ---
if [ ! -f "$WORKDIR/.env" ]; then
  echo "WARNING: $WORKDIR/.env not found — the bot will fail to start until you create it" >&2
  echo "         (cp .env.example .env in that directory and fill it in)." >&2
fi

mkdir -p "$LA_DIR" "$LOG_DIR"

# Reload cleanly if already installed (idempotent).
if [ -f "$PLIST_DEST" ]; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

render > "$PLIST_DEST"
launchctl load "$PLIST_DEST"

echo "Installed + loaded: $PLIST_DEST"
echo "  command:    ${BOT_ARGV[*]}"
echo "  workdir:    $WORKDIR"
echo "  logs:       $STDOUT_LOG"
echo "              $STDERR_LOG"
echo
echo "Check it:     launchctl list | grep $LABEL"
echo "Tail logs:    tail -f \"$STDERR_LOG\""
echo "Stop/disable: launchctl unload \"$PLIST_DEST\""
echo
echo "Before running the bot manually (./run.sh), unload this agent first or you'll get a Telegram Conflict (two pollers, one token)."
