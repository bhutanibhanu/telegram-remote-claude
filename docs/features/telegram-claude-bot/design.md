# Design: Telegram → Claude Code bot

## Goal
Let the user control Claude Code on their Mac from their phone via a Telegram bot
(phone → bot → `claude -p` → reply), securely and autonomously.

## Requirements
- Telegram message → run through Claude Code on the Mac → reply in Telegram.
- Claude runs with `--dangerously-skip-permissions` (no approval prompts).
- Security: only respond to allowlisted chat id(s); ignore everyone else. Token + ids from `.env`.
- Long replies chunked to Telegram's 4096-char limit; progress indicator while Claude runs.
- Per-chat working directory; solid error handling; README + `.env.example`.

## Approach
- **Transport:** `python-telegram-bot` (v21, async) long-polling.
- **Claude:** shell out to `claude -p --output-format json --dangerously-skip-permissions`,
  prompt via stdin; parse `result` + `session_id`. Resume per-chat with `--resume <session_id>`.
  Uses the user's existing Claude Code auth — no API key.
- **State:** in-memory per-chat session id + cwd, optionally persisted to JSON.
- **Concurrency:** per-chat async lock; reject overlapping turns (`ClaudeBusy`) so a chat's
  resumed session is never run twice at once.
- **Safety:** subprocess via exec arg-list (no shell); prompt over stdin (no arg injection);
  allowlist enforced via PTB chat filter **and** an in-handler check.

## Non-goals (v1)
- Streaming partial output (typing indicator only).
- Multi-user roles, rate limiting, launchd packaging.

## Key decisions
- Plain-text replies (not Markdown) to avoid parse errors on arbitrary Claude output.
- `_invoke` isolated for testability (subprocess mocked in unit tests).
