# P5 Concurrency — QA Record

_Two-reviewer QA per the pipeline: a same-model **Verifier subagent** (full-diff, isolated context) + cross-model **Codex** (`gpt-5.x` via `codex exec`). Plus a per-task independent reviewer on T1–T10 during the build. The cross-model Codex was again decisive — it caught concurrency/lifecycle bugs both same-model passes missed (consistent with the `cross-model-qa-catches-concurrency` finding)._

## Verifier subagent (same-model, full P5 diff) — SHIP
- **Blockers:** none. Gates green (729 at the time), 3 mutation probes (cross-project routing, slot-leak, RB6 persist-drift) all caught loudly.
- **Non-blocking:** `set_session_id` docstring overstates its contract vs the RB1 best-effort swallow in `_persist`; a backgrounded `/yolo` run has no loud inline surface until switched-to (matches D4, SB5 intact — defensible); `notify_done/error` same-kind-within-1s throttle effectively unreachable (≤1 terminal/turn).
- **Coverage note:** two concurrent *background* status bursts isn't a reachable state (background runs silent inline per D4); live SDK concurrent-open-holds = T11.
- _Note: the Verifier reasoned about the throttle window for **terminals** and dismissed it as unreachable — but missed that **permission/plan/ask** prompts can be multiple-per-turn. Codex caught that (B1 below). This is exactly the cross-model value._

## Codex round 1 (cross-model) — NO_SHIP → 2 blockers + 3 non-blocking
- **B1** `stream_session.py` `_notify_background`: throttle keyed `(project, kind)` suppressed a 2nd **distinct-`tool_use_id`** permission/plan prompt within the ~1s window — the request was in the pending index but **no keyboard was sent** → unanswerable until backstop.
- **B2** `stream_session.py` busy-guard: only checked `lock.locked()`, but a **queued** turn holds no lock → a 2nd message to a queued project appended a 2nd `_QueuedTurn` (project queued behind itself, violates D6).
- **NB1** `/cancel` of a queued-only turn drained the waiter but reported `0` ("nothing in flight"). **NB2** "Queued behind N" counted only `_running`, not queued-ahead. **NB3** `/reset` while the active project was queued was unpinned.
- **Fix round 1** (`35285e2`), red-green per finding + independent reviewer AGREE:
  - B1 → `_should_notify` keys actionable holds `(project, kind, tool_use_id)`; distinct id always sends, same-id coalesces; terminals stay `(project, kind)`.
  - B2 → new `_is_queued(state, rt)`; busy-guard rejects when running OR already queued.
  - NB1 → cancel counts drained queued turns. NB2 → position = running + queued-ahead. NB3 → `/reset` drains the not-yet-started queued turn (no orphan), consistent with reset-while-running.
  - 729 → 737 tests.

## Codex round 2 (cross-model, fresh pass) — NO_SHIP → 1 blocker + 1 race
- **Blocker** `stream_session.py:713` `_notify_background_ask`: the round-1 B1 fix gated only the **bell** line for a same-id re-emit; the per-question keyboard loop still ran → a same-`tool_use_id` background **ask** duplicated its keyboards (same-id coalescing not actually achieved for asks).
- **Race** (Codex suggested-test #2, elevated to a fix — a real TOCTOU in B2's own guard): in the slot-transfer window after a queued waiter is popped/resolved but before it acquires its lock, `_is_queued` is False and `lock.locked()` is False → a same-project message slips the guard.
- **Fix round 2** (`c2a0e19`), red-green + independent reviewer AGREE (hammered the wedge risk):
  - Ask path → `_notify_background_ask` early-returns the **entire** ping on a same-id throttle (mirrors permission/plan); distinct-id still sends.
  - Race → per-`_ProjectRuntime.inflight` marker, set right after the busy-guard passes (no await before set) and cleared **only** in an outer end-of-turn `finally` wrapping `_acquire_slot` + slot-release. Busy-guard now `inflight or lock.locked() or _is_queued`. `is_busy()` left lock-based so queued≠busy + `/reset`-drains-queued preserved.
  - Reviewer traced + probed every exit path (normal, mid-stream raise, drain-cancel CancelledError, post-wait StreamingBusy, SB2 refusal, resume-failure) — `inflight` clears on all; could not construct a wedge.
  - 737 → 743 tests.
- **Durable wedge guard** (`6c5fba5`, tests-only): committed 3 regressions pinning `inflight` clears on mid-stream-raise / post-wait-busy / SB2-refusal (a stuck marker permanently wedges a project — manual probes evaporate, so these are committed). Teeth-confirmed (neuter the clear → all 3 red). 743 → **746 tests**.

## Codex round 3 (cross-model, fresh pass) — NO_SHIP → 3 blockers (1 root cause + send-gate)
- Confirmed the round-1/2 fixes closed ("same-id AskEvents early-return the whole ping; `inflight` set with no await after the guard, cleared by an outer finally"), but found the slot-transfer window has a **deeper facet** + a send-gate hole:
- **B-r3-1** `stream_session.py`: round-2's `inflight` marker made the transfer-window turn visible to the **busy-guard**, but `/cancel`·`/reset`·`/rm` still only drained futures **in** `run_queue` or cancelled a **live** engine — a control command in the pop→lock window missed the turn → **zombie-runs after cancel/reset/rm**.
- **B-r3-2** `bot.py`: `/rm` (lock-based `is_busy`) could delete a project whose transferred waiter was about to start (persist-to-removed-record race).
- **B-r3-3** `render.py`: `ChatSendGate` let a verbatim send reserve the same future timestamp as already-reserved status → two sends per interval (combined budget violated under status churn).
- **Fix round 3** (`079d7c3`), red-green per blocker + independent reviewer AGREE (hammered the abort wedge + the send-gate reframe):
  - B-r3-1/2 (shared root cause) → per-`_ProjectRuntime` **`abort` `asyncio.Event`**: cleared in `handle_message` alongside `inflight=True` (no await between), checked after `_acquire_slot` before the lock AND inside the lock before `_ensure_engine`; `/cancel`/`/reset`/`/rm` set abort + drain + cancel-engine (covers queued → window → running); `cmd_rm` → inflight-aware `request_remove` (refuses live-engine run, drains window/queued before `store.remove`). Net: a control command on an in-flight project → turn never starts, `_running`→0, slot not leaked, no session persisted, abort always cleared before a fresh turn (wedge structurally impossible — reviewer mutation-confirmed).
  - B-r3-3 → single combined `_tail`; **no two sends share an interval**; verbatim keeps priority (jumps ahead of future status) but takes the next free slot vs an already-committed one. Reviewer independently validated (exhaustive grid scan + a 200s discrete-event sim) that verbatim is **not starved** — latency converges to ~`(cap+1)×interval`, far inside the 60-min answer-hold backstop → the relay deadlock does NOT reappear. Two existing gate tests reframed to the correct collision-free semantics (the old "verbatim ~1 interval behind a fully-committed deep backlog" property is provably incompatible with collision-freedom + operationally irrelevant — realistic backlog ≤ `MAX_CONCURRENT_RUNS`).
  - 746 → **752 tests**.

## Codex round 4 (cross-model, fresh pass) — **SHIP**
- **Blockers: none.** Confirmed the round-3 fixes genuinely closed: _"abort is set before control mutations, checked after slot transfer and inside the project lock; `/rm` is pre-abort/drain/remove ordered; `ChatSendGate` now enforces a combined no-collision tail."_ Ran the targeted prior-blocker tests + the full concurrency matrix — all pass. Remaining risk = live-SDK behavior (T11), called out honestly in the handoff.
- **One non-blocking** (then fixed, `fa2781e`): `/cancel <name>` in the transfer window aborted the turn correctly but `_cancel_project` returned `0` (the window turn is in neither the drained-queue nor live-engine bucket) → operator saw "Nothing in flight" for a turn it DID cancel. Fixed: count a slot-transfer-window abort (`rt.inflight and drained == 0 and rt.engine is None`) as one cancelled unit — mutually exclusive with the running/queued buckets (mutation-probed, no double-count). Safety logic untouched. 752 → **754 tests**.

## Verdict summary
- **Verifier subagent: SHIP. Codex (cross-model): SHIP at round 4**, after **6 real concurrency/lifecycle bugs across 3 NO_SHIP rounds that the same-model passes missed** (distinct-id prompt suppression, self-queue, ask same-id coalesce, the slot-transfer TOCTOU + its control-command facet + the `/rm` persist-race, and the send-gate combined-budget collision). All fixed red-green, each independently reviewed AGREE; the catastrophic paths (project wedge, zombie run) carry committed regressions. **The cross-model Codex was again decisive** (`cross-model-qa-catches-concurrency`).

## Final gates (worktree `.venv`)
- `pytest -q` → **754 passed** · `ruff check .` clean · `mypy claude_tg` clean · `secret_scan.py` clean (191 files).

## Decision
- **QA COMPLETE — both reviewers SHIP.** Per the standing autonomous directive ("Codex QA iterated to SHIP") the 3 NO_SHIP rounds were fixed + re-QA'd rather than paused. The one remaining ship gate is **T11 — the live two-project-concurrent phone-verify** (the ADR-001 live-SDK-tolerates-concurrent-open-holds assumption is unprovable in CI; mock-engine tests bypass the real PTB callback path). On T11 PASS → merge P5 to `main`.
- **Deferred to P6 hardening (non-blocking, recorded):** `notify_last` prune-on-resolve (bounded + transient today); the `_is_resume_failure` text-heuristic live confirmation (carried from P4); the `ChatSendGate` verbatim-base priority logic is redundant given the collision guard (reviewer-proven inert in all reachable states) — simplify + tighten the two reframed "jumps-ahead" test docstrings; the in-lock abort check guards a sub-window unreachable under the one-in-flight invariant (defense-in-depth, not separately teethed)._
- **Deferred to P6 hardening (non-blocking, recorded):** `notify_last` prune-on-resolve (bounded + transient today); the `_is_resume_failure` text-heuristic live confirmation (carried from P4); the `ChatSendGate` verbatim-base priority logic is redundant given the collision guard (reviewer-proven inert in all reachable states) — simplify + tighten the two reframed "jumps-ahead" test docstrings; the in-lock abort check guards a sub-window unreachable under the one-in-flight invariant (defense-in-depth, not separately teethed)._
