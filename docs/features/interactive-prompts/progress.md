# Progress: interactive-prompts (P3)

_Plan generated 2026-06-21 from design.md · 2 tasks · autonomous supervised build_

> **P3 — interactive prompts / run skills from the phone.** The headline workflow: type `/grill` (or
> `/pipeline`, `/scaffold`, …) in Telegram and the bot runs that skill **in the live Claude session**. P1
> already shipped the interactive-tool relay (AskUserQuestion → buttons + "Other"; ExitPlanMode →
> Approve/Reject; free-text reply; async answer-hold + backstop + `/cancel`; coalesced rendering) and P2
> added per-tool permission gating. **The only missing piece is a way to LAUNCH a skill** — today an
> unregistered slash-command matches no handler and is silently dropped. P3 adds that one path and verifies
> the full loop composes. It reuses P1 + P2 wholesale; it does not rebuild them. Builds on P2
> (`feat/permission-gating`, tip `dd9375d`) — this worktree carries the full P1 + P2 code.
>
> Decisions (G-Scope, design.md): **D1** launch = passthrough (any non-bot slash-command forwarded verbatim
> to the session; bot commands take precedence) · **D2** unknown/typo'd command → forward, let Claude handle
> (no allowlist) · **D3** full-loop verify scope = `/grill` only (`/pipeline` covered-by-mechanism).

## Cross-cutting acceptance (applies where relevant)
- **SB1** — the launch path is a new inbound surface and MUST be allowlist-checked: a non-allowlisted /
  forged command can **never** start a skill (reuses the same `_ok` recheck + `allowed` filter as every
  other handler) — T1, re-confirmed live T2.
- **RB1** — a malformed / empty / garbage slash-command never crashes the bot (defensive handler; forwarded
  as an ordinary turn or no-op) — T1.
- **ADR-001 C5** — a slash-command skill is invocable in-session; the passthrough realizes C5 without new
  capability (the operator could already send free text; any risky tool the skill attempts is still gated
  by P2) — T1/T2.
- Everything else (SB2–SB6, RB2/RB4/RB5) is **inherited unchanged** from P1/P2 — P3 adds no new risky
  surface beyond the launch. Regression floor: **342** P1+P2 tests.

## Task list
- [ ] T1 — Skill-launch passthrough (`on_skill_command` in `bot.py`) · unit
- [ ] T2 — Live `/grill` end-to-end verify + owner phone checklist · live probe

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked

## Tasks

### T1 — Skill-launch passthrough
- **Goal:** Add one `on_skill_command` handler to `bot.py` that forwards any *unregistered* slash-command
  verbatim to the active session's turn path (the same path `on_message` uses), so `/grill`, `/pipeline`, …
  start as skills in-session; the bot's own commands take precedence; the new surface is SB1-guarded.
- **Depends on:** none
- **Files (expected):** `claude_tg/bot.py` (new `on_skill_command`, its `add_handler` registration, `HELP_TEXT`
  line) + its test module. **Relay (`render.py`/`stream_session.py`/engine), gating (`permissions.py`), and
  the one-shot runner are untouched.**
- **Acceptance:**
  - WHEN an allowlisted chat sends a slash-command that is **not** a registered bot command (`/start /help
    /reset /cancel /pwd /cd /yolo /unyolo`), the system SHALL forward `update.message.text` **verbatim**
    (command + args, leading `/` intact) to the active session via the same turn path `on_message` uses
    (streaming → the streaming session's message handling → `engine.send`; one-shot → `runner.run`).
  - WHEN an allowlisted chat sends a **registered** bot command (e.g. `/reset`, `/cd /tmp`), the system SHALL
    handle it via its own `CommandHandler` and SHALL NOT forward it through the passthrough (PTB
    first-match-wins; the passthrough `MessageHandler(allowed & filters.COMMAND)` is registered **after** the
    specific `CommandHandler`s).
  - WHEN a **non-allowlisted** chat sends any slash-command, the system SHALL NOT launch a skill (SB1 — the
    `allowed` filter + the same `_ok` recheck used by other handlers; no session call occurs).
  - WHEN the command text is empty / whitespace / malformed (no message text, `/` only, unicode garbage),
    the handler SHALL NOT raise (RB1) — it forwards as an ordinary turn or no-ops, leaving the session usable.
  - `HELP_TEXT` SHALL note that other slash-commands run as skills in the Claude session.
- **Tests:** unit, substrate mocked. Assert: (a) a non-bot command forwards the verbatim text to the session
  turn path; (b) a registered bot command is NOT intercepted by the passthrough (its own handler runs); (c) a
  non-allowlisted chat triggers no session call (SB1); (d) empty/`/`-only/garbage never raises and leaves the
  session usable (RB1); (e) **both** one-shot and streaming engine modes route the forwarded command. Test
  behavior, not implementation detail.
- **Status:** todo

### T2 — Live `/grill` end-to-end verify + owner phone checklist
- **Goal:** Prove the launch path composes the full loop **live** — a `/grill` run from a chat reaches the
  session and drives P1's relay + P2's gating to a written design doc — and hand the owner a phone-verify
  checklist for P3 acceptance.
- **Depends on:** T1
- **Files (expected):** `spikes/p3-skill-launch-verify/` (throwaway, contained; `--mock` self-test +
  live mode mirroring `spikes/p2-permission-verify/verify_permissions.py`); `docs/features/interactive-prompts/verify.md`.
  No production code.
- **Acceptance:**
  - WHEN the harness runs live and a non-bot slash-command (`/grill`) is sent through the passthrough, it
    SHALL reach the real engine (`engine.send` receives the verbatim command) and a complete `/grill` loop
    SHALL run to a **written design doc**, with questions surfaced as relay prompts and any file write gated
    by P2 — verdict + scrubbed transcript. **(live probe; not in CI)**
  - The harness SHALL also assert a **bot command still wins** (a `/reset`-style registered command is not
    forwarded) and a **non-allowlisted** sender cannot launch (SB1), at whatever layer the harness can drive.
  - **Containment (the live-verify lesson):** the launched skill's file writes SHALL be directed to an
    **absolute temp path** outside the repo; cleanup SHALL **sweep `$HOME` and `~/.claude`** for the harness's
    generated filenames and flag/remove any stray (an *allowed* tool is NOT sandboxed — ADR-001: cwd is not
    an OS boundary). Repo unchanged after the run.
  - No API key; host CLI auth; effects contained; evidence scrubbed (SB3). A `--mock` mode self-tests
    deterministically without network.
  - The owner checklist (`verify.md`) SHALL give exact phone steps under `ENGINE_MODE=streaming`: launch a
    skill, answer its questions via buttons / "Other", see it finish to a doc; confirm bot commands still
    win; confirm a non-allowlisted chat can't launch.
- **Tests:** none — the live verdict + scrubbed transcript + `--mock` self-test + owner checklist are the
  artifacts.
- **Status:** todo

## Rules
- **Flag-gated, branch-only.** All work on `feat/interactive-prompts`; **never merge to main**; the live
  one-shot bot keeps running unchanged (one-shot remains the safe default; no `ENGINE_MODE` flip in P3).
- **Reuse, don't rebuild.** P1's relay + answer-hold + backstop + cancel + coalesced rendering and P2's
  per-tool gating are inherited verbatim — P3 adds **only** the launch passthrough on top. **Only `bot.py`
  changes in production code (T1).**
- **Anti-goals:** skill discovery / menu UI; per-skill bot commands; argument validation; multi-project (P4);
  concurrency (P5); crash-recovery (P4); any new permission posture.
- **No ADR** — the launch is a straightforward, fully-reversible wiring choice captured by D1–D3 in design.md.
- **Substrate mocked in unit tests;** the live probe (T2) is isolated, contained, scrubbed, no API key.
- **`progress.md` is the single source of task truth;** `state.json` tracks the phase.
- **Build method:** autonomous supervised — per task, an isolated Implementer subagent writes code+tests and
  runs the gates (does NOT commit); the orchestrator re-runs gates + reads the security-critical code, then a
  fresh independent reviewer subagent probes the tests; **auto-commit on green gates + reviewer AGREE**. Two
  commits per task (`feat(...)`/`spike(...)`/`test(...)` then `build(...): tick TX done (<sha>)`); push after
  each. Clean single-line messages, **no Co-Authored-By trailer**. Gates (from the worktree `.venv`):
  `pytest` (floor 342), `ruff check .`, `mypy`, `python scripts/secret_scan.py`.
