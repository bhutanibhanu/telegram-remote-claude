# spikes/p3-skill-launch-verify — T2 live end-to-end verify (skill-launch passthrough)

Throwaway, contained harness that proves the P3 **headline workflow** — type `/grill` in
Telegram and the skill runs in the **live Claude session** — composes end-to-end. Pairs with
the owner phone-verify checklist at `docs/features/interactive-prompts/verify.md`.

P1 shipped the interactive relay (AskUserQuestion → buttons / "Other"; the async answer-hold),
P2 added per-tool permission gating, and T1 added `on_skill_command` to `claude_tg/bot.py`
(any *unregistered* slash-command is forwarded **verbatim** to the active session via the shared
`_run_turn`). T2 proves the **launch path drives the full loop**: `/grill` reaches the engine and
runs a real interactive `/grill` loop (AskUserQuestion → answers, file writes gated) to a written
doc. ADR-001 C5 already PASSED (a slash-command skill IS invocable in-session), so this is
expected to work.

## What it does

`verify_skill_launch.py` has two modes (mirrors `spikes/p2-permission-verify/verify_permissions.py`):

### (1) `--mock` — the IMPLEMENTER's deterministic self-test (NO live Claude)

Two trials, both must PASS for an overall PASS:

- **`launch_smoke`** — bridges T1's unit test (the bot boundary) DOWN to `engine.send`. It
  constructs the **real** `TelegramClaudeBot` over a **real** `StreamingSession` whose engine is
  built (via the injected `engine_factory`, mirroring `_default_engine_factory`) over a
  **recording fake substrate** (`RecordingSubstrate` — conforms to the `Substrate` protocol; its
  `send` RECORDS the prompt and yields a `ResultEvent`). It calls
  `bot.on_skill_command(<update "/grill build me X">, ctx)` and asserts the fake substrate's
  `send` received **`"/grill build me X"` VERBATIM** (leading `/` + args intact). It also routes
  `/reset` through the bot's own `cmd_reset` and asserts `/reset` **never reaches the substrate**
  as a skill (bot commands win — D1).
- **`scripted_drive_loop`** — drives the SAME ask→answer→permission→result drive-loop the live
  probe uses, against a scripted fake substrate (`ScriptedGrillSubstrate`) that — exactly like
  `SdkSubstrate` — surfaces an `AskUserQuestion` and a risky `Write` **through the engine's
  decision callback** (parking until the harness resolves each). Asserts: ≥ 1 `AskEvent` surfaced;
  the first option of each question answered (and the native answers-map **rode back** to the
  substrate as `updated_input["answers"]`); the `Write` was `allow_once`'d and only ran because we
  allowed it; the loop reached a non-error `ResultEvent` with the brief written under the temp cwd.

### (2) `--live` — the ORCHESTRATOR's real probe (real Claude, contained)

One trial: **`live_grill_loop`**. Builds the REAL engine over a real `SdkSubstrate`
(`cwd = tempfile.mkdtemp()` **outside the repo**, `permission_mode="default"`, no skip-permissions
flag — mirrors `_default_engine_factory`) with a harness-owned `PermissionPolicy`. Sends
`/grill <concrete idea>` (leading `/` **verbatim** — that IS the point) and drives the loop:
answer the first option of every `AskEvent`, `allow_once` the doc `Write`, stop on `ResultEvent`.
Bounded by an ask-round budget (≤ 12) + a wall-clock cap (600 s) so it can never hang.

## The drive-loop (identical for the scripted + live runs)

`drive_grill_until_result`: `async for ev in engine.send(prompt)` →

- `AskEvent` → `answers = {q["question"]: q["options"][0]["label"] for q in ev.questions}` →
  `engine.resolve(ev.tool_use_id, QuestionAnswer(answers=answers))` (the EXACT shape
  `StreamingSession._resolve_ask_option` uses: `answers_from_ask` → `QuestionAnswer` →
  `engine.resolve`).
- `PermissionEvent` → `engine.resolve(ev.tool_use_id, PermissionDecision(verdict="allow_once"))`
  (mirrors `StreamingSession._resolve_permission`).
- `PlanEvent` → `PlanVerdict(approve=True)` (grill may propose a plan before writing).
- `ResultEvent` → stop.

## PASS predicates (tolerant of model nondeterminism — assert on MECHANICS, never prose)

- **Core:** ≥ 1 `AskEvent` was surfaced — the unregistered `/grill`, forwarded verbatim, LAUNCHED
  the skill and drove the relay.
- the loop ran to a non-error `ResultEvent` without hanging after answers were injected.
- **Bonus (recorded, NOT hard-failed):** a brief was written under the temp cwd. If it landed
  outside, the sweep FLAGS it.
- A launch that never reaches the engine, or a model that never asks, yields **FAIL/PARTIAL —
  never a false PASS** (mutation-checked: a no-ask substrate fails the ≥ 1-ask predicate).

## Modes

```sh
# self-test (NO live Claude) — launch-smoke + scripted drive-loop; the IMPLEMENTER runs this:
cd <repo> && .venv/bin/python spikes/p3-skill-launch-verify/verify_skill_launch.py --mock

# LIVE against real Claude (the ORCHESTRATOR runs this; produces the committed evidence):
cd <repo> && .venv/bin/python spikes/p3-skill-launch-verify/verify_skill_launch.py --live
```

Default (no flag) is `--mock` (the safe, no-network self-test).

## Containment / hygiene (the load-bearing lesson — mirrors P2)

The substrate/grill `cwd` is a fresh `tempfile.mkdtemp()` **outside the repo**, `rmtree`'d
regardless of outcome. **A live-ALLOWED write is NOT sandboxed (ADR-001: cwd is not an OS
boundary)** — `/grill` may write its brief to an absolute path or `$HOME` — so the harness keeps a
**SWEEP set** of the doc filenames grill is likely to produce (a unique `PROJECT_BRIEF_<uuid>.md`
it instructs grill to write to the **absolute temp path**, plus grill's common default doc names)
and in cleanup **sweeps BOTH `$HOME` AND `~/.claude`** for them, **removing + FLAGGING** any that
landed outside the temp cwd. Repo `git status --porcelain` is asserted **unchanged** before/after;
`~/.claude/projects/<temp-cwd>` transcript dirs are cleaned; `descendant_claude_pids()` is asserted
empty after `engine.stop()`; **every recorded string is scrubbed** (SB3) via the P0
`record_criterion` recorder (the `AskEvent` summary surfaces only question text + option labels, no
secrets; the `PermissionEvent` summary is already body-free). Host CLI auth, **no API key**
(asserted unset at start; **aborts** if set). No `--dangerously-skip-permissions` anywhere (SB5).
Evidence → `evidence/<name>.{json,transcript.txt}` + overall
`p3_skill_launch_verify.{json,transcript.txt}`.

Reuses `spikes/p1-async-latency/_common.py` (scrub / record_criterion / descendant_claude_pids /
clean_project_transcript_dir / git_porcelain) via `sys.path` — **no production file is modified**;
the harness only **imports** `claude_tg` (`bot`, `engine`, `stream_session`, `permissions`,
`config`).
