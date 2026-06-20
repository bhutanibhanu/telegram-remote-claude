# Progress: Telegram → Claude Code bot

- [x] Scaffold repo, venv, deps (python-telegram-bot 21, pytest)
- [x] Config loader (`.env`, allowlist, defaults, validation)
- [x] Chunking util (lossless, Telegram-safe)
- [x] Claude runner (subprocess, json parse, per-chat resume, timeout, errors, busy-lock)
- [x] Optional session/cwd persistence (atomic JSON)
- [x] Telegram transport (allowlist filter + guard, commands, typing indicator, chunked replies, error handler)
- [x] Entry point (`main.py`, `run.sh`)
- [x] Unit tests (config, util, session_store, claude_runner) — 30 passing
- [x] README + `.env.example` + design doc
- [ ] Subagent review loop (correctness / security / edge-cases / run-it) → fix blockers
- [ ] Codex QA
- [ ] Phone-side setup by user (BotFather token + chat id)

## Acceptance criteria
- `pytest` green.
- Only allowlisted chats served; others ignored.
- Claude invoked with `--dangerously-skip-permissions`; context persists per chat; `/reset` clears it.
- Replies > 4096 chars are chunked; errors/timeouts return a clean message, never crash the bot.
