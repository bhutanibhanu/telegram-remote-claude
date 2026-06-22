# Progress: interactive-prompts (P3)

_Plan generated 2026-06-21 from design.md · 2 tasks · autonomous supervised build_

> **✅ P3 COMPLETE (2026-06-21).** T1–T2 done, committed, and pushed on `feat/interactive-prompts` (stacked on
> P2's `feat/permission-gating`, which carries P1+P2). The live `/grill` end-to-end verify (T2) is **PASS**
> against real Claude: typing `/grill` (an unregistered slash-command) is forwarded **verbatim** to the
> session and **launches the skill** — 2 AskUserQuestion rounds surfaced + answered, a `Write` gated +
> allowed, run to a written brief, all contained. The headline ("run `/grill` from the phone") works: P1's
> relay + P2's gating **compose** under the P3 launch path. Remaining before cutover: owner phone-verify per
> [`verify.md`](verify.md), then flip `ENGINE_MODE=streaming` (default stays `oneshot` — the safe live path).

> **🔧 Owner phone-verify (2026-06-21) surfaced + fixed real relay bugs** — none caught by the programmatic
> probes (T2/P1-T9), which all drove `engine.resolve` directly and bypassed the real Telegram
> update/callback path. **(1) Deadlock:** the answer-hold parks a turn handler awaiting the operator's tap,
> but PTB processed updates **sequentially**, so the tap queued behind the parked turn → neither progressed.
> Fixed: `concurrent_updates(True)` in `build_application`. **(2) Multi-question asks:** a single
> `AskUserQuestion` carries several questions under one `tool_use_id`; the first tap resolved the whole ask
> with a 1-of-N answer map (stranding the rest). Fixed: answers **accumulate** and resolve once all are
> answered, and each question renders as its **own message + keyboard** (not one stacked wall of buttons).
> **(3) Status spam:** identical status edits hit Telegram "message not modified" → the fallback re-sent a
> fresh message every interval. Fixed: skip identical edits; calm stable wording ("💭 Claude is thinking…").
> All three with regression tests (relay end-to-end via real PTB is otherwise untested); full suite green.

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
- [x] T1 — Skill-launch passthrough (`on_skill_command` in `bot.py`) · unit (52fb42d)
- [x] T2 — Live `/grill` end-to-end verify + owner phone checklist · live probe (d6b13ca)

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
- **Status:** done (52fb42d) — one new `on_skill_command` handler in `bot.py` forwards an unregistered
  slash-command verbatim (leading `/` + args intact) via a shared `_run_turn` extracted from `on_message`
  (behavior-preserving no-op refactor — only a local `text`→`reply` rename). Registered as
  `MessageHandler(allowed & filters.COMMAND, …)` **after** the `CommandHandler`s, so PTB first-match-wins
  lets the bot's own commands win (a real `/reset` is never forwarded). **SB1** defense in depth: the
  `allowed` chat filter on the registration **and** the `_ok` recheck as the handler's first line — a
  non-allowlisted chat reaches neither runner nor streaming session. **RB1**: missing message / `None` /
  empty / whitespace text no-op; `/`-only + unicode garbage forward as ordinary turns; never raises.
  `HELP_TEXT` updated. **19 unit tests** (verbatim forward one-shot + streaming; all 8 reserved commands
  win via PTB's **real** `check_update` over the actual handler list; unauthorized → no session call in
  both modes + dropped at the routing layer; empty/whitespace/`/`-only/garbage/missing never raise + session
  still usable). Gates green from `.venv` (**361 passed**, ruff/mypy/secret-scan clean — above the 342
  floor). Independent reviewer **AGREE** with 3 load-bearing mutation probes (guard removed → SB1 tests fail;
  registration reordered → bot-commands-win tests fail; empty-text guard removed → RB1 tests fail). **Only
  `claude_tg/bot.py` changed in production** (+47/−3); relay/gating/runner untouched.

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
- **Status:** done (d6b13ca) — `spikes/p3-skill-launch-verify/verify_skill_launch.py` drives the launch path
  end-to-end. `--mock` self-test: **launch-smoke** (the REAL bot + REAL `StreamingSession` over a recording
  substrate → `on_skill_command("/grill …")` reaches `Substrate.send` **verbatim**, one hop below
  `engine.send` — bridges T1's bot-boundary test) + **scripted drive-loop** (ask→answer→permission(allow_once)
  →result; the native answers-map rides back). **LIVE run (host CLI auth, NO API key, 120.1s): OVERALL PASS**
  — the unregistered `/grill`, forwarded verbatim, reached the engine and **LAUNCHED the skill**: 2 AskEvents
  surfaced + answered (first option each), 1 risky `Write` gated + `allow_once`'d, ran to a non-error
  ResultEvent, brief written to the exact absolute temp path. **P1 relay + P2 gating compose under the P3
  launch path.** Containment held (temp cwd OUTSIDE the repo, repo unchanged, no leaked CLI pids,
  `$HOME`/`~/.claude` swept clean, evidence scrubbed — SB3). Predicates code-driven + tolerant (assert on
  mechanics: ≥1 AskEvent + non-error result; doc-write is a bonus) — a non-launch can't false-pass.
  Independent reviewer **AGREE** with 2 load-bearing mutation probes (passthrough drops command →
  launch-smoke FAILs; zero asks → live predicate can't PASS). **Orchestrator re-verify caught + fixed an SB3
  gap** — the *overall* evidence file wasn't scrubbing the live session id (per-trial files were); now the
  driver passes the collected session ids as `extra_secrets`, and the committed evidence is a post-fix live
  re-run (PASS, UUID-grep clean). `docs/features/interactive-prompts/verify.md` is the owner phone checklist.
  **No production code changed in T2** (spike + docs only).

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
