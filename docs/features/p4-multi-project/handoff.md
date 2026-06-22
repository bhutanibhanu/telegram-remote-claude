# Feature Handoff: p4-multi-project

## Goal
Turn the single-session interactive remote into a **multi-project** one that **survives restarts** —
a per-chat registry of named projects (each its own cwd + Claude conversation) driven by
`/projects` `/new` `/switch` `/rm`, persisted and resumable, with SB2 on `/new` and RB3 fail-clean.
Streaming-only; one-shot stays the safe default. (P4 of the roadmap; ADR-004; D1–D8.)

## Files changed
```
 claude_tg/bot.py                           | 274 +++++++++-
 claude_tg/session_store.py                 | 459 +++++++++++++++-
 claude_tg/stream_session.py                | 497 +++++++++++++----
 docs/adr/ADR-004-multi-project-sessions.md | 170 ++++++
 docs/features/p4-multi-project/design.md   | 351 ++++++++++++
 docs/features/p4-multi-project/progress.md | 136 +++++
 docs/features/p4-multi-project/state.json  |   7 +
 docs/features/p4-multi-project/verify.md   | 107 ++++
 tests/test_bot_streaming.py                | 835 ++++++++++++++++++++++++++++-
 tests/test_multi_project.py                | 692 ++++++++++++++++++++++++
 tests/test_security_reliability.py         |  10 +-
 tests/test_session_store.py                | 470 +++++++++++++++-
 tests/test_stream_session.py               | 748 ++++++++++++++++++++++++--
 13 files changed, 4581 insertions(+), 175 deletions(-)
```
Three production files: `session_store.py` (schema v2 + migration + flat one-shot view + registry
CRUD + SB4), `stream_session.py` (per-active-project rework + SB2 cwd re-validation + RB3 notice),
`bot.py` (`/projects /new /switch /rm`, `/pwd`/`/cd` rework). Rest is the ADR, feature docs, tests.

## How to run
- Gates (from the worktree `.venv`): `pytest -q` (576 pass), `ruff check .`, `mypy claude_tg`,
  `python scripts/secret_scan.py`.
- Live (owner): set `ENGINE_MODE=streaming` + `STATE_FILE` + `ALLOWED_ROOTS`, `.venv/bin/python main.py`,
  then run `docs/features/p4-multi-project/verify.md` from the phone (one bot instance per token).

## Expected behavior
- `/new <name> <path>` confines the path to `ALLOWED_ROOTS` (SB2), requires an existing dir, validates
  the name (SB4), creates + auto-switches. `/projects` lists with the active marker; `/switch <name>`
  flips active; `/rm <name>` deletes a non-active project (active is protected).
- Two projects keep independent `(session_id, cwd)`; each turn resumes the ACTIVE project's session.
- A restart resumes every project (registry persisted, atomic + `0600`, schema v2). `/yolo` + grants
  are transient and reset to gating-ON on restart (SB5).
- `/switch` and `/new` are **refused while a turn is in flight** — load-bearing: a mid-hold active-
  project change would deadlock the parked answer-hold relay.
- A drifted cwd (now outside roots) is refused on the next turn (SB2 fail-closed). A failed resume
  falls back to a fresh session with an operator notice (RB3); an interrupted-at-crash turn comes
  back idle, no auto-replay, no hang.
- One-shot mode unchanged (the flat store view preserves the pre-P4 contract).

## Test plan
- **Automated (CI):** 576 tests. Store: v1→v2 migration (idempotent), flat-view one-shot regression
  (OLD-vs-NEW differential), corrupt/unknown-version fail-safe, registry CRUD, SB4 name matrix incl.
  `fullmatch` anti-regression. Streaming: per-project resume/persist, restart transient-reset, SB2
  cwd re-validation, resume-failure notice, RB3 interrupted-turn. Bot: each command happy+error, SB1,
  busy-guard (false-pass-checked), store-None RB1, `/new` SB2 (out-of-root/symlink/not-a-dir/dup,
  `ALLOW_ANY_PATH`). Integration (`test_multi_project.py`): two-projects-independent, restart-resumes-
  both, full lifecycle, **busy-guard during a real answer-hold then resolve** (the headline pin),
  atomicity, SB6 malformed-doc never-crash.
- **Manual (owner phone-verify, authoritative — not in CI):** `verify.md` (a)–(j) — the real PTB
  callback path + real-Claude per-project resume across a real restart.

## Known risks
- **`session_store.py` is the one change touching the live one-shot path** (the flat view over the
  active project). Pinned by the OLD-vs-NEW differential + one-shot regression tests, but it's the
  first place to look if one-shot misbehaves.
- **The busy-guard is load-bearing for relay correctness** (not just UX). If a future change lets the
  active project change mid-hold, the answer-hold relay deadlocks. Pinned by the integration test.
- **Auto-created `default` vs out-of-roots `workdir`** (flagged for owner): if `ALLOWED_ROOTS`
  excludes `workdir`, the auto-`default` project's first turn is refused (arguably correct SB2). The
  `from_env` default includes `workdir`, so the common case is fine.
- Real-Claude per-project resume (two distinct sessions, two cwds, across a real restart) is
  substrate behavior only mock-tested here — the phone-verify is the real proof.

## Open questions
- **Design refinement to confirm:** no-active-project AUTO-CREATES `default` at `workdir` (vs the
  design's "prompt /new"). Backward-compatible with P1–P3 UX; flagged for owner sign-off.
- Deferred (not in P4): persist-to-turn-captured-project-name (defense-in-depth; busy-guard makes it
  moot; P5's correlation envelope supersedes it); `/rename`; `/projects` switch-buttons; the pre-
  existing `/reset`-during-a-held-turn deadlock (`/cancel` is the escape hatch); the
  `ENGINE_MODE=streaming` default flip (owner's call).
