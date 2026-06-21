# Progress: session-substrate-feasibility

_Plan generated 2026-06-20 from design.md · 19 tasks · supervised build_

> **P0 feasibility spike (GATE 1).** Deliverables are the C1–C6 evidence matrix, the
> drafted normalized engine interface, and a filled **ADR-001 (Proposed)** + go/no-go.
> The `spikes/session-substrate/` tree is throwaway-quality but **retained** as a
> reproducible, non-production reference (design S3). Per the design's Test policy, the
> spike is **not** held to the repo's 53-test snapshot and gets **no unit-test suite** —
> the *evidence* (runnable checks + scrubbed transcripts) is the verification artifact.
> The **only** code with a real unit test is the secret scrubber (T2), because secret
> hygiene (SB3) gates every committed transcript.
>
> A criterion task is **done when it emits a clear `PASS / FAIL / PARTIAL` with a captured,
> scrubbed transcript** — a `FAIL`/`PARTIAL` is a valid, useful outcome (fail-clean, never
> a silent hang). Acceptance never requires a specific verdict.

## Cross-cutting acceptance (applies to EVERY task)

These three were added at plan approval and gate the whole spike. The build loop must hold
each task to them where relevant; they are also bound inline to the tasks they most affect.

- **X1 — Controlled risky tests (not an OS sandbox).** WHEN a check exercises a risky tool,
  it SHALL use a dedicated temporary fixture directory and prefer deterministic sentinel `Write`
  operations over arbitrary Bash. Where the substrate exposes a permission callback, the harness
  SHALL resolve targets and deny anything outside the fixture. The check SHALL record repository
  status before/after and verify only the expected sentinel changed. If a substrate cannot enforce
  containment, the result SHALL say so explicitly as `PARTIAL`/`FAIL`; cwd alone must never be
  described as a security boundary. (Primary tasks: T7, T14.)
- **X2 — Isolated, recorded deps.** WHEN the spike needs third-party packages, they SHALL be
  installed into an **isolated, git-ignored** virtual environment scoped to the spike, with
  **exact versions recorded** in a committed spike-local lock. Production dependency files
  (`requirements.txt`, `requirements-dev.txt`) SHALL remain byte-for-byte unchanged.
  (Primary task: T1; verified again at T4.)
- **X3 — Secret-scan before evidence lands.** WHEN any task is about to commit a transcript,
  log, or other evidence artifact, an **automated secret scan** SHALL run over the
  transcripts and the staged files, and the **clean result SHALL be recorded** in the
  evidence dir before the commit. A non-clean scan blocks the commit. (Primary tasks: every
  evidence-producing task T4, T6–T17; consolidated final gate at T18.)

## Task list
- [x] T1 — Spike scaffold + isolated, ignored venv + recorded deps (9792e15)
- [x] T2 — Secret scrubber utility + unit test (bc9facf)
- [x] T3 — Evidence recorder + transcript-capture helper (dc2a0c3)
- [x] T4 — Preflight: Python + `claude` CLI versions + Agent SDK probe (587334d)
- [x] T5 — A-harness: persistent session lifecycle (start/resume/send/stop) (053ca9b)
- [x] T6 — A · C1 bidirectional streaming check (fb4247e)
- [x] T7 — A · C2 per-tool permission decision check (sandboxed) (c8d9f3c)
- [x] T8 — A · C3 AskUserQuestion answered programmatically (4eeaa9f impl; b9a7360 correction — PASS native, was PARTIAL)
- [x] T9 — A · C4 ExitPlanMode approve/reject + feedback (275a9b1 — PASS)
- [x] T10 — C5 test skill fixture (emits permission + AskUserQuestion/ExitPlanMode) (e54507a)
- [x] T11 — A · C5 skill invocation through the same channels (95362da — PASS)
- [x] T12 — A · C6 session resume by id across process runs (a5c3452 — PASS)
- [x] T13 — B-harness: CLI `stream-json` driver + permission-mechanism discovery (fee27d8 — PASS)
- [x] T14 — B · C2 permission decision over the wire (sandboxed) (dba7d5f — PASS)
- [x] T15 — B · C3 AskUserQuestion over `stream-json` (90cdc1d — PASS native)
- [x] T16 — B · C4 ExitPlanMode over `stream-json` (acc586f — PASS)
- [x] T17 — `run_all.py`: C1–C6 PASS/FAIL/PARTIAL matrix (+ B contingency) (ec93ecf)
- [x] T18 — Draft normalized engine interface + final evidence secret-scan gate (95ffbf3)
- [ ] T19 — Fill ADR-001 (Proposed) + go/no-go writeup

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked

## Tasks

### T1 — Spike scaffold + isolated, ignored venv + recorded deps
- **Goal:** Stand up the `spikes/session-substrate/` tree and an isolated, git-ignored venv with exact dependency versions recorded, touching no production file.
- **Depends on:** none
- **Files (expected):** `spikes/session-substrate/` (dir skeleton + `README.md`), `spikes/session-substrate/.venv/` (created, ignored), `spikes/session-substrate/requirements.lock` (committed), `.gitignore` (only if the spike venv isn't already covered).
- **Acceptance:**
  - WHEN setup runs, the system SHALL create `spikes/session-substrate/.venv/` and `git status` SHALL NOT list it as untracked (ignored). **(X2)**
  - WHEN any package is installed, its exact pinned version SHALL be written to `spikes/session-substrate/requirements.lock`. **(X2)**
  - The production files `requirements.txt` and `requirements-dev.txt` SHALL remain byte-for-byte unchanged (verify with `git diff`). **(X2)**
  - No file under `engine/`, `bot.py`, `claude_runner.py`, `session_manager.py`, `permissions.py`, or `render.py` is created or modified (design anti-goal).
- **Tests:** none — scaffold; verified by `git status`/`git diff` per acceptance.
- **Status:** done (9792e15) — 2 independent reviewers: ALL PASS.

### T2 — Secret scrubber utility + unit test
- **Goal:** A reusable `scrub()` that redacts tokens/keys/secrets from text before any transcript is written, with a real unit test — the one tested unit in the spike (SB3).
- **Depends on:** T1
- **Files (expected):** `spikes/session-substrate/scrub.py`, `spikes/session-substrate/test_scrub.py`.
- **Acceptance:**
  - WHEN `scrub()` receives text containing an API-key/bearer-token/`sk-`-style pattern or the host's auth token, it SHALL replace each with a fixed redaction marker and leave non-secret text intact.
  - WHEN `scrub()` receives text with no secrets, it SHALL return it unchanged.
  - The scrubber SHALL be the single chokepoint every transcript write passes through (see T3).
- **Tests:** **Real pytest unit test** (`test_scrub.py`) asserting: known token shapes are redacted; benign text is preserved; idempotent on already-scrubbed text. This is the only required automated test in the spike.
- **Status:** done (bc9facf) — 16 tests pass under spike-local pytest.ini; 2 independent reviewers (incl. adversarial leak/over-redaction probes): ALL PASS.

### T3 — Evidence recorder + transcript-capture helper
- **Goal:** Shared helper that records a per-criterion `PASS/FAIL/PARTIAL` result with its captured session transcript, routing every write through the T2 scrubber.
- **Depends on:** T2
- **Files (expected):** `spikes/session-substrate/evidence_recorder.py`, `spikes/session-substrate/evidence/` (output dir, created on first run).
- **Acceptance:**
  - WHEN a check reports a verdict, the recorder SHALL persist `{criterion, verdict, observed-reason}` plus the captured transcript under `evidence/`.
  - WHEN a transcript is written, it SHALL pass through `scrub()` first; no raw, unscrubbed transcript is ever persisted. **(X3 precondition)**
  - WHEN a check produces no verdict (hang/crash), the recorder SHALL still write a `FAIL` with the observed reason (fail-clean), never leave an empty/silent artifact.
- **Tests:** none — throwaway helper; correctness rides on T2's test + manual inspection of `evidence/`.
- **Status:** done (dc2a0c3) — 4 independent reviewers across 3 rounds; final 2 (incl. adversarial): ALL PASS. Two real defects caught & fixed: (1) criterion id was written unscrubbed into JSON/header/filenames → now scrubbed (contents) + whitelist-sanitized (filename) + path-traversal containment; (2) fail-clean robustness — recorder-side flush failures no longer mask the original check exception, and the authoritative result JSON is written first so a failed transcript write never leaves a lone half-artifact.

### T4 — Preflight: Python + `claude` CLI versions + Agent SDK probe
- **Goal:** Record Python and `claude` CLI versions and empirically probe whether the Agent SDK package exists/installs (prior research got this wrong) — the first evidence artifact.
- **Depends on:** T3
- **Files (expected):** `spikes/session-substrate/preflight.py`, `spikes/session-substrate/evidence/preflight.*`.
- **Acceptance:**
  - WHEN preflight runs, it SHALL record the Python version, the `claude` CLI version, and the Agent SDK **package name + version, or its documented absence**, into `evidence/`.
  - WHEN the Agent SDK is installed, its exact version SHALL be reflected in `requirements.lock` (re-verify **X2**); production dep files stay unchanged.
  - The preflight evidence SHALL pass the secret scan before commit. **(X3)**
- **Tests:** none — evidence-producing harness; the recorded output is the artifact.
- **Status:** done (587334d) — PASS: `claude-agent-sdk==0.2.105` exists, installs into the spike venv, and imports (Python 3.14.5, claude CLI 2.1.183) — refutes the prior "SDK doesn't exist" research. SDK+deps pinned in `requirements.lock` (X2), production deps untouched; evidence scrubbed + secret-scanned clean (X3); independent audit returned SUPPORTED. T4 probes existence/version/import only — no C1–C6 claim.

### T5 — A-harness: persistent session lifecycle (start/resume/send/stop)
- **Goal:** Stand up the Agent SDK persistent client and the `start` / `resume` / `send` / `stop` lifecycle the C1–C6 checks drive against.
- **Depends on:** T4
- **Files (expected):** `spikes/session-substrate/harness_sdk.py`.
- **Acceptance:**
  - WHEN `start()` is called, a persistent multi-turn session SHALL be established using the host's existing CLI auth (no API key).
  - WHEN `stop()` is called, the session SHALL terminate cleanly without leaking processes.
  - The lifecycle SHALL expose the seams (`send`, event stream out) the criterion checks need.
- **Tests:** none — throwaway harness; exercised by T6–T12.
- **Status:** done (053ca9b) — `SDKSessionHarness` over claude-agent-sdk==0.2.105: start/resume/send/stop with session-id capture + SDK-message passthrough (event stream out) and a bounded per-message timeout. Live self-check PASS (start → 2 multi-turn sends over one stable session_id → clean stop, no leaked CLI process; host CLI auth, no API key). Two independent reviewers returned ALL PASS; evidence `evidence/t5_lifecycle_smoke.*` (scrubbed, X3); spike-only, no production files touched. Note: cross-process resume is the seam only — proven later in C6/T12; leak check is a descendant-pid heuristic (can't see reparented orphans).

### T6 — A · C1 bidirectional streaming check
- **Goal:** Prove a persistent session can take a new operator message in mid-session and emit a continuous event stream out.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c1_streaming.py`, `evidence/c1.*`.
- **Acceptance:**
  - WHEN the check runs ≥2 turns over one live session, it SHALL capture the streamed events and emit `PASS/FAIL/PARTIAL` with the observed reason. **(X3)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (fb4247e) — **C1 PASS**. Two turns over one persistent session (stable session_id); a 2nd operator message mid-session was accepted without a new session. Genuine `text_delta` events arrived **before** the completed `AssistantMessage` and the terminal `Result/success(is_error=False)` in both turns. Streaming is **coarse-grained (≈3–4 deltas/turn), not guaranteed token-level**; some runs also emit `signature_delta` (extended-thinking) events — the PASS rests on observed `text_delta` deltas. Host CLI auth, no API key; evidence `evidence/c1.*` scrubbed (X3); spike-only, no production files. **Two independent reviewers returned ALL PASS** (12/12 bullets each). C1 only — no C2–C6 claim.

### T7 — A · C2 per-tool permission decision check (sandboxed)
- **Goal:** Prove code is consulted before a risky tool runs and its allow/deny is honored — one risky tool denied (does not execute), a second allowed (does).
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c2_permission.py`, `evidence/c2.*`.
- **Acceptance:**
  - WHEN a risky tool (e.g. `Write`/`Bash`) is denied programmatically, it SHALL NOT execute; WHEN a second is allowed, it SHALL execute — both captured with verdict + transcript. **(X3)**
  - The check SHALL follow **X1**: use a deterministic sentinel operation in a temporary fixture,
    enforce resolved-target containment through the permission callback where supported, and record
    repository status before/after. Any inability to enforce containment SHALL be documented in the verdict.
- **Tests:** none — verdict + transcript + recorded containment checks are the artifact.
- **Status:** done (c8d9f3c) — **C2 PASS**. A real SDK permission callback (`can_use_tool`, `permission_mode=default`, no bypass) **denied one Write before execution** → the denied marker **remained absent**; a second Write was **allowed** and produced the expected sentinel (`ALLOWED_OK`). The **same tool (Write) received opposite per-request decisions** (deny then allow), proving a genuine per-request gate, not global tool config; callback fired both times (not refusal/no-call), denied not executed (not post-exec failure). Resolved-path containment is **deny-by-default** (outside-fixture + traversal denied, unit-checked); **containment is policy-level, not OS/kernel sandboxing**. Disposable `/tmp` fixture cleaned up; no new repo changes during the run; evidence `evidence/c2.*` scrubbed (X3). **Both independent reviewers returned ALL PASS** (19/19 each). C2 only — no C1/C3–C6 claim.

### T8 — A · C3 AskUserQuestion answered programmatically ⭐
- **Goal:** Prove a multiple-choice question raised mid-session is intercepted and answered programmatically (no TTY), and the session proceeds on that answer.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c3_ask.py`, `evidence/c3.*`.
- **Acceptance:**
  - WHEN AskUserQuestion fires mid-session, the check SHALL answer it from code with no TTY and confirm the session continued on that answer — verdict + transcript. **(X3)** *(make-or-break)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (4eeaa9f impl; **corrected b9a7360**) — **C3 (A) PASS — NATIVE**. _(Original verdict was
  PARTIAL; CORRECTED to native PASS after T15 adversarial review discovered the native answer shape — the
  original probe injected the WRONG shape and so wrongly concluded "no native answer API exists".)_ A real
  AskUserQuestion request is intercepted programmatically via `can_use_tool` with **no TTY** and **answered
  NATIVELY** by returning `PermissionResultAllow(updated_input={**input, "answers": {question_text:
  chosen_label}})` — the documented `AskUserQuestionOutput.answers` map keyed by question text → label. The
  tool_result is **NOT an error** ("Your questions have been answered: …You can now continue with these answers
  in mind.") and the session continues on the **code-selected** option. **Code-driven:** differing Alpha/Bravo
  trials each continued on the code pick under a neutral prompt; adversarial review confirmed an **off-menu
  injected answer ("Zucchini") is honored verbatim** → the SDK genuinely uses the injected map. Multi-select
  works (comma-separated labels). The earlier wrong shapes (`selected`/`selectedOption` flags / extra top-level
  fields) silently no-op ("The user did not answer the questions") — that was the PARTIAL root cause, a probe
  defect, NOT a substrate limitation. The deny-with-answer-message workaround also still works and is retained
  as a documented non-native FALLBACK (`is_error=True`). No API key; evidence `evidence/c3.*` regenerated,
  scrubbed + secret-scan clean (X3). **Three fresh independent reviewers (acceptance + live-reproduction,
  adversarial incl. off-menu injection, make-or-break/ADR specialist) all AGREE PASS** (and independently
  reproduced it). **UNPROVEN / P1-must-validate:** free-text "Other" (`response` field), `annotations`, and
  **multi-question asks (1–4 questions; only single question tested)** — build the `answers` map per-question on
  verbatim question text. **C3 is substrate-NEUTRAL** (native PASS on A and B). C3 only — no C1/C2/C4–C6 claim.

### T9 — A · C4 ExitPlanMode approve/reject + feedback ⭐
- **Goal:** Prove a proposed plan is surfaced and can be approved **or** rejected with feedback programmatically, and the session honors the verdict.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c4_plan.py`, `evidence/c4.*`.
- **Acceptance:**
  - WHEN ExitPlanMode surfaces a plan, the check SHALL approve it in one path and reject-with-feedback in another, confirming the session honored each — verdict + transcript. **(X3)** *(make-or-break)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (275a9b1) — **C4 PASS**. ExitPlanMode fired as a real tool request through the
  real SDK permission callback (`can_use_tool`, `permission_mode=plan`, no bypass) in two isolated
  trials. **APPROVE** (`PermissionResultAllow`) → native CLI "User has approved your plan". **REJECT**
  (`PermissionResultDeny(message=…)`) → "Plan rejected …"; the model stayed in plan mode and the plan
  was **never approved → no execution greenlit**. The reject feedback injects a **code-only marker**
  (`ADD_LOGGING_STEP`) the neutral prompt never mentions; `feedback_honored` asserts against the
  **FULL** revised structured plan **OR** the model's revision reply (either suffices — both prove
  the code-supplied feedback influenced the post-rejection session). **T9-repair (Reviewer-B defect
  fixed):** the prior probe asserted the marker against a `[:400]`-truncated plan; it now uses the
  full plan text, keeps the `[:200]` preview for readability only, records both signals separately
  + which one supported the run. On the committed run `marker_in_full_revised_plan=False`,
  `marker_in_revision_response=True` → PASS via the reply signal. **Caveats:** whether the marker
  lands in the structured-plan FIELD is **model-variant** (a reviewer reproduced both False and True
  across runs, and even an empty revised-plan field — the combined gate held PASS each time); reject
  feedback rides the **`PermissionResultDeny(message=...)` channel** (the natural rejection channel,
  not a dedicated plan-feedback API, and depends on the model reading it); **post-approval ARBITRARY
  execution is NOT tested** (contained: Bash/Edit/out-of-fixture-Write/AskUserQuestion denied; cwd is
  a disposable temp fixture; plan-scratch cleaned). Host CLI auth, no API key; evidence `evidence/c4.*`
  scrubbed + secret-scan clean (X3); spike-only, no production files. **Two fresh independent
  reviewers — acceptance + live-reproduction, and adversarial-reproducibility + evidence-audit — both
  AGREE PASS** with no objective defect; reviewer 2 observed the structured-plan signal flip False→True
  across runs while the combined gate stayed PASS, confirming the repair removed the prior fragility.
  C4 only — no C1/C2/C3/C5/C6 claim. **Substrate B still requires its own C4 probe at T16** (design S1).

### T10 — C5 test skill fixture
- **Goal:** A minimal custom slash-command skill that deliberately emits a permission request **and** an AskUserQuestion (and/or ExitPlanMode), to drive C5 through the same channels as C2–C4.
- **Depends on:** T1
- **Files (expected):** `spikes/session-substrate/test_skill/` (skill definition).
- **Acceptance:**
  - WHEN the skill is invoked in-session, it SHALL emit at least one permission prompt and one interactive-tool prompt (AskUserQuestion and/or ExitPlanMode).
- **Tests:** none — fixture; exercised by T11.
- **Status:** done (e54507a) — fixture `test_skill/spike-c5-probe/SKILL.md` (name `spike-c5-probe`).
  Valid SKILL.md frontmatter; instructions deterministically drive (1) a per-tool **permission**
  request — a single contained `Write` of `c5_skill_sentinel.txt` (C2 channel) — and (2) an
  **AskUserQuestion** (2-option single-select, `Alpha`/`Bravo` — C3 channel), then a machine-checkable
  `C5_DONE:<option>` completion line. Hard constraints forbid other tools/files and ExitPlanMode
  (C5 design allows "AskUserQuestion **and/or** ExitPlanMode"). Stored plainly (not under `.claude/`)
  so the committed fixture is never mistaken for live config; T11 copies it into a disposable
  `<tempcwd>/.claude/skills/` and loads it via `skills=["spike-c5-probe"]`, `setting_sources=["project"]`.
  No production files; no repo-root `.claude/`; no secrets. **Two independent reviewers (acceptance +
  adversarial) both ACCEPT** the fixture. Behavioral emission proof is deferred to T11; reviewers
  flagged for T11: **allow** the sentinel Write so the skill proceeds, and run **differing-option
  trials** to prove the `C5_DONE` value is code-driven. _(Update: the earlier note that "C5 completion
  inherits C3's PARTIAL deny-channel caveat" is WITHDRAWN — C3 was subsequently corrected to a native
  PASS on both substrates (T8/T15); C5's capability is unaffected/strengthened.)_

### T11 — A · C5 skill invocation through the same channels
- **Goal:** Prove the C5 test skill is invoked in-session and its interactive prompts flow through the **same** permission/question/plan channels proven in C2–C4, driven to completion.
- **Depends on:** T7, T8, T9, T10
- **Files (expected):** `spikes/session-substrate/checks/c5_skill.py`, `evidence/c5.*`.
- **Acceptance:**
  - WHEN the test skill runs, its permission + interactive prompts SHALL be answered over the same code paths as C2–C4 and the skill driven to completion — verdict + transcript. **(X3)**
  - Any tool the skill runs SHALL stay within the temp fixture sandbox. **(X1)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (95362da) — **C5 PASS**. The throwaway `spike-c5-probe` skill (T10) was **invoked
  in-session** on CLI 2.1.185 (real `Skill` tool_use → CLI tool_result `Launching skill:
  spike-c5-probe`), loaded as a project skill from a disposable temp cwd's `.claude/skills/` tree
  (`skills=["spike-c5-probe"]`, `setting_sources=["project"]`). Its interactive prompts flowed through
  the **same `can_use_tool` channels** as C2/C3 (the Skill tool is auto-approved via allowed_tools, but
  Write and AskUserQuestion are NOT in allowed_tools so they route through the callback): the per-tool
  **permission** request (`Write c5_skill_sentinel.txt`) was answered over the **C2** code path and
  honored (sentinel present with `C5_SKILL_RAN`); the **AskUserQuestion** was answered over the **C3**
  code path; the skill was driven to its `C5_DONE` completion in both trials. The completion option is
  **code-driven**: trial 1 (code picks Alpha) → `C5_DONE:Alpha`, trial 2 (code picks Bravo) →
  `C5_DONE:Bravo` — differing picks each echoed under a neutral invocation prompt that never names the
  options. Containment (X1): repo git status unchanged by the run (only the in-fixture sentinel
  changed); out-of-fixture/traversal Writes + Bash/Edit/ExitPlanMode denied; fixtures (incl. `.claude`
  tree) rmtree'd; policy-level, not OS sandbox. Host CLI auth, no API key; evidence `evidence/c5.*`
  scrubbed + secret-scan clean (X3). **C3-PATH NOTE (updated — supersedes the earlier "inherited PARTIAL
  caveat"):** C3 is now a **native PASS** on both substrates (T8/T15) — the AskUserQuestion answer is
  delivered natively via the documented `AskUserQuestionOutput.answers` map on the permission-**allow**
  channel. This C5 run happened to exercise the (still-valid) deny-with-answer-message FALLBACK; **C5's
  capability is unaffected and strengthened** — the native answer channel is available to C5's prompts too,
  and C5's acceptance was always channel-routing + drive-to-completion (proven code-driven via differing
  Alpha/Bravo picks), which is unchanged. **Two fresh independent reviewers (acceptance + live-reproduction, and
  adversarial + evidence-audit) both AGREE PASS**, each independently reproducing PASS; no objective
  defect (one optional regex-hardening nit that can only ever cause a false FAIL, never inflate).
  C5 only — no C1/C2/C3/C4/C6 claim beyond reusing their proven code paths.

### T12 — A · C6 session resume by id across process runs
- **Goal:** Prove a session can be resumed by id from a **fresh process** with history intact.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c6_resume.py`, `evidence/c6.*`.
- **Acceptance:**
  - WHEN run A starts a session and records its id, and run B (separate process) resumes that id, the check SHALL demonstrate retained context — verdict + transcript. **(X3)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (a5c3452) — **C6 PASS**. Genuine **cross-process** resume proven: phase A (a separate
  python interpreter) planted a **code-generated, unguessable** codeword (`C6_<hex>`, via
  `secrets.token_hex`) in a fresh session and recorded its id; phase B (a **distinct** process,
  spawned via `subprocess`) **resumed by id** and returned the exact codeword **though phase B's
  prompt never contained it** → conversation history was retained across process runs. The resumed
  id was the **same** (`fork_session` unset → continue, not fork). **Two negative controls** were
  both blind: a **same-cwd no-resume** control (rules out shared-cwd leakage — retention comes from
  resuming the id, not from sharing the project dir) and a diff-cwd no-resume control (rules out
  guessing). **OBSERVED operational property:** resuming the same id from a **DIFFERENT cwd FAILS**
  (`No conversation found with session ID`) → **resume is cwd/project-scoped**; the CLI persists
  transcripts at `~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`. **P1 rule:** persist
  `(session_id, cwd)` together and resume only from the original project cwd. **NOT TESTED (deferred
  to P1 RB3):** resume after a crash mid-turn (torn transcript / fail-clean), concurrent/double
  resume of the same id (substrate does NOT prevent double-attach — the engine must), aged sessions,
  resume across a CLI/SDK upgrade — C6 proves the clean-restart happy path only. **RB6 housekeeping:**
  project transcript dirs survive working-dir deletion (the check now cleans the dirs it creates).
  Text-only containment; host CLI auth, no API key; evidence `evidence/c6.*` scrubbed + secret-scan
  clean (X3). **Four fresh independent reviewers total** — acceptance+live-reproduction, adversarial,
  a session-recovery/reliability specialist, and an enhancement-confirmation reviewer — **all AGREE
  PASS**; the cwd-coupling caveats and the sharper same-cwd control were added in response to the
  adversarial + specialist findings. C6 only — no C1–C5 claim.

### T13 — B-harness: CLI `stream-json` driver + permission-mechanism discovery
- **Goal:** Drive `claude -p --input-format stream-json --output-format stream-json`, parse the event stream, and record how permission decisions are actually answered over the wire on the installed CLI version (the design flags `--permission-prompt-tool` as absent on v2.1.183 — record the real mechanism).
- **Depends on:** T4
- **Files (expected):** `spikes/session-substrate/harness_cli.py`, `evidence/cli_permission_mechanism.*`.
- **Acceptance:**
  - WHEN the harness runs, it SHALL parse the `stream-json` event stream and record the observed event/message shapes.
  - It SHALL record the actual on-the-wire permission mechanism on the installed CLI version (flag, hook, MCP permission tool, or none) as evidence. **(X3)**
- **Tests:** none — harness; exercised by T14–T16.
- **Status:** done (fee27d8) — **PASS**. `harness_cli.py` drives the **raw** `claude` CLI (2.1.185) over
  `stream-json` **directly** (stdlib `subprocess` + manual NDJSON + reader threads; **no claude-agent-sdk
  import** — substrate B is a genuinely independent fallback). Parses the event stream and records the
  observed message/event shapes: `system`/init (session_id, tools, mcp_servers, permissionMode, model),
  `assistant` (content blocks: thinking/text/tool_use), `user` (tool_result), `result` (subtype/usage/
  session_id), plus the control plane `control_request`/`control_response`. **Permission mechanism on
  2.1.185 (the discovery):** the bidirectional **stream-json control protocol** — the driver sends an
  `initialize` control_request, the CLI sends `can_use_tool` control_requests, the driver answers with a
  `control_response` (`{behavior:"allow", updatedInput:<input>}` or `{behavior:"deny", message:…}`). It is
  **activated by the undocumented `--permission-prompt-tool stdio` spawn flag** (still **ABSENT from
  `--help`** on 2.1.185 — updates ADR-001's stale v2.1.183 note): **verified live in both directions** —
  WITHOUT the flag, **no `can_use_tool` reaches the driver** (CLI auto-decides via `--allowedTools`,
  default-mode auto-DENY); WITH it, the round-trip works (allow → sentinel created; light deny → file
  absent). Wire gotcha recorded: an **allow** `control_response` MUST carry `updatedInput` (else ZodError);
  a wire **deny** still rides `subtype:success`. NOT `--permission-prompt-tool <mcp>` and NOT "none".
  Host CLI auth, no API key; session_id scrubbed; evidence `evidence/cli_permission_mechanism.*` secret-scan
  clean (X3); contained (temp cwd, sentinel-only allow, project-dir cleanup, no process leak). **Two fresh
  independent reviewers (acceptance + live-reproduction, adversarial + evidence-audit) both AGREE PASS**,
  each independently reproducing the round-trip AND the negative control (flag off → 0 `can_use_tool`).
  Light mechanism demo only — **rigorous C2 allow/deny+containment is T14**. **ADR/P1 caveats to carry:**
  (1) **undocumented-flag risk** — B couples directly to `--permission-prompt-tool stdio` on the host CLI
  binary (pin/monitor the CLI version; treat a missing/changed flag as fail-clean); (2) harness gaps for
  T15/T16 — add a `control_cancel_request` branch; (3) A-vs-B is the **same** control protocol but B
  hand-rolls NDJSON/threads/timeouts the SDK provides → more maintenance surface + sharper version coupling.

### T14 — B · C2 permission decision over the wire (sandboxed)
- **Goal:** Prove (or disprove) that the CLI `stream-json` path can answer a per-tool permission decision — denied tool does not execute, allowed tool does.
- **Depends on:** T13
- **Files (expected):** `spikes/session-substrate/checks/c2_permission_cli.py`, `evidence/c2_cli.*`.
- **Acceptance:**
  - WHEN a risky tool is denied/allowed over the wire, the check SHALL confirm honored behavior — verdict + transcript. **(X3)**
  - The check SHALL follow **X1**: use a deterministic sentinel operation in a temporary fixture,
    enforce resolved-target containment where the CLI mechanism permits it, and record repository
    status before/after. Any inability to enforce containment SHALL be documented in the verdict.
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (dba7d5f) — **C2 (B) PASS**, held to the same standard as substrate-A C2 (T7) and over the
  real wire. Via the T13 `CLISessionHarness` (raw CLI stream-json, **no claude-agent-sdk**): a `can_use_tool`
  control_request for **`Write` → denied marker arrived over the wire**, the deny `control_response` was
  honored and the tool **did NOT execute** (file absent; `tool_result is_error=True` is the denial itself,
  not a post-exec failure; the model genuinely issued the tool_use, so not a no-call); a second `Write` →
  in-fixture allowed sentinel was **answered allow and executed** (`ALLOWED_OK` present). The **SAME tool
  (Write) received opposite per-request decisions** by resolved target path (Write in neither allowedTools
  nor disallowedTools) → a genuine **per-request gate over the wire**, not static `--allowedTools`. Resolved-path
  **containment** deny-by-default unit-checked (outside-fixture + abs/rel traversal denied) and an adversarial
  reviewer additionally confirmed a live model-issued out-of-fixture Write was denied end-to-end. Repo git
  status unchanged by the run; disposable temp fixture; containment is **policy-level, not OS sandbox**. Host
  CLI auth, no API key; evidence `evidence/c2_cli.*` scrubbed + secret-scan clean (X3); harness_cli.py
  unchanged. **Two fresh independent reviewers (acceptance + live-reproduction, adversarial) both AGREE PASS**,
  each independently reproducing deny-blocks + allow-executes + same-tool-opposite + callback-fired-both-times.
  **A-vs-B note (for ADR):** functionally equivalent (B's evidence is arguably stronger — it requires an
  independent on-the-wire `can_use_tool` frame, not just an in-process callback log); asymmetries: B couples to
  the undocumented `--permission-prompt-tool stdio` flag and needs the `updatedInput`-on-allow workaround +
  hand-rolled NDJSON/threads/timeouts. C2 only — no C1/C3–C6 claim.

### T15 — B · C3 AskUserQuestion over `stream-json` ⭐
- **Goal:** Prove (or disprove) that an AskUserQuestion can be answered programmatically over the CLI `stream-json` protocol.
- **Depends on:** T13
- **Files (expected):** `spikes/session-substrate/checks/c3_ask_cli.py`, `evidence/c3_cli.*`.
- **Acceptance:**
  - WHEN AskUserQuestion appears in the stream, the check SHALL answer it over the wire and confirm continuation — verdict + transcript. **(X3)** *(make-or-break; tested on B unconditionally)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (90cdc1d) — **C3 (B) PASS — NATIVE, over the wire**. Via the T13 `CLISessionHarness` (raw CLI
  stream-json, no claude-agent-sdk): AskUserQuestion arrives as an **ordinary `can_use_tool` control_request**
  (no special subtype) and is **answered programmatically (no TTY) NATIVELY** by returning an **ALLOW**
  `control_response` whose `updatedInput` carries the **documented `AskUserQuestionOutput.answers` map keyed by
  the question text → the code-chosen label**. The tool_result is **NOT an error** (`is_error in {None,False}`)
  and reads "Your questions have been answered: …You can now continue with these answers in mind."; the turn
  completes (`result subtype=success`) and the session continues on the code-selected option. **Code-driven:**
  two trials under a neutral prompt that never names the pick each continued on the CODE-chosen label
  (pick[0]=Alpha→Alpha, pick[1]=Bravo→Bravo); an adversarial reviewer further confirmed an **off-menu injected
  answer ("Zucchini", not in the options) was honored verbatim** → the CLI genuinely USES the injected map,
  not coincidental model agreement. Multi-select works via the native answers-map (comma-separated labels).
  **This CORRECTS the earlier PARTIAL** (the prior probe injected the wrong shape — `selected`/`selectedOption`
  flags / extra top-level fields — and so wrongly concluded "no native answer exists"). The deny-with-answer
  workaround also still works and is retained as a documented non-native FALLBACK (rides `is_error=True`).
  Containment (temp cwd, risky tools denied, repo unchanged); host CLI auth, no API key; evidence
  `evidence/c3_cli.*` scrubbed + secret-scan clean (X3); harness gained an additive `control_cancel_request`
  branch (0 observed). **Three fresh independent reviewers (acceptance + live-reproduction, adversarial incl.
  off-menu injection, make-or-break/ADR specialist) all AGREE PASS.** **UNPROVEN / P1-must-validate:** free-text
  "Other" (`response` field), `annotations`, and **multi-question asks (schema allows 1–4 questions; only single
  question tested)** — P1 must build the `answers` map per-question keyed on verbatim question text and spread
  the full original input. **C3 is substrate-NEUTRAL** (native PASS on A and B alike → does not decide A vs B).

### T16 — B · C4 ExitPlanMode over `stream-json` ⭐
- **Goal:** Prove (or disprove) that a plan can be approved/rejected-with-feedback programmatically over the CLI `stream-json` protocol.
- **Depends on:** T13
- **Files (expected):** `spikes/session-substrate/checks/c4_plan_cli.py`, `evidence/c4_cli.*`.
- **Acceptance:**
  - WHEN ExitPlanMode appears in the stream, the check SHALL approve in one path and reject-with-feedback in another, confirming honored behavior — verdict + transcript. **(X3)** *(make-or-break; tested on B unconditionally)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** done (acc586f) — **C4 (B) PASS**, over the wire, mirroring the repaired substrate-A C4 (T9) standard.
  Via the T13 `CLISessionHarness` (raw CLI stream-json, no claude-agent-sdk; `permission_mode=plan`, no bypass):
  ExitPlanMode arrives as an ordinary `can_use_tool` control_request (plan in `input.plan`). **APPROVE** (ALLOW
  control_response) → native "User has approved your plan"; **REJECT** (DENY control_response `message`=feedback
  with code marker `ADD_LOGGING_STEP`) → "Plan rejected …", the model stayed in plan mode and the plan was
  **never approved → no execution greenlit**. **feedback_honored** asserts against the **FULL** revised structured
  plan **OR** the model's revision reply (the T9-repair semantics; NOT a truncated value); both signals recorded +
  which supported the run (committed run: structured-plan field False, reply True → honored via reply; an
  independent reviewer reproduced BOTH True — structured-plan placement is **model-variant**, gate held PASS).
  Code-driven (marker absent from the neutral prompt). **Schema-confirmed native mechanism (C3 lesson applied):**
  the adversarial reviewer inspected `ExitPlanModeOutput` (no `feedback`/`rejectionReason`/decision field — binary
  allow/deny), found a `plan_approval_request`+feedback path in the CLI binary but proved it is the **multi-agent
  teammate subsystem** (NOT reachable via single-agent `can_use_tool`), and **live-tested that enriched deny fields
  are ignored** (only `message` passes through) → the deny-`message` channel **IS** the genuine native
  reject-with-feedback mechanism; **no missed native shape** (unlike C3). Containment (X1): temp cwd; Bash/Edit/
  out-of-fixture-Write/AskUserQuestion denied; repo unchanged; plan-scratch cleaned; `control_cancel_request`
  counter = 0 observed. **POST-APPROVAL ARBITRARY EXECUTION NOT TESTED** (contained). Host CLI auth, no API key;
  evidence `evidence/c4_cli.*` scrubbed + secret-scan clean (X3); harness_cli.py unchanged. **Three fresh
  independent reviewers (acceptance + live-reproduction, adversarial + schema/binary deep-dive, make-or-break/ADR
  specialist) all AGREE PASS**, each independently reproducing approve+reject+feedback. **GATE-1 NOTE:** with C3
  (native, both substrates) and C4 (both substrates) now PASS, **both make-or-break criteria are GREEN on A and B
  → the design's "constrained mode" re-plan risk is retired.** **C4 is substrate-NEUTRAL** (same allow/deny +
  deny-message mechanism on both → does not decide A vs B). C4 only — no C1/C2/C3/C5/C6 claim.

### T17 — `run_all.py`: C1–C6 PASS/FAIL/PARTIAL matrix (+ B contingency)
- **Goal:** Aggregate all checks into one runnable entry point that prints the C1–C6 matrix across substrate A and B, and runs B on the **remaining** criteria only where A came back FAIL/PARTIAL (design S1).
- **Depends on:** T6, T7, T8, T9, T11, T12, T14, T15, T16
- **Files (expected):** `spikes/session-substrate/run_all.py`, `evidence/matrix.*`.
- **Acceptance:**
  - WHEN `run_all.py` runs, it SHALL print a C1–C6 × {A,B} matrix of `PASS/FAIL/PARTIAL` and persist it to `evidence/`. **(X3)**
  - WHEN substrate A is FAIL/PARTIAL on a non-make-or-break criterion, the corresponding B check SHALL also be exercised; otherwise B beyond C2/C3/C4 MAY be skipped (recorded as N/A with reason).
  - Re-running on the same machine SHALL use the same documented procedure and stable evidence
    format; verdict discrepancies caused by model/substrate variability SHALL be preserved and
    explained rather than hidden or treated automatically as a harness failure.
- **Tests:** none — the matrix output is the artifact.
- **Status:** done (ec93ecf) — `run_all.py` aggregates the canonical per-criterion evidence JSONs into the
  **C1–C6 × {A,B} matrix**, printed + persisted to `evidence/matrix.json` + `evidence/matrix.md` (scrubbed, X3).
  **Result:** C1 A=PASS/B=N-A · C2 A=PASS/B=PASS · C3 A=PASS/B=PASS · C4 A=PASS/B=PASS · C5 A=PASS/B=N-A ·
  C6 A=PASS/B=N-A — **no FAIL, no PARTIAL** (C3 is native PASS on both after the correction). **B-contingency
  (design S1) is ENFORCED, not just displayed:** B is tested unconditionally on C2/C3/C4; for C1/C5/C6 (A PASS,
  non-make-or-break) B is **N-A with reason**; an adversarial reviewer verified that injecting an A FAIL/PARTIAL
  on a contingent criterion makes `run_all.py` emit a CONTINGENCY VIOLATION and **exit non-zero** (does not
  silently N-A). Reproducible: re-running default mode is pure aggregation (no live calls) and byte-stable except
  the segregated `generated_at` timestamp; model-variance footnotes are preserved verbatim from source reasons
  (no verdict recomputed/auto-flipped); fail-clean MISSING cell on absent/corrupt evidence. An optional
  documented `--rerun` re-executes the checks (not the default; not run). Header records SDK/CLI versions,
  GATE-1-GREEN (both make-or-break PASS on both substrates), C2/C3/C4 substrate-neutrality, and the A-vs-B
  asymmetry (B's undocumented `--permission-prompt-tool stdio` coupling + hand-rolled plumbing; B scope = C2/C3/C4
  per S1). **Two fresh independent reviewers (acceptance + reproduction, adversarial + S1-enforcement + fail-clean
  probes) both AGREE** the matrix is accurate, honest, and reproducible. _(Companion forward-correction 6d0224c
  refreshed the T11/C5 evidence wording to drop the now-false "no native C3 answer API" claim — C5 verdict
  unchanged PASS.)_

### T18 — Draft normalized engine interface + final evidence secret-scan gate
- **Goal:** From the observed event/decision shapes, draft the "events in / decisions out" normalized engine interface P1 inherits; then run the consolidated secret scan over all transcripts + staged files and record the clean result before evidence is committed.
- **Depends on:** T17
- **Files (expected):** `spikes/session-substrate/normalized_interface.md`, `spikes/session-substrate/evidence/secret-scan.txt`.
- **Acceptance:**
  - The drafted interface SHALL cover Events out (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`), Decisions in (permission verdict, question answer, plan verdict, free-text reply, cancel), and Lifecycle (`start`, `resume`, `send`, `stop`) — a draft contract, not an implementation.
  - WHEN evidence is about to be committed, an automated secret scan SHALL run over **all transcripts and the staged files**, and a **clean result SHALL be recorded** in `evidence/secret-scan.txt`; a non-clean scan blocks the commit. **(X3, consolidated)**
- **Tests:** none — design artifact + recorded scan result.
- **Status:** done (95ffbf3) — two deliverables. (1) `normalized_interface.md` — a clearly-marked **P0 DRAFT
  contract** (not an implementation) covering all required surfaces grounded in the spike's OBSERVED shapes
  on BOTH substrates: **Events out** (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`),
  **Decisions in** (permission verdict allow-once/allow-session/deny[+reason]/modified-input, question answer,
  plan verdict approve/reject+feedback, free-text reply, cancel), **Lifecycle** (`start`, `resume`, `send`,
  `stop`). Records the load-bearing empirical findings: C3 question answer = **native `answers` map keyed by
  question text on the ALLOW channel** (deny-message fallback; free-text "Other" + multi-question asks flagged
  UNPROVEN); C4 plan verdict = approve(allow)/reject(deny+message), **no native plan-feedback field**
  (schema-confirmed), post-approval execution still permission-gated; C2 allow **must carry `updatedInput`**
  (ZodError gotcha on B); C6 resume **cwd/project-scoped** (persist `(session_id, cwd)`), `fork_session=False`
  continues same id, guard double-attach; cancel via `control_cancel_request`/disconnect (untested-but-handled
  on B). (2) `evidence/secret-scan.txt` — **consolidated X3 gate**: scan over all 49 spike text files;
  **OVERALL VERDICT CLEAN** — all 33 evidence/docs files (every transcript, result JSON, matrix, README, lock,
  SKILL, both new files) free of secret-shaped content; the 9 secret-SHAPED hits are confined to the
  scrubber's own regex library + deliberate fake test fixtures + `token`/`secret`-named identifiers
  (non-gating, by design). **Two fresh independent reviewers (acceptance + independent evidence-class scan;
  adversarial + full-tree independent regex hunt) both AGREE** the interface is complete/evidence-grounded/
  accurate (no invented API; UNPROVEN flags correct) and gave a **DEFINITIVE finding: no real secret of any
  kind anywhere in the spike tree** (evidence OR source); the EVIDENCE-vs-SOURCE split is legitimate.

### T19 — Fill ADR-001 (Proposed) + go/no-go writeup
- **Goal:** Fill ADR-001 to status *Proposed* — Decision + justification, per-C1–C6 evidence log, the normalized engine interface, and Consequences (incl. any C3/C4 workaround or hybrid) — and write the owner-facing go/no-go recommendation. Acceptance (G-ADR) is out of scope.
- **Depends on:** T18
- **Files (expected):** `docs/adr/ADR-001-session-substrate.md`, `docs/features/session-substrate-feasibility/go-no-go.md`.
- **Acceptance:**
  - ADR-001 SHALL be filled with the chosen substrate + justification, the C1–C6 evidence log, the normalized interface, and Consequences; status set to **Proposed** (NOT Accepted — owner decides at G-ADR).
  - The ADR Decision/Options SHALL be consistent with the recorded evidence matrix (no claim beyond what was observed); if C3/C4 failed on both substrates, the writeup SHALL state the constrained-mode shape and recommend re-plan before P1.
  - A go/no-go writeup SHALL give the owner a clear recommendation grounded in the matrix.
- **Tests:** none — documentation deliverable.
- **Status:** todo

## Rules
- **No production code.** Nothing under `engine/`, `bot.py`, `claude_runner.py`,
  `session_manager.py`, `permissions.py`, `render.py` is created or changed; the spike imports
  nothing from prod and prod imports nothing from the spike (design anti-goal).
- **No unit-test suite** beyond the T2 scrubber test; evidence is the verification artifact.
- **A criterion task's "done" is a clear verdict + scrubbed transcript**, not a PASS.
- **Every evidence commit passes the secret scan first (X3).** Risky tests stay sandboxed
  (X1). Deps stay isolated/recorded with production dep files untouched (X2).
- **`progress.md` is the single source of task truth;** `state.json` tracks only the phase.
