# Claude Telegram Bot

Control **Claude Code** on your Mac from your phone via **Telegram**.

```
You (Telegram)  ──▶  this bot (on your Mac)  ──▶  claude -p ...  ──▶  reply back to Telegram
```

- Each Telegram chat maps to one resumable Claude session (context is kept; `/reset` to clear).
- Claude runs with `--dangerously-skip-permissions` so it executes without approval prompts
  (see **Security** below — this is why the allowlist matters).
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
| `CLAUDE_SKIP_PERMISSIONS` | | `true` | Bypass Claude permission prompts |
| `CLAUDE_STATE_FILE` | | none | Persist sessions/cwd across restarts |

## Security ⚠️

This bot lets a Telegram chat run Claude Code on your Mac **with permissions bypassed** — i.e.
Claude can edit files and run shell commands without asking. Protect it:

- **Keep `TELEGRAM_BOT_TOKEN` secret** (it's git-ignored; never commit `.env`).
- Only your **allowlisted chat id(s)** are served; all other chats are ignored.
- Run it on a machine/working dir you trust. Consider a narrower `CLAUDE_WORKDIR`.
- Anyone who obtains your token **and** is on the allowlist could control your Mac — treat it like an SSH key.

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
