# ADR-005 — Background concurrency & the session/run correlation model

> Derived from the P5 design ([`docs/features/p5-concurrency/design.md`](../features/p5-concurrency/design.md))
> and its scoping decisions **D1–D9** (the three hard calls — **D2/D5/D7** — flagged ⚠️ for owner review
> at G-Scope). Builds on **ADR-004** (the single-active-run **D2** invariant + the busy-guards this ADR
> relaxes; the per-project `_ProjectRuntime`; the answer-hold relay; the explicit deferral of the
> correlation envelope to P5) and **ADR-001** (the normalized-interface gap: *"P4/P5 must extend events +
> decisions with a session/run correlation envelope so an inbound answer routes to the correct pending
> request"* — ADR-005 **closes that gap at the relay/consumer, not in the engine**; plus the double-attach
> and async-permission-latency caveats, now multiplied across concurrent open holds). Reuses the P1
> `Coalescer` (injected-clock, pure-decision RB5 throttle), the `tool_use_id`-keyed `PendingRegistry`, the
> `decode_callback` codec, and the P4 SB2 resolver — all **unchanged**. Sibling of
> [ADR-001](ADR-001-session-substrate.md) / [ADR-002](ADR-002-async-answer-hold.md) /
> [ADR-003](ADR-003-permission-gating.md) / [ADR-004](ADR-004-multi-project-sessions.md).

- **Status:** **Proposed** — chosen model for P5; owner reviews with the P5 branch (D2/D5/D7 ratified at G-Scope).
- **Date:** 2026-06-23
- **Deciders:** repo owner
- **Related:** ADR-001 (substrate + the session/run **correlation-envelope gap** + double-attach +
  async-permission-latency caveat); ADR-004 (D2 single-active-run + the `_ChatState`/`_ProjectRuntime`
  split P5 relaxes); `docs/features/p5-concurrency/design.md` (D1–D9);
  `docs/cross-cutting-requirements.md` (RB5/SB1/SB3/SB6/RB1–RB3/RB6); `docs/interactive-remote-design.md`
  §P5 (the Run entity "one active per session; many across sessions in P5").

---

## Context

P1–P4 shipped a real interactive remote that now drives **several named projects** per chat, each its own
`(session_id, cwd)`, surviving restart (ADR-004). But it is **strictly one run at a time**: `/switch`,
`/new`, and `/reset` are **refused while a turn is in flight** (ADR-004 **D2**). A long `/pipeline` build in
one project therefore **blocks the whole remote** — the operator must let the turn finish or `/cancel`
before touching another project.

The reason is mechanical, and it is exactly the gap ADR-001 deferred. The relay routes **every** inbound
answer (button tap, free-text reply) to `_active_engine` — the *active* project's engine — and holds
live-turn state on the **chat** (`_ChatState` has single `pending_ask`/`pending_plan` slots and one turn
lock per chat). Flipping the active project mid-hold would strand the parked turn against the wrong engine
— a relay **deadlock**, not just bad UX. ADR-004 D2 accepted that coupling on purpose: *"with one active
run, a pending permission/ask/plan request unambiguously belongs to that run, so the ADR-001 correlation
gap need not be closed in P4."*

P5 ("Background concurrency + notifications", roadmap §P5 — the last *capability* phase) **relaxes** that
invariant. A run continues after the operator `/switch`es away; **N projects** run concurrently per chat
(bounded); an inbound answer routes to the **correct** pending request **across projects**; the bot
**proactively notifies** when a non-foreground project needs the operator or terminates; and **RB5
rate-limit safety holds under concurrent runs**. This ADR records the concurrency model, the correlation
mechanism, the busy-guard relaxation, the state relocation, and the throttle/notification/routing model —
the decisions that are hardest to reverse and most load-bearing for the P6 threat model.

**Constraints inherited from ADR-001 / ADR-004 that bind this decision:**
- The normalized engine interface is **single-session / single-active-run**; the **session/run
  correlation envelope** is the documented "P4/P5" gap — and **ADR-005 closes it** (ADR-004 D2 left it
  open by design).
- The substrate does **not** guard double-attach — P5 runs N *distinct* sessions (one per project)
  concurrently, **never** two attachments to the same `session_id`; the engine still owns that guard.
- **Async permission latency is ADR-001's single most load-bearing unproven assumption** — and P5 now
  holds **multiple** permission callbacks open at once (one per awaiting project, each up to the 60-min
  backstop, across concurrent SDK clients). Only the live verify (T11) can confirm the SDK tolerates it.
- **RB5 is the least-supported by P0** — the substrate carries no throttling contract; coalescing is the
  consumer's job. P5 is where RB5 is **first fully built under concurrency**.
- Containment is **policy-level, not an OS sandbox** (SB2) — unchanged.

---

## Decision drivers

- **Don't break the live bot.** One-shot stays the safe default; all P5 concurrency is **streaming-only**,
  behind the existing `ENGINE_MODE` flag (the default flip is **not** in scope — owner's call). `main`
  stays runnable.
- **Close the ADR-001 gap at the consumer, not the engine.** The engine **already** stamps the envelope
  (`session_id` + `tool_use_id`) on every event/decision; P5 must honor it **above** the engine and must
  **not** fork the proven engine/`PendingRegistry`.
- **Reuse, don't rebuild.** Route over the existing `PendingRegistry.resolve(tool_use_id, …)`, the
  unchanged `decode_callback`, the `Coalescer` style for the new send gate, and the per-project runtime
  ADR-004 already keyed.
- **Never silently misroute, never silently drop** (SB1/SB6) — a tap for project A must **provably** never
  resolve B's request; at the cap, excess **queues** rather than dropping.
- **Stay transient.** P5 adds **no** persisted state and **no** durable run journal — RB3
  abandon-and-lazy-resume stands.

---

## Decision

Adopt **per-project concurrent runs** routed by a **relay-layer pending-request index**, with a
**per-deployment cap + FIFO queue**, **proactive name-prefixed notifications**, a **per-chat send budget**,
and **per-project live-turn state**. The nine load-bearing choices (design D1–D9):

1. **D1 — Concurrency model: one active run per *project*; N projects concurrent per chat.** The literal
   reading of ADR-001's Run entity ("one active per session; many across sessions"). The P4 **per-chat**
   turn lock becomes a **per-project** lock (one `asyncio.Lock` per `_ProjectRuntime`). A second message to
   the **same** busy project still raises `StreamingBusy` (unchanged per-project UX); a message to a
   **different** idle project starts a concurrent run. Bounded by D6.

2. **D3 — Correlation envelope = a relay-layer pending-request index `{tool_use_id → (project, kind)}`
   (the core P5 addition; closes the ADR-001 gap *at the consumer*).** The engine **already** stamps
   `session_id` + `tool_use_id` on every injected `AskEvent`/`PlanEvent`/`PermissionEvent`, and
   `PendingRegistry.resolve(tool_use_id, …)` already routes by id. P4 collapsed that to `_active_engine` +
   one pending slot per chat. **P5's envelope work is therefore a relay refactor, not an engine change.**
   A per-chat **pending-request index** replaces `_ChatState`'s single `pending_ask`/`pending_plan` slots;
   it is populated when a project's stream injects an ask/plan/permission and torn down on
   resolve/cancel/backstop-noop/turn-end. `resolve_callback` / `_resolve_free_text` / `handle_cancel` look
   the id up in the index and resolve against the **owning project's** engine — **`_active_engine` is
   retired from the resolve path.** `tool_use_id` is globally unique (an SDK `toolu_…`/UUID), so it alone
   is a sufficient routing key; `session_id` is the **disambiguator/validator** — the relay asserts the
   held event's `session_id` matches the project's current engine `session_id` before resolving
   (**defense-in-depth** against a stale id colliding after a resume; on mismatch it no-ops). **`callback_data`
   is unchanged**: it already carries `tool_use_id`, the codec is at its byte budget (worst-case ask frame
   ~57 of 64 B; a project field would overflow it), and it needs no project field because the index maps
   id→project at resolve time. *(Alternative — embed the project in `callback_data` — rejected on the byte
   budget; the index is both cheaper and the natural home for the `held_event`/accumulator.)*

3. **D2 ⚠️ — Relax the busy-guard: `/switch` and `/new` become FREE; `/reset` stays guarded *per
   project*.** P4 refused `/switch`/`/new`/`/reset` while busy **only because** `_active_engine` routed
   every answer to the active project, so flipping `active` mid-hold stranded the parked turn (ADR-004 D2's
   deadlock). Once routing is by id (D3), switching away **no longer strands anything** — an inbound tap
   resolves by its envelope to whatever project owns it, not "the active one." So `/switch`/`/new` run
   while other projects are mid-run (**that is the headline**); their SB2 cwd re-validation (ADR-004) and
   SB4 name validation are **unchanged**. `/reset` **still refuses** while **that project** is busy —
   resetting drops the project's engine and would orphan its **own** parked hold (`/cancel` it first);
   when the active project is idle (even if *others* run), `/reset` proceeds and clears **only** the active
   project's session. *(This is the genuinely-hard call; the correctness guarantee that makes it safe is
   **entirely** D3's id-routing.)*

4. **D7 ⚠️ — Lift live-turn state `_ChatState` → `_ProjectRuntime`; add a per-project `status`.** The P4
   live-turn fields (status-line id/text, `pending_ask`/`pending_plan`/`ask_answers`, free-text capture)
   live on `_ChatState` **because** there was one turn per chat; with N concurrent runs they move to the
   **per-project** runtime (each running project owns its status line + pending slots + accumulator).
   `_ChatState` shrinks to a coordinator (the pending index, a **foreground** marker, the free-text
   target, the run queue + running-count, the per-chat send gate). A per-project `status` enum
   (`idle`/`running`/`awaiting_approval`/`awaiting_answer`/`awaiting_plan`/`queued`) feeds `/projects`.
   **The load-bearing invariant SURVIVES: a resolve / cancel still runs concurrently with the held turn
   (lock-free)** — it now finds the held event + accumulator on the owning project's runtime (via the D3
   index → project → runtime) instead of on `_active_engine`/`_ChatState`. *(The largest structural
   refactor — the whole streaming layer is built on "one turn per chat → live state on the chat" — but
   mechanical: the fields relocate one level down.)*

5. **D6 — Concurrency cap + FIFO queue (`MAX_CONCURRENT_RUNS`, default 3).** A new env key bounds
   simultaneously-*executing* runs **across the process** (protects host CPU + the shared CLI/SDK + the
   Telegram send budget). At the cap, a new turn is **accepted and queued** (FIFO, **per chat**; the
   operator is told `⏳ queued behind N run(s)`) — **never refused** (refusing defeats multitasking) and
   **never silently dropped** (**SB6 fail-closed → queue**). A finishing run pops the next. A project is
   never queued behind *itself* (still `StreamingBusy`). Parse like `ANSWER_BACKSTOP_SECONDS`: empty/unset/`0`
   → default 3; non-positive-non-zero / non-integer → **fail loud at startup**. The run **counter** is
   global (per-deployment); the **queue** is per-chat (no cross-chat semantics).

6. **D4 — Proactive notifications: inline iff foreground, else a name-prefixed ping.** Four triggers —
   needs approval (`PermissionEvent`), asks a question (`AskEvent`), proposes a plan (`PlanEvent`),
   finishes/errors (`ResultEvent`/`ErrorEvent`). When the event's project **is** the chat's current
   foreground, render inline as today (**no extra ping**); when it is a **background** project, send a
   name-prefixed message (`🔔 <name> — Claude needs approval` / `✅ <name> — done` / `⚠️ <name> — <short>`)
   carrying the **same keyboard** the inline render would (so the tap routes by D3 regardless of
   foreground). "Foreground" is a per-chat marker (default = the store's `active`); reading it is
   read-only. **SB3 — body-free:** a ping carries only the project name + the engine's already-body-free
   `tool_input_summary`; the `render.py` helpers are **pure** (no I/O) and **never re-derive** a summary
   from raw input — no Write body / Bash secret ever reaches a ping. Coalesced/throttled through the D8
   sender.

7. **D8 — RB5 under concurrency: per-project coalescer + a per-chat send budget.** P4 had one `Coalescer`
   per turn (per chat). P5 keeps **one coalescer per running project** (each status line throttles
   independently — a burst in A never resets B's throttle) **and** adds a **per-chat global send-rate
   gate** (Telegram's ceiling is ~1 msg/s/chat). The gate is a **pure class over an injected clock**
   mirroring `Coalescer` (it only *decides* whether/when a send is due; the awaiting stays in the
   session/bot). All sends/edits/notifications for a chat funnel through it. **Verbatim is priority over
   coalesced status** and is **rate-ordered, never dropped** (an ask/plan/error/result must reach the
   operator); the gate orders sends and **never** alters a body (RB6/SB3). It gates *sends*, **never** the
   resolve path (no deadlock of a held turn). Optional `RENDER_CHAT_SEND_INTERVAL_SECONDS` (~1 s default;
   invalid → fail loud); no new **required** env. **RB5 is first fully built here (RB7 test).**

8. **D5 ⚠️ — Free-text routing: most-recently-prompted project, with two escape hatches; never silently
   misroute.** A free-text reply (an ask "Other" answer or a plan-reject feedback) is a plain message with
   **no inline `tool_use_id`** — inherently ambiguous with several projects awaiting. Default: tapping
   "Other"/"Reject" on a project arms **that** project as the per-chat free-text target and the bot replies
   a **name-qualified** prompt (`✏️ <name> — type your answer…`); the **next** plain message resolves that
   target (via its runtime, D7). If a different project arms capture first, the **newest wins** (the prompt
   said which). **Escape hatches:** (a) Telegram **reply-to-message** — the relay maps `message_id →
   tool_use_id` when it sends a prompt and routes a reply by that id (an explicit override of the
   most-recent default); (b) an explicit **`/to <name> <text>`** command (allowlist-gated like every
   command — **no new callback surface**). If the armed target is gone/ambiguous and nothing disambiguates,
   the relay **no-ops** (and may ask which project) rather than resolve the wrong one — the **"never
   silently misroute"** bar, an SB-adjacent correctness guarantee. *(The one genuinely-hard product call;
   the routing rule lives in one small resolver so the owner can tighten — always require reply-to/`/to` —
   or loosen it in one edit. The `message_id → tool_use_id` map is bounded — dropped on resolve/turn-end.)*

9. **D9 — Concurrency-aware cancel / reset / rm.** `handle_cancel(chat_id, name=None|"all")`: `/cancel` →
   the **active** project's run; `/cancel <name>` → **that** project's; `/cancel all` → **every** running
   project for the chat. Each clears that project's pending-index entries + free-text marker and stays
   **lock-free** (it unblocks a held turn). `/reset` → per-project busy-guarded (D2). `/rm` already refuses
   the active project and purges its runtime (ADR-004 `forget_project`); under concurrency it
   **additionally refuses a currently-running** project ("cancel it first") so a live engine is never torn
   down mid-turn.

### Engine & persistence — unchanged

The **engine is unchanged** (design §"engine — unchanged"): every event already carries `session_id` (all
7 kinds) and `tool_use_id` (Ask/Plan/Permission/ToolUse/Error); `Engine.resolve` / `Engine.cancel` /
`Engine.session_id` and the per-engine `PendingRegistry` (60-min backstop, keyed by `tool_use_id`) are the
seams P5 routes **above**. **Nothing new is persisted.** The schema-v2 registry (ADR-004) is unchanged; the
pending index, foreground marker, free-text target, run queue, running-count, per-project status, locks,
and status lines are **all transient in-memory** — reset on restart, exactly like P4's `/yolo`/grants.

### SB1 property P6 will verify

A tap resolves a request via the **per-chat** pending index, so it can only ever hit a request **the
operator's own concurrent runs created**, and the id→one-owner map means **a tap for project A can never
resolve project B's request** (a mismatched/absent id no-ops, RB1). Routing-by-id happens **strictly after**
the bot's `_authorized` recheck in `on_callback` (unchanged); a `callback_data` that `decode_callback`
rejects (foreign/stale/malformed → `None`) resolves nothing. **An unauthenticated or misrouted tap must
never approve a plan / allow a tool in *any* project** — the load-bearing SB1 invariant P6 re-verifies.

---

## Consequences

**What P6 must threat-model.** The new surfaces are **cross-project answer routing** (the D3 index — P6
must verify "a tap for A can never resolve B" explicitly, as T10/T11 assert end-to-end) and the
**notification channel** (D4 — no secret/raw-body leakage in a ping, SB3). The two new commands (`/cancel
<name>`, `/to <name>`) ride the same `allowed`-filtered path; **no new callback surface** is added.

**RB5 (rate-limit safety) — first fully built here.** Per-project coalescers + the per-chat send budget
(D8) keep N concurrent projects + their pings under Telegram's ~1 msg/s/chat. RB7 is the dedicated
concurrent-burst test: bounded send rate, **no dropped verbatim**.

**RB3 (restart/resume) — unchanged.** A run in flight at crash is **abandoned**; the project comes back
idle; lazy-resume on the next message. P5 adds **no** durable run/queue persistence — on restart there are
simply no in-flight runs to reconcile (the queue/run-status/index are in-memory only).

**RB6 (persistence integrity) — unchanged.** No schema change; each concurrent run persists **its own**
project's `session_id` on `result` (independent writes; the store's per-project keying isolates them — no
clobber). A dedicated two-concurrent-persists test pins it.

**RB1/RB2 (isolation) — preserved + widened.** Bad input never crashes; a per-project engine/substrate
error fails clean and **does not affect other concurrent runs** (one runtime's failure is contained; its
lock is released and the scheduler still pops the next — watch the failure path for a slot-leak).

**SB carry-ins.** **SB1** trust boundary unchanged — every routed decision-in stays operator-authenticated;
routing-by-id resolves only after `_authorized`. **SB3** notifications are body-free (project name + the
existing summary; helpers never re-derive). **SB5** `/yolo` stays per-project, transient, loud,
reset-on-restart (several projects may each be in yolo, each loud on its own turns). **SB6** fail closed:
at the cap excess **queues**; a misrouted/stale id **no-ops**; a cwd drifted out of roots is still refused
per turn (ADR-004 SB2 re-validation, per concurrent run). **`main` stays runnable** — one-shot is unchanged
and concurrency is streaming-only behind `ENGINE_MODE`.

**ADR-001 / ADR-004 alignment.** ADR-005 **closes** the ADR-001 "P4/P5 correlation envelope" gap — at the
**relay/consumer**, confirming ADR-001's "the consumer extends the contract" framing — and **relaxes**
ADR-004's D2 single-active-run invariant (the per-project keying ADR-004 chose makes this **additive — no
rewrite**). It honors ADR-001's **double-attach** caveat (N *distinct* sessions, never two attachments to
one id) and surfaces, but cannot itself retire, the **async-permission-latency** caveat — now multiplied
into **multiple concurrent open holds**, each an independent `asyncio.Future`+backstop in its project's
engine; the live verify (T11) must exercise **two simultaneous holds** to confirm the SDK tolerates
concurrent open callbacks. **This ADR does not contradict ADR-001/002/003/004.**

**What is explicitly NOT decided here (deferred).** Multi-operator / cross-chat concurrency (never — SB1);
**durable** run/queue persistence across restart (RB3 keeps abandon-and-lazy-resume; a future durable run
journal layers over the per-project records — additive); per-project approval policies; `/rename`;
voice/status-dashboard; the `ENGINE_MODE=streaming` **default flip** (owner's call); the full SB
consolidation + threat model (**P6**).

**Risks.** (1) *D5 free-text ambiguity* — the genuinely-hard product call; mitigated by most-recent +
name-echoed prompts + reply-to/`/to`, the never-misroute no-op bar, and a single-spot resolver (⚠️
owner-review). (2) *D7 the largest refactor* — mitigated by the mechanical one-level-down move and the
preserved lock-free-resolve invariant; heavy per-project lock + index unit coverage (⚠️ owner-review).
(3) *RB5 under concurrency* (first built here) — per-project coalescers + per-chat gate + the RB7 test;
the gate must never gate the resolve path. (4) *ADR-001's async-latency assumption at scale* — multiple
concurrent open holds; only T11 (two live simultaneous holds) confirms it. (5) *Host resource use* — N
concurrent Claude sessions; the `MAX_CONCURRENT_RUNS` cap (D6) is the lever (documented in P7). (6) *Slot
leak on the failure path* — a turn that raises must decrement the counter + pop the next exactly once.
