# Claude Telegram Bot

Control **Claude Code** on your Mac from your phone via **Telegram**.

```
You (Telegram)  ──▶  this bot (on your Mac)  ──▶  claude -p ...  ──▶  reply back to Telegram
```

- Each Telegram chat maps to one resumable Claude session (context is kept; `/reset` to clear).
- **Tool use requires your approval by default.** In streaming mode (`ENGINE_MODE=streaming`) you get an Allow/Deny prompt in Telegram before Claude runs a risky tool, and file/search tools are confined to `ALLOWED_ROOTS` (an out-of-root path prompts too). The old allow-all behavior is an explicit, loud opt-in (`CLAUDE_SKIP_PERMISSIONS=true`) — **off by default**.
- Only **allowlisted chat ids** can talk to the bot; everyone else is ignored.

## Requirements

- macOS with **Claude Code already installed and logged in** (`claude` on your PATH — test with `claude -p "hi"`).
- Python 3.11+.

## Setup

```bash
cd ~/dev/claude-telegram-bot
cp .env.example .env        # then edit .env (see next section)
./run.sh                    # creates venv, installs deps, starts the bot
```

## 📱 Phone side (do this part yourself)

1. **Create the bot:** in Telegram, message **@BotFather** → `/newbot` → pick a name/username → copy the **token**.
2. **Get your chat id:** message **@userinfobot** → it replies with your numeric **Id**.
3. **Fill `.env`:**
   ```ini
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_ALLOWED_CHAT_IDS=<your id from userinfobot>
   CLAUDE_WORKDIR=/Users/ray/dev      # where Claude starts (changeable with /cd)
   ```
4. **Start it on the Mac:** `./run.sh`
5. **Message your bot** from your phone. Send `/help` first.

## Commands

| Command | What it does |
|---|---|
| *(any text)* | Sent to Claude Code; the reply comes back in chat |
| `/help` | Show help |
| `/reset` | Start a fresh Claude session (drop context) |
| `/pwd` | Show the current working directory |
| `/cd <path>` | Change the working directory for this chat |

Long replies are split into Telegram-sized chunks automatically; a "typing…" indicator shows while Claude works.

## Configuration (`.env`)

| Key | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | From @BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | ✅ | — | Comma-separated numeric ids |
| `CLAUDE_WORKDIR` | | home dir | Default working directory |
| `CLAUDE_BIN` | | `claude` | Path to the claude binary |
| `CLAUDE_MODEL` | | Claude default | e.g. `claude-opus-4-8` |
| `CLAUDE_TIMEOUT_SECONDS` | | `600` | Per-turn timeout |
| `CLAUDE_SKIP_PERMISSIONS` | | `false` | **Allow-all bypass — OFF by default** (gate ON). `true` = Claude runs every tool with no approval prompt (loud WARNING at startup). Prefer streaming mode + per-tool approval instead. |
| `ENGINE_MODE` | | `oneshot` | `streaming` enables interactive Telegram approval prompts, multi-project sessions, and concurrency (recommended). |
| `ALLOWED_ROOTS` | | `CLAUDE_WORKDIR` | Comma-separated roots that `/cd`, `/new`, **and Claude's file/search tools** are confined to (out-of-root tool use prompts for approval). |
| `ALLOW_ANY_PATH` | | `false` | `true` disables path confinement entirely (you take the wheel). |
| `CLAUDE_STATE_FILE` | | none | Persist sessions/cwd across restarts |

## Security ⚠️

This bot lets a Telegram chat run Claude Code on your Mac — it can edit files and run shell
commands. The defences, in layers:

- **Approval gate (on by default).** In streaming mode you approve each risky tool via a Telegram Allow/Deny prompt; safe reads/searches inside your roots run automatically. The allow-all bypass (`CLAUDE_SKIP_PERMISSIONS=true`) is off by default and warns loudly at startup.
- **Path confinement.** `/cd`, `/new`, **and Claude's own file/search tools** are confined to `ALLOWED_ROOTS` (defaults to `CLAUDE_WORKDIR`); a tool targeting a path outside the roots prompts for approval. Set a narrow `ALLOWED_ROOTS`. (`Bash` commands are not statically path-checked — grant `Bash` only when you mean it.)
- **Allowlist.** Only your **allowlisted chat id(s)** are served (on every message *and* button tap); all other chats are ignored.
- **Keep `TELEGRAM_BOT_TOKEN` secret** (it's git-ignored; never commit `.env`). Anyone who obtains your token **and** is on the allowlist could control your Mac — treat it like an SSH key.
- Run it on a machine/working dir you trust.

## Keeping it running

`./run.sh` runs in the foreground. To keep it alive after you close the terminal, use `tmux`,
`nohup ./run.sh &`, or a `launchd` agent. (A `launchd` plist is a reasonable future addition.)

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Architecture: `claude_tg/config.py` (env), `claude_runner.py` (subprocess + per-chat
sessions), `session_store.py` (optional persistence), `bot.py` (Telegram transport),
`util.py` (chunking). The Claude subprocess call is isolated in `ClaudeRunner._invoke` so the
logic is unit-tested without invoking Claude or the network.
