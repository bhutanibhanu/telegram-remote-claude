# Feature Handoff: p11-sessions (T1 + T2)

## Goal
Make the bot a control plane for **every** Claude Code session on the Mac — see all
sessions on the phone (even the live orchestrator), and attach/drive any of them — without
ever corrupting a session that is live elsewhere. (T3 live-mirror is deferred to a follow-up
cycle; this handoff covers T1 discovery + `/sessions` and T2 attach/fork.)

## Files changed
```
 claude_tg/bot.py                       | 139 +++++++   (/sessions, /attach, [Attach] callback, menu+HELP)
 claude_tg/engine/adapter_sdk.py        |  29 +-       (SdkSubstrate.resume(fork=) → ClaudeAgentOptions(fork_session))
 claude_tg/engine/engine.py             |  24 +-       (Engine.resume(fork=) threading)
 claude_tg/engine/substrate.py          |  11 +-       (Substrate protocol resume(fork=))
 claude_tg/render.py                    | 269 +++++++++++++-  (sessions_listing, attach callback codec, keyboard)
 claude_tg/sessions_discovery.py        | 660 +++++++++++++++++++++++++++++++++  (NEW — discovery adapter + composite liveness)
 claude_tg/stream_session.py            | 446 +++++++++++++++++++++-  (attach_session, _ensure_engine fork, AttachOutcome)
 tests/test_bot_streaming.py            | 262 ++++++++++++-
 tests/test_engine.py                   |  54 ++-
 tests/test_render.py                   | 200 ++++++++++
 tests/test_sessions_discovery.py       | 394 ++++++++++++++++++++  (NEW)
 tests/test_stream_session.py           | 425 +++++++++++++++++++++
 15 files changed, ~2960 insertions(+)
```

## How to run
- Gates (worktree `.venv`): `pytest -q` (1168 pass), `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`.
- Live (streaming): `ENGINE_MODE=streaming .venv/bin/python main.py` then in Telegram:
  - `/sessions` → lists every discoverable Claude session on the Mac (incl. this bot's own + the live orchestrator), short id + `<code>`-wrapped cwd + title + relative age + 🟢/⚪ live/idle, bot-known ones marked `✓ <name>`, active marked `→`; tappable `[📎 Attach]` per row.
  - `/attach <session-id>` (or tap `[Attach]`) → adopts that session as a project and switches to it; the next message resumes + drives it.

## Expected behavior
- **Discovery (T1):** `/sessions` enumerates `~/.claude/projects/<cwd>/<id>.jsonl` via the SDK (`list_sessions`, re-exported from `_internal`, **pinned** `==0.2.105`, isolated in `sessions_discovery.py`). Liveness is composite: transcript mtime < ~180s OR a `ps` `claude … (--resume|--session-id) <id>`/`stream-json` proc OR a validated `~/.claude/sessions/<pid>.json` entry. **The live orchestrator (bare `claude --continue`, no id in argv) is detected via the registry, pid validated by `lstart`→epoch** (TZ-robust; a naive `procStart`-string compare was wrong by +4h on this Mac — fixed). Body-free (title/first-prompt truncated + HTML-escaped; never transcript bodies). RB1: a missing/odd `~/.claude` or an SDK/`ps` failure → clean empty list, never crashes.
- **Attach (T2) — the never-co-drive guarantee:**
  - IDLE target → continue the same id (`fork_session=False`).
  - LIVE-elsewhere target → **FORK** (`fork_session=True` → new id, base transcript untouched) + operator told "active elsewhere — attached a fork so I don't corrupt it".
  - **Liveness UNCERTAIN** (the probe itself errored — `liveness_degraded`) → **FORK on doubt** (the write-path safe default) + operator told "couldn't confirm idle, forked rather than risk co-driving". (A *missing* transcript = confident no-signal = NOT degraded; only a real probe exception forks-on-doubt.)
  - The forked id arrives on the first turn and is what gets persisted — the base id is **never** seeded/written.
- **SB2:** the discovered cwd (can be anywhere on the Mac) is canonicalized (`resolve_within_roots`, resolve-before-check) and confined to `ALLOWED_ROOTS`; out-of-root (with `ALLOW_ANY_PATH` off) is **refused, nothing adopted**.
- **SB1:** `/attach` rides the allowed-chat filter + `_ok`; the `[Attach]` callback rechecks `_authorized` before any adopt; the callback id is pattern-bounded (`[A-Za-z0-9-]{1,48}`; injection/over-long → rejected).
- Works in both modes: discovery is machine-wide; in one-shot the bot-project annotation is just empty and there's no attach keyboard.

## Test plan
- **Automated (1168, +70 over the 1098 floor):** discovery mapping + each liveness signal independently + the OR + the orchestrator no-id/registry-epoch case + RB1 across every seam; merge/dedup-by-id with bot projects; SB3 truncate+escape; attach idle→continue / live→fork / **uncertain→fork** / out-of-root→refused / unknown-id→clean / SB1 (cmd+callback) / appears-in-/projects / drives-a-turn-persisting-the-forked-id-not-the-base; engine `fork_session` threading + no-base-seed; callback codec round-trip/rejection.
- **Mutation probes (in-suite + independent reviewer, all fail-closed):** flip fork→False (co-drive) fails; bypass `resolve_within_roots` (out-of-root adopt) fails; ignore `liveness_degraded` (drop fork-on-doubt) fails.
- **Manual (live-verify, this phase):** `/sessions` shows the live orchestrator as 🟢; attach a live session → observe the FORK + the operator notice; out-of-root refusal; the adopted session drives a normal turn through the permission gate.

## Known risks
- The SDK discovery APIs are `_internal` (re-exported) → **pinned**, isolated in one adapter; an SDK bump must re-verify `list_sessions`/`SDKSessionInfo` shape.
- Liveness is a *hint* — an idle-attached session that goes live in another process between attach and the first resume is the one inherent (design-accepted, design.md:12) narrow co-drive window; the fork-on-doubt default covers probe failure, not this race.
- Independent reviewer cosmetic note: re-attaching the *base* id after a fork-turn makes a second forked project (still safe — forks again, never co-drives; just a duplicate project entry).

## Open questions
- T3 live-mirror (`/watch <id>`) deferred — build as its own cycle (SB3-scrub raw tool bodies, send-queue flood control). Tracked in progress.md as `[~]`.
