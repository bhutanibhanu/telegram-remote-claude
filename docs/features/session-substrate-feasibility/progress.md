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
- [ ] T5 — A-harness: persistent session lifecycle (start/resume/send/stop)
- [ ] T6 — A · C1 bidirectional streaming check
- [ ] T7 — A · C2 per-tool permission decision check (sandboxed)
- [ ] T8 — A · C3 AskUserQuestion answered programmatically
- [ ] T9 — A · C4 ExitPlanMode approve/reject + feedback
- [ ] T10 — C5 test skill fixture (emits permission + AskUserQuestion/ExitPlanMode)
- [ ] T11 — A · C5 skill invocation through the same channels
- [ ] T12 — A · C6 session resume by id across process runs
- [ ] T13 — B-harness: CLI `stream-json` driver + permission-mechanism discovery
- [ ] T14 — B · C2 permission decision over the wire (sandboxed)
- [ ] T15 — B · C3 AskUserQuestion over `stream-json`
- [ ] T16 — B · C4 ExitPlanMode over `stream-json`
- [ ] T17 — `run_all.py`: C1–C6 PASS/FAIL/PARTIAL matrix (+ B contingency)
- [ ] T18 — Draft normalized engine interface + final evidence secret-scan gate
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
- **Status:** todo

### T6 — A · C1 bidirectional streaming check
- **Goal:** Prove a persistent session can take a new operator message in mid-session and emit a continuous event stream out.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c1_streaming.py`, `evidence/c1.*`.
- **Acceptance:**
  - WHEN the check runs ≥2 turns over one live session, it SHALL capture the streamed events and emit `PASS/FAIL/PARTIAL` with the observed reason. **(X3)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

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
- **Status:** todo

### T8 — A · C3 AskUserQuestion answered programmatically ⭐
- **Goal:** Prove a multiple-choice question raised mid-session is intercepted and answered programmatically (no TTY), and the session proceeds on that answer.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c3_ask.py`, `evidence/c3.*`.
- **Acceptance:**
  - WHEN AskUserQuestion fires mid-session, the check SHALL answer it from code with no TTY and confirm the session continued on that answer — verdict + transcript. **(X3)** *(make-or-break)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

### T9 — A · C4 ExitPlanMode approve/reject + feedback ⭐
- **Goal:** Prove a proposed plan is surfaced and can be approved **or** rejected with feedback programmatically, and the session honors the verdict.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c4_plan.py`, `evidence/c4.*`.
- **Acceptance:**
  - WHEN ExitPlanMode surfaces a plan, the check SHALL approve it in one path and reject-with-feedback in another, confirming the session honored each — verdict + transcript. **(X3)** *(make-or-break)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

### T10 — C5 test skill fixture
- **Goal:** A minimal custom slash-command skill that deliberately emits a permission request **and** an AskUserQuestion (and/or ExitPlanMode), to drive C5 through the same channels as C2–C4.
- **Depends on:** T1
- **Files (expected):** `spikes/session-substrate/test_skill/` (skill definition).
- **Acceptance:**
  - WHEN the skill is invoked in-session, it SHALL emit at least one permission prompt and one interactive-tool prompt (AskUserQuestion and/or ExitPlanMode).
- **Tests:** none — fixture; exercised by T11.
- **Status:** todo

### T11 — A · C5 skill invocation through the same channels
- **Goal:** Prove the C5 test skill is invoked in-session and its interactive prompts flow through the **same** permission/question/plan channels proven in C2–C4, driven to completion.
- **Depends on:** T7, T8, T9, T10
- **Files (expected):** `spikes/session-substrate/checks/c5_skill.py`, `evidence/c5.*`.
- **Acceptance:**
  - WHEN the test skill runs, its permission + interactive prompts SHALL be answered over the same code paths as C2–C4 and the skill driven to completion — verdict + transcript. **(X3)**
  - Any tool the skill runs SHALL stay within the temp fixture sandbox. **(X1)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

### T12 — A · C6 session resume by id across process runs
- **Goal:** Prove a session can be resumed by id from a **fresh process** with history intact.
- **Depends on:** T5
- **Files (expected):** `spikes/session-substrate/checks/c6_resume.py`, `evidence/c6.*`.
- **Acceptance:**
  - WHEN run A starts a session and records its id, and run B (separate process) resumes that id, the check SHALL demonstrate retained context — verdict + transcript. **(X3)**
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

### T13 — B-harness: CLI `stream-json` driver + permission-mechanism discovery
- **Goal:** Drive `claude -p --input-format stream-json --output-format stream-json`, parse the event stream, and record how permission decisions are actually answered over the wire on the installed CLI version (the design flags `--permission-prompt-tool` as absent on v2.1.183 — record the real mechanism).
- **Depends on:** T4
- **Files (expected):** `spikes/session-substrate/harness_cli.py`, `evidence/cli_permission_mechanism.*`.
- **Acceptance:**
  - WHEN the harness runs, it SHALL parse the `stream-json` event stream and record the observed event/message shapes.
  - It SHALL record the actual on-the-wire permission mechanism on the installed CLI version (flag, hook, MCP permission tool, or none) as evidence. **(X3)**
- **Tests:** none — harness; exercised by T14–T16.
- **Status:** todo

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
- **Status:** todo

### T15 — B · C3 AskUserQuestion over `stream-json` ⭐
- **Goal:** Prove (or disprove) that an AskUserQuestion can be answered programmatically over the CLI `stream-json` protocol.
- **Depends on:** T13
- **Files (expected):** `spikes/session-substrate/checks/c3_ask_cli.py`, `evidence/c3_cli.*`.
- **Acceptance:**
  - WHEN AskUserQuestion appears in the stream, the check SHALL answer it over the wire and confirm continuation — verdict + transcript. **(X3)** *(make-or-break; tested on B unconditionally)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

### T16 — B · C4 ExitPlanMode over `stream-json` ⭐
- **Goal:** Prove (or disprove) that a plan can be approved/rejected-with-feedback programmatically over the CLI `stream-json` protocol.
- **Depends on:** T13
- **Files (expected):** `spikes/session-substrate/checks/c4_plan_cli.py`, `evidence/c4_cli.*`.
- **Acceptance:**
  - WHEN ExitPlanMode appears in the stream, the check SHALL approve in one path and reject-with-feedback in another, confirming honored behavior — verdict + transcript. **(X3)** *(make-or-break; tested on B unconditionally)*
- **Tests:** none — verdict + transcript is the artifact.
- **Status:** todo

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
- **Status:** todo

### T18 — Draft normalized engine interface + final evidence secret-scan gate
- **Goal:** From the observed event/decision shapes, draft the "events in / decisions out" normalized engine interface P1 inherits; then run the consolidated secret scan over all transcripts + staged files and record the clean result before evidence is committed.
- **Depends on:** T17
- **Files (expected):** `spikes/session-substrate/normalized_interface.md`, `spikes/session-substrate/evidence/secret-scan.txt`.
- **Acceptance:**
  - The drafted interface SHALL cover Events out (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`), Decisions in (permission verdict, question answer, plan verdict, free-text reply, cancel), and Lifecycle (`start`, `resume`, `send`, `stop`) — a draft contract, not an implementation.
  - WHEN evidence is about to be committed, an automated secret scan SHALL run over **all transcripts and the staged files**, and a **clean result SHALL be recorded** in `evidence/secret-scan.txt`; a non-clean scan blocks the commit. **(X3, consolidated)**
- **Tests:** none — design artifact + recorded scan result.
- **Status:** todo

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
