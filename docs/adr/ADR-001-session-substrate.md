# ADR-001 — Session Substrate for the Interactive Claude Code Remote

> **P0 spike complete; Proposed pending G-ADR.** Filled by the session-substrate feasibility
> spike from recorded evidence. The owner accepts (or revises) this at **G-ADR**; until then no
> later pipeline (P1+) is scoped or built.

- **Status:** **Proposed** — P0 spike complete; Proposed pending G-ADR (owner decides)
- **Date:** 2026-06-20 (Proposed; decided by owner at G-ADR)
- **Deciders:** repo owner
- **Related:** [`docs/interactive-remote-design.md`](../interactive-remote-design.md) ·
  [`docs/cross-cutting-requirements.md`](../cross-cutting-requirements.md) ·
  [`docs/features/session-substrate-feasibility/design.md`](../features/session-substrate-feasibility/design.md) ·
  spike: `spikes/session-substrate/` (evidence: `spikes/session-substrate/evidence/`,
  normalized interface: `spikes/session-substrate/normalized_interface.md`)

---

## Context

The remote must drive an **interactive** Claude Code session programmatically from Python:
stream Claude's activity out (to relay to Telegram), feed operator replies back in, answer
**permission requests**, and answer **interactive tools** (AskUserQuestion, ExitPlanMode) that
skills such as `/grill` and `/pipeline` emit. The current one-shot `claude -p --output-format json`
runner cannot do any of this.

A prior doc-only research pass was **unreliable** (it wrongly concluded the Claude Agent SDK does
not exist, and it never empirically tested whether interactive tools can be answered
programmatically). Therefore this decision is settled by a **throwaway spike that actually runs the
session**, not by documentation alone.

**This ADR is a genuine gate (GATE 1).** If neither substrate can answer interactive prompts
programmatically, the product's headline capability changes shape ("constrained mode") and the
roadmap is re-planned before P1.

**GATE 1 outcome (this spike): GREEN.** Both make-or-break criteria — C3 (AskUserQuestion) and
C4 (ExitPlanMode) — were answered programmatically with **no TTY on BOTH substrates** (Agent SDK
and raw CLI `stream-json`). The constrained-mode / re-plan risk is **retired**. See the matrix at
`spikes/session-substrate/evidence/matrix.md` and the Evidence log below.

**Environment of record.** Python 3.14.5; `claude-agent-sdk==0.2.105`; `claude` CLI **2.1.183**
(preflight) / **2.1.185** (C-criterion checks, observed). Host CLI auth only — **no API key**, no
paid API path (confirmed unset throughout the spike). All results were observed on a **macOS dev
host** (per design); cross-platform behavior — especially path sanitization for the cwd-scoped
resume transcript path — is **unproven**.

---

## Decision drivers / success criteria

The spike produced a documented **yes / no / partial (+ how)** for each criterion, each backed by a
runnable check and a scrubbed transcript. Results (substrate **A** = Agent SDK, **B** = raw CLI
`stream-json`):

- [x] **C1 — Bidirectional streaming.** **A = PASS** · B = N/A (design S1; A passed a
      non-make-or-break criterion). ≥2 turns over one persistent session; a mid-session operator
      message was accepted and a continuous event stream came out. *Streaming is coarse-grained
      (≈3–4 deltas/turn), not guaranteed token-level.*
- [x] **C2 — Per-tool permission decision.** **A = PASS · B = PASS.** Code is consulted before a
      risky tool runs; allow/deny is honored. **Substrate-neutral** (same mechanism on both).
- [x] **C3 — AskUserQuestion ⭐ (make-or-break).** **A = PASS (native) · B = PASS (native).** A
      multiple-choice question is intercepted and answered programmatically (no TTY) via the
      documented `AskUserQuestionOutput.answers` map on the **allow** channel; the session proceeds
      on the code-selected answer. **Substrate-neutral.** *(The A verdict was originally PARTIAL and
      was corrected to native PASS — see the Evidence log.)*
- [x] **C4 — Plan approval / ExitPlanMode ⭐ (make-or-break).** **A = PASS · B = PASS.** A plan is
      surfaced and approved (allow) or rejected-with-feedback (deny + message); the session honors
      each verdict. **Substrate-neutral.**
- [x] **C5 — Skill invocation.** **A = PASS** · B = N/A (design S1). A custom slash-command skill is
      invoked in-session and its interactive prompts flow through the same C2/C3 channels, driven to
      a code-driven completion.
- [x] **C6 — Session resume.** **A = PASS** · B = N/A (design S1). A session is resumed by id from a
      separate process with history intact. *Resume is **cwd/project-scoped**; persist
      `(session_id, cwd)`.*

**C3 and C4 are the make-or-break criteria — both PASS on both substrates ⇒ GATE 1 GREEN.**

---

## Options considered

### Option A — Claude Agent SDK (Python) — **CHOSEN as primary**
- **Summary:** persistent in-process client (`ClaudeSDKClient`) with a `can_use_tool` permission
  callback, skill loading, and session resume; drives Claude Code under the hood. Transport,
  control handshake, and session bookkeeping are owned by the SDK.
- **Package + version:** `claude-agent-sdk==0.2.105` (probed to exist, install into the spike venv,
  and import — refuting the prior "SDK does not exist" research). Evidence:
  `evidence/preflight.*`.
- **C1–C6 evidence (substrate A):**
  - **C1 PASS** — 2 turns over one stable `session_id`; genuine incremental `StreamEvent` deltas
    streamed *before* the terminal `ResultMessage` in both turns. Coarse-grained (≈3–4 deltas/turn);
    some runs also emit extended-thinking `signature_delta` events. `evidence/c1.*`.
  - **C2 PASS** — the SAME tool (Write) received opposite per-request decisions via `can_use_tool`
    (`permission_mode=default`, no bypass): denied → did not execute (file absent); allowed →
    executed (sentinel present). Resolved-path containment is **policy-level (deny-by-default), not
    an OS sandbox**. `evidence/c2.*`.
  - **C3 PASS (native)** — `AskUserQuestion` answered with no TTY by returning
    `PermissionResultAllow(updated_input={**input, "answers": {question_text: chosen_label}})`; the
    tool_result is **not** an error and the session continues on the code-chosen option. Proven
    code-driven (committed evidence: differing Alpha/Bravo picks each honored, plus native
    multi-select; an adversarial reviewer additionally reproduced an off-menu injected answer being
    honored verbatim — reviewer finding, not in the committed `evidence/c3.*` transcript).
    `evidence/c3.*`.
  - **C4 PASS** — `ExitPlanMode` (`permission_mode=plan`): approve = `PermissionResultAllow()` →
    "User has approved your plan"; reject = `PermissionResultDeny(message=feedback)` → "Plan
    rejected"; the model stayed in plan mode and revised on the code-injected feedback marker.
    `evidence/c4.*`.
  - **C5 PASS** — `spike-c5-probe` skill invoked in-session; its permission + AskUserQuestion prompts
    routed through the same C2/C3 callbacks; code-driven completion (differing picks echoed).
    `evidence/c5.*`.
  - **C6 PASS** — cross-process resume by id (same id, `fork_session` unset → continue) returned a
    code-planted codeword the resuming process never sent; two blind negative controls.
    `evidence/c6.*`.
- **Pros:**
  - Exercised + PASS on **all of C1–C6** (broadest proven coverage).
  - Managed transport: in-process `can_use_tool` callback, control handshake, NDJSON framing,
    timeouts, and session bookkeeping owned by the SDK — least code to maintain.
  - No dependency on undocumented CLI flags; the native answer/verdict mechanics are reached without
    touching CLI spawn args.
- **Cons:**
  - SDK is a third-party dependency at an early version (`0.2.105`) — version drift is a
    maintenance concern (mitigated: pin + monitor).
  - Same untested edges as the substrate-neutral mechanics below (free-text "Other", multi-question
    asks, crash-mid-turn / concurrent / aged / upgrade resume).

### Option B — `claude` CLI `stream-json` protocol — **proven, credible fallback**
- **Summary:** drive `claude --output-format stream-json --verbose -p --input-format stream-json
  [--permission-mode …] --permission-prompt-tool stdio …`; parse the NDJSON event stream; answer
  permission/interactive prompts over the **bidirectional control protocol**. Stdlib only — **no SDK
  import** (a genuinely independent fallback).
- **Observed event/message types** (`harness_cli.py`, `evidence/cli_permission_mechanism.*`):
  `system` (`subtype:init` — `session_id`, `tools`, `mcp_servers`, `permissionMode`,
  `slash_commands`, `model`, `cwd`); `assistant` / `user` (content blocks: `text` / `tool_use` /
  `tool_result`); `stream_event` (with `--include-partial-messages`); `result` (terminal per-turn
  frame); and the control plane `control_request` / `control_response` / `control_cancel_request`.
- **Permission mechanism on CLI 2.1.185 (the discovery — UPDATES the stale "flag absent on
  v2.1.183" note in the prior skeleton):** the bidirectional **stream-json control protocol**. The
  driver sends an `initialize` control_request; the CLI sends `can_use_tool` control_requests; the
  driver answers with a `control_response` of `{behavior:"allow", updatedInput:<input>}` or
  `{behavior:"deny", message:<reason>}`. This channel is **activated by the undocumented
  `--permission-prompt-tool stdio` spawn flag** — still **ABSENT from `claude --help`** on 2.1.185,
  but accepted and functional. **Verified live in both directions:** WITHOUT the flag, no
  `can_use_tool` reaches the driver (the CLI auto-decides from `--allowedTools` / `--permission-mode`,
  default-mode auto-DENY); WITH it, the round-trip works. It is **not** `--permission-prompt-tool
  <mcp>` and **not** "none". Wire gotcha: an **allow** control_response **must** carry `updatedInput`
  (else `ZodError ... expected record`); a wire **deny** still rides `subtype:"success"` (success =
  the round-trip completed, not that the tool was allowed).
- **C2/C3/C4 evidence (substrate B — design S1 scope):**
  - **C2 PASS** — same tool (Write) denied then allowed over the wire by resolved target; denied
    never executed; repo untouched. `evidence/c2_cli.*`.
  - **C3 PASS (native)** — AskUserQuestion arrives as an ordinary `can_use_tool` control_request (no
    special subtype) and is answered over the wire by an ALLOW carrying the native `answers` map;
    not an error; turn completes; code-driven (committed evidence: differing Alpha/Bravo picks each
    honored, plus native multi-select; the off-menu injection honored verbatim is an adversarial
    reviewer reproduction, not in the committed `evidence/c3_cli.*` transcript). `evidence/c3_cli.*`.
  - **C4 PASS** — ExitPlanMode approve (allow) / reject (deny + message feedback) honored over the
    wire; schema-confirmed there is **no native plan-feedback field** (binary allow/deny);
    feedback rides the deny `message`. `evidence/c4_cli.*`.
- **Pros:**
  - Fully independent of the SDK (stdlib only) — a real fallback if the SDK is unavailable/regresses.
  - On-the-wire evidence is arguably *stronger* for C2/C3/C4 (requires an actual `can_use_tool`
    control frame, not just an in-process callback log).
- **Cons:**
  - Couples directly to the **undocumented `--permission-prompt-tool stdio` flag** (absent from
    `--help`) — version-fragile; a missing/changed flag must be treated as fail-clean.
  - Hand-rolls NDJSON framing, the `initialize` handshake, reader/stderr threads, bounded timeouts,
    and the `updatedInput`-on-allow workaround — more surface to maintain than the SDK's managed path.
  - Coverage limited to C2/C3/C4 per design S1; **C1/C5/C6 on B are untested**.

### Option C — Hybrid (only if needed) — **NOT triggered**
- **When it would be required:** per design S1, Option C is recorded *only if* substrate A needs B
  (or hooks / workflow constraints) to cover the make-or-break C3/C4.
- **Why it is NOT required here:** substrate A covers **C3 and C4 natively** (C3 via the `answers`
  map on the allow channel; C4 via allow/deny + deny-message feedback), with no help from B or
  hooks. The trigger condition is not met, so Option C is **not** the decision.

---

## Decision

**Adopt Substrate A (`claude-agent-sdk==0.2.105`) as the primary session substrate, with Substrate
B (raw `claude` CLI `stream-json` control protocol) retained as a proven, credible fallback.
Option C (hybrid) is NOT adopted.**

Rationale: GATE 1 is GREEN — both make-or-break criteria pass on both substrates, so the headline
interactive capability is real. C2/C3/C4 are **substrate-neutral** (they PASS on both A and B via the
*same* allow/deny + native-`answers`-map + approve/reject-with-deny-message mechanics), so they do
**not** discriminate A from B. The **true differentiator is maintainability / managed transport** —
B couples to the undocumented, `--help`-absent `--permission-prompt-tool stdio` flag and hand-rolls
NDJSON framing, threads, timeouts, and the `updatedInput`-on-allow workaround, whereas A reaches the
identical mechanics through the SDK's managed in-process callback (least code to maintain). A
**supporting** point is coverage: A was exercised and PASSed on *all* of C1–C6, while B's C1/C5/C6
are **untested-by-design** (per S1, *because A already passed* — they are not failures). Since A and
B share the same control protocol, B's breadth is **expected-but-unproven**, so this is a
demonstrated-vs-expected gap, not a capability gap, and the margin it adds is modest. A
**B-primary choice would be defensible** on the same evidence; the decision (A primary) nonetheless
stands on the maintenance/managed-transport argument. B is not discarded — its independent
on-the-wire C2/C3/C4 evidence makes it a real fallback if the SDK is ever unavailable or regresses.
A hybrid is unnecessary because A needs no help from B/hooks to cover C3/C4. The owner accepts or
revises this at G-ADR.

## Normalized engine interface (output of P0)

The full draft contract is `spikes/session-substrate/normalized_interface.md` (a **specification,
not an implementation** — no engine is built in P0). It is grounded in the observed message/event/
decision shapes of **both** substrates; the load-bearing points are summarized here.

**Events out (engine → bot)** — seven kinds, each mapped from both substrates:
- `text` — assistant prose; incremental deltas available (A: `StreamEvent` `content_block_delta`;
  B: `stream_event` with `--include-partial-messages`). *Coarse-grained, not token-level (C1).*
- `tool_use` — model about to use a tool (`tool_name`, `tool_input`, `tool_use_id`).
- `ask` ⭐ — an `AskUserQuestion` (`questions[]` with `question`, `options[].label`, `multiSelect`).
- `plan` ⭐ — an `ExitPlanMode` plan (`plan: str`, `tool_use_id`).
- `error` — tool/turn/driver failure; clean failure, never a hang (RB2).
- `result` — terminal per-turn frame carrying `session_id`, `is_error`, `subtype`.
- `status` — lifecycle/health (`init` / `connected` / `disconnected` / `rate_limit`); the
  `rate_limit` carrier is the natural home for RB5 signals. A `rate_limit_event` frame WAS observed
  once in the CLI stream during T13 (`cli_permission_mechanism.transcript.txt` records event-type
  counts incl. `rate_limit_event:1`), so the carrier exists / was observed — but dedicated
  rate-limit **handling** (RB5) was not exercised or proven.

**Decisions in (bot → engine)** — five kinds, with the proven mechanism on both substrates:
- **permission verdict** — allow / deny [+reason] / modified-input. A:
  `PermissionResultAllow(updated_input=…)` / `PermissionResultDeny(message=…)`. B: control_response
  `{behavior:"allow", updatedInput:…}` / `{behavior:"deny", message:…}`. **Empirical contract:** an
  ALLOW on B **must** carry `updatedInput` (ZodError otherwise); allow-session vs allow-once is
  engine-side state over the per-request primitive.
- **question answer** ⭐ — the **native `answers` map keyed by question text → label** on the ALLOW
  channel (both substrates). Deny-with-message is a documented non-native FALLBACK only.
- **plan verdict** ⭐ — approve (allow) / reject-with-feedback (deny + `message`). **No native
  plan-feedback field** (schema-confirmed); feedback rides the deny message; whether it lands in the
  structured-plan field is model-variant (the robust signal is the model's revision reply).
- **free-text reply** — an ordinary mid-session operator message (same seam as `send`).
- **cancel** — A: `stop()` (disconnect); B: `control_cancel_request` (CLI→driver observed) /
  close stdin. Operator-initiated mid-turn cancel is UNTESTED-but-handled.

**Lifecycle calls:** `start`, `resume`, `send`, `stop` — mapped to `harness_sdk.py` (A) and
`harness_cli.py` (B). **Key empirical contract for `resume`:** transcripts persist at
`~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`; the id is **not** a global handle —
resuming the same id from a different cwd FAILS. Persist `(session_id, cwd)` and resume only from the
original cwd; `fork_session` unset continues the same id; the substrate does **not** prevent
double-attach.

## Consequences

**What P1 inherits.**
- The normalized engine interface above as the "events in / decisions out" contract its streaming
  engine is built against (`normalized_interface.md`).
- A primary substrate (A) proven on all of C1–C6 and a fallback (B) proven on C2/C3/C4.
- The retained spike tree (`spikes/session-substrate/`) as a re-runnable, non-production reference
  (design S3) — never imported by production; removable only via a separately reviewed P1 task once
  equivalent integration evidence exists.

**Normalized-interface gaps (P1+ must extend the drafted contract).**
- The drafted interface is **single-session / single-active-run.** P4 (multi-project) and P5
  (concurrency) must **extend** events + decisions with a **session/run correlation envelope** so an
  inbound answer routes to the correct pending request (e.g. the right `session_id` + `tool_use_id` /
  control-request id). This correlation key is **not** in the P0 contract.
- **Throttling / coalescing (RB5) is the consumer's responsibility** (or needs a hint added to the
  event stream) — the P0 contract carries no throttling/coalescing guarantee.

**Required workarounds (carry into P1).**
- **C2:** an ALLOW must carry the (possibly unchanged) tool input as `updatedInput` / `updated_input`
  — on B this is mandatory (ZodError otherwise); the engine preserves it.
- **C3:** answer AskUserQuestion via the **native `answers` map keyed on verbatim question text** on
  the ALLOW channel; build the map per-question; keep deny-with-message as a documented fallback.
- **C4:** reject-with-feedback rides the **deny `message`** channel (no native plan-feedback field).
  Approving a plan must **not** be treated as a blanket greenlight: post-approval execution was
  contained/untested in the spike, so P1 **MUST** ensure post-approval tool calls remain gated by the
  C2 permission path (this is a requirement P1 owns, not a behavior the spike proved).
- **C6:** persist `(session_id, cwd)` together and resume only from the original project cwd; **guard
  against concurrent / double-attach** at the engine level (the substrate does not).
- **B (only if B is ever used):** pin and monitor the CLI version; B's `can_use_tool` channel depends
  on the **undocumented `--permission-prompt-tool stdio` flag** — treat a missing/changed flag as
  fail-clean.

**Cross-cutting requirements this decision must address.**
- **SB3 (secret hygiene):** honored in the spike — every transcript passes a single scrub() chokepoint
  and a consolidated secret scan recorded CLEAN (`evidence/secret-scan.txt`); P1 carries the same
  no-secrets-in-logs discipline into production logging.
- **RB2 (clean failure):** both harnesses bound every turn (timeouts / idle timeouts) and fail clean
  rather than hang — P1's engine inherits this as the `error` event contract; never a silent hang.
- **RB3 (restart/resume correctness):** C6 proves the clean-restart happy path only; **crash-mid-turn
  (torn transcript), aged sessions, and resume across a CLI/SDK upgrade are NOT tested** — P1 must
  validate these and ensure an in-flight-at-crash turn fails clean.
- **RB6 (persistence integrity):** the `(session_id, cwd)` coupling and cwd-scoped transcript path are
  the persistence facts P1's atomic, `0600`, forward-compatible state must accommodate.
- **SB2/SB4 (path confinement / no injection):** the spike's containment was **policy-level, not an OS
  sandbox** (cwd is never a security boundary); P1 must implement real path confinement per SB2.
- **SB1 (authenticated decision-in):** every DECISION-IN (permission verdict, question answer, plan
  verdict, cancel, free-text reply) must be operator-**authenticated** before it reaches the engine,
  and button-callback taps must be allowlist-checked (a new attack surface introduced by the
  interactive UI). An unauthenticated callback must **never** approve a plan or allow a tool. P1 owns
  this.
- **SB5 (bypass off by default, loud when on):** the spike used **no bypass** (`permission_mode`
  default/plan only); `--dangerously-skip-permissions` is removed from the default path. P1's engine
  must keep any bypass OFF by default and LOUD when on — it must not introduce a bypass as a default.
- **SB6 (fail CLOSED):** the spike confirmed default-mode auto-**DENY** (incl. substrate B WITHOUT
  `--permission-prompt-tool stdio`, where no `can_use_tool` reaches the driver and the CLI
  auto-denies). P1 carries fail-closed defaults + a documented blast radius; a missing/changed B flag
  must fail closed (never fall open to auto-allow).

**Unresolved risks / NOT-tested (P1 must validate).**
- C3 free-text **"Other"** (the `response` field), `annotations`, and **multi-question** asks (schema
  allows 1–4 questions; only single-question, 2–3-option probes were run).
- C4 **post-approval ARBITRARY / out-of-fixture execution.** In the C4 approve trial the model DID
  write an in-fixture sentinel (`greet.py`) post-approval — i.e. one ALLOWED in-fixture Write
  executed — so it is not true that "only denied tools were tested." What is NOT tested is
  post-approval arbitrary / out-of-fixture execution (execution was otherwise contained:
  Bash / Edit / out-of-fixture Write / AskUserQuestion denied).
- C6 **crash-mid-turn / concurrent (double-attach) / aged / CLI-or-SDK-upgrade** resume.
- C1/C5/C6 on **substrate B** (untested per design S1).
- C1 streaming is **coarse-grained (≈3–4 deltas/turn), not token-level**.
- Dedicated rate-limit **handling** (RB5), allow-session scope, and operator mid-turn cancel (RB4)
  were not exercised. (A `rate_limit_event` frame was observed once in T13, so the `status`
  `rate_limit` carrier exists — but RB5 handling on it is unproven; CANCEL is proven only as
  full-disconnect via `stop()`, not as an operator-initiated mid-turn cancel.)
- **Shared CLI-version coupling.** A and B share the **same CLI control protocol**, so a `claude`
  CLI upgrade can break **both** substrates at once; the SDK merely relocates the coupling into its
  own compat matrix. The B fallback is **NOT** version-independent of A.
- **Async permission latency (single most load-bearing unproven assumption).** Every C2/C3/C4 result
  was answered **synchronously, in-process**. Production must hold a permission callback /
  `control_request` open while a human taps a Telegram button (potentially up to the 60-min backstop).
  Whether the SDK callback / CLI control round-trip tolerates a multi-minute delay is **UNTESTED**.
- **C4 reject-feedback is model-interpretation-dependent on BOTH substrates** — there is no
  structured plan-feedback channel; feedback rides the deny `message` and depends on the model
  reading/revising per that text, so it is sensitive to model updates.

**What P1 MAY assume.**
- Interactive prompts (permission, AskUserQuestion, ExitPlanMode) **can** be answered programmatically
  with no TTY on the chosen substrate (A) — GATE 1 is GREEN.
- The single-question AskUserQuestion native `answers`-map answer path works and is code-driven.
- ExitPlanMode approve/reject-with-feedback works via allow / deny+message.
- A session can be resumed cross-process by id from the **same cwd** with history intact.
- A coarse-grained outgoing event stream (text deltas before completion) is available.
- Substrate B is a viable fallback for the make-or-break C2/C3/C4 if needed.
- Live rendering will require **consumer-side coalescing / throttling**: streaming is coarse-grained
  (≈3–4 deltas/turn) with **no throttling contract** from the substrate. RB5 is P1's first
  reliability requirement and is the **least-supported by P0** — assume P1 builds it, not that the
  substrate provides it.
- CANCEL is proven **only** as a full-disconnect (`stop()`), **not** as an operator-initiated
  mid-turn cancel (RB4 unproven) — assume P1 must build/validate mid-turn cancel.

**What P1 MUST NOT assume.**
- That free-text "Other" or multi-question AskUserQuestion asks work (unproven).
- That a plan-feedback **field** exists, or that approving a plan greenlights arbitrary execution
  (post-approval execution stays permission-gated and is untested).
- That a session id is a global handle (it is **cwd/project-scoped**), that the substrate prevents
  double-attach, or that crash-mid-turn / aged / upgrade resume is safe (all untested).
- That streaming is token-level.
- That `cwd` is a security boundary, or that spike containment equals an OS sandbox.
- That substrate B works on C1/C5/C6 (untested), or that its `--permission-prompt-tool stdio` flag is
  stable across CLI versions (it is undocumented).

**Migration note (from the current one-shot runner).** The current prod path is `claude_tg/bot.py` →
`claude_tg/claude_runner.py` (package-relative import) running one-shot `claude -p --output-format
json`. P1 replaces this with a persistent, streaming session engine built on Substrate A behind the
normalized interface above (`start` / `resume` / `send` / `stop`; events out / decisions in). The
EXISTING runner already implements **`(session_id, cwd)` persistence**, **`--resume`**, a
**resume-failure recovery heuristic** (`_is_resume_failure` → clear session + retry fresh), and a
**per-chat in-flight lock** (`asyncio.Lock` per chat, `ClaudeBusy`) — these are assets P1 should
**HARVEST, not rebuild**. The real shift is **not merely swapping a runner behind an interface**: it
is introducing a **STATEFUL, LONG-LIVED bidirectional connection**, which brings a new failure class
(idle / wedge / reconnect / double-attach) the one-shot runner never had. The one-shot runner is
superseded, not extended; per the Test policy, obsolete one-shot tests may be rewritten/removed in
favor of streaming + interaction + SB/RB coverage. The spike tree imports nothing from prod and prod
imports nothing from the spike; it is reference only.

## Evidence log

Per criterion: what was run, what was observed, evidence path, reviewer confidence. SDK
`claude-agent-sdk==0.2.105`; CLI 2.1.183 (preflight) / 2.1.185 (checks); host CLI auth, **no API
key**. Matrix: `spikes/session-substrate/evidence/matrix.{md,json}`. Preflight:
`evidence/preflight.*` (Python 3.14.5; SDK exists/installs/imports — refutes prior research). CLI
mechanism: `evidence/cli_permission_mechanism.*` (control protocol via undocumented
`--permission-prompt-tool stdio`).

| Crit | Substrate | Verdict | What was run / observed | Evidence | Reviewer confidence |
|------|-----------|---------|-------------------------|----------|---------------------|
| **C1** | A | **PASS** | 2 turns over one stable `session_id`; mid-session message accepted; incremental `StreamEvent` deltas streamed *before* terminal `ResultMessage` (turn1 incr=3, turn2 incr=3). Coarse-grained, not token-level; some runs emit `signature_delta`. | `evidence/c1.*` | 2 independent reviewers: ALL PASS |
| C1 | B | N/A | Design S1: A PASS on a non-make-or-break criterion ⇒ B not run. | — | — |
| **C2** | A | **PASS** | Same tool (Write) denied then allowed via `can_use_tool` (`permission_mode=default`, no bypass); denied did not execute; allowed produced sentinel; repo untouched. Containment policy-level, not OS sandbox. | `evidence/c2.*` | 2 reviewers: ALL PASS (19/19 each) |
| **C2** | B | **PASS** | Same over the wire: `can_use_tool` control_request → deny honored (file absent), allow honored (sentinel). Same tool opposite per-request decisions; repo untouched. | `evidence/c2_cli.*` | 2 reviewers (incl. adversarial): AGREE PASS |
| **C3** ⭐ | A | **PASS (native)** | AskUserQuestion answered no-TTY via `PermissionResultAllow(updated_input={…, "answers": {question_text: label}})`; tool_result not an error; session continues on code pick. Code-driven in the committed transcript via differing Alpha/Bravo picks (+ native multi-select); an adversarial reviewer separately reproduced an off-menu "Zucchini" answer honored verbatim (reviewer finding, not in `evidence/c3.*`). **Originally PARTIAL → CORRECTED to native PASS** when adversarial review found the right shape (the prior probe injected the wrong shape). | `evidence/c3.*` | 3 reviewers (incl. adversarial off-menu + ADR specialist): AGREE PASS, reproduced |
| **C3** ⭐ | B | **PASS (native)** | Same over the wire: AskUserQuestion is an ordinary `can_use_tool`; answered by ALLOW carrying the native `answers` map; not an error; turn `success`. Code-driven in the committed transcript via differing Alpha/Bravo picks (+ native multi-select); off-menu injection honored is an adversarial reviewer reproduction, not in `evidence/c3_cli.*`. Substrate-neutral with A. | `evidence/c3_cli.*` | 3 reviewers: AGREE PASS, reproduced |
| **C4** ⭐ | A | **PASS** | ExitPlanMode (`permission_mode=plan`): approve=`Allow` → "User has approved your plan"; reject=`Deny(message=feedback)` → "Plan rejected"; model stayed in plan mode and revised on code marker `ADD_LOGGING_STEP`. No native feedback field (binary allow/deny); structured-plan placement model-variant (gate = full plan OR revision reply). On approve, one ALLOWED in-fixture Write (`greet.py`) executed post-approval; post-approval ARBITRARY / out-of-fixture execution NOT tested (Bash/Edit/out-of-fixture-Write/AskUserQuestion denied). | `evidence/c4.*` | 2 reviewers (incl. adversarial-reproducibility): AGREE PASS |
| **C4** ⭐ | B | **PASS** | Same over the wire: approve(allow)/reject(deny+message) honored; schema deep-dive confirmed no native plan-feedback field (the `plan_approval_request` path is the multi-agent subsystem, unreachable via single-agent `can_use_tool`); enriched deny fields ignored (only `message` passes). Substrate-neutral with A. | `evidence/c4_cli.*` | 3 reviewers (incl. schema/binary deep-dive): AGREE PASS |
| **C5** | A | **PASS** | `spike-c5-probe` skill invoked in-session (Skill tool fired); permission + AskUserQuestion prompts routed through the C2/C3 callbacks; driven to `C5_DONE`; code-driven (Alpha/Bravo differ). Containment policy-level; repo untouched. (This run used the deny-message fallback for the ask; native path equally available.) | `evidence/c5.*` | 2 reviewers: AGREE PASS, reproduced |
| C5 | B | N/A | Design S1: A PASS on a non-make-or-break criterion ⇒ B not run. | — | — |
| **C6** | A | **PASS** | Cross-process resume by id (same id, `fork_session` unset → continue) returned a code-planted codeword the resuming process never sent; two blind negative controls. **Resume is cwd/project-scoped** (diff-cwd resume FAILS). Crash-mid-turn / concurrent / aged / upgrade resume NOT tested. | `evidence/c6.*` | 4 reviewers (incl. recovery specialist): ALL AGREE PASS |
| C6 | B | N/A | Design S1: A PASS on a non-make-or-break criterion ⇒ B not run. | — | — |

**Matrix summary:** C1 A=PASS/B=N/A · C2 A=PASS/B=PASS · C3 A=PASS/B=PASS · C4 A=PASS/B=PASS ·
C5 A=PASS/B=N/A · C6 A=PASS/B=N/A. **No FAIL, no PARTIAL.** Secret scan over the spike tree:
**CLEAN** (`evidence/secret-scan.txt`).
