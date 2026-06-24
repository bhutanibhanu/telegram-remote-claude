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
# pollers will hit a Telegram `Conflict`. As a safety net, install ABORTS if it detects a
# bot already running by hand (it won't start a second, conflicting poller).
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

# B2: list PIDs of any RUNNING bot poller (manual `./run.sh`, `python main.py`,
# `python -m claude_tg`, or a `claude-telegram-bot` console script), one per line. Used
# AFTER unloading our own LaunchAgent so anything still alive is a manual poller — starting
# a second one on the same token would hit a Telegram `Conflict`. Excludes this installer
# (its own path contains "claude-telegram-bot") and the inner grep/ps via PID filtering.
running_bot_pids() {
  # `ps -axww -o pid=,command=`: every process, full (un-truncated) command, no header.
  # Match the known start forms, then drop our own PID + this script's path so the guard
  # never trips on itself.
  ps -axww -o pid=,command= 2>/dev/null | awk -v self="$$" -v script="${BASH_SOURCE[0]}" '
    {
      pid = $1
      # rebuild the command (everything after the pid column)
      cmd = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", cmd)
      if (pid == self) next            # this installer process
      if (index(cmd, script)) next     # any invocation of this script (e.g. a subshell)
      # Lower-cased copy so the interpreter test catches python / python3.x AND the macOS
      # framework "Python" (capital P) regardless of case. POSIX tolower() — BSD-awk safe.
      lc = tolower(cmd)
      # Match the known start forms. NB on boundaries (avoid false positives):
      #  * claude-telegram-bot: the CONSOLE SCRIPT — its name as an executable basename or a
      #    bare arg (preceded by start/space/"/", followed by end/space). This is NOT a plain
      #    substring test, so a process merely RUNNING OUT OF a dir named "claude-telegram-bot*"
      #    (e.g. ".../claude-telegram-bot/.venv/bin/python -m pytest") does not trip it.
      #  * a python interpreter (python, python3.x, or the framework "Python") running a
      #    main.py SCRIPT: require a python interpreter token (at start or after "/", followed
      #    by whitespace) AND a main.py argument at a path/arg boundary (preceded by
      #    start/space/"/", followed by end/space). The boundary excludes test_main.py /
      #    foo_main.py, and the interpreter test excludes editors (vim/nvim /x/main.py).
      if (cmd ~ /(^|[[:space:]]|\/)[Cc]laude-telegram-bot($|[[:space:]])/ \
          || cmd ~ /-m[[:space:]]+claude_tg/ \
          || (lc ~ /(^|\/)python[0-9.]*[[:space:]]/ \
              && lc ~ /(^|[[:space:]]|\/)main\.py($|[[:space:]])/)) {
        print pid
      }
    }
  '
}

if [ "$MODE" = "uninstall" ]; then
  uninstall
  exit 0
fi

# XML-escape a value before substituting it into the plist (NB). The plist is XML, so a
# WorkingDirectory / PATH / argv token containing & < > would otherwise produce an invalid
# plist that launchd rejects. Order matters: & first (so we don't double-escape the &
# introduced by < / >), then < and >. (" and ' are legal inside an XML element body, so
# they need no escaping here.)
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

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
  # NB: XML-escape each token before it goes into the plist <string> body.
  ARGV_XML+="        <string>$(xml_escape "$tok")</string>"$'\n'
done < <(resolve_bot_command)
ARGV_XML="${ARGV_XML%$'\n'}"

RESOLVED_PATH="$(resolve_path)"

render() {
  # Stream the template line-by-line; when we hit the single-token __BOT_COMMAND__
  # <string> line, emit the full (possibly multi-line) argv block instead, then
  # substitute the scalar placeholders.
  #
  # NB: XML-escape each scalar before it lands in the plist (the values are <string>
  # bodies). A WorkingDirectory / PATH with & < > would otherwise yield an invalid plist.
  # The substitution uses bash parameter expansion (literal replace), NOT sed: an escaped
  # value contains `&`, which sed would treat as "the whole match" in its replacement text,
  # re-corrupting the output. `${var//find/replace}` has no such metacharacter pitfalls and
  # is bash 3.2-safe.
  local wd path_v out_v err_v
  wd="$(xml_escape "$WORKDIR")"
  path_v="$(xml_escape "$RESOLVED_PATH")"
  out_v="$(xml_escape "$STDOUT_LOG")"
  err_v="$(xml_escape "$STDERR_LOG")"
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *'<string>__BOT_COMMAND__</string>'*)
        printf '%s\n' "$ARGV_XML"
        ;;
      *)
        line="${line//__WORKING_DIR__/$wd}"
        line="${line//__PATH__/$path_v}"
        line="${line//__STDOUT_LOG__/$out_v}"
        line="${line//__STDERR_LOG__/$err_v}"
        printf '%s\n' "$line"
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

# Reload cleanly if already installed (idempotent). Unloading FIRST stops the bot this
# agent manages, so the B2 guard below only sees pollers we don't control (i.e. manual ones).
if [ -f "$PLIST_DEST" ]; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  # launchctl unload returns before the child has fully exited; give it a beat so the
  # guard doesn't misread our own just-stopped agent as a manual poller.
  sleep 1
fi

# B2: one token == one poller. After unloading our own agent, anything still polling is a
# MANUAL bot (./run.sh / python main.py / python -m claude_tg). Loading the always-on agent
# now would put a SECOND poller on the same token → Telegram `Conflict`. Warn + abort and
# let the user stop it, rather than silently starting a conflicting poller.
MANUAL_PIDS="$(running_bot_pids || true)"
if [ -n "$MANUAL_PIDS" ]; then
  # shellcheck disable=SC2086
  PID_LIST="$(echo $MANUAL_PIDS | tr '\n' ' ')"
  echo "ERROR: a Claude Telegram bot is already running (PID(s): ${PID_LIST%% })." >&2
  echo "       Installing the keep-alive agent now would start a SECOND poller on the same" >&2
  echo "       token and Telegram would reject both with a 'Conflict'." >&2
  echo >&2
  echo "       Stop the running bot first, then re-run this installer:" >&2
  echo "         - if it's a foreground ./run.sh: press Ctrl-C in that terminal" >&2
  echo "         - otherwise: kill ${PID_LIST%% }" >&2
  echo >&2
  echo "       (The keep-alive agent must be the ONLY poller — see deploy/README.md.)" >&2
  exit 1
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
