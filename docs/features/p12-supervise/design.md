# P12 — Supervise: live thinking + plan-mode approval

_Status: **design / scope** (orchestrator reviews before `/plan`). Roadmap-v2 Phase 4
("Supervise"). Worktree `claude-telegram-bot-p12`, branch `feat/p12-supervise`, off
`main@8afa52c`. SDK **pinned** `claude-agent-sdk==0.2.105`._

---

## 1. The vision (owner)

Supervise Claude's work remotely from the phone — **SEE what it's THINKING** and
**APPROVE its PLAN before it executes**. Two candidate features:

1. **Live thinking** — surface Claude's extended-thinking/reasoning to Telegram as it
   streams, so the owner watches the reasoning, not just the actions.
2. **Plan-mode approval** — run a turn in PLAN mode; when Claude proposes a plan, present
   it to the owner for **Approve** (→ execute) / **Reject** (→ revise), reusing the
   existing per-tool approval-gate UX.

---

## 2. SPIKE FINDINGS (verified against the installed SDK — evidence, not assumption)

All probes ran the installed `claude-agent-sdk==0.2.105` via the READY p11 venv python
(`/Users/ray/dev/claude-telegram-bot-p11/.venv/bin/python`, read-only; the p11 worktree
was never modified). Probes ran in a throwaway scratch cwd. Source inspected at
`…/site-packages/claude_agent_sdk/{types.py,client.py,_internal/message_parser.py}`.

### 2.1 Thinking — **fully feasible; verified shape**

**Verdict: showable, with one config flag and one new event type.**

* **Static SDK shape (types.py):**
  * `ThinkingBlock` is a dataclass with exactly two fields: `thinking: str` (the reasoning
    text) and `signature: str` (an opaque crypto signature). It is a member of the
    `ContentBlock` union (so it can appear in `AssistantMessage.content`).
  * **There is NO `RedactedThinkingBlock` class in the SDK.** The message parser
    (`_internal/message_parser.py`) matches `block["type"]` with arms for
    `text / thinking / tool_use / tool_result / server_tool_use / advisor_tool_result`
    and **no `case _` default** — so a `redacted_thinking` block from the API is **silently
    dropped** by the parser (it never becomes a `ContentBlock`, never reaches the bot). This
    is a *safety win*: redacted thinking can't accidentally leak through `AssistantMessage`.
  * Enabling text thinking: `ClaudeAgentOptions(thinking=…)`. Shapes:
    `{"type":"adaptive","display":"summarized"}` (model decides depth, returns text),
    `{"type":"enabled","budget_tokens":N,"display":"summarized"}`, or `{"type":"disabled"}`.
    There is also `effort: "low"|"medium"|"high"|"xhigh"|"max"` and a deprecated
    `max_thinking_tokens`.
  * **`ThinkingDisplay` default is the gotcha** (verbatim from types.py): *"Opus 4.7+
    defaults to `"omitted"` (signature-only); pass `"summarized"` to receive text."* So to
    get **readable** thinking the bot MUST pass `display:"summarized"` — otherwise the model
    returns a `ThinkingBlock` with an empty/omitted `thinking` field (signature only).

* **Live probe A — `include_partial_messages=True`, `thinking={"type":"adaptive","display":"summarized"}`,
  prompt "what is 17*23, show reasoning":**
  ```
  StreamEvent event types: {message_start, content_block_start×2, content_block_delta×7,
                            content_block_stop×2, message_delta, message_stop}
  StreamEvent delta types : {thinking_delta: 3, signature_delta: 1, text_delta: 3}
  content_block_start: content_block.type='thinking' keys=['type','thinking','signature']
  thinking_delta delta = {'type':'thinking_delta','thinking':'This','estimated_tokens':None}
  signature_delta delta = {'type':'signature_delta','signature':'EvkCC…'}  (556 chars, opaque)
  AssistantMessage block: ThinkingBlock(thinking="This is straightforward arithmetic…",
                                        signature="EvkCC…")   ← full reasoning text
  ```
  **Confirmed:** thinking streams as `content_block_delta` whose `delta["type"]=="thinking_delta"`,
  carrying the incremental text in `delta["thinking"]` — exactly parallel to `text_delta`.
  The opaque `signature` arrives separately as `signature_delta` (and on the final block).
  The assembled `AssistantMessage` also carries a `ThinkingBlock` with the full text.

* **Today the bot drops both:**
  * `adapter_sdk.normalize` only emits a `TextEvent` for `delta["type"] in ("text_delta","text")`
    (line ~193) — `thinking_delta` returns `None`.
  * `_normalize_block` has branches for `TextBlock / ToolUseBlock / ToolResultBlock` only —
    a `ThinkingBlock` falls through to `return None` (dropped).
  * **Production runs `include_partial_messages=False`** (the `_default_engine_factory`
    builds `SdkSubstrate(...)` without setting it; default False — confirmed in
    `adapter_sdk.__init__` and the `_TurnDedup` docstring). So the bot receives **no
    `StreamEvent` at all** today; incremental text comes only from assembled
    `AssistantMessage` blocks. **To stream thinking live we must turn partial messages on**
    (a real delta with cost: more wire traffic + the twin-render concern, see §6/RB-notes).

### 2.2 Plan mode — **fully feasible; the path already exists, only the entry is missing**

**Verdict: plan-mode interception WORKS cleanly through the bot's existing P2/P6 gate.**

* **`permission_mode="plan"` is a real `PermissionMode`** (types.py `PermissionMode =
  Literal["default","acceptEdits","plan","bypassPermissions","dontAsk","auto"]`). It is a
  valid `ClaudeAgentOptions(permission_mode=…)` value AND can be switched mid-session via
  `ClaudeSDKClient.set_permission_mode(mode)` (client.py:319 — *"only works with streaming
  mode"*; supports `'plan'`).

* **`ExitPlanMode` is NOT an SDK symbol** — `grep` over the whole package finds zero hits.
  It is a CLI-level tool that surfaces *through the permission channel* as an ordinary
  `ToolUseBlock(name="ExitPlanMode")`.

* **Live probe B — `permission_mode="plan"`, intercept `can_use_tool`, ask for a plan:**
  ```
  [can_use_tool] tool_name='ExitPlanMode' input_keys=['plan','planFilePath']
                 tool_use_id='toolu_01JVvmRPTg3g8tmh4Kh735eZ'
    ExitPlanMode 'plan' field present=True  value='# Plan: Add greet(name)…'  (1106 chars markdown)
  ```
  **Confirmed:** ExitPlanMode hits `can_use_tool` with the **full plan markdown in
  `tool_input["plan"]`** (plus a new `planFilePath` field, ignorable). The bot **already**
  maps this: `adapter_sdk.PLAN_TOOL = "ExitPlanMode"` → `_normalize_block` builds
  `PlanEvent(plan=tool_input["plan"], tool_use_id=…)`, and the engine injects the
  pending-synced authoritative `PlanEvent` via the permission channel (see
  `engine._drain_substrate` dedup comment). The plan keyboard
  (`render.plan_keyboard` → `[✅ Approve] [✋ Reject + feedback]`), the `PlanVerdict`
  decision, reject-feedback-rides-the-deny-message, and the P6 hold/backstop are **all
  built and shipped** (P0/P1).

* **Live probe C — approve ExitPlanMode, observe resume:**
  ```
  Timeline: ToolUse(Write) ToolUse(ToolSearch) ToolUse(ExitPlanMode)
            can_use_tool(ExitPlanMode) → ALLOW
            ToolUse(Write) can_use_tool(Write) → ALLOW → "Done. Created scratch_probe.txt"
  ```
  **Two critical findings:**
  1. **Approving ExitPlanMode resumes execution with NO explicit `set_permission_mode` call.**
     After ALLOW, the SDK proceeded and the model's subsequent `Write` fired `can_use_tool`
     again. So **Approve → execute "just works"** over the existing gate. (We may still call
     `set_permission_mode("default")` on approve as defense-in-depth — see §4 T-PLAN — but
     it is not strictly required for execution to resume.)
  2. **Plan mode does NOT auto-suppress non-ExitPlanMode tools.** During the plan turn the
     model *attempted* `Write` and `ToolSearch` **before** ExitPlanMode, and those reached
     `can_use_tool` too. This is exactly **ADR-001 caveat C4**, which is the load-bearing
     safety contract: *"Approving a plan does NOT greenlight arbitrary execution; post-
     approval tool calls MUST remain gated by the C2 permission path."* The bot's P2/P6 gate
     already enforces this (every risky tool hits the gate independently). **The design must
     not weaken it** — a plan turn's stray tool calls are gated/denied like any other.

* **SB2 finding (containment):** in probe C the model resolved the bare filename
  `scratch_probe.txt` against **`$HOME`** (wrote `/Users/ray/scratch_probe.txt`), not the
  probe cwd. The stray was swept and removed. Takeaway: **SB2 path confinement
  (`allowed_roots`) is the real backstop** for where a plan's approved Writes land — the
  plan text is advisory; confinement is enforced at tool-use time, unchanged by P12.

### 2.3 Spike conclusion

Both features are **grounded and shippable** — no guessing required.
- **Plan-mode approval** is the *cheaper* of the two: the entire render/decision/hold path
  already exists; the only missing piece is **a way to start (or switch) a turn into
  `permission_mode="plan"`.**
- **Live thinking** needs a new `ThinkingEvent`, two `normalize` branches, a renderer with
  collapse/cap/toggle, and turning `include_partial_messages=True` on a thinking turn.

Nothing here requires the "closest achievable alternative" fallback the brief asked us to
consider — plan-mode interception is genuinely clean. (Had it not been, the fallback would
have been a non-executing "preview" turn that just shows the plan text; we do not need it.)

---

## 3. Scope (GROUNDED — only what the SDK supports)

P12 ships **two** features, each independently valuable and independently testable:

### Feature A — Plan-mode approval (`/plan`)
Start the next turn in `permission_mode="plan"`. Claude reasons + proposes, then calls
ExitPlanMode; the bot shows the plan (already does) with **Approve / Reject + feedback**.
Approve resumes execution (still gated per-tool by P2/P6); Reject feeds the feedback back as
a revision. **One-shot per turn** (the next turn after a `/plan`), so the operator opts in
deliberately and a normal turn is unchanged.

### Feature B — Live thinking stream (`/thinking on|off`, default off)
When enabled for a project, the bot surfaces Claude's reasoning as a **collapsible /
capped "🧠 thinking" status line** that updates in place as it streams (reusing the
existing coalesced status line + send-gate), then is **cleared at turn end** (like the
existing "💭 thinking…" line). Off by default (cost + flooding posture); per-project toggle.

**Explicitly OUT of scope for P12** (defer / not needed):
- *Show-diff-before-approve* (roadmap lists it under P12) — depends on capturing the raw
  Write/Edit body, which today is deliberately body-free (SB3). Real value but a separate
  scope with its own SB3 analysis; **defer to a later phase** to keep P12 shippable.
- Persisting thinking transcripts, a thinking history command, or an expand-full-thinking
  fetch — v1 shows the live tail only.
- Changing the default permission mode or the default thinking display globally.

---

## 4. Task breakdown (build order; each has acceptance criteria + tests)

Ordered so the **cheaper, lower-risk plan feature lands first**, then the thinking feature.
Each task is independently verifiable (pure-unit where possible; one live phone-verify per
feature at the end).

### T-PLAN-1 — `permission_mode` is per-turn settable on the substrate/engine
**What:** thread a per-turn `permission_mode` ("default" | "plan") into the turn. Two viable
mechanisms (pick at plan time):
  (a) **session-creation param** — build a fresh plan-mode session for the plan turn (mirrors
      how `model` is threaded via `_bound_factory`/`_build_options`); simplest, no mid-session
      mutation, but forces a session rebuild for the plan turn; **or**
  (b) **mid-session switch** — call `client.set_permission_mode("plan")` before the turn and
      `set_permission_mode("default")` after the plan resolves (SDK-supported, streaming-only).
**Recommendation:** start with (a) for the spike-simplicity (no new client method on the
substrate Protocol), revisit (b) if a session rebuild per `/plan` proves heavy. The plan
already injects/holds correctly regardless of which mechanism sets the mode.
**Acceptance:** a turn driven with plan mode set causes Claude to call ExitPlanMode (the
existing `PlanEvent` + keyboard surface); a normal turn is byte-for-byte unchanged.
**Test:** unit — `_build_options(permission_mode="plan")` (or the set_permission_mode call)
is issued exactly when requested and omitted otherwise; existing adapter tests stay green.

### T-PLAN-2 — `/plan` command (SB1-gated) arms the next turn as a plan turn
**What:** add `cmd_plan` to `bot.py`: sets a **per-project, one-shot** "next turn is a plan
turn" marker on `_ProjectRuntime` (transient, in-memory, RB3 — never survives restart).
`handle_message`'s turn driver reads + clears it and drives that one turn in plan mode.
Register the handler, add to `COMMAND_MENU` + `HELP_TEXT` (the lock-step invariant test).
**Acceptance:** `/plan` from the allowlisted chat arms the marker and replies a confirm
("📋 Next message runs in plan mode — I'll show the plan for approval."); an *un*allowlisted
chat is rejected by `_ok` before anything (SB1); the marker is cleared after one turn (the
turn *after* the planned one is normal).
**Test:** unit — `cmd_plan` calls `_ok`; arms exactly one turn; `COMMAND_MENU`/handler
lock-step test passes. A plan turn drives the substrate with plan mode (mock substrate).

### T-PLAN-3 — Approve resumes; Reject revises; backstop/cancel safe (reuse P6)
**What:** verify (and, where the one-shot marker interacts with the hold, wire) that
**Approve** → `PlanVerdict(approve=True)` → allow → execution resumes **still per-tool
gated** (C4); **Reject** → `PlanVerdict(approve=False, feedback=…)` → deny-with-feedback →
Claude revises. If using mechanism (b), switch back to "default" on approve. Confirm the P6
hold/backstop/`/cancel` discipline holds for a plan hold (it already does for ExitPlanMode —
this task is mostly *tests that pin the contract*, plus the mode-restore on approve if (b)).
**Acceptance (C4 — the security-critical one):** after Approve, a subsequent risky tool
(Write/Bash) **still hits the permission gate** (is NOT auto-allowed by the plan approval);
a denied/timed-out plan hold ends the turn cleanly (RB2/RB4), never hangs, never auto-allows.
**Test:** unit/integration — approve→next-tool-still-gated; reject→deny message carries the
feedback; backstop on a plan hold = deny (RB4); `/cancel` during a plan hold aborts clean.

### T-THINK-1 — `ThinkingEvent` type + normalize branches (pure)
**What:** add `ThinkingEvent(text: str, incremental: bool, redacted: bool=False,
session_id, kind="thinking")` to `engine/types.py` (and the `Event` union + `__all__`). In
`adapter_sdk`: (i) extend the StreamEvent delta branch to emit `ThinkingEvent(incremental=True)`
for `delta["type"]=="thinking_delta"` (text from `delta["thinking"]`); **drop `signature_delta`**
(opaque — never surfaced); (ii) add a `ThinkingBlock` branch to `_normalize_block` →
`ThinkingEvent(incremental=False)`. **SB3:** never emit the `signature` field anywhere.
**Acceptance:** a constructed `thinking_delta` StreamEvent and a constructed `ThinkingBlock`
both normalize to a `ThinkingEvent` carrying only the text; `signature_delta` → `None`; a
`redacted_thinking` shape (defensively) → either dropped or `redacted=True` with NO text.
**Test:** unit on `normalize`/`_normalize_block` with fake SDK objects (no live session) —
mirrors the existing `test_engine.py`/adapter tests. Pin that `signature` never appears in
any emitted event.

### T-THINK-2 — Render `ThinkingEvent` as a capped, collapsed status line (SB3)
**What:** in `render.py`, `render_event` handles `ThinkingEvent`:
  * incremental → `op="edit_status"` with a `🧠` prefix, **capped** to the last N chars
    (a "thinking tail" — never the whole reasoning, to avoid flooding) so the Coalescer
    folds a burst into a few in-place edits (reuse the existing status-line + send-gate path);
  * a `redacted=True` event → a fixed **`🧠 (thinking hidden)`** line, **never raw** (SB3);
  * the line is **cleared at turn end** by the existing bot turn-end cleanup (which already
    clears "💭 thinking…").
Add a render cap constant (e.g. last ~280 chars) + a glyph; keep it pure (no I/O).
**Acceptance:** a long thinking stream produces a bounded number of in-place edits (not a
message per delta), shows only the recent tail, never shows a signature, and shows the fixed
hidden-line for redacted; the status line disappears at turn end.
**Test:** unit on `render_event(ThinkingEvent)` + a Coalescer test feeding many thinking
deltas asserts a bounded edit count (mirrors the existing incremental-text coalesce test).

### T-THINK-3 — `/thinking on|off` (SB1) + enable partial messages only when on
**What:** add `cmd_thinking` (SB1-gated) toggling a **per-project** thinking flag (transient,
RB3). When **on** for a project, that project's session is built with
`thinking={"type":"adaptive","display":"summarized"}` **and** `include_partial_messages=True`
(threaded via the engine factory like `model`); when **off**, neither is set
(`include_partial_messages` stays False — today's behavior, no `StreamEvent` traffic). Add to
`COMMAND_MENU` + `HELP_TEXT`. **Cost/flood posture:** off by default; the toggle is the SB5-style
explicit opt-in (loud confirm on enable).
**Acceptance:** `/thinking on` → the next fresh session for that project streams thinking;
`/thinking off` → no thinking, no partial-message traffic; toggling is per-project and never
survives restart; an unallowlisted chat is rejected (SB1).
**Test:** unit — `cmd_thinking` calls `_ok`, flips the flag, threads both SDK options when on
and neither when off; lock-step menu test passes.

### T-THINK-4 — twin-render / dedup interaction with partial messages on
**What:** with `include_partial_messages=True`, the **final answer text** now arrives BOTH as
streamed `text_delta` (incremental TextEvents → status line) AND as the assembled
`AssistantMessage`/`ResultEvent` (verbatim). The existing `_TurnDedup` (stream_session.py)
already handles the assembled-vs-result duplication for the `False` case; confirm it still
holds with partials on, and that incremental text stays in the (cleared-at-end) status line
so it does not double-post the final answer. Thinking deltas go to the status line and are
cleared, so they never become a permanent message.
**Acceptance:** a thinking-on answer turn posts the final answer **exactly once** (verbatim),
shows live thinking + live text in the transient status line, and clears the status line at
end — no duplicate final-answer message, no leaked thinking message.
**Test:** integration on `_drive_turn` with a fake substrate that emits thinking_delta +
text_delta + assembled text + result; assert exactly one verbatim final answer and a cleared
status line.

### T-VERIFY — live phone-verify (both features), gates, docs
**What:** run the live phone-verify (Telegram Web via Playwright — bot `@your_test_bot`,
chat <your-chat-id>): (1) `/plan` → see plan → Approve → see it execute (a gated tool still
prompts) → Reject path shows revision; (2) `/thinking on` → send a reasoning prompt → watch
the 🧠 line stream + collapse → final answer once → line cleared; redacted case shows the
fixed hidden line if it occurs. Run all four gates green. Refresh `HANDOFF.md`.
**Acceptance:** all gates pass; both features verified on a real phone; evidence scrubbed
(UUID-grep the evidence dir per the live-verify-evidence-scrubbing rule before committing).

---

## 5. Delta on P0–P11 (what changes vs what is reused)

**Reused unchanged (the heavy lifting is already done):**
- The **plan render + decision + hold path** (P0/P1): `PlanEvent`, `render.plan_keyboard`,
  `PlanVerdict`, reject-feedback-via-deny-message, `engine._permission_hold` /
  `PendingRegistry` / the 60-min backstop / `/cancel`, the `_drain_substrate` ask/plan dedup,
  the per-chat pending index + route-by-`tool_use_id` (ADR-005 D3), the SB1 `_authorized`
  recheck on every callback.
- The **coalesced status line + per-chat send-gate** (P4/P5 RB5/D8) — the natural home for
  live thinking deltas (and already where the "💭 thinking…" placeholder lives).
- The **per-tool permission gate** (P2/P6) — the C4 backstop that keeps plan-approval from
  greenlighting arbitrary execution.
- The **engine-factory option-threading** pattern (`model` via `_bound_factory` /
  `_build_options`) — the template for threading `permission_mode`, `thinking`, and
  `include_partial_messages` per project/turn.

**New, additive (no rewrites):**
- `ThinkingEvent` type + two `normalize` branches (the adapter currently *drops* thinking).
- `render_event` arm for `ThinkingEvent` (capped/collapsed status line; redacted → fixed line).
- `cmd_plan` (one-shot per-turn plan marker) + `cmd_thinking` (per-project flag); both
  SB1-gated, both in `COMMAND_MENU`/`HELP_TEXT` (lock-step invariant).
- Per-turn `permission_mode` plumbing; per-project `thinking`+`include_partial_messages`
  plumbing — all transient in-memory (RB3 — never survive restart).

**Nothing here touches:** the persisted registry schema (ADR-004 v2 — no change), the
substrate Protocol's existing methods (a `set_permission_mode` wrapper, if we choose
mechanism (b) in T-PLAN-1, is additive), or the body-free render discipline.

---

## 6. Safety / robustness rules (the non-negotiables)

- **SB1 (authenticated decision-in) — every new command + the existing callbacks.**
  `cmd_plan` and `cmd_thinking` MUST call `_ok` (the `_authorized` allowlist check) before
  any effect, exactly like every other command. Plan Approve/Reject taps already route
  through `on_callback`'s `_authorized` recheck + route-by-`tool_use_id` (a tap for project A
  can never resolve B's plan — ADR-005 D3). No new callback kind is needed (Approve/Reject
  reuse the shipped `KIND_PLAN` codec).

- **SB3 (no secret/raw-body leakage) — the thinking-specific rules:**
  * Thinking is the model's reasoning and is **showable** — but **`signature` is opaque and
    is NEVER surfaced** (drop `signature_delta`; never put `ThinkingBlock.signature` on any
    event). The SDK parser already **drops `redacted_thinking`** (no block class, no default
    case), so it cannot leak through `AssistantMessage`; if a `redacted` flag is ever set on
    a `ThinkingEvent` it renders only the fixed **`🧠 (thinking hidden)`** line, never raw.
  * Thinking can be **LONG** → **collapse/cap/toggle to avoid flooding, reusing the
    send-gate.** The live line shows only a bounded recent **tail** (cap constant), folds via
    the existing Coalescer into a few in-place edits (RB5/D8), is **off by default** (the
    `/thinking` opt-in), and is **cleared at turn end**. The plan text is already chunked +
    HTML-rendered with a plain fallback (shipped).
  * `include_partial_messages` carries the same SB3 weight as today's stream: nothing logs
    raw bodies; the thinking tail is content (rendered, not logged).

- **C4 (ADR-001) — plan approval grants nothing about tools.** Approving a plan does **not**
  auto-allow execution: every post-approval risky tool independently hits the P2/P6 gate
  (proven live in probe C). The design's T-PLAN-3 acceptance pins this. A plan turn's stray
  pre-ExitPlanMode tool calls (probe B showed Write/ToolSearch) are gated/denied normally;
  SB2 path confinement is the backstop for where any approved Write lands.

- **RB1 (never crash on bad input).** A malformed/stale plan or thinking event, an unknown
  delta type, a missing `thinking`/`plan` field → no-op / fixed fallback, never an exception
  (mirrors the existing `decode_callback`/render fallbacks). An unknown thinking shape
  (incl. a future `redacted_thinking`) defaults to hidden, never raw (SB3 fail-safe).

- **RB2/RB4 (never hang; backstop/cancel don't wedge).** A plan hold is bounded by the same
  ~60-min answer-backstop as any decision (auto-**deny** on expiry, never auto-allow); the
  adapter's per-message liveness timeout is suspended while the hold is open (the shipped
  `_hold_depth` logic) so a long human approval is not charged as a silent Claude. `/cancel`
  aborts a plan turn cleanly. Enabling thinking does not change the liveness bound (deltas
  keep the stream live).

- **RB3 (transient state).** The `/plan` one-shot marker and the `/thinking` per-project flag
  are in-memory on `_ProjectRuntime`, dropped on restart (like `/yolo` and allow-session
  grants, ADR-004 D3) — supervision posture never silently survives a restart.

- **RB5/D8 (rate-limit safety under concurrency).** Thinking deltas ride the existing
  per-project coalescer + per-chat send-gate; with N concurrent projects the gate bounds
  outbound, verbatim (the plan, the final answer) keeps priority and is never dropped, and
  the gate never gates the resolve path (no deadlock).

---

## 7. Inherited constraints (carried from P0–P11)

- **Gates (all must be green before merge):** `python -m pytest`, `ruff check .`, `mypy`,
  `python scripts/secret_scan.py` (SB3). Cross-model Codex QA + live phone-verify per the
  per-phase pipeline.
- **SDK pinned:** `claude-agent-sdk==0.2.105`. No upgrade in P12.
- **One bot per token.** The live bot uses the oneshot default for safety; phone-verify uses
  the dedicated verify bot `@your_test_bot` (id <bot-id>, chat <your-chat-id>).
- **Do not contradict accepted ADRs.** ADR-001 C2/C3/C4, ADR-002 (answer-hold + backstop),
  ADR-003 (gate taxonomy + `/yolo` loud/off-by-default), ADR-004 (schema v2 unchanged, cwd
  immutable, transient grants), ADR-005 (route-by-id correlation, D8 send-gate), ADR-006
  (known limits). P12 is purely additive over all of them.
- **Lazy SDK import + pure `normalize`/render.** Keep `normalize` and `render_event` pure +
  SDK-import-free at module top so the mock-based unit tests need no SDK/CLI (existing rule).

---

## 8. Open decisions for `/plan` review (flag, don't guess)

1. **T-PLAN-1 mechanism:** session-rebuild (a) vs `set_permission_mode` mid-session (b).
   Recommend (a) first for simplicity; (b) is SDK-supported if rebuild cost matters.
2. **Thinking display knob:** ship `display:"summarized"` fixed, or expose `effort`
   (`low…max`) too? Recommend fixed `summarized` for v1 (one knob: on/off).
3. **Thinking tail cap size** (e.g. last ~280 chars) — tune during T-THINK-2 against a real
   long reasoning stream on the phone.
4. **`include_partial_messages` always-on for thinking turns only** (recommended) vs a global
   flag — recommend per-project/thinking-scoped to avoid the twin-render cost on normal turns.
