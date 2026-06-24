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
- **Attach (T2) — the never-co-drive guarantee. The BINDING fork-vs-continue decision is made at the FIRST WRITE, not at attach time, and is persisted so it survives a restart:**
  - On `/attach`, the session is adopted as an active project pinned to the base `(session_id, cwd)` and a **persisted `fork_pending` marker** is written (the store DOES persist the base id transiently — see below). The attach reply does **not** promise an outcome; it says it will "resume — forking automatically if it's active elsewhere, so I never corrupt a live session." The binding decision is deferred to the first message.
  - At the **first resume** of a `fork_pending` project (`_ensure_engine`), the base id's **current** liveness is **re-probed** (`SessionDiscovery.probe_one(id, cwd) → (running, degraded)`, a fresh composite ps/registry/mtime snapshot):
    - re-probe **IDLE & confident** → continue the same id (`fork_session=False`);
    - re-probe **LIVE** → **FORK** (`fork_session=True` → new id, base transcript untouched);
    - re-probe **UNCERTAIN** (a signal couldn't be gathered — `degraded`) → **FORK on doubt** (the write-path safe default).
  - This closes the restart gap (the in-memory intent is gone after a restart, but the persisted `fork_pending` re-triggers the re-probe → re-decides instead of co-driving) **and** the attach→first-write race (an idle-at-attach session that has since gone live is caught by the re-probe at the write).
  - **Re. the base id:** the store **does** persist the base `session_id` on the adopted project transiently — it is the resume target until the first turn completes. The SDK *adapter* avoids seeding the base id into `self.session_id` on a fork (the forked id arrives on the first turn and is what `_persist` then writes), and `fork_pending` is **cleared (persisted) only after the first successful turn** so the now-forked/continued id (ours alone) is what every subsequent resume continues — never re-forking. (Corrects the earlier "base id never seeded/written" claim, which conflated the adapter's no-seed with the store's transient persist.)
  - The operator is told the ACTUAL outcome when the first turn forks (a fork notice), not over-promised at attach.
- **SB2:** the discovered cwd (can be anywhere on the Mac) is canonicalized (`resolve_within_roots`, resolve-before-check) and confined to `ALLOWED_ROOTS`; out-of-root (with `ALLOW_ANY_PATH` off) is **refused, nothing adopted**.
- **SB1:** `/attach` rides the allowed-chat filter + `_ok`; the `[Attach]` callback rechecks `_authorized` before any adopt; the callback id is pattern-bounded (`[A-Za-z0-9-]{1,48}`; injection/over-long → rejected).
- Works in both modes: discovery is machine-wide; in one-shot the bot-project annotation is just empty and there's no attach keyboard.

## Test plan
- **Automated (1191):** discovery mapping + each liveness signal independently + the OR + the orchestrator no-id/registry-epoch case + RB1 across every seam; **B1 — degradation surfaced from INSIDE the scans (swallowed ps nonzero/timeout + corrupt registry entry → `liveness_degraded`)**; dedup-by-id; merge with bot projects; SB3 truncate+escape; attach mechanics (lookup / SB2 / naming / idempotency / persisted `fork_pending`); **the BINDING first-write re-probe — idle→continue / live→fork / uncertain→fork**, the **B2 restart** (recreate the session from the same store before the first turn → still forks) and **B3 race** (idle-at-attach → live-at-first-write → forks) cases, the forked id persisted (not the base), and `fork_pending` cleared after the first clean turn (no re-fork on the 2nd turn); SB1 (cmd+callback); appears-in-/projects; engine `fork_session` threading + no-base-seed; callback codec round-trip/rejection; store `set/get_fork_pending` round-trip (persists across a fresh store instance).
- **Mutation probes (in-suite, all fail-closed):** (a) freeze the decision at attach / skip the first-write re-probe → the B2 restart + B3 race + first-write-fork tests fail; (b) ignore the swallowed-scan-failure degraded signal → the B1 tests fail; (c) flip fork→continue at first write → the live-at-write test fails; (d) bypass `resolve_within_roots` (out-of-root adopt) → the SB2 test fails. Each passes when restored.
- **Manual (live-verify, this phase):** `/sessions` shows the live orchestrator as 🟢; attach a live session → observe the FORK + the operator notice; out-of-root refusal; the adopted session drives a normal turn through the permission gate.

## Known risks
- The SDK discovery APIs are `_internal` (re-exported) → **pinned**, isolated in one adapter; an SDK bump must re-verify `list_sessions`/`SDKSessionInfo` shape.
- Liveness is a *hint* — an idle-attached session that goes live in another process between attach and the first resume is the one inherent (design-accepted, design.md:12) narrow co-drive window; the fork-on-doubt default covers probe failure, not this race.
- Independent reviewer cosmetic note: re-attaching the *base* id after a fork-turn makes a second forked project (still safe — forks again, never co-drives; just a duplicate project entry).

## Open questions
- T3 live-mirror (`/watch <id>`) deferred — build as its own cycle (SB3-scrub raw tool bodies, send-queue flood control). Tracked in progress.md as `[~]`.
