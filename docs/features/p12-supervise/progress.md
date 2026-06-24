# Progress: p12-supervise

_From design.md · roadmap-v2 P12 "supervise" · supervised build. Baseline 1248 tests. Both features SDK-spike-PROVEN (plan-mode interception + thinking_delta streaming)._

## Task list
- [ ] T-PLAN — Plan-mode approval (`/plan`): per-turn `permission_mode="plan"` plumbing + `/plan` one-shot arm + Approve-resumes/Reject-revises with the C4 contract (post-approval tools STILL gated) + backstop/cancel-safe (reuses the shipped ExitPlanMode→PlanEvent→plan_keyboard→PlanVerdict→P6-hold machinery)
- [ ] T-THINK — Live thinking (`/thinking on|off`, default off): `ThinkingEvent` + normalize branches (drop signature; redacted→opaque, never raw) + capped/collapsed 🧠 status line (reuse coalesced status + send-gate, cleared at turn end) + per-project toggle that enables `include_partial_messages` + `display:"summarized"` only when on + twin-render/dedup holds with partials on
- [ ] T-VERIFY — live phone-verify both (plan: arm→plan shown→approve→next tool still gated; reject→revise; thinking: toggle on→🧠 streams→cleared) + 4 gates + HANDOFF + merge

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked

## Tasks

### T-PLAN — Plan-mode approval (`/plan`)
- **Files:** `engine/adapter_sdk.py` + `engine/engine.py` + `engine/substrate.py` (per-turn `permission_mode` threading — mechanism (a): session-creation param like `model`, OR (b) `set_permission_mode`; pick simplest), `stream_session.py` (`_ProjectRuntime` one-shot plan marker, RB3 transient; turn driver reads+clears it), `bot.py` (`cmd_plan` SB1 + COMMAND_MENU + HELP lock-step).
- **Accept:** `/plan` (SB1) arms a one-shot per-project marker → that one turn runs in plan mode → Claude calls ExitPlanMode → the existing PlanEvent + `[✅ Approve]/[✋ Reject+feedback]` keyboard shows; **Approve → execution resumes but every subsequent risky tool STILL hits the permission gate (ADR-001 C4 — the security-critical line, NOT auto-allowed)**; Reject → deny-with-feedback → Claude revises; a backstop/timeout or `/cancel` on a plan hold ends the turn clean (RB2/RB4), never auto-allows; a normal (non-`/plan`) turn is byte-for-byte unchanged; marker cleared after one turn + never survives restart (RB3).
- **Tests:** `_build_options`/set_permission_mode issued only when armed; `cmd_plan` calls `_ok` + arms exactly one turn + menu/handler lock-step; **C4 — approve→next-tool-still-gated** (mutation-probe: if approval auto-allowed later tools, the test fails); reject carries feedback; backstop on a plan hold = deny; `/cancel` aborts clean.

### T-THINK — Live thinking stream (`/thinking on|off`)
- **Files:** `engine/types.py` (`ThinkingEvent`), `engine/adapter_sdk.py` (two normalize branches: `thinking_delta`→incremental ThinkingEvent, `ThinkingBlock`→full; **drop `signature`; `redacted_thinking`/RedactedThinkingBlock → opaque fixed line, NEVER raw**; enable `include_partial_messages` + `thinking={"type":"adaptive","display":"summarized"}` ONLY when the project has thinking on), `render.py` (capped/collapsed 🧠 status line, cleared at turn end), `stream_session.py` (per-project thinking flag; twin-render dedup must hold with partials on), `bot.py` (`cmd_thinking` SB1 + menu + HELP).
- **Accept:** `/thinking on` (SB1, per-project, default OFF) → the next turns surface reasoning as a coalesced/capped `🧠` line via the existing send-gate, cleared at turn end; `/thinking off` → no thinking, normal turn (partials NOT enabled → no wire cost); `redacted_thinking` never renders raw (opaque indicator only); thinking is capped (no flood); the assistant's final answer is unchanged + not duplicated (twin-render dedup holds with partials on); RB1 — a malformed thinking block never crashes.
- **Tests:** normalize emits ThinkingEvent for `thinking_delta`/`ThinkingBlock`, drops `signature`, redacted→opaque; partials/`display` enabled ONLY when on; render caps + clears; toggle per-project; **dedup — final answer rendered once with thinking on** (mutation-probe the dedup); SB1 + menu lock-step.

### T-VERIFY — live-verify + merge
Cross-model Codex QA (iterate to SHIP) → live phone-verify (plan arm→approve→C4-gated; reject→revise; thinking on→stream→clear) → 4 gates → HANDOFF refresh → merge to `main`.
