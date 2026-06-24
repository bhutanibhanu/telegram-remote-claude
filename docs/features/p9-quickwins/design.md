# P9 — Quick Wins & Foundations

_First phase of roadmap-v2 (see `docs/roadmap-v2.md`). Delta on shipped P0–P8 (`main`). Low-risk, high value/effort improvements that make the bot immediately nicer AND set up discoverability/observability for the bigger phases (P10 multimodal, P11 session control plane). No SDK-capability uncertainty — all six features are local app code._

## Problem
P0–P8 shipped a powerful bot, but day-to-day ergonomics have cheap wins: commands are discoverable only via a wall-of-text `/help`; no at-a-glance health/cost view; every turn uses one global model; repeated prompts must be re-typed; notifications are chatty.

## Success
- Commands appear in Telegram's native `/` menu; first-time user gets a short welcome.
- `/status` gives an at-a-glance health view; turn cost/usage is visible (data already captured today, just discarded).
- Pick fast vs deep model per turn; save+replay common prompts; notifications compact + tappable.
- Gates green (875 floor); no change to the core turn/permission/concurrency engine. Each feature live-verified.

## Anti-goals
No engine/permission/concurrency changes. No new SDK capabilities. No multi-user. Keep SB1/SB2/SB3 (authn on every new command + callback; body-free).

## Features (each ~one task)
- **T1 — Command menu + onboarding:** `setMyCommands` at startup (real command list + descriptions); one-time welcome on a chat's first message. Authn-gated.
- **T2 — `/status` health:** uptime, ENGINE_MODE, gate/yolo, active vs MAX_CONCURRENT_RUNS, per-project status (reuse `/projects` internals), last-error. Body-free, paths in `<code>`. SB1.
- **T3 — Cost/usage:** surface `ResultEvent.total_cost_usd`+`num_turns` (currently dropped) in the done-footer; persist per-project cumulative cost; show in `/status`. Omit gracefully if absent.
- **T4 — Model routing `/fast`·`/deep`:** per-project model override threaded into `ClaudeAgentOptions` (streaming) / `-p --model` (oneshot); applies next turn; shown in `/status`; invalid→default.
- **T5 — Macros `/save`·`/run`:** store prompt templates per-chat in the session store (atomic+0600, SB4 name); `/run name [args]` expands (`$1`/`$*`) + fires as a turn; `/macros`, `/unsave`. Persists (RB6); unknown→clean (RB2/RB1).
- **T6 — Notification polish + chips:** suppress link previews; `[Open <project>]` switch button on attention/done; "(N more waiting)" queued counter; scoped quick-reply chips on free-text prompts. Body-free (SB3).

## Build order
T1 → T2 → T3 (feeds T2) → T4 → T5 → T6 → verify + Codex QA + live-verify + merge.

## Inherited facts / constraints
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`. Floor: 875 tests.
- Every new command gates on `_authorized` (SB1); paths wrapped `<code>`; HTML-escaped if HTML mode; notifications body-free (SB3); store writes atomic+0600 (RB6); never crash (RB1) / fail clean (RB2).
- Per-phase pipeline: supervised build (Implementer + reviewer) → Codex QA → live phone-verify → merge. One bot per token; don't rotate the token mid-run.
