# Progress: p5-concurrency

_Plan generated 2026-06-23 from design.md · 11 tasks · supervised build (autonomous: isolated Implementer + independent reviewer, auto-commit on green + AGREE)_

> **Context for the builder.** Delta on a shipped codebase (P1–P4 on `feat/p5-concurrency`,
> branched off `main` @ `b589527`). **Every task keeps `main` runnable** — one-shot is the safe
> default; all P5 concurrency is **streaming-only** (`ENGINE_MODE=streaming`), behind the existing
> S4 flag (the owner has not flipped the default — don't). Locked decisions **D1–D9** live in
> [`design.md`](design.md); the concurrency + correlation rationale is **ADR-005** (T1) and must not
> contradict [ADR-001](../../adr/ADR-001-session-substrate.md) (the correlation-envelope gap +
> double-attach + async-latency caveats) or [ADR-004](../../adr/ADR-004-multi-project-sessions.md)
> (D2 single-active-run + the `_ChatState`/`_ProjectRuntime` split P5 relaxes).
>
> **The headline:** relax P4's single-active-run invariant. A run continues after `/switch`; N
> projects run concurrently per chat (bounded by a cap + FIFO queue); an inbound tap / free-text reply
> routes to the **owning** project via a per-chat **pending-request index** (the ADR-001 gap, honored
> **at the relay** — the engine already stamps `session_id`+`tool_use_id` on every injected
> ask/plan/permission event and `PendingRegistry.resolve(tool_use_id, …)` already routes by id); the
> bot **proactively notifies** when a non-foreground project needs the operator or terminates; and
> **RB5 holds under concurrency**.
>
> **Reuse, don't rebuild.** The engine is **unchanged** (D3 / design §"engine — unchanged"): every
> event carries `session_id` (all 7 kinds) and `tool_use_id` (Ask/Plan/Permission/ToolUse/Error);
> `Engine.resolve(tool_use_id, decision) -> bool`, `Engine.cancel(tool_use_id=None) -> int`,
> `Engine.session_id` (property), and the per-Engine `PendingRegistry` (keyed by `tool_use_id`, 60-min
> backstop) are the seams P5 routes **above**. Harvest: `Coalescer` (injected-clock, pure-decision
> RB5 throttle — the model for the new send-budget gate), `decode_callback` (carries `tool_use_id`,
> **unchanged**), `resolve_within_roots` (SB2), the registry CRUD (`get_active`/`get_project`/
> `list_projects`/`create`/`switch`/`remove`/`touch`), `_is_resume_failure` (QF3 recovery).
>
> **Load-bearing invariant carried from P4 (T4/T5 review).** A resolve / cancel runs **concurrently
> with the held turn** (the turn is parked inside `engine.send`; the callback handler calls
> `engine.resolve` on the same loop to unblock it) — so the resolve path is and stays **lock-free**.
> P5 changes only *which engine* a resolve finds: the pending index (id→project) instead of
> `_active_engine`. PTB `concurrent_updates(True)` is already on (`bot.build_application`) — required
> for the answer-hold relay; do not remove it.
>
> **Gates** (from the worktree `.venv`): `pytest`, `ruff check .`, `mypy`,
> `python scripts/secret_scan.py`. Regression floor = the **P1–P4 suite green** (count is **not** an
> acceptance gate — test policy). Commits: clean single-line, **no Co-Authored-By trailer**.
>
> **Owner-review flags surfaced at G-Scope (design D2/D5/D7):** the three hard calls are baked into
> the tasks below (D2 = T6, D5 = T9, D7 = T4). If the owner adjusts any at the gate, the affected task
> is where it lands.

## Task list
- [x] T1 — ADR-005: concurrency model + the session/run correlation envelope (93a352e)
- [x] T2 — Pending-request index + id-routed resolve/cancel (the core; retire `_active_engine`) (7ecb402)
- [x] T3 — Notification triggers: inline-vs-`🔔 name —` by foreground (D4; render strings + SB3) (bc33b40)
- [x] T4 — Lift live-turn state `_ChatState`→`_ProjectRuntime` + per-project `status` (D7) ⚠️ (bc21e9f)
- [ ] T5 — Per-project turn lock + concurrent runs; `is_busy(chat_id, name)` (D1)  [+T4-review: wrap end-of-turn status/status-line reset in try/finally (concurrent+persistent runs mustnt stick at running/awaiting_*); add held-ask→/cancel→status idle test]
- [ ] T6 — Concurrency cap + FIFO queue (`MAX_CONCURRENT_RUNS`, D6) + config key
- [ ] T7 — Relax the busy-guards: `/switch`/`/new` free; `/reset` per-project-guarded (D2) ⚠️  [+T3-review: when wiring notify-error, pass body-free ErrorKind NOT event.message; add SB3-at-call-site test + foreground-routing test]
- [ ] T8 — RB5 under concurrency: per-project coalescer + per-chat rate-gated sender (D8)
- [ ] T9 — Free-text routing + `/cancel <name>|all`, `/rm`-running-refused, `/to` (D5/D9) ⚠️
- [ ] T10 — Integration / SB·RB·regression matrix (RB7 + cross-project routing + RB6)  [+T2-review gap: pin two CONCURRENT multi-question asks accumulate independently (per-id, no cross-contamination)]
- [ ] T11 — Live verify: two-project-concurrent phone-verify + verify.md checklist

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked · ⚠️ owner-review decision baked in (D2/D5/D7)

## Tasks

### T1 — ADR-005: concurrency model & the session/run correlation envelope
- **Goal:** Record the P5 architecture decisions (D1–D9) as `docs/adr/ADR-005-concurrency-correlation.md`, Status **Proposed**, before any code — the load-bearing record of *why* the relay routes by a pending index and *why the engine is unchanged*.
- **Depends on:** none
- **Files (expected):** `docs/adr/ADR-005-concurrency-correlation.md`
- **Acceptance:**
  - WHEN ADR-005 is written, it SHALL record: (1) the **concurrency model** — one active run per *project*, N concurrent per chat, the per-deployment soft cap + per-chat FIFO queue (D1/D6); (2) **the correlation envelope** — that the engine **already** stamps `session_id`+`tool_use_id` on every injected ask/plan/permission and `PendingRegistry.resolve(tool_use_id,…)` already routes by id, so P5 honors the ADR-001 gap via a **relay-layer pending-request index** `{tool_use_id → (project, kind)}` routing every decision-in to the **owning** project (closing the gap **at the consumer, not the engine**, D3), AND **why `callback_data` is unchanged** (byte budget — worst-case ask frame ~57/64 B; the id is a sufficient routing key; the index maps id→project at resolve time); (3) **relaxing the D2 busy-guard** — why `/switch`/`/new` become free once routing is by id, and why `/reset` stays per-project-guarded; (4) **moving live-turn state `_ChatState`→`_ProjectRuntime`** and **why the lock-free-resolve invariant survives** (D7); (5) the **notification trigger + throttle model** (D4); (6) **RB5 under concurrency** = per-project coalescer + per-chat send budget (D8); (7) **free-text routing** = most-recent-prompt + reply-to / `/to`, with the **"never silently misroute"** bar (D5); (8) **nothing new persisted** — in-memory queue/status/index; **RB3 unchanged** (no durable run journal).
  - It SHALL cite ADR-001 (the gap + double-attach + the single-most-load-bearing async-permission-latency caveat — now *multiple* concurrent open holds) and ADR-004 (D2 + the split it relaxes), and SHALL NOT contradict ADR-001/002/003/004.
  - It SHALL state the **SB1 property P6 will verify**: a tap resolves only via the per-chat index, so it can only hit a request the operator's own runs created, and **a tap for project A can never resolve project B's request** (id→one owner; mismatched/absent id no-ops).
- **Tests:** none — decision record only.
- **Status:** todo

### T2 — Pending-request index + id-routed resolve/cancel (the core)
- **Goal:** Replace `_ChatState`'s single `pending_ask`/`pending_plan` slots with a per-chat **pending-request index** `{tool_use_id → (project_name, kind, held_event)}`; route `resolve_callback` / `_resolve_free_text` / `handle_cancel` by the index against the **owning project's** engine; **retire `_active_engine` from the resolve path**. **The ADR-001 gap, closed at the relay.** *(This task may stage on the still-single-active-run base — T2 routes by id but at most one project runs until T5; that is intentional and keeps each commit green.)*
- **Depends on:** T1
- **Files (expected):** `claude_tg/stream_session.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN a turn's engine injects an `AskEvent`/`PlanEvent`/`PermissionEvent`, the relay SHALL register `tool_use_id → (project, kind, event)` in a per-chat `pending_index` (keyed off the project the turn is running on); the entry SHALL be **cleared** on resolve / cancel / backstop-noop / turn-end (no leak across turns).
  - WHEN `resolve_callback(chat_id, data)` decodes a `tool_use_id`, it SHALL look that id up in `pending_index`, resolve against **that project's** engine (`rt.engine.resolve(tool_use_id, decision)`), and SHALL NOT consult `_active_engine`; an id **absent** from the index SHALL resolve nothing and return `handled=False` (RB1, benign no-op).
  - WHEN the held event's `session_id` does **not** match the owning project's engine `session_id` (defense-in-depth against a stale id after a resume), the relay SHALL refuse to resolve and no-op (never resolve the wrong session).
  - WHEN `handle_cancel` runs, it SHALL clear the relevant `pending_index` entries for the cancelled project(s) and remain lock-free (it unblocks a held turn).
  - **SB1:** routing-by-id SHALL happen only after the bot's `_authorized` recheck (unchanged in `on_callback`); a `callback_data` that `decode_callback` rejects (foreign/stale/malformed → `None`) SHALL resolve nothing.
- **Tests (mock engine, single + staged-multi):** a tap whose id is in the index resolves the **right** project's engine; a tap for a **different** project's id (two entries present) resolves that other project, never the first; a stale/foreign id no-ops (`handled=False`); the index entry is cleared after resolve and after `handle_cancel`; held-event `session_id` mismatch → no-op; SB1 unauthenticated path still drops (existing `test_bot` coverage holds). Multi-question ask accumulation (`ask_answers`) still resolves only when all questions answered.
- **Status:** todo
- **Risk:** ⚠️ **Core + entangled with T4** (the index lives on `_ChatState` but the held event / answer accumulator move to `_ProjectRuntime` in T4). Keep T2 = "route by id, slots still where P4 had them"; T4 = "relocate the slots". If the Implementer finds the two cannot cleanly separate, **merge T2+T4** (flag at build) rather than half-move state.

### T3 — Notification triggers: inline-vs-`🔔 name —` by foreground (D4)
- **Goal:** Add the **pure render strings** for name-prefixed proactive notifications and a `/projects` status-label map to `render.py`, and the relay decision *"inline-render iff the turn's project == current foreground, else send a `🔔/✅/⚠️ <name> —` notification"*. **SB3: body-free** (project name + the engine's existing `tool_input_summary` only — never re-derive a summary from raw input).
- **Depends on:** T2
- **Files (expected):** `claude_tg/render.py`, `claude_tg/stream_session.py`, `tests/test_render.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN a project that is **not** the chat's current foreground (the P4 "active") injects a permission/ask/plan event, the relay SHALL send a **name-prefixed** message (`🔔 <name> — Claude needs approval` / `… asks a question` / `… proposes a plan`) carrying the SAME keyboard the inline render would (so the tap routes by T2's index regardless of which project is foreground); WHEN the project **is** the foreground, it SHALL render inline as today (**no extra ping**).
  - WHEN a non-foreground project emits a terminal `ResultEvent`/`ErrorEvent`, the relay SHALL send `✅ <name> — done` / `⚠️ <name> — <short>`; a foreground terminal renders inline as today.
  - The notification helpers in `render.py` SHALL be **pure** (no I/O), and SHALL carry only the project name + the event's already-body-free summary (**SB3** — no raw Write body / Bash secret ever in a ping; the helper never re-derives a summary).
  - A "foreground" SHALL be a per-chat marker (default = the store's `active`); reading it SHALL be read-only (no project creation).
- **Tests:** background permission/ask/plan → a `🔔 <name> —` send + the right keyboard; foreground ask → inline only (no ping); background result → `✅ <name> —`, error → `⚠️ <name> —`; **SB3** — a permission notification body contains the summary, never the raw tool input (mutation-probe: a secret in `tool_input` never appears in the rendered string); helper purity (no I/O).
- **Status:** todo

### T4 — Lift live-turn state `_ChatState`→`_ProjectRuntime` + per-project status (D7) ⚠️
- **Goal:** Move the live-turn fields (status-line id/text, `pending_ask`/`pending_plan`/`ask_answers`, `awaiting_text_*` free-text capture) from `_ChatState` to `_ProjectRuntime`, and add a per-project `status` enum (`idle`/`running`/`awaiting_approval`/`awaiting_answer`/`awaiting_plan`/`queued`) for `/projects`. **The largest structural refactor** (the whole streaming layer is built on "one turn per chat → live state on the chat"). The lock-free-resolve invariant MUST survive.
- **Depends on:** T2
- **Files (expected):** `claude_tg/stream_session.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN two projects each have a turn, each SHALL hold its **own** status line (id/text), its **own** pending ask/plan + `ask_answers` accumulator, and its **own** free-text-capture marker on its `_ProjectRuntime` — a status edit / pending answer for one project SHALL NOT touch the other (independence).
  - WHEN a tap or free-text reply resolves a request, it SHALL still run **concurrently with the held turn** (lock-free) — finding the held event + accumulator on the **owning project's** runtime (via T2's index → project → runtime), never on `_ChatState`.
  - The relay SHALL expose a **per-project status** readable for `/projects` (T7-render): a project with no runtime defaults to `idle`; a running project reports `running`; a project holding a pending ask/plan/permission reports the matching `awaiting_*`; a queued turn (D6/T6) reports `queued`.
  - WHEN `reset(chat_id)` runs, it SHALL clear **the active project's** runtime live-turn state (status line + pending slots + free-text marker), not a chat-global slot.
- **Tests (mock substrate):** two `_ProjectRuntime`s hold independent status lines + pending slots; a resolve against project A's held ask works while project B has its own pending ask un-touched; status enum transitions (idle→running→awaiting_*→idle) for a single project; `reset` clears only the active project's live-turn fields; the existing single-project status-line dedupe + transient-delete behavior is preserved (regression).
- **Status:** todo
- **Risk:** ⚠️ **OWNER-REVIEW (D7) + largest refactor.** Touches the P4 `_ChatState`/`_ProjectRuntime` split the streaming layer is built on. Mechanical (fields relocate one level down) but broad. **Likely the highest-risk task — flag for possible split** into T4a (relocate the four field-groups, keep behavior) + T4b (add the `status` enum + transitions) if the diff is large or review stalls.

### T5 — Per-project turn lock + concurrent runs; `is_busy(chat_id, name)` (D1)
- **Goal:** Move the turn lock from `_ChatState` to `_ProjectRuntime` (one `asyncio.Lock` per project), so a message to an **idle** project starts a run even while other projects run; a second message to the **same** running project still raises `StreamingBusy`. Add a per-project `is_busy(chat_id, name)` variant (keep the chat-level `is_busy(chat_id)` = "any project busy" for back-compat) for the T7 busy-guards.
- **Depends on:** T4
- **Files (expected):** `claude_tg/stream_session.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN a message targets the chat's active (idle) project, `handle_message` SHALL acquire **that project's** lock and drive the turn; a concurrent message to a **different** idle project SHALL acquire **its own** lock and run **concurrently** (two engines live at once — the P4 `_stop_other_started` single-active-run stop is removed / no longer stops a concurrently-running project).
  - WHEN a second message targets a project whose turn is **already in flight**, `handle_message` SHALL raise `StreamingBusy` (per-project — unchanged UX per project).
  - `is_busy(chat_id, name)` SHALL report whether **that** project's lock is held; `is_busy(chat_id)` SHALL report whether **any** project for the chat is busy.
  - **RB1/RB2 isolation:** WHEN one project's turn raises inside `_drive_turn`, its lock SHALL be released and **other concurrent runs SHALL be unaffected** (one runtime's failure is contained to itself).
  - **RB6:** two concurrent turns SHALL each persist their **own** project's `session_id` on `result` (independent writes; no clobber — the store's per-project keying isolates them).
- **Tests (mock substrate):** two projects' turns interleave to completion concurrently (the `_drive_turn`s overlap); same-project second message → `StreamingBusy`; different-project second message → runs concurrently; `is_busy(chat_id, name)` true only for the busy project; a forced exception in project A's turn releases A's lock and leaves project B's concurrent turn running; two concurrent results persist to distinct project records (no clobber). **Note:** the cap is T6 — until then "concurrent" is unbounded in the test harness; T6 adds the bound.
- **Status:** todo
- **Risk:** removing `_stop_other_started`'s "stop the other started engine" behavior is the moment two engines are live at once — exercises ADR-001's *multiple concurrent open holds* assumption. Unit-provable with mocks; the **real** SDK-tolerates-concurrent-holds proof is T11 (live).

### T6 — Concurrency cap + FIFO queue (`MAX_CONCURRENT_RUNS`, D6)
- **Goal:** Bound *executing* runs across the process with a soft cap (`MAX_CONCURRENT_RUNS`, default 3); excess turns **queue FIFO per chat** (the operator is told `⏳ queued behind N run(s)`), and a finishing run pops the next. Add the config key. Pure in-memory; bounded; no persistence.
- **Depends on:** T5
- **Files (expected):** `claude_tg/config.py`, `claude_tg/stream_session.py`, `tests/test_config.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN the number of *executing* runs (across all projects/chats in the process) is **below** `MAX_CONCURRENT_RUNS`, a new turn for an idle project SHALL start immediately; WHEN **at** the cap, the turn SHALL be **accepted and queued** (FIFO, per chat), the operator SHALL be told it queued (a one-time `⏳ queued behind N run(s)` notice — D6), and it SHALL start when a slot frees (a finishing run pops the next). It SHALL NOT be refused (refusing defeats multitasking) and SHALL NOT silently drop (**SB6 fail-closed → queue, never drop**).
  - A second message to a project whose own turn is in flight SHALL still raise `StreamingBusy` (a project is never queued behind **itself**).
  - WHEN `MAX_CONCURRENT_RUNS` is empty/unset/`0`, the system SHALL use the default **3**; a non-positive non-zero / non-integer value SHALL fail loud at startup (parse like `ANSWER_BACKSTOP_SECONDS`).
  - WHEN a queued turn's project is later targeted again or removed before it runs, the queue SHALL degrade safely (RB1 — no crash; a dequeued-but-removed project no-ops).
  - A queued project SHALL report `queued` to `/projects` (T4 status / T7 render).
- **Tests:** `MAX_CONCURRENT_RUNS` parse matrix (unset/empty/`0`→3; `"5"`→5; `"-1"`/`"x"`→raise) in `test_config.py`; with cap=2, two runs execute and a 3rd queues then starts when one finishes (FIFO order preserved); a same-project second message → `StreamingBusy` (not queued); a one-time "queued" notice is sent; one run's failure frees its slot and the queue advances.
- **Status:** todo
- **Risk:** the run-counter / queue is shared process-wide (cap is per-deployment) but routing/queue are per-chat (anti-goal: no cross-chat semantics) — keep the **counter** global, the **queue** per chat. Watch for a slot-leak on the failure path (a turn that raises must decrement the counter + pop the next exactly once).

### T7 — Relax the busy-guards: `/switch`/`/new` free; `/reset` per-project-guarded (D2) ⚠️
- **Goal:** Remove the `is_busy(chat_id)` refusal from `cmd_switch` and `cmd_new` (background runs are the headline — switching no longer strands anything now routing is by id, T2); keep `cmd_reset`'s refusal but rewire it to the **per-project** `is_busy(chat_id, active_name)` (T5). Render the per-project **status column** in `cmd_projects` (D7).
- **Depends on:** T2, T4, T5
- **Files (expected):** `claude_tg/bot.py`, `tests/test_bot_streaming.py`
- **Acceptance:**
  - WHEN `/switch <name>` or `/new <name> <path>` is sent **while another project is mid-run**, the system SHALL proceed (no `is_busy` refusal) — the prior run keeps running in the background; the SB2 cwd re-validation on `/switch`/`/new` (QF2 / existing) and SB4 name validation are **unchanged**.
  - WHEN `/reset` is sent **while the active project's own turn is in flight**, the system SHALL refuse (`is_busy(chat_id, active_name)`) with "`/cancel` it first" (resetting drops *that* project's engine → would orphan its own parked hold); WHEN the active project is **idle** (even if *other* projects are running), `/reset` SHALL proceed and clear only the active project's session.
  - WHEN `/projects` is sent, each line SHALL show the per-project **status** (`running` / `awaiting approval` / `awaiting answer` / `awaiting plan` / `queued` / `idle`) alongside the existing name/cwd/active-marker/last-active (read from T4's per-project status; a project with no runtime → `idle`).
  - All commands SHALL stay allowlist-gated (**SB1**) and never crash on bad/missing args (RB1).
- **Tests:** `/switch` mid-run (another project busy) succeeds and the prior run is untouched (the prior project's lock still held); `/new` mid-run succeeds; `/reset` while the active project is busy → refused; `/reset` while active is idle but another project runs → succeeds (clears only active); `/projects` renders each status label correctly (running / awaiting_* / queued / idle); SB1 non-allowlisted ignored.
- **Status:** todo
- **Risk:** ⚠️ **OWNER-REVIEW (D2 — "the big one").** This is the headline behavior change. The correctness guarantee that makes it safe is **entirely** T2's id-routing (a mid-run `/switch` no longer strands the held turn because the tap resolves by id, not `_active_engine`). The **highest-value regression test** is the inverse of P4's busy-guard test: a held turn in A + `/switch B` + a real tap on A's prompt must resolve **A** (covered end-to-end in T10).

### T8 — RB5 under concurrency: per-project coalescer + per-chat rate-gated sender (D8)
- **Goal:** Keep **one `Coalescer` per running project** (each project's status line throttles independently — already per-turn, now per-project) AND add a **per-chat global send-rate gate** (a tiny injected-clock, pure-decision min-interval / token-bucket class mirroring `Coalescer`, ~1 msg/s/chat) so *all* sends/edits/notifications for a chat funnel through one rate-gated sender and N concurrent projects + their pings never burst past Telegram's ceiling. **RB5 first fully built here under concurrency (RB7).**
- **Depends on:** T3, T5
- **Files (expected):** `claude_tg/render.py` (the new gate class — pure timing), `claude_tg/stream_session.py` (wire the gate over the per-project coalescers + the notification sends), `claude_tg/config.py` (optional `RENDER_CHAT_SEND_INTERVAL_SECONDS`, default ~1 s), `tests/test_render.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - The new send-budget gate SHALL be a **pure class** over an **injected clock** (no real sleeps; `Coalescer`'s style) that *decides* whether/when a send is due; the actual awaiting stays in the session/bot (the render layer only decides timing, as `Coalescer` does today).
  - WHEN several projects in one chat emit status bursts **concurrently**, the combined send/edit rate for that chat SHALL stay bounded (≤ the per-chat budget) and **no verbatim message (ask/plan/error/result/assembled-text) SHALL be dropped** (verbatim still flushes — it is rate-ordered, not discarded).
  - Each running project SHALL retain its **own** `Coalescer` (a status burst in project A SHALL NOT reset/steal project B's status throttle).
  - WHEN `RENDER_CHAT_SEND_INTERVAL_SECONDS` is unset, a sensible default (~1 s) SHALL apply; an invalid value SHALL fail loud at startup (parse like the other numeric keys). No new **required** env.
  - **RB6/SB3 unaffected:** the gate orders sends; it never alters a message body (no summary re-derivation).
- **Tests (injected clock, no sleeps):** the gate's pure decisions (leading-edge immediate, subsequent within-interval deferred, due after interval) mirror the `Coalescer` test style; **RB7 scenario** — two projects each emit a burst of N status + a verbatim each, concurrently → the chat's emitted send count is bounded by the budget and **all verbatim survive** (ordered, not dropped); per-project coalescers remain independent; config parse matrix for the interval key.
- **Status:** todo
- **Risk:** the per-chat gate serializes across *independent* concurrent turns — ensure it never **deadlocks** a held turn (the gate must gate *sends*, never the resolve path; verbatim ask/plan must still reach the operator so they can answer — a starved status line is fine, a starved ask is not). Make verbatim **priority** over coalesced status in the gate ordering.

### T9 — Free-text routing + `/cancel <name>|all`, `/rm`-running-refused, `/to` (D5/D9) ⚠️
- **Goal:** Make free-text answers route correctly under concurrency (D5: most-recent-prompt target + name-echoed prompt + reply-to-message + `/to <name>` escape hatch) and make cancel/rm concurrency-aware (D9): `handle_cancel(chat_id, name=None|"all")`; `/cancel` (active) / `/cancel <name>` / `/cancel all`; `/rm` additionally refuses a **currently-running** project; the optional `/to <name> <text>` command. **The "never silently misroute" bar (D5).**
- **Depends on:** T2, T4, T7
- **Files (expected):** `claude_tg/stream_session.py`, `claude_tg/bot.py`, `claude_tg/render.py` (name-qualified `✏️ <name> — type your answer…` prompt string), `tests/test_stream_session.py`, `tests/test_bot_streaming.py`
- **Acceptance:**
  - WHEN the operator taps "Other"/"Reject" on a project's prompt, the relay SHALL arm **that** project as the per-chat free-text target and the bot SHALL reply a **name-qualified** prompt (`✏️ <name> — type your answer…`); the **next** plain message SHALL resolve that target's pending free-text request (via its runtime, T4). If a **different** project arms free-text capture before the operator replies, the **newest** target wins (the prompt said which — D5 default).
  - WHEN the operator **replies-to** a specific prompt message (Telegram reply-to), the relay SHALL route by that message's `tool_use_id` (the relay maps `message_id → tool_use_id` when it sends a prompt) — an explicit disambiguation that overrides the most-recent default.
  - WHEN the operator sends `/to <name> <text>`, the system SHALL route `<text>` as the free-text answer/feedback to `<name>`'s pending free-text request (allowlist-gated like every command — **no new callback surface**); an unknown name / no-pending → a clear no-op message (RB1).
  - The relay SHALL **never silently misroute** a free-text answer: if the armed target is gone / ambiguous and no reply-to/`/to` disambiguates, it SHALL no-op (and may ask which project) rather than resolve the wrong project.
  - `handle_cancel(chat_id, name=None)` SHALL cancel the **active** project's run; `handle_cancel(chat_id, "all")` SHALL cancel **every** running project for the chat; `handle_cancel(chat_id, name)` SHALL cancel **that** project's run; each SHALL clear that project's pending-index entries + free-text marker and remain **lock-free**. `cmd_cancel` SHALL parse the optional `<name>`/`all` arg.
  - WHEN `/rm <name>` targets a **currently-running** project, the system SHALL refuse ("cancel it first") in addition to the existing refuse-the-active-project + `forget_project` purge (so a live engine is never torn down mid-turn — D9).
- **Tests:** arm-Other-on-A then a plain reply resolves A; arm-Other-on-A then arm-Other-on-B then reply resolves **B** (newest wins); reply-to A's prompt while B is armed resolves **A** (override); `/to work <text>` routes to `work`; ambiguous/gone target no-ops (no misroute); `/cancel` (active) aborts only the active run; `/cancel <name>` aborts only that run, leaving others running; `/cancel all` aborts every run; `/rm` of a running project refused; `/rm` of an idle non-active project still purges (existing behavior holds).
- **Status:** todo
- **Risk:** ⚠️ **OWNER-REVIEW (D5 — the one genuinely-hard product call).** The owner may want a stricter rule (always require reply-to/`/to`) or looser. Keep the routing decision (most-recent vs reply-to vs `/to`) in **one** small resolver so a rule change is a one-spot edit. The reply-to `message_id → tool_use_id` map is new transient per-chat state — bound it (drop on resolve/turn-end) so it can't grow unboundedly.

### T10 — Integration / SB·RB·regression matrix (RB7)
- **Goal:** Prove the design's cross-task acceptance criteria end-to-end (no single task owns these) — the cross-project routing property, RB5-under-concurrency, RB6 no-clobber, RB1/RB2 isolation, RB3 restart, and the **one-shot-unchanged** regression. **No production code.**
- **Depends on:** T2, T3, T4, T5, T6, T7, T8, T9
- **Files (expected):** `tests/test_multi_project.py` (extend) and/or a new `tests/test_concurrency.py`; **no production code**
- **Acceptance:**
  - **⭐ Cross-project routing (the load-bearing SB1 property, P6 will re-verify):** WHEN project A holds a pending ask/permission AND the chat foreground is switched to B AND a tap arrives whose `callback_data` carries **A's** `tool_use_id`, it SHALL resolve **A's** request and **never** B's — verified end-to-end so that if any task regresses the index routing, this **fails loudly**. Conversely a tap for B's id never resolves A.
  - **SB1 under concurrency:** a non-allowlisted tap is dropped; a forged/foreign `callback_data` resolves nothing in **any** project; a stale id no-ops.
  - **RB7 (RB5 under concurrency):** several projects emitting status bursts concurrently produce a **bounded** per-chat send rate and **no dropped verbatim** message.
  - **RB6:** two concurrent runs persist to **distinct** project records without clobber.
  - **RB1/RB2 isolation:** one concurrent run's engine/substrate error fails clean and does **not** affect the other concurrent runs.
  - **RB3:** on restart there are **no in-flight runs** — every project comes back idle; the in-memory queue/run-status/index do not persist; lazy-resume on next message (unchanged).
  - **Cap/queue (D6):** at the cap, the (N+1)th turn queues and starts when a slot frees; `/projects` shows `queued`.
  - **One-shot unchanged:** one-shot mode behaves **exactly as pre-P5** against a v2 store (regression assertion).
- **Tests:** all of the above scenarios via the session/registry/render layers with mock substrate + a scripted multi-project event interleaving; the **P1–P4 floor still green**.
- **Status:** todo

### T11 — Live verify: two-project-concurrent phone-verify + verify.md checklist
- **Goal:** Verify against **real** Claude (not in CI), two projects concurrently, and hand the owner a phone-checklist. **Mandatory real-Telegram path** — per the project memory, `engine.resolve` probes bypass the PTB callback path, so a real two-project concurrent run with a **real button tap** is the only authoritative proof of cross-project routing on the live relay.
- **Depends on:** T10
- **Files (expected):** non-production verify harness under `spikes/` or `scripts/`; `docs/features/p5-concurrency/verify.md`
- **Acceptance:**
  - WHEN run against real streaming Claude, the harness/checklist SHALL: start a **long** turn in project A, `/switch B` (or `/new`) **without refusal**, work interactively in B while A keeps running; A's permission/ask/plan SHALL fire as a `🔔 A — …` notification and a **real button tap** SHALL resolve **A** while B is foreground (A's answer never misrouted to B; B undisturbed); A's completion/error SHALL arrive as `✅ A —` / `⚠️ A —`; `/cancel <name>` SHALL abort the right run; the **cap/queue** SHALL be exercised with **≥4** starts (the 4th queues at default cap 3 and starts when a slot frees); two concurrent permission holds SHALL both stay open (the ADR-001 *concurrent-open-holds* live confirmation). Evidence contained to an **absolute temp dir** + **scrubbed** (SB3); **UUID-grep before commit** (per the live-verify memories — the overall summary can leak a raw session id).
  - `verify.md` SHALL enumerate the manual owner phone-checklist mirroring the above (the authoritative live check at the Verify+QA gate).
- **Tests:** this IS the live verification (manual / real-Claude; not in CI).
- **Status:** todo
