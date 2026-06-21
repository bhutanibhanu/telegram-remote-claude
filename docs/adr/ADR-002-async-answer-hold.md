# ADR-002 — Async answer-hold mechanism (interactive/permission decisions)

> Derived from the **T1 de-risk spike** (P1), `spikes/p1-async-latency/` (evidence:
> `spikes/p1-async-latency/evidence/p1_async_latency.*`). Grounds the P1 engine's handling of
> interactive prompts the operator answers asynchronously from Telegram. Sibling of
> [ADR-001](ADR-001-session-substrate.md) (Substrate A).

- **Status:** **Proposed** — chosen mechanism for P1; owner reviews with the P1 branch.
- **Date:** 2026-06-21
- **Deciders:** repo owner
- **Related:** ADR-001 (session substrate); `docs/features/streaming-engine/design.md`;
  `spikes/session-substrate/normalized_interface.md` (the decisions-in contract).

---

## Context

The P1 streaming engine must surface Claude's interactive tools — **AskUserQuestion** and
**ExitPlanMode** — to the operator on Telegram and **wait for the operator's answer**, which may
arrive **minutes** later (a button tap / reply), bounded by a **60-minute backstop** (decision-log
#4). On Substrate A the model raises these (and, in P2, per-tool permission requests) through the
SDK's `can_use_tool` callback. The open question (the #1 P1 risk): **can the callback be held open
that long without timing out or wedging the session, and how should the engine implement the hold +
backstop + cancel?**

## Decision drivers / evidence (T1)

T1 ran live (`claude-agent-sdk==0.2.105`, host CLI auth, no API key) and recorded **PASS**:

- A `can_use_tool` callback **held open 120 s** was honored on **ALLOW** (tool executed), **DENY**
  (tool blocked), and the **native AskUserQuestion `answers`-map** (session continued on the
  code-chosen option) — verdicts `t1_1_allow` / `t1_2_deny` / `t1_3_interactive` = PASS.
- A **300 s (5-min) hold** succeeded with **no ceiling observed** (`t1_4_ceiling` = PASS).
- A **harness-side backstop timer** auto-resolved a pending decision (**DENY + notify**) at 60 s and
  the **same session remained usable** for a subsequent turn (`t1_5_backstop` = PASS).
- **Root cause confirmed at SDK source** (`claude_agent_sdk/_internal/query.py`): the inbound
  `can_use_tool` control request is `await`ed with **no `fail_after`/timeout** wrapper; each inbound
  request is its own task, cancelled only by a CLI `control_cancel_request`. The 60 s timeout in the
  SDK is on the **outbound** path (initialize / interrupt / set_permission_mode) only — it does
  **not** bound the held callback.

Two independent reviewers confirmed the verdict and the SDK fact at source.

## Decision

**Implement the answer-hold as an engine-side pending-decision `Future` per interactive/permission
request, awaited inside `can_use_tool`, resolved by exactly one of: (a) the operator's decision
routed in from Telegram, (b) a harness-side backstop timer (auto-resolve → DENY + notify), or
(c) `/cancel`. The engine never relies on the SDK to bound the hold; the backstop is the engine's
own `asyncio` timer racing the operator `Future`.**

Mechanism (what P1/T5 builds):
1. When the engine receives an `ask` / `plan` (or, in P2, permission) request, it creates a
   `PendingDecision` keyed by the request's `tool_use_id` (+ session id), emits the corresponding
   normalized **event out**, and `await`s the decision `Future` inside the `can_use_tool` callback.
2. **Resolve by operator:** the bot's allowlist-checked callback handler (T7, SB1) maps an inbound
   button tap / "Other" reply / plan verdict to the pending key and resolves the `Future` with the
   decision (allow / deny+reason / native `answers`-map / approve / reject+feedback).
3. **Resolve by backstop:** a per-request timer (default 60 min, **configurable**) races the
   operator `Future`; whichever completes first wins. On backstop expiry the engine resolves the
   decision as **DENY + notify** and leaves the session usable (proven in T1.5).
4. **Resolve by cancel:** `/cancel` resolves the pending `Future` (clean abort) and unwinds the turn
   without wedging state (RB4-shape).
5. **Routing key:** `tool_use_id` (+ session id) so an answer reaches the correct pending request —
   forward-compatible toward P4/P5 multi-session correlation.

## Consequences

**What P1 inherits / must do.**
- Build the `PendingDecision` + backstop timer + cancel + routing in T5; the bot callback handler
  (T7) is the only thing that resolves an operator decision and it is **SB1-allowlist-checked**.
- The backstop **must be the engine's own timer**, never a reliance on the SDK holding one control
  request for 60 min. Default 60 min, configurable down.

**Unresolved risk / NOT tested (carry-forward).**
- The 60-min figure is an **extrapolation**: T1 empirically held only **≤5 min** and established the
  SDK imposes no callback timeout. **Any CLI/model-side ceiling in the 5–60 min band is UNTESTED** —
  e.g. a CLI request-idle timeout, an API streaming/inactivity timeout, or a model-turn wall-clock
  cap. Mitigations: (a) the engine-side backstop must be set **below any later-observed ceiling**;
  (b) consider a **keep-alive** (periodic no-op / interrupt-safe ping) if long holds are needed;
  (c) run a **one-off ~10–15 min ceiling probe** before relying on holds > 5 min in production.
  If a ceiling < the desired backstop is found, the engine resolves pending decisions before it and
  re-prompts, rather than letting the session error.
- Operator-initiated **mid-turn cancel** and the full backstop UX are exercised at the mechanism
  level here (T1.5) and built in T5; end-to-end Telegram behavior is verified at T9.

**Migration note.** This mechanism replaces the one-shot runner's synchronous request/response with a
stateful, long-lived hold; combined with ADR-001's stateful-connection caveats (idle/wedge/
double-attach), the engine must fail clean (RB2) if a hold is interrupted.
