# Normalized engine interface — P0 DRAFT (inherited by P1)

> **Status: P0 DRAFT contract.** Drafted by the session-substrate spike (T18) from the
> EMPIRICALLY OBSERVED message/event/decision shapes of the two candidate substrates. This is the
> "events out / decisions in" contract P1's streaming engine is built against — **a specification,
> not an implementation.** No engine is built in P0 (design anti-goal). Every shape below is cited
> to the spike harness/check that observed it; nothing here is invented beyond what was recorded.
>
> - **Substrate A** = `claude-agent-sdk==0.2.105` in-process Agent SDK
>   (`harness_sdk.py`; checks `c1_streaming.py`, `c2_permission.py`, `c3_ask.py`, `c4_plan.py`,
>   `c5_skill.py`, `c6_resume.py`).
> - **Substrate B** = raw `claude` CLI 2.1.185 over the bidirectional `stream-json` control
>   protocol, stdlib only, NO SDK import (`harness_cli.py`; checks `c2_permission_cli.py`,
>   `c3_ask_cli.py`, `c4_plan_cli.py`).
> - Evidence verdicts: `evidence/matrix.md`. GATE-1 GREEN — C1–C6 all PASS on A; the make-or-break
>   C2/C3/C4 PASS on BOTH A and B.
>
> **Sources of truth for ADR-001.** The contract is consistent with the recorded evidence and makes
> no claim beyond what was observed. Empirical caveats are flagged inline as **[FLAG]**.

---

## 0. Shape glossary (what each substrate emits/accepts on the wire)

These are the raw building blocks the normalized contract maps onto. Both are observed live.

**Substrate A (SDK message objects, from `harness_sdk.py` + checks):**
- `SystemMessage` — `subtype` (`init`/...), `data` dict carrying `session_id`, tools, model, etc.
- `AssistantMessage` — `content: list[block]` where a block is `TextBlock(.text)`,
  `ToolUseBlock(.name, .input)`, or `ToolResultBlock(.is_error, .content)`.
- `StreamEvent` — incremental deltas when `include_partial_messages=True`; `.event["type"]` is
  `content_block_delta` / `message_delta` (incremental) or `message_start` / `content_block_start` /
  `content_block_stop` / `message_stop` (framing). (`c1_streaming.py` `INCREMENTAL_EVENT_TYPES`.)
- `UserMessage` — carries tool_result blocks echoed back into the turn.
- `ResultMessage` — terminal per-turn frame: `.subtype`, `.is_error`, `.session_id`, cost/turns.
- Permission callback `can_use_tool(tool_name, tool_input, context)` returning
  `PermissionResultAllow(updated_input=...)` or `PermissionResultDeny(message=...)`.

**Substrate B (NDJSON `stream-json` frames, from `harness_cli.py` docstring + checks):**
- `system` (`subtype:init`) — carries `session_id`, `tools`, `mcp_servers`, `permissionMode`,
  `slash_commands`, `model`, `cwd`.
- `assistant` / `user` — `message.content[]` block list; block `type` ∈ `text` / `tool_use`
  (`.name`, `.input`) / `tool_result` (`.is_error`, `.content`).
- `stream_event` — only with `--include-partial-messages`.
- `result` — terminal per-turn frame: `subtype` (`success`/`error_*`), `is_error`, `session_id`,
  `num_turns`, `duration`, `usage`, `total_cost_usd`, `result` text.
- `control_request` / `control_response` — the control plane. The CLI sends `can_use_tool`
  control_requests (`request.tool_name`, `request.input`, `request.permission_suggestions`); the
  driver answers with a `control_response` whose `response` is `{behavior:"allow", updatedInput}` or
  `{behavior:"deny", message}`. (Note: a deny still rides a `subtype:"success"` control_response —
  "success" = the round-trip, not allowing the tool.)
- `control_cancel_request` — CLI abandons an in-flight control_request (carries the original
  request_id). **[FLAG]** Observed 0 times across every C3/C4 flow; the harness branch is defensive.

---

## 1. Events out (engine → bot)

Seven event kinds. For each: purpose, fields, and the mapping from BOTH substrates.

### `text`
- **Purpose:** assistant prose / model reply chunks to render to the operator.
- **Fields:** `text: str`, `incremental: bool` (token-delta vs assembled), `turn_id`.
- **A:** `AssistantMessage` → concatenate `TextBlock.text` (`harness_sdk.assistant_text`); incremental
  deltas via `StreamEvent` `content_block_delta` when `include_partial_messages=True` (C1 PASS:
  genuinely incremental deltas streamed before completion in both turns, `c1.json`).
- **B:** `assistant` frame → `message.content[]` blocks with `type:"text"` (`c3_ask_cli.py` text
  accumulation); incremental via `stream_event` frames with `--include-partial-messages`.

### `tool_use`
- **Purpose:** the model is about to use a tool — surface "what it's doing" before the result.
- **Fields:** `tool_name: str`, `tool_input: dict` (rendered safely — log lengths, not bodies; see
  `safe_input_summary` in `c2_permission.py`), `tool_use_id`.
- **A:** `ToolUseBlock(.name, .input)` inside an `AssistantMessage` (`c2_permission.stream_events`).
- **B:** `assistant` frame block with `type:"tool_use"` (`.name`, `.input`)
  (`c2_permission_cli.summarize_block`).

### `ask`  ⭐ (make-or-break C3 — answered NATIVELY on both substrates)
- **Purpose:** surface an `AskUserQuestion` to the operator as a multiple-choice prompt.
- **Fields:** `questions: list[{ question:str, header:str, options:[{label, description}],
  multiSelect:bool }]`, `tool_use_id`.
- **A:** the model issues an `AskUserQuestion` `ToolUseBlock` routed through `can_use_tool`; the
  question schema is `tool_input["questions"][0]` with `.question`, `.options[].label`,
  `.multiSelect` (`c3_ask.question_of/options_of/multiselect_of`).
- **B:** arrives as an **ordinary `can_use_tool` control_request — NO distinct subtype**; the schema
  is in `request.input.questions[]`; `meta` carries `display_name` + `tool_use_id`
  (`c3_ask_cli.py` mechanism note). **[FLAG]** not special-cased on the wire.
- See §2 "question answer" for how the bot's answer is delivered.

### `plan`  ⭐ (make-or-break C4 — approve/reject honored on both substrates)
- **Purpose:** surface a proposed plan (`ExitPlanMode`) for approve/reject.
- **Fields:** `plan: str` (the proposed plan text), `tool_use_id`.
- **A:** `ExitPlanMode` `ToolUseBlock` in `permission_mode="plan"`, routed through `can_use_tool`;
  plan text is `tool_input["plan"]` (`c4_plan.py`).
- **B:** ordinary `can_use_tool` control_request (no distinct subtype) when running
  `--permission-mode plan`; plan text rides `request.input.plan` (`c4_plan_cli.py` mechanism note).
- **[FLAG]** `ExitPlanModeOutput` schema has NO native feedback/rejectionReason field — approve/reject
  is a binary allow/deny (schema-confirmed in `c4_plan_cli.py`). See §2 "plan verdict".

### `error`
- **Purpose:** a tool failed, or the substrate/turn failed — render a clean failure, never a hang
  (RB2 / fail-clean).
- **Fields:** `kind` (`tool_error` / `turn_error` / `driver_error`), `message: str`,
  `is_error: bool`, optional `tool_use_id`.
- **A:** `ToolResultBlock.is_error == True`; or a `ResultMessage` with `.is_error == True`; or an
  exception surfaced by the bounded `send()` (`harness_sdk.send` `asyncio.wait_for` timeout).
- **B:** `tool_result` block with `is_error:true`; or a `result` frame `subtype:"error_*"` /
  `is_error:true`; or a `CLIDriverError` from a turn/idle timeout, dead subprocess, or stdout EOF
  (`harness_cli.send` bounded two ways → fails clean, never hangs).

### `result`
- **Purpose:** the turn is complete — carries the terminal status + the `session_id` to persist.
- **Fields:** `session_id: str`, `is_error: bool`, `subtype` (`success`/`error_*`), `num_turns`,
  cost/usage (where present).
- **A:** `ResultMessage` (`.session_id`, `.subtype`, `.is_error`); iteration ends after it
  (`harness_sdk.send`). `session_id` first captured from `SystemMessage.data` / `ResultMessage`.
- **B:** `result` frame; the turn's NDJSON stream terminates at it (`harness_cli.send` returns on
  `type=="result"`). `session_id` captured from `system/init` and refreshed from `result`.

### `status`
- **Purpose:** non-content lifecycle/health signals — session init, model/tooling, rate limits,
  connect/disconnect — for operator-facing status and throttling (RB5).
- **Fields:** `phase` (`init` / `connected` / `disconnected` / `rate_limit`), `session_id`,
  `model`, `tools`, `permission_mode`, plus any rate-limit detail.
- **A:** `SystemMessage` (`subtype:init`, `data` carries session_id/model/tools); connect/disconnect
  bracket the session (`harness_sdk.start/stop`).
- **B:** `system` (`subtype:init`) frame carrying `session_id` / `permissionMode` / `model` /
  `tools` / `mcp_servers` / `slash_commands` (`harness_cli` `init_frame`); the `initialize`
  control handshake reply carries commands/models/account/pid.
- **[FLAG]** A dedicated `rate_limit_event` was NOT exercised by the spike (text/permission flows
  only); listed as the natural carrier for RB5 signals, to be confirmed in P1.

---

## 2. Decisions in (bot → engine)

Five decision kinds. For each: the proven mechanism on BOTH substrates, plus empirical flags.

### permission verdict — `allow-once` / `allow-session` / `deny [+reason]` / `modified-input`
- **A:** return from `can_use_tool`: `PermissionResultAllow(updated_input=...)` to allow (optionally
  rewriting input), `PermissionResultDeny(message=reason)` to deny. Proven C2 PASS: same tool
  (Write) denied once + allowed once, denied never executed, repo untouched (`c2.json`).
- **B:** answer the `can_use_tool` control_request with a `control_response` `response` of
  `{behavior:"allow", updatedInput:<input>}` or `{behavior:"deny", message:"<reason>"}`
  (`harness_cli.allow_tool/deny_tool`). Proven C2 PASS over the wire (`c2_cli.json`).
- **[FLAG] `updatedInput` gotcha (B):** an ALLOW control_response MUST carry `updatedInput` as a
  record; omitting it returns `Tool permission request failed: ZodError ... expected record` and the
  tool fails. The harness defaults `updatedInput` to the original tool input on allow (mirrors the
  SDK). The engine MUST preserve this on B. (Verified live; `harness_cli.allow_tool` docstring.)
- **[FLAG] allow-once vs allow-session:** both substrates expose per-request allow/deny (proven).
  Session-scoped "allow for the rest of the session" was NOT separately exercised by the spike — P1
  implements it as engine-side state that auto-allows matching subsequent requests (the substrate
  primitive is still the per-request verdict above).
- **[FLAG] modified-input** (`updatedInput` / `updated_input`) is the proven channel for rewriting a
  tool call before it runs; it is also the carrier for the native question answer (below).

### question answer (AskUserQuestion) — the NATIVE `answers` map  ⭐
- **Proven mechanism (both substrates, C3 PASS native):** answer on the **ALLOW channel** by
  returning an allow whose updated input carries the documented `AskUserQuestionOutput` **`answers`
  map keyed by QUESTION TEXT → selected option label**:
  - **A:** `PermissionResultAllow(updated_input={**tool_input, "answers": {question_text: label}})`
    (`c3_ask.py`). tool_result is NOT an error and reads *"Your questions have been answered… You can
    now continue with these answers in mind."* (`c3.json`).
  - **B:** `{behavior:"allow", updatedInput:{**input, "answers":{question_text: label}}}` via
    `allow_tool(updated_input=...)` (`c3_ask_cli.py`); turn `result` subtype `success` (`c3_cli.json`).
  - Multi-select: comma-separated labels in the one answer string keyed by the question.
  - Proven CODE-DRIVEN: two trials with different code picks each echoed the code's choice.
- **[FLAG] deny-with-message is a documented FALLBACK, not native:** `deny(message="The user
  selected: <label>")` also drives the session but rides the error/deny channel
  (`tool_result is_error`) and depends on the model reading prose. Retained as a secondary path only
  (`c3_ask.py` / `c3_ask_cli.py`), and is what C5 happened to exercise.
- **[FLAG] UNPROVEN:** free-text **"Other"** (the `response` field) and **multi-question** asks
  (1–4 questions in one AskUserQuestion) beyond the single 2–3-option probe were not exercised.

### plan verdict (ExitPlanMode) — `approve` / `reject +feedback`
- **A:** approve = `PermissionResultAllow()` → native *"User has approved your plan. You can now start
  coding."*; reject = `PermissionResultDeny(message=<feedback>)` → *"Plan rejected. <feedback>"*; the
  model stays in plan mode and revises (`c4_plan.py`, C4 PASS `c4.json`).
- **B:** approve = `{behavior:"allow"}`; reject = `{behavior:"deny", message:<feedback>}` via
  `deny_tool(...)` (`c4_plan_cli.py`, C4 PASS `c4_cli.json`).
- **[FLAG] NO native plan-feedback field** (schema-confirmed: `ExitPlanModeOutput` has
  `plan/isAgent/filePath/hasTaskTool/planWasEdited/awaitingLeaderApproval/requestId`, no
  feedback/rejectionReason). **Reject feedback rides the DENY `message` channel** — the model must
  read and revise per that text. Whether the injected feedback lands in the structured plan field is
  **model-variant**; the proven-robust signal is the model's revision reply.
- **[FLAG] post-approval execution is still gated by the permission path** — approving a plan does
  NOT bypass per-tool permission; subsequent tool calls still route through the permission verdict
  above. The spike contained execution (Bash/Edit/out-of-fixture Write denied) and did NOT test
  arbitrary post-approval execution.

### free-text reply
- **Purpose:** an ordinary operator message mid-session (not answering a tool prompt).
- **A:** `harness.send(prompt)` → SDK `query(prompt)` opens a new turn over the same session.
- **B:** write a `user` frame on stdin: `{type:"user", message:{role:"user", content:<prompt>},
  parent_tool_use_id:null, session_id:"default"}` then read the turn (`harness_cli.send`).
- This is the same seam as `send` in §3; "free-text reply" is its decision-side framing.

### cancel
- **Purpose:** abort a waiting/in-flight run cleanly (RB4).
- **A:** disconnect via `harness.stop()` (terminates the CLI subprocess cleanly, idempotent).
- **B:** the wire has a `control_cancel_request` (CLI→driver direction is observed; the SDK mirrors
  it as cancel-with-no-response); driver-initiated cancel maps to closing stdin / `stop()`
  (terminate→kill→join, idempotent).
- **[FLAG] UNTESTED-BUT-HANDLED on B:** `control_cancel_request` was observed 0 times across all
  C3/C4 flows; the harness branch that drops it (so it is not mis-delivered as a turn event) is
  defensive. Operator-initiated mid-turn cancel is a P1 RB4 concern, not proven by the spike.

---

## 3. Lifecycle calls

Four calls; mapped to both substrates (`harness_sdk.py` / `harness_cli.py` seams).

### `start`
- **A:** construct `SDKSessionHarness(cwd, permission_mode, can_use_tool, ...)`, `await start()` —
  `ClaudeSDKClient.connect()` on a fresh persistent session (host CLI auth; no API key).
- **B:** `CLISessionHarness(...).start()` spawns
  `claude --output-format stream-json --verbose -p --input-format stream-json
  [--permission-mode …] [--permission-prompt-tool stdio] [--allowedTools …] [--disallowedTools …]`
  then `initialize()` (the control handshake) before the first send.

### `resume`
- **A:** `harness.resume(session_id)` → `ClaudeAgentOptions(resume=session_id)` then connect
  (`harness_sdk.resume`). C6 PASS: cross-process resume retained context (`c6.json`).
- **B:** constructor `resume=<session_id>` → `--resume <id>` spawn arg (seam exposed; the rigorous
  cross-process proof was run on A per design S1).
- **[FLAG] resume is CWD/PROJECT-SCOPED (C6 key finding):** the CLI persists transcripts at
  `~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`. The session id ALONE is not a global
  handle — resuming the same id from a DIFFERENT cwd FAILS with *"No conversation found with session
  ID"*. **The engine MUST persist `(session_id, cwd)` together and resume only from the original
  cwd.** (`c6_resume.py` cwd-coupling probe.)
- **[FLAG] `fork_session` left unset (default False) → resume CONTINUES the same id** (does not fork
  to a new id). **[FLAG] The substrate does NOT prevent double-attach** — the engine MUST guard
  against concurrent/double resume of the same id (`c6_resume.py` limitations).

### `send`
- **A:** `async for msg in harness.send(prompt, timeout=…)` — one operator turn; yields SDK messages;
  iteration ends after the turn's `ResultMessage`; bounded by `timeout` (fail-clean, RB2).
- **B:** `for ev in harness.send(prompt, turn_timeout=…, idle_timeout=…)` — writes the `user` frame,
  yields parsed NDJSON dicts; ends at the `result` frame; bounded by total wall-clock AND per-line
  idle timeout (fail-clean, RB2). `can_use_tool` is answered out-of-band by the reader thread.

### `stop`
- **A:** `await harness.stop()` — `disconnect()`, terminate the CLI subprocess; idempotent.
- **B:** `harness.stop()` — close stdin, `terminate()`→`kill()`, join reader/stderr threads; no
  leaked process; idempotent.

---

## 4. Substrate notes (A vs B)

Both substrates expose the **same logical contract** — the events, decisions, and lifecycle above map
onto each. The make-or-break interactions (C2 permission, C3 AskUserQuestion, C4 ExitPlanMode) are
**substrate-neutral**: each PASSes on BOTH via the same allow/deny + native-`answers`-map +
approve/reject-with-deny-message mechanics, so they do not by themselves decide A vs B.

The asymmetry is in **maintenance and version-coupling**, not capability:

- **B couples directly to the UNDOCUMENTED `--permission-prompt-tool stdio` spawn flag.** It is
  ABSENT from `claude --help` on CLI 2.1.185 but accepted and functional — it is THE switch that
  routes `can_use_tool` permission checks back to the driver. Without it the CLI silently
  auto-decides tools from `--allowedTools`/`--permission-mode` and never sends `can_use_tool`
  (verified live in `cli_permission_mechanism`). B also hand-rolls NDJSON framing, the `initialize`
  handshake, reader/stderr threads, bounded timeouts, and the `updatedInput`-on-allow workaround.
  This is more code to maintain and is coupled to undocumented CLI internals across versions — pin /
  monitor the CLI version and treat a missing/changed flag as fail-clean.
- **A (the SDK) provides the same contract MANAGED:** the in-process `can_use_tool` callback,
  transport, handshake, and session bookkeeping are owned by `claude-agent-sdk`. Less surface to
  maintain; the same native answer/verdict mechanics are reached without touching CLI flags.

Per design S1, B was exercised on the make-or-break/backbone C2/C3/C4 to make the fallback credible;
A covers all of C1–C6.

---

## 5. What this draft does and does not claim

- **Grounded:** every shape is cited to an observed harness/check; the contract adds NO API not seen
  in the spike. Verdicts: `evidence/matrix.md`.
- **DRAFT for P1:** P1's streaming engine is built against this contract and may refine field names /
  add the rate-limit and allow-session details flagged **[FLAG]** above once exercised.
- **Open items P1 must confirm (flagged above):** free-text "Other" + multi-question asks; a
  dedicated `rate_limit_event`; allow-session scope; operator-initiated mid-turn cancel (RB4);
  post-approval arbitrary execution; double-attach guard on resume; crash-mid-turn resume (RB3).
