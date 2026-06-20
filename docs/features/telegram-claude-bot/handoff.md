# Feature Handoff: telegram-claude-bot

## Goal
Control Claude Code on the Mac from a phone via a Telegram bot (phone → bot → `claude -p` → reply).

## Files changed
New project. Key files:
- `claude_tg/config.py` — `.env`/env config (token, allowlist, workdir, model, timeout, skip_permissions, state file).
- `claude_tg/claude_runner.py` — runs `claude -p --output-format json --dangerously-skip-permissions [--resume <id>]`, prompt via stdin; per-chat session continuity; expired-session auto-retry; per-chat busy lock; timeout/error handling.
- `claude_tg/session_store.py` — optional atomic JSON persistence of per-chat session id + cwd.
- `claude_tg/bot.py` — python-telegram-bot transport: allowlist (filter + guard), `/help /reset /pwd /cd`, typing indicator, chunked replies, error handler.
- `claude_tg/util.py` — UTF-16-aware message chunking (Telegram 4096 limit).
- `main.py`, `run.sh` — entry point.
- `tests/` — 49 tests (config, util, session_store, claude_runner, bot).

## How to run
```bash
cd ~/dev/claude-telegram-bot
cp .env.example .env   # set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_CHAT_IDS
./run.sh
```
Test: `source .venv/bin/activate && pytest`

## Expected behavior
- Only allowlisted chat ids are served; all others ignored.
- Each chat = one resumable Claude session; `/reset` clears it; context persists across restarts (if `CLAUDE_STATE_FILE` set).
- Claude runs with permissions bypassed (no prompts). Replies chunked to Telegram's limit; "typing…" shown while working.
- Errors/timeouts return a clean message; the bot never crashes on bad input.

## Test plan
- Automated (49 tests): config parsing/validation; UTF-16 lossless chunking; session store; runner cmd-building, session resume, expired-session retry, error/timeout/busy/parse paths; bot auth/ignore, chunking, commands, busy+typing-stop, empty-output notice.
- Manual (needs token): create bot via @BotFather, set `.env`, `./run.sh`, message from phone.

## Known risks
- `--dangerously-skip-permissions`: Claude runs commands without asking — mitigated by the chat-id allowlist + secret token (documented in README Security).
- `/cd` is unconfined (any dir) — within the documented trust model (owner already has full control).

## Open questions
- Streaming partial output (currently typing-indicator only) — deferred.
- launchd packaging for always-on — deferred (README notes tmux/nohup).
