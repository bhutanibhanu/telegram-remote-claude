# Design: P5 — Background concurrency + notifications

> **Feature slug:** `p5-concurrency` · **Pipeline:** P5 (depends on P4; the last *capability* phase).
> Worktree `feat/p5-concurrency`, branched off `main` (tip `b589527`) — it carries the full
> P1 streaming engine, P2 permission gate, P3 interactive prompts, **and P4 multi-project +
> restart recovery**. Parents:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) §P5 (roadmap lines 265–270, decision #5 "concurrency is last", the Run entity "one active per session; many across sessions in P5", the "Background event" interaction-mapping row) ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md) (**RB5** rate-limit safety under concurrency, **SB1** authn on every routed decision-in, RB3, SB6) ·
> [`docs/adr/ADR-001-session-substrate.md`](../../adr/ADR-001-session-substrate.md) (**the core P5 addition** — the "Normalized-interface gaps" consequence: "P4 (multi-project) and P5 (concurrency) must EXTEND events + decisions with a session/run correlation envelope so an inbound answer routes to the correct pending request (session_id + tool_use_id / control-request id)"; RB5 is "least-supported by P0"; double-attach / cancel notes) ·
> [`docs/features/p4-multi-project/design.md`](../p4-multi-project/design.md) + [`docs/adr/ADR-004-multi-project-sessions.md`](../../adr/ADR-004-multi-project-sessions.md) (**D2 single-active-run + the busy-guard P5 relaxes**; the per-project `_ProjectRuntime`; the answer-hold relay; the deferral of the correlation envelope to here).
>
> **The point of P5:** **relax P4's single-active-run invariant.** A run continues after the operator
> `/switch`es away; multiple projects can have in-flight runs at once; an inbound answer (button tap /
> free-text reply) routes to the **correct** pending request **across projects** via a **session/run
> correlation envelope** (the ADR-001 gap); the bot **proactively notifies** when a non-active project
> needs approval, asks a question, finishes, or errors; a **per-deployment concurrency cap + queue**
> bounds it; and **RB5 rate-limit safety holds under concurrent runs**. This is the roadmap's
> highest-complexity phase, deliberately built last on a proven single-run base.

---

## Scoping decisions

> No owner grill for this pipeline — the orchestrator scoped it from the docs + shipped code and
> **chose sensible defaults**. The three genuinely-hard ones (**D2**, **D5**, **D7**) are flagged
> **⚠️ OWNER-REVIEW** for confirmation at G-Scope; the rest are low-risk extensions of P4.

| # | Decision | Value |
|---|---|---|
| **D1** | **Concurrency model** | **One active run per PROJECT; N projects run concurrently per chat.** This is the literal reading of ADR-001's Run entity ("one active per session; many across sessions"). The P4 per-chat turn lock becomes a **per-project** turn lock (one `asyncio.Lock` per `_ProjectRuntime`, not one per `_ChatState`). A second message to the **same** project while it is busy still raises `StreamingBusy` (unchanged UX per project); a message to a **different** project starts a concurrent run. Bounded by D6. |
| **D2** ⚠️ | **Busy-guard fate (the big one)** | **`/switch` / `/new` become FREE; `/reset` stays guarded *per project*.** P4 refused `/switch`/`/new`/`/reset` while busy *only because* `_active_engine` routed every inbound answer to the **active** project — flipping `active` mid-hold stranded the parked turn (a relay deadlock, per ADR-004 D2). P5 removes that coupling with **correlation-id routing** (D3): an inbound tap/reply routes by its envelope to **whatever project's** pending request owns it, not "the active one". So switching away no longer strands anything — `/switch`/`/new` run while other projects are mid-run (that *is* the headline). `/reset` still refuses while **that project** is busy (it drops the project's engine → would orphan its own parked hold; `/cancel` first — unchanged rationale, now scoped per project). |
| **D3** | **Correlation envelope (the core P5 addition)** | **Route every decision-in by `tool_use_id`, disambiguated by `(chat_id, session_id)`.** The engine **already** stamps `session_id` + `tool_use_id` on every injected `AskEvent`/`PlanEvent`/`PermissionEvent`, and `PendingRegistry.resolve(tool_use_id, …)` already routes by id. P5 lifts that correlation **up to the `StreamingSession`/bot layer**, which P4 collapsed to "the active project". A per-chat **pending-request index** `{tool_use_id → (project_name, kind, held_event)}` replaces `_ChatState`'s single `pending_ask`/`pending_plan` slots. `resolve_callback` / `_resolve_free_text` / `handle_cancel` look the id up in that index and resolve against **that** project's engine — never `_active_engine`. `callback_data` is **unchanged** (it already carries `tool_use_id`; see D-note on the byte budget). |
| **D4** | **Notifications** | **Proactive message when a NON-active project needs the operator or terminates.** Four triggers: (a) needs approval (`PermissionEvent`), (b) asks a question (`AskEvent`), (c) proposes a plan (`PlanEvent`), (d) finishes / errors (`ResultEvent`/`ErrorEvent`). When the project is the **active foreground** one the operator is watching, render inline as today (no extra ping). When it is a **background** project, prefix with the project name (`🔔 work — Claude needs approval`, `✅ work — done`) so the operator knows *which* project and can answer it (the keyboard tap routes by D3 regardless of which project is active). **Coalesced + throttled per project (RB5, D8).** |
| **D5** ⚠️ | **Free-text routing ambiguity (genuinely hard)** | **Bind free-text to the MOST-RECENTLY-PROMPTED project, with the project name echoed in the prompt; ambiguity resolved by an explicit reply or a `/to <name>` prefix.** A free-text reply (an ask "Other" answer or a plan-reject feedback) is a plain message with no inline `tool_use_id` — so with several projects awaiting free-text it is **inherently ambiguous**. Default: the bot tracks a per-chat **free-text target stack**; tapping "Other"/"Reject" on a project arms *that* project as the target and the bot replies `✏️ work — type your answer…` (name-qualified). The **next** plain message resolves that target. If a *different* project arms free-text capture before the operator replies, the newest wins (the prompt said which). **Escape hatches:** (a) Telegram **reply-to-message** — if the operator replies to the specific prompt message, route by that message id (we map `message_id → tool_use_id` when we send a prompt); (b) an explicit **`/to <name> <text>`** command. This is flagged for owner review — it is the one genuinely hard product call in P5. |
| **D6** | **Concurrency cap + queue** | **Per-deployment soft cap `MAX_CONCURRENT_RUNS` (default 3); excess runs QUEUE FIFO per chat.** A new env key bounds simultaneously-*executing* runs across all projects/chats in the process (protects host CPU + the shared CLI/SDK + Telegram send budget). At the cap, a new turn is **accepted and queued** (the operator is told `⏳ queued behind N run(s)`), not refused — refusing would be hostile when the whole point is multitasking. A per-project run is never queued behind *itself* (that is still `StreamingBusy`). Cap is configurable; `0`/unset → default 3. |
| **D7** ⚠️ | **Per-project run lifecycle + status** | **Lift live-turn state from `_ChatState` to `_ProjectRuntime`; `/projects` shows per-project run status.** The P4 live-turn fields (status-line id/text, `pending_ask`/`pending_plan`, free-text capture) live on `_ChatState` *because* there was one active turn per chat. With N concurrent runs they must move to the **per-project** runtime (each running project has its own status line + its own pending slots). `/projects` gains a status column: `running` / `awaiting approval` / `awaiting answer` / `awaiting plan` / `queued` / `idle`. Flagged for owner review because it is the largest structural refactor (it touches the P4 `_ChatState`/`_ProjectRuntime` split the whole streaming layer is built on). |
| **D8** | **RB5 under concurrency** | **Per-(chat, project) coalescer + a per-chat global send budget.** P4 had one `Coalescer` per turn (per chat). P5 keeps **one coalescer per running project** (so each project's status line throttles independently) AND adds a **chat-level send-rate limiter** (Telegram's ceiling is ~1 msg/s/chat — N concurrent projects flushing at once would burst past it). All sends/edits/notifications for a chat funnel through one rate-gated sender. **Dedicated RB5-under-concurrency test (RB7).** |
| **D9** | **Cancel / reset / rm under concurrency** | **`/cancel` cancels the ACTIVE project's run by default, `/cancel <name>` (or `/cancel all`) targets a specific / every run; `/reset` per-project-busy-guarded (D2); `/rm` already safe.** `handle_cancel` becomes per-project (resolve the named project's engine, or the active one by default, or all). `/rm` of a non-active project already refuses the active one and purges its runtime (P4 `forget_project`) — under concurrency it must additionally refuse a project that is **currently running** (cancel it first) to avoid tearing down a live engine mid-turn. |

---

## The basics

**Elevator pitch.** Kick off a long task in one project, `/switch` to another and work there while the
first keeps running, and get **pinged** the moment any project needs you — "🔔 `work` needs approval",
"✅ `bot` done". Tap the button or reply and it routes to the **right** project, no matter which one is
in front of you. True multitasking from your phone, with pings when you're needed.

**The actual problem.** P4 shipped multi-project, but it is **strictly one run at a time**: `/switch`,
`/new`, and `/reset` are **refused while a turn is in flight** (ADR-004 D2). The operator must let the
current turn finish or `/cancel` before touching another project — so a long `/pipeline` build in one
project **blocks the whole remote**. The reason is mechanical: the relay routes every inbound answer to
`_active_engine` (the *active* project's engine) and holds live-turn state on the **chat** (`_ChatState`
has single `pending_ask`/`pending_plan` slots and one turn lock per chat). Flipping the active project
mid-hold would strand the parked turn against the wrong engine — a deadlock, not just bad UX. P5 removes
that coupling: it adds the **session/run correlation envelope** the ADR-001 normalized interface
explicitly deferred to "P4/P5", routes every decision-in **by its request id to the owning project**,
relaxes the busy-guards, runs **N projects concurrently** under a cap, and **notifies** proactively when
a non-foreground project needs the operator or finishes.

**Who it's for.** The **single allowlisted operator** (the P1 model, unchanged), now genuinely
multitasking across their projects from the phone. Still one operator; **still one run per project**;
now **many runs across projects**.

**Definition of success (concrete).** Under `ENGINE_MODE=streaming`, from the phone:
- Start a long turn in project **A**, then `/switch B` (or `/new`) **without being refused** — A keeps
  running in the background while B is interactive.
- A's `[Allow once]/[Allow session]/[Deny]` (or its AskUserQuestion / plan) **fires as a proactive
  `🔔 A — …` notification**, and tapping/answering it **resolves A's** pending request **while B is the
  active project** — A's answer is never misrouted to B and B is undisturbed.
- A free-text "Other"/reject reply routes to the project that prompted it (D5: most-recent-prompt,
  name-echoed; reply-to-message / `/to <name>` disambiguate).
- A's completion/error arrives as `✅ A — done` / `⚠️ A — …` even though B is in front.
- **`/projects`** shows each project's run status (`running` / `awaiting …` / `queued` / `idle`).
- With many concurrent runs, status edits + notifications **stay under Telegram's rate limit** (RB5)
  and the number of *executing* runs never exceeds `MAX_CONCURRENT_RUNS` (excess **queue**, D6).
- **`/cancel`** (active), **`/cancel <name>`**, **`/cancel all`** cleanly abort the right run(s);
  **`/reset`** is refused only while **that** project is busy; one-shot mode is **unchanged**.
- **CI green**, RB5-under-concurrency + correlation-routing tests pass, `main` stays runnable.

**Anti-goals (explicit non-features).**
- **Not multi-operator** — still one allowlisted operator per deployment (D1 concurrency is across
  *projects*, not *people*; the SB1 trust boundary is untouched).
- **No cross-chat concurrency semantics** — projects are per-chat (P4 D1); the global cap (D6) bounds
  the *process* but routing/notification are per-chat.
- **No auto-resume of an interrupted background run** across a restart — RB3 (P4 D7) stands: a turn
  in flight at crash is abandoned, the project comes back idle, lazy-resume on next message. P5 does
  **not** add durable run queues that survive restart (the cap/queue are **in-memory**).
- **No new persisted state** — run status, queue, pending index, free-text target, and notification
  throttle are all **transient in-memory** (like P4's `/yolo`/grants). The schema-v2 store is unchanged.
- **No per-project approval policies**, **no `/rename`**, **no `ENGINE_MODE=streaming` default flip**
  (owner's call), **no voice/dashboard** (future).
- **Not** changing `callback_data` — it already carries `tool_use_id` (the routing key); embedding a
  project name would blow Telegram's 64-byte limit (see the D3 byte-budget note).

**Constraints.** Builds on P1–P4 (streaming engine, `pending.py` answer-hold/backstop/cancel keyed by
`tool_use_id`, permission gate, interactive prompts, the SB1 callback boundary, `paths.py` SB2
resolver, the schema-v2 registry store, the `_ChatState`/`_ProjectRuntime` split). `claude-agent-sdk
==0.2.105`, host CLI auth, **no API key**. Single operator. **RB5 and the correlation envelope are the
two things first fully built here.** ADR-001's caveat binds: **the substrate does not guard
double-attach** — P5 runs N *distinct* sessions (one per project) concurrently, never two attachments
to the *same* session id, and the engine still owns that guard.

---

## Requirements

### Functional — ranked

1. **Correlation envelope + id-routed decisions (D3, the core addition; ADR-001 gap; SB1).** Replace
   `_ChatState`'s single `pending_ask`/`pending_plan` slots with a per-chat **pending-request index**
   keyed by `tool_use_id → (project_name, kind, held_event)`. `resolve_callback`, `_resolve_free_text`,
   and `handle_cancel` resolve via the index against the **owning project's** engine — **not**
   `_active_engine`. The index entry is added when the engine injects an ask/plan/permission event
   (the relay sees it on that project's stream) and cleared on resolve/cancel/backstop/turn-end. Every
   decision-in is still **operator-authenticated** at the bot before it reaches the engine (SB1 — the
   `_authorized` recheck in `on_callback` is unchanged; routing-by-id does not weaken it).
2. **Per-project concurrent runs (D1/D6).** Move the turn lock from `_ChatState` to `_ProjectRuntime`
   (one per project). A message to an idle project starts a run even if other projects are running; a
   second message to the **same** running project raises `StreamingBusy` (per-project). A
   **concurrency cap** (`MAX_CONCURRENT_RUNS`, D6) bounds *executing* runs across the process; excess
   turns **queue FIFO** (per chat) and start when a slot frees — the operator is told they queued.
3. **Relax the busy-guards (D2).** Remove the `is_busy` refusal from `cmd_switch` and `cmd_new` — both
   run freely while other projects are mid-run (background runs are the headline). Keep `cmd_reset`'s
   refusal but scope it to **that project's** busy state (resetting a project drops its engine → would
   orphan its own parked hold; `/cancel` first). `is_busy(chat_id)` gains a per-project variant
   `is_busy(chat_id, name)`.
4. **Proactive notifications (D4; RB5).** When an event needing the operator (permission/ask/plan) or a
   terminal event (result/error) belongs to a **non-foreground** project, send a **name-prefixed**
   message (`🔔 <name> — …` / `✅ <name> — done` / `⚠️ <name> — …`). A foreground project renders
   inline as today (no duplicate ping). Notifications are **coalesced/throttled** through the same
   per-chat rate-gated sender as status edits (D8) so a burst across projects never floods.
5. **Per-project run status + `/projects` (D7).** Lift the live-turn fields (status-line id/text,
   pending slots, free-text capture) from `_ChatState` to `_ProjectRuntime`. `/projects` shows each
   project's status: `running` / `awaiting approval` / `awaiting answer` / `awaiting plan` / `queued` /
   `idle` (read from the per-project runtime, defaulting to `idle` for a project with no runtime).
6. **Concurrency-aware cancel / reset / rm (D9).** `/cancel` → the active project's run; `/cancel
   <name>` → that project's run; `/cancel all` → every running project for the chat. `/reset` →
   per-project-busy-guarded (D2/#3). `/rm` → already refuses the active project and purges the runtime
   (P4 `forget_project`); additionally **refuse a currently-running** project (cancel it first) so a
   live engine is never torn down mid-turn.

### Non-functional

- **RB5 — rate-limit safety under concurrency (first fully built here; P1 built the single-stream
  case).** A **per-(chat, project) coalescer** throttles each project's status line independently, AND a
  **per-chat global send budget** (token-bucket / min-interval gate, ~1 msg/s/chat) serializes *all*
  sends/edits/notifications for a chat so N concurrent projects + their pings never burst past
  Telegram's limit. **Dedicated test (RB7):** several projects emitting bursts concurrently produce a
  bounded send rate and no dropped verbatim messages.
- **SB1 — authn on every routed decision-in (now across projects).** Every inbound (message, command,
  **button tap**) is still allowlist-checked at the bot before any routing. Routing-by-correlation-id
  resolves to a project's engine **only after** SB1 passes; a forged/foreign `callback_data` still
  decodes to `None` and resolves nothing; an id not in the pending index is a benign no-op. **An
  unauthenticated or misrouted tap must never approve a plan / allow a tool in *any* project.**
  Dedicated tests: non-allowlisted tap dropped; a tap whose id belongs to project A never resolves a
  request in project B; a stale id no-ops.
- **RB1/RB2 preserved** — bad input never crashes; a per-project engine/substrate error fails clean and
  **does not affect other concurrent runs** (one project's failure is isolated to its runtime).
- **RB3 preserved (P4 D7)** — a run in flight at crash is abandoned; the project comes back idle;
  lazy-resume on next message. P5 adds **no** durable run/queue persistence (the cap/queue are
  in-memory; on restart there are simply no in-flight runs to reconcile).
- **RB4 preserved** — `/cancel` + the 60-min backstop still cleanly abort a waiting run and leave the
  session usable; now **per request across projects** (the backstop already keys by `tool_use_id`, so
  it routes correctly under concurrency with no change to `pending.py`).
- **RB6 preserved** — no schema change; per-project `session_id` persistence on `result` is unchanged
  (each concurrent run persists to *its own* project record; the writes are independent and the store's
  per-chat/per-project keying already isolates them). **Dedicated test:** two concurrent runs persist to
  distinct project records without clobber.
- **SB6 — fail closed** — at the concurrency cap, excess **queues** (never silently drops); a misrouted
  id no-ops; a project whose cwd drifted out of roots is still refused per turn (P4 SB2 re-validation,
  unchanged); a forged tap resolves nothing.
- **SB3/SB4/SB5 preserved** — notifications carry the **project name + the existing body-free summary**
  (SB3: no raw tool bodies in a ping); no message text is interpolated into a shell (SB4); `/yolo` stays
  per-project, transient, loud, reset-on-restart (SB5; now several projects may each be in yolo — each
  shows its own loud marker on its own turns).
- **Scale/latency** — single operator; the global cap (D6, default 3) bounds host + CLI/SDK + Telegram
  load; the per-chat send budget bounds message rate. No RPS/availability targets beyond "stays under
  Telegram's limit and the host can run N concurrent Claude sessions".

### Future (does today's design survive P6+?)

P5 is the last *capability* phase. **P6 (security audit)** consolidates SB across the now-larger surface
— the new things to threat-model are **cross-project answer routing** (can a tap for A ever resolve B's
request? — must be provably no) and the **notification channel** (no secret/raw-body leakage in a
proactive ping). **P7 (packaging)** must document that N concurrent Claude sessions raise host resource
use (the cap is the lever). The in-memory queue/run-status is deliberately **not** persisted — if a
future phase wants "resume my background runs across restart" it layers a durable run journal over the
per-project records; P5's per-project keying makes that additive. **No rewrite required** for P6–P9.

---

## Architecture (delta from P4)

**Today (the P4 seam P5 extends).** Everything routes through "the active project":
- `bot.py` — `cmd_switch`/`cmd_new`/`cmd_reset` **refuse while `is_busy(chat_id)`** (the D2 busy-guard).
  `on_callback` (SB1 boundary) → `streaming.resolve_callback(chat_id, data)`. `_on_message_streaming`
  binds `send`/`edit`/`delete` closures and calls `streaming.handle_message(chat_id, …)`.
- `stream_session.py` — **`_ChatState`** holds the per-chat turn **lock**, the single status-line id,
  the single `pending_ask`/`pending_plan`, and the single free-text capture marker. **`_active_runtime`
  / `_active_engine`** resolve the *active* project. `handle_message` takes the per-chat lock (one turn
  per chat). `resolve_callback` / `_resolve_free_text` / `handle_cancel` all act on `_active_engine`.
  `_ProjectRuntime` holds per-project `engine`/`cwd`/`policy`/`started`.
- `engine/engine.py` — **already** stamps `session_id` + `tool_use_id` on every injected
  `AskEvent`/`PlanEvent`/`PermissionEvent`; `resolve(tool_use_id, decision)` and `cancel(tool_use_id)`
  already route by id; `PendingRegistry` keys everything by `tool_use_id`.
- `render.py` — `callback_data` codec already carries `tool_use_id` (`a|<id>|q.o`, `p|<id>|a`,
  `m|<id>|o`); one `Coalescer` per turn (RB5 single-stream).

**P5 delta:**

```
                          bot.py (commands, SB1)                         StreamingSession (per chat)
 /switch B  ──▶  (NO busy-guard — D2 relaxed) store.switch ───────────▶  foreground := B  (runs in A keep going)
 /new ...   ──▶  (NO busy-guard — D2 relaxed) create + switch
 message→A  ──▶  handle_message(chat_id, A) ──┐  acquire A's PER-PROJECT lock (D1) ; if >cap → QUEUE (D6)
 message→B  ──▶  handle_message(chat_id, B) ──┤  acquire B's PER-PROJECT lock  ── CONCURRENT with A's run
                                              │
   engine(A).send ── injects AskEvent(session_id=A_sid, tool_use_id=T1) ──┐
   engine(B).send ── injects PermissionEvent(... tool_use_id=T2) ─────────┤
                                              ▼                           ▼
                                 per-chat PENDING INDEX (D3):  { T1 → (A, ask, evt), T2 → (B, perm, evt) }
                                              │
 tap [Allow] (callback_data "m|T2|o") ──▶ on_callback (SB1 _authorized) ──▶ resolve_callback(chat_id,"m|T2|o")
        decode → tool_use_id=T2 → index[T2] → (B,perm) → engine(B).resolve(T2, allow)   ← routes to B, not active(=B or A)
 reply "use X" (plain text)        ──▶ free-text target = most-recent-prompt (D5) → engine(<target>).resolve(...)
                                              │
 A needs approval while B foreground ──▶ NOTIFY: send "🔔 A — Claude needs approval" + keyboard (D4)
 A finishes                          ──▶ NOTIFY: send "✅ A — done"  (D4)
                                              │
 ALL sends/edits/notifs for the chat ──▶ per-chat RATE-GATED SENDER (~1 msg/s) over per-project coalescers (RB5/D8)
restart  ──▶  (P4 RB3 unchanged) registry loads; NO in-flight runs; queue/run-status are in-memory only
```

### Components (new / changed)

- **`claude_tg/stream_session.py`** (the bulk of P5):
  - **`_ProjectRuntime` gains live-turn state (D7).** Add `lock: asyncio.Lock` (per-project turn lock,
    moved off `_ChatState`), `status_message_id`/`status_text` (this project's status line),
    `pending_ask`/`pending_plan`/`ask_answers`/`awaiting_text_*` (this project's pending slots), and a
    `status` enum (`idle`/`running`/`awaiting_*`/`queued`) for `/projects` (D7).
  - **`_ChatState` shrinks to a coordinator.** Keeps `runtimes`, and **gains** the per-chat
    **pending-request index** `pending_index: dict[str, _PendingRef]` where `_PendingRef =
    (project_name, kind)` (D3), a **foreground** marker (which project the operator is "watching" — the
    P4 "active" project, used to decide inline-vs-notify, D4), a **free-text target** (D5), a **run
    queue** + a count of running projects (D6), and a reference to the **per-chat rate-gated sender**
    (D8).
  - **`handle_message(chat_id, text)`** resolves the **target project** (active for a new turn), takes
    **that project's** lock (not the chat's). At the cap → enqueue + notify. Free-text capture checks the
    **target project's** `awaiting_text_*` (now resolved via the free-text target, D5), not the chat's.
    Drives the turn against that project's engine + its own coalescer; on each injected ask/plan/perm it
    **registers `tool_use_id` in `pending_index`** and decides inline-render vs `🔔 name —` notify by
    comparing the turn's project to the current foreground (D4).
  - **`resolve_callback` / `_resolve_free_text` / `handle_cancel`** route by the **pending index**:
    decode → `tool_use_id` → `pending_index[id]` → `(project, kind)` → resolve against **that project's**
    engine. **`_active_engine` is retired** from the resolve path (kept only where a genuinely
    foreground-only action is meant). `handle_cancel(chat_id, name=None|"all")` (D9).
  - **The run scheduler (D6):** a tiny per-chat FIFO. `handle_message` either runs immediately (under
    cap) or appends a queued turn; a finishing run pops the next. Pure in-memory; bounded by
    `MAX_CONCURRENT_RUNS`.
- **`claude_tg/bot.py`:**
  - **Remove** the `is_busy` refusal from `cmd_switch` / `cmd_new` (D2). **Keep** it in `cmd_reset`,
    rewired to the per-project `is_busy(chat_id, active_name)`.
  - **`cmd_cancel`** parses an optional `<name>` / `all` (D9) and calls the per-project
    `handle_cancel`.
  - **`cmd_projects`** renders the per-project status column (D7).
  - **New optional `cmd_to`** (`/to <name> <text>`, D5 escape hatch) — routes a free-text answer to a
    named project's pending free-text request. Registered like the other commands (SB1 `allowed`
    filter). *(Owner-review: this command is part of the D5 decision; drop it if the owner prefers
    reply-to-message only.)*
  - **The notification send path (D4)** is just the existing `send` closure, called by the streaming
    session with a name-prefixed body. `on_callback` is **unchanged** (SB1 boundary intact) — only what
    `resolve_callback` does *internally* changes (index lookup vs `_active_engine`).
- **`claude_tg/render.py`:**
  - **`callback_data` codec UNCHANGED** (D3 byte-budget note below). Add small pure helpers for the
    **name-prefixed notification strings** (`notify_needs_approval(name)`, `notify_done(name, …)`,
    `notify_error(name, …)`) and a `/projects` **status label** map — kept here with the other render
    strings (no I/O).
  - **The per-chat rate-gated sender (D8)** is a new tiny class (a min-interval / token-bucket gate over
    the injected clock, mirroring `Coalescer`'s injected-clock + pure-decision style) so the cross-project
    send-budget logic is unit-testable with no real sleeps. The actual awaiting stays in `bot.py`/the
    session (the render layer only *decides* timing, exactly as `Coalescer` does today).
- **`claude_tg/config.py`:** add `MAX_CONCURRENT_RUNS` (parse like `ANSWER_BACKSTOP_SECONDS`: empty/unset
  → default **3**; must be a positive int). Optionally a `RENDER_CHAT_SEND_INTERVAL_SECONDS` for the D8
  budget (default ~1 s). No new **required** env; defaults keep `main` runnable.
- **`engine/`** — **unchanged.** The correlation key (`session_id` + `tool_use_id`) is **already on
  every event and decision**; the engine is already per-project (one `Engine` per `_ProjectRuntime`),
  and `PendingRegistry` already routes/cancels/backstops by `tool_use_id`. P5 adds the routing **above**
  the engine, not inside it — confirming ADR-001's "the consumer extends the contract" framing.

### The correlation-envelope design (D3 — the core P5 addition)

The ADR-001 gap says: *"P4/P5 must extend events + decisions with a session/run correlation envelope so
an inbound answer routes to the correct pending request (session_id + tool_use_id / control-request
id)."* The shipped engine **already** put the envelope on the wire — every injected `AskEvent` /
`PlanEvent` / `PermissionEvent` carries both `session_id` and `tool_use_id`, and the engine's
`PendingRegistry.resolve(tool_use_id, …)` routes by id. What P4 did **not** do (because D2 forbade
concurrency) is honor that envelope **at the relay**: it collapsed every inbound answer to
`_active_engine` and kept one pending slot per chat. **P5's envelope work is therefore a relay-layer
refactor, not an engine change.** The relay maintains a per-chat **pending-request index** `{tool_use_id
→ (project_name, kind)}`, populated when a project's stream injects an ask/plan/permission and torn down
on resolution; `resolve_callback`/`_resolve_free_text`/`handle_cancel` look the **id from the
callback_data** (button taps) or the **free-text target** (plain replies) up in the index and resolve
against the **owning project's** engine — so a tap for project A resolves A's request even while B is the
foreground project. `tool_use_id` is globally unique (an SDK `toolu_…`/UUID), so it alone is a sufficient
routing key; `session_id` is the **disambiguator/validator** (the relay can assert the held event's
`session_id` matches the project's current session before resolving — defense-in-depth against a stale id
colliding after a resume). **`callback_data` stays unchanged** because it already carries `tool_use_id`
and the codec is at its byte budget (the worst-case ask frame is ~57 of 64 bytes; a project name would
overflow it) — and it doesn't need a project field, since the index maps id→project at resolve time.

### Data model (no schema change)

**Nothing new is persisted.** The schema-v2 registry (`{version, chats:{<id>:{active, projects:{<name>:
{cwd, session_id, created_at, last_active}}}}}`) is **unchanged** — concurrent runs each persist their
own project's `session_id` on `result` exactly as P4 does (independent writes to distinct project
records; the store's per-chat/per-project keying isolates them, RB6). All P5 runtime state is
**transient in-memory** on `_ChatState`/`_ProjectRuntime` (pending index, foreground marker, free-text
target, run queue, running-count, per-project status + locks + status lines), reset on restart — exactly
like P4's `/yolo`/grants (D3/SB5). On restart there are **no in-flight runs** to reconcile (RB3): the
registry loads, every project is idle, lazy-resume on next message.

### Auth / security model (unchanged trust boundary)

Secret token + chat-id allowlist (SB1). The new inbound surfaces are **`/cancel <name>`**, **`/to <name>
…`** (text commands — same `allowed`-filtered + `_ok`-rechecked path as every P4 command; **no new
callback surface**) and the **notification sends** (outbound only — no new inbound). `on_callback`
remains the authoritative SB1 button boundary; routing-by-correlation-id happens **only after**
`_authorized` passes, and resolving by id can only ever hit a request the *operator's own* concurrent
runs created (the index is per the operator's chat). **The load-bearing SB1 invariant P6 will verify: a
tap can never resolve a request the operator is not authorized for, and a tap for one project can never
resolve another project's request** (the index maps each id to exactly one owning project; a mismatched
or absent id no-ops).

---

## Risks & open questions

**Top risks**
1. *(Product — the genuinely-hard call, D5)* **Free-text routing ambiguity.** A plain reply has no
   inline id, so with several projects awaiting free-text it is inherently ambiguous.
   *Mitigation:* most-recent-prompt default + name-echoed prompts + two escape hatches (reply-to-message,
   `/to <name>`). **Flagged ⚠️ for owner review** — the owner may prefer a stricter rule (always require
   reply-to / `/to`) or a looser one. The relay must **never silently misroute** a free-text answer to
   the wrong project (better to ask "which project?" than guess wrong) — that is the SB-adjacent
   correctness bar.
2. *(Technical — the largest refactor, D7)* **Lifting live-turn state from `_ChatState` to
   `_ProjectRuntime`.** The whole streaming layer is built on the P4 "one turn per chat → live state on
   the chat" assumption (the `_ChatState`/`_ProjectRuntime` split, the per-chat lock, the lock-free
   resolve). *Mitigation:* the move is mechanical (the fields already exist; they relocate one level
   down) and the **lock-free-resolve invariant is preserved** (a resolve still runs concurrently with
   the held turn — now it just finds the engine via the index instead of `_active_engine`). Heavy unit
   coverage on the per-project lock + index. ADR-005 records why the split moves.
3. *(Reliability — first built here)* **RB5 under concurrency.** N projects flushing status + N
   notifications can burst past Telegram's ~1 msg/s/chat. *Mitigation:* per-project coalescers **plus** a
   per-chat global rate-gated sender (D8); a dedicated concurrent-burst RB5 test (RB7).
4. *(Technical/operational)* **The async-permission-latency assumption at scale (ADR-001's "single most
   load-bearing unproven assumption").** P5 holds **multiple** permission callbacks open at once (one per
   awaiting project), each potentially up to the 60-min backstop, across concurrent SDK clients.
   *Mitigation:* each hold is an independent `asyncio.Future`+backstop in its project's engine (proven in
   isolation by P2/P4); the live-verify checklist must exercise **two simultaneous holds** to confirm the
   SDK tolerates concurrent open callbacks. Flag if the SDK serializes control requests across clients.
5. *(Operational)* **Host resource use.** N concurrent Claude sessions = N CLI/SDK processes + N model
   streams. *Mitigation:* `MAX_CONCURRENT_RUNS` cap (D6, default 3) + queue; documented in P7.
6. *(Reliability)* **One run's failure must not poison others.** *Mitigation:* per-project runtimes are
   already isolated (separate engine/policy/lock); a `_drive_turn` exception is contained to that
   project's turn and its slot is released (the scheduler still pops the next). Dedicated test.

**Open questions (resolve during build / at G-Scope)**
- **D2/D5/D7 owner confirmation** (the three ⚠️ decisions) — the orchestrator chose defaults; the owner
  ratifies or adjusts at G-Scope.
- **Foreground concept (D4):** is "the active project" exactly the operator's foreground, or should the
  bot notify even for the active project if the operator has since switched? Default: notify iff the
  project ≠ current `active`; tune in build.
- **`MAX_CONCURRENT_RUNS` default** (D6) — 3 is a guess; settle against real host behavior in
  live-verify.
- **Queue visibility** — do we surface queue position live (edit "queued behind N")? Default: a one-time
  "queued" notice + the `/projects` `queued` status; richer live position is deferrable.
- **`/cancel all` blast radius** — confirm it only cancels the **operator's own** chat's runs (it does —
  per-chat), never another deployment's.

**ADRs to write before code**
- **ADR-005 — Concurrency & the session/run correlation model.** The load-bearing P5 record: (1) the
  **concurrency model** (one run per project, N per chat, the per-deployment cap + FIFO queue — D1/D6);
  (2) **the correlation envelope** — that the engine already stamps `session_id`+`tool_use_id` and that
  P5 honors it via a **relay-layer pending-request index** routing every decision-in to the owning
  project (closing the ADR-001 gap **at the consumer**, not the engine — D3), and **why `callback_data`
  is unchanged** (byte budget; id is sufficient; index maps id→project); (3) **relaxing the D2
  busy-guard** — why `/switch`/`/new` become free once routing is by id and why `/reset` stays
  per-project-guarded (D2); (4) **moving live-turn state from `_ChatState` to `_ProjectRuntime`** and why
  the lock-free-resolve invariant survives (D7); (5) **the notification trigger + throttle model** (D4);
  (6) **RB5 under concurrency** = per-project coalescer + per-chat send budget (D8); (7) **free-text
  routing** = most-recent-prompt + reply-to / `/to` (D5, with the "never silently misroute" bar); (8)
  **nothing new persisted; in-memory queue/status; RB3 unchanged** (no durable run journal). Grounded in
  ADR-001 (the gap + double-attach + async-latency caveats), ADR-004 (D2 + the `_ChatState`/
  `_ProjectRuntime` split it relaxes), and the cross-cutting RB5/SB1.

---

## SDLC plan (delta)

**No SDLC change from P1–P4** — same GitHub Actions CI (tests + ruff + mypy + secret-scan), same lenient
ruff/mypy baseline, same substrate-mocked unit tests + a live-verify probe (not in CI). The P1–P4 test
suite is the **regression floor** (count is **not** an acceptance gate — test policy); P5 adds
**correlation-routing**, **per-project concurrency + queue/cap**, **notification**, **RB5-under-
concurrency**, and **concurrency-aware cancel/reset/rm** coverage, plus the SB1 cross-project-routing
tests. Branch `feat/p5-concurrency`; supervised-autonomous per-task build (isolated Implementer +
independent reviewer, auto-commit on green + AGREE), same as P1–P4. `main` stays runnable (one-shot is
the safe default; concurrency is streaming-only and gated by the existing `ENGINE_MODE` flag — the flip
remains the owner's call). **Live-verify must use real phone-verify** (per the project memory: the relay
deadlocks without PTB `concurrent_updates(True)` — already on — and `engine.resolve` probes bypass the
PTB callback path, so a real two-project concurrent run with a real button tap is mandatory to prove
cross-project routing).

---

## Roadmap

**In scope (P5 / this pipeline):** ADR-005 → the **correlation envelope** (relay pending-request index +
id-routed `resolve_callback`/`_resolve_free_text`/`handle_cancel`, retiring `_active_engine` from the
resolve path) → **per-project concurrency** (per-project turn lock; the cap + FIFO queue) → **relax the
D2 busy-guards** (`/switch`/`/new` free; `/reset` per-project-guarded) → **lift live-turn state to
`_ProjectRuntime`** + per-project status on `/projects` → **proactive notifications** (name-prefixed,
inline-vs-notify by foreground) → **RB5 under concurrency** (per-project coalescer + per-chat rate-gated
sender) → **concurrency-aware cancel/reset/rm** (+ optional `/cancel <name>|all`, `/to <name>`) → live
verify + owner phone-verify checklist.

**Out of scope (deferred):** multi-operator / cross-chat concurrency (never — SB1); **durable** run/queue
persistence across restart (RB3 keeps abandon-and-lazy-resume; a future durable run journal is additive);
per-project approval policies; `/rename`; voice notes / status-dashboard message (future); the
`ENGINE_MODE=streaming` **default flip** (owner's call); the **full SB consolidation + threat model**
(**P6** — which must specifically threat-model cross-project answer routing + the notification channel).

**Expected build order (input to `/plan`):**
1. **ADR-005** — concurrency & correlation model (from D1–D9 + ADR-001/ADR-004).
2. **Correlation envelope (D3)** — add the per-chat `pending_index`; register an id→(project, kind) on
   each injected ask/plan/permission; route `resolve_callback`/`_resolve_free_text`/`handle_cancel` by
   the index; retire `_active_engine` from the resolve path; assert held-event `session_id` matches
   (defense-in-depth). Unit (mock engine): tap for A resolves A while B is active; stale/foreign id
   no-ops; SB1 unauthenticated tap dropped.
3. **Per-project run state (D7)** — move the turn lock + status line + pending slots + free-text capture
   from `_ChatState` to `_ProjectRuntime`; add the per-project `status`; preserve the lock-free-resolve
   invariant. Unit (mock substrate): two projects hold independent turn state; resolve still runs
   concurrently with a held turn.
4. **Concurrency + cap/queue (D1/D6)** — per-project lock (same-project second message → `StreamingBusy`;
   different project → concurrent); `MAX_CONCURRENT_RUNS` + FIFO queue + the "queued" notice + a finishing
   run pops the next. Config key. Unit: 2 concurrent runs interleave; a 4th queues at cap=3; one run's
   failure doesn't block another (isolation).
5. **Relax the busy-guards (D2)** — drop the `is_busy` refusal from `cmd_switch`/`cmd_new`; rewire
   `cmd_reset` to per-project `is_busy(chat_id, name)`. Unit: `/switch` mid-run succeeds and the prior
   run keeps going; `/reset` mid-run on the same project is refused, on an idle project succeeds.
6. **Notifications (D4)** — name-prefixed `🔔/✅/⚠️` for non-foreground projects; inline for the
   foreground; routed through the rate-gated sender. Unit (mock send): background ask → a `🔔 name —`
   send; foreground ask → inline (no extra ping); SB3 (no raw body in the ping).
7. **RB5 under concurrency (D8)** — per-project coalescer + per-chat rate-gated sender (injected clock,
   pure timing). Unit + the **RB5-under-concurrency** scenario (RB7): concurrent bursts across projects →
   bounded send rate, no dropped verbatim.
8. **Concurrency-aware cancel/reset/rm + optional `/cancel <name>|all`, `/to <name>` (D9/D5)** —
   per-project `handle_cancel`; `/rm` refuses a running project; the free-text target + escape hatches.
   Unit: `/cancel <name>` aborts only that run; `/rm` of a running project refused; `/to work …` routes a
   free-text answer to `work`.
9. **SB/RB/regression test matrix (RB7)** — cross-project routing (tap-for-A-never-resolves-B);
   SB1 unauthenticated/forged tap dropped under concurrency; RB5-under-concurrency; RB6 two-concurrent-
   persists-no-clobber; RB1/RB2 one-run-failure-isolated; RB3 restart→no-in-flight→idle; one-shot
   **unchanged** regression; the P1–P4 floor still green.
10. **Live verify + owner checklist** — real Claude, **two projects concurrently**: long run in A while
    interactive in B; A's permission/ask fires as a `🔔 A —` notification and a **real button tap**
    resolves A while B is active (proves cross-project routing on the live PTB path); A's completion
    notifies; `/cancel <name>`; cap/queue with ≥4 starts; contained + scrubbed; `verify.md`
    phone-checklist.

---

## Security posture & blast radius (SB1/SB3/SB5/SB6)

P5 **widens what runs concurrently**, not the trust model:

- **SB1 — every routed decision-in stays operator-authenticated.** The allowlist + the `_authorized`
  recheck in `on_callback` are **unchanged**; routing-by-correlation-id happens strictly **after** SB1
  passes. The new routing's security property: a tap resolves a request via the **per-chat** pending
  index, so it can only ever hit a request **the operator's own concurrent runs created** — and the
  id→project map means **a tap for one project can never resolve another project's request** (a
  mismatched/absent id no-ops, RB1). A forged/foreign `callback_data` still decodes to `None`. The two
  new commands (`/cancel <name>`, `/to <name>`) ride the same `allowed`-filtered path; **no new callback
  surface**. P6 must verify the cross-project-routing property explicitly.
- **SB3 — notifications are body-free.** A proactive ping carries the **project name + the existing
  body-free `tool_input_summary`** (lengths, not contents — the engine already produced it for the
  inline render); the notification helpers in `render.py` never re-derive a summary from raw input and
  never log content. No raw Write body / Bash secret ever appears in a ping.
- **SB5 — `/yolo` stays per-project, transient, loud, reset-on-restart.** Under concurrency several
  projects may each be in yolo; each shows **its own** loud `⚠️` marker on **its own** turns (no global
  silent allow-all). Bypass never survives a restart (in-memory, like P4).
- **SB6 — fail closed under concurrency.** At the cap, excess **queues** (never silently dropped); a
  misrouted/stale id **no-ops** (never auto-approves); a project whose cwd drifted out of roots is still
  refused per turn (P4 SB2 re-validation, unchanged) — independently per concurrent run; one run's
  failure fails clean and does not open another (RB1/RB2).
- **Blast radius (SB6).** Still a **single allowlisted operator**; concurrency widens *how many of
  Claude's runs are live at once* (bounded by `MAX_CONCURRENT_RUNS`), not *what gates them* — every risky
  tool in every concurrent run still hits the P2 permission gate on its project's policy. The full SB
  consolidation + threat model (cross-project routing + the notification channel) is **P6**.
