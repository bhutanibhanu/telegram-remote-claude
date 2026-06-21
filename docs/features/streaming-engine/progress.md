# Progress: streaming-engine (P1)

_Plan generated 2026-06-21 from design.md · 9 tasks · autonomous supervised build_

> **P1 — interactive streaming session engine.** Builds on ADR-001 (Substrate A =
> `claude-agent-sdk==0.2.105`). Ships behind `ENGINE_MODE` (`oneshot` default → `streaming`); the
> live one-shot bot is never broken. Per-tool permission gating is **P2** (not here); multi-project
> P4; concurrency P5; crash-recovery P4; Substrate-B adapter = documented slot only.
>
> **GATE:** **T1 (de-risk spike) gates the engine.** If T1 is FAIL/PARTIAL, STOP — escalate to the
> owner and redesign the async answer flow before any engine code (T4+). An honest PASS/PARTIAL/FAIL
> is the correct outcome of an empirical probe; never grind it to PASS.

## Cross-cutting acceptance (applies where relevant)
- **SB1** authn on every inbound **incl. button-callback taps** (allowlist) — primary T7.
- **SB2** `/cd` path confinement (canonicalize + `ALLOWED_ROOTS` containment) — primary T8.
- **SB3** secret hygiene (token never logged, sensitive output kept out of logs, `0600` state) — T3/T6/T8.
- **SB4** no injection (never build shell/args from message text) — T7/T8.
- **SB6** safe defaults / fail-closed; **no new bypass** introduced in P1 (SB5 bypass-removal is P2) — T7.
- **RB1** never crash on bad input · **RB2** clean failure (the `error` event; never a silent hang) ·
  **RB5** rate-limit safety (throttle/coalesce under burst) · **RB7** each of RB1/RB2/RB5 has a test — T8.

## Task list
- [x] T1 — Async-latency de-risk spike (answer-hold) ⭐ **GATE** · live probe (18476f7 — PASS)
- [x] T2 — ADR-002: async answer-hold mechanism · doc (36652c9)
- [x] T3 — CI + project test harness baseline · config (a4560f3)
- [x] T4 — Engine core: normalized types + substrate seam (A adapter) + lifecycle + events-out · unit (mock) (cbfef05)
- [x] T5 — Decisions-in + async answer-hold + 60-min backstop + cancel · unit (mock) (f050f57)
- [x] T6 — Render layer: events→Telegram, inline keyboards, coalesce/throttle (RB5) · unit (6b9cf11)
- [x] T7 — Wire bot.py: ENGINE_MODE switch + SB1 callback handler + /cancel + routing · unit (3b90693)
- [ ] T8 — /cd path policy (SB2) + SB/RB test suite (RB7) · unit
- [ ] T9 — Live end-to-end verify (programmatic, real Claude) + owner phone-verify checklist · live probe

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked

## Tasks

### T1 — Async-latency de-risk spike (answer-hold) ⭐ GATE
- **Goal:** Empirically prove the SDK `can_use_tool` callback (and the interactive-tool answer round-trip) tolerates a multi-minute async human delay and a configurable backstop **without timing out or wedging the session** — before any engine code is written against that assumption.
- **Depends on:** none
- **Files (expected):** `spikes/p1-async-latency/` (isolated, git-ignored venv + `requirements.lock`; throwaway harness + scrubbed evidence). Reuses the P0 spike's scrubber/recorder pattern. No production files.
- **Acceptance:**
  - WHEN a real `can_use_tool` callback is held open for **~2 minutes** before returning a decision, the session SHALL accept the decision and **continue** (allow → tool runs; deny → tool blocked) — verdict + scrubbed transcript. **(live probe)**
  - WHEN an interactive tool (AskUserQuestion / ExitPlanMode) answer is deferred ~2 minutes then injected (native `answers` map / allow|deny+feedback), the session SHALL continue on that answer.
  - WHEN a pending request exceeds a **configurable backstop** (tested at a SHORT interval, e.g. 60–120 s — NOT a literal 60-min wait), the harness SHALL auto-resolve (deny) and the session SHALL remain usable (RB4-shape).
  - The check SHALL emit a clear **PASS / PARTIAL / FAIL** with evidence; **a FAIL/PARTIAL GATES the engine** (stop + escalate to owner; do not build T4+ until resolved or the answer flow is redesigned). No API key (host CLI auth). Effects contained; evidence scrubbed + secret-scanned (SB3).
- **Tests:** none — the verdict + scrubbed transcript is the artifact (mirrors P0 evidence policy).
- **Status:** done (18476f7) — **PASS — GATE GREEN, engine (T4+) may proceed.** Live run (15.2 min, host CLI
  auth, no API key): **120 s** holds inside `can_use_tool` were honored on ALLOW (tool executed), DENY (tool
  blocked), and the **native AskUserQuestion `answers`-map** (session continued on the code-chosen label
  `Bravo`); a **300 s (5-min) ceiling probe** showed **no ceiling**; a **harness-side backstop** auto-resolved a
  pending decision (DENY + notify) at 60 s and the **same session stayed usable** (2nd turn OK). Root cause
  confirmed at SDK source (`claude_agent_sdk/_internal/query.py`): the inbound `can_use_tool` control request is
  `await`ed with **no `fail_after`/timeout** (the 60 s timeout is OUTBOUND only), so the SDK does not bound the
  hold — the **60-min backstop is implementable engine-side as a timer over a pending-decision Future** (the T1.5
  pattern). Containment: no leaked CLI pids, repo untouched during the run, deny-by-default path policy; evidence
  scrubbed (the unscrubbed stdout redirect log was dropped before commit). **Two fresh independent reviewers
  (acceptance + adversarial) both AGREE PASS**, each confirming the SDK no-timeout fact at source and reproducing
  the wiring via a fast smoke. **CAVEAT for ADR-002 (T2):** the 60-min figure is an **extrapolation** from a
  ≤5-min empirical hold + the SDK no-timeout fact — any **CLI/model-side ceiling in the 5–60 min band**
  (request-idle / streaming-inactivity / model-turn wall-clock) is **UNTESTED**. ADR-002 must (a) disclose this,
  (b) make the backstop a **harness-side timer over a Future** (never depend on the SDK holding one control
  request 60 min), (c) recommend keep-alive or a backstop set below any later-observed ceiling, and consider a
  one-off ~10–15 min ceiling probe before relying on holds > 5 min. Minor (deferred, verdict-neutral): T1.2's
  `session_continued` predicate is lenient (`is_error is not None`) though the run had `is_error=False`; T1.3's
  echo check is backed by the native tool-result so not guess-ambiguous.

### T2 — ADR-002: async answer-hold mechanism
- **Goal:** Record, from T1 evidence, how a pending interactive/permission request is held, resolved, timed out (60-min backstop), and cancelled — the contract T4–T7 implement.
- **Depends on:** T1
- **Files (expected):** `docs/adr/ADR-002-async-answer-hold.md`.
- **Acceptance:**
  - ADR-002 SHALL document the hold/resolve/timeout(backstop)/cancel mechanism grounded in T1's recorded evidence (no claim beyond what T1 observed); status **Proposed**; consistent with ADR-001.
  - IF T1 was PARTIAL/FAIL, ADR-002 SHALL state the constrained shape / required redesign instead.
- **Tests:** none — documentation deliverable.
- **Status:** done (36652c9) — `docs/adr/ADR-002-async-answer-hold.md` written, status **Proposed**, grounded
  in T1's PASS evidence (120s/300s holds honored; SDK has no `fail_after` on the inbound `can_use_tool`).
  **Decision:** answer-hold = an engine-side `PendingDecision` Future per request (keyed by `tool_use_id` +
  session id), awaited inside `can_use_tool`, resolved by (a) the operator's SB1-checked Telegram decision,
  (b) a **harness-side backstop timer** (auto-resolve → DENY+notify; default 60 min, configurable), or
  (c) `/cancel` — the engine never relies on the SDK to bound the hold. Carries the reviewer caveat: the
  **5–60 min CLI-side ceiling is UNTESTED** (extrapolated from ≤5 min + the no-timeout fact) → backstop below
  any observed ceiling, keep-alive if needed, optional ~10–15 min ceiling probe before long holds. Claims
  audited against T1 evidence; consistent with ADR-001 + the normalized-interface decisions-in contract.

### T3 — CI + project test harness baseline
- **Goal:** Stand up GitHub Actions CI (decision-log #6) gating merges, and ensure the existing behavioral test snapshot runs as regression coverage; substrate mocked, no live Claude/network in CI.
- **Depends on:** none (sequenced after T2)
- **Files (expected):** `.github/workflows/ci.yml`; lint/type-check config (e.g. `ruff`/`mypy` config) if absent; test config. No change to production runtime code.
- **Acceptance:**
  - WHEN a push/PR occurs, CI SHALL run **tests + lint + type-check + secret-scan** and report status (branch protection requires green — documented). **(SB3 secret-scan)**
  - The existing test suite SHALL pass under CI; **no test invokes live Claude or the network** (substrate is mocked).
  - WHEN CI runs, it SHALL NOT require an API key or host CLI auth.
- **Tests:** the existing suite runs green locally + the workflow is valid (lint the YAML / dry-run the job steps locally where possible).
- **Status:** done (a4560f3) — GitHub Actions CI stood up (`.github/workflows/ci.yml`): on push + PR, matrix
  Python 3.12/3.13, runs **pytest + ruff + mypy + secret-scan**; documents that branch protection must require
  the check green (decision-log #6). `pyproject.toml` adds a **lenient-but-green** ruff (E/F/I/B; E501 off) +
  mypy (`ignore_missing_imports`, non-strict, scoped to `claude_tg`+`main.py`, one `bot.py` `union-attr`
  override) baseline with explicit "tighten later" notes. `scripts/secret_scan.py` (SB3) is a dependency-light
  offline scan of **git-tracked** files reusing the P0 scrubber patterns (anthropic/openai/telegram/aws/bearer/
  private-key + credential assignments), with placeholder allowlist. **Verified locally by me:** pytest **53
  passed**, `ruff check` clean, `mypy` clean (7 files), secret-scan clean (102 files, exit 0) AND it correctly
  FAILS on injected real-looking secrets; ci.yml parses. **No test touches the network/live-Claude/API key**
  (substrate mocked via `_invoke` monkeypatch; grep confirms no subprocess/socket/http in `tests/`). Scope:
  **requirements.txt + production runtime `.py` + spikes/ untouched**; only `.github/`, `pyproject.toml`,
  `scripts/`, and dev-deps (`ruff`,`mypy`) in requirements-dev.txt changed. (Comprehensive review deferred to the
  end-phase Verify+QA per the pipeline.)

### T4 — Engine core: normalized types + substrate seam (A adapter) + lifecycle + events-out
- **Goal:** Implement the normalized engine contract for real: event/decision types (carrying a session id), a `Substrate` adapter protocol (A adapter built on `claude-agent-sdk`; B = documented slot), the `start/resume/send/stop` lifecycle, and events-out normalization — selectable via `ENGINE_MODE` with `oneshot` remaining the default.
- **Depends on:** T2, T3
- **Files (expected):** `claude_tg/engine/` (`types.py`, `substrate.py` protocol + `adapter_sdk.py`, `engine.py`), `claude_tg/config.py` (`ENGINE_MODE`). Harvest `(session_id, cwd)` persistence + per-chat lock from existing modules; do not modify `claude_runner.py`'s one-shot path.
- **Acceptance:**
  - WHEN the engine runs `start → send → stop` over a **MOCK substrate adapter**, it SHALL emit normalized events (`text`, `tool_use`, `ask`, `plan`, `error`, `result`, `status`) each carrying a `session_id`; lifecycle terminates cleanly (no leak). **(unit, mock substrate — no live Claude)**
  - WHEN `resume(session_id)` is called over the mock, it SHALL re-attach and continue (the `(session_id, cwd)` coupling honored).
  - WHEN `ENGINE_MODE` is unset/`oneshot`, the existing one-shot path SHALL be used unchanged (default preserved); `streaming` selects the new engine.
  - The `Substrate` seam SHALL have a built A adapter and a documented (not-implemented) B adapter slot (anti-goal: no B build).
  - WHEN the substrate errors/times out, the engine SHALL emit a clean `error` event (RB2), never hang.
- **Tests:** unit — event normalization mapping, lifecycle, resume, ENGINE_MODE selection, RB2 clean-failure; all against a mock substrate (no network).
- **Status:** done (cbfef05) — `claude_tg/engine/` package: **types.py** (7 events-out + 5 decisions-in
  dataclasses, each event carrying `session_id`; the single load-bearing `decision_to_substrate` mapper
  honoring every contract [FLAG] — native `answers`-map keyed by question text, plan-reject rides the deny
  message, allow always returns a dict for the B `updatedInput` gotcha); **substrate.py** (`Substrate`
  Protocol + documented NotImplemented **B-adapter slot**); **adapter_sdk.py** (Substrate-A over
  claude-agent-sdk, **lazy SDK imports** so the package/mock-tests need no SDK; pure `normalize()` mapping
  SDK messages→events incl. AskUserQuestion→Ask / ExitPlanMode→Plan / ToolResult(is_error)→Error /
  RateLimitEvent→Status; bounded send → `driver_error` ErrorEvent, **never hangs (RB2)**; SB3-safe input
  summary); **engine.py** (lifecycle passthrough + events-out stream + the decision seam; the answer-hold
  itself is T5). `config.py` adds **ENGINE_MODE** (default `oneshot`); `requirements.txt` pins
  **claude-agent-sdk==0.2.105** (the de-risked version; ADR-001 pin+monitor). **Verified by me on 0.2.105:**
  pytest **99 passed** (53 existing + 46 new), ruff clean, mypy clean (12 files), secret-scan clean.
  **One fresh independent reviewer** (correctness + contract-conformance + behavioral-tests + RB2 + lazy-import
  + scope) **AGREES done, no required fixes** (introspected real SDK signatures; probed the wired seam).
  Scope: **bot.py / claude_runner.py / main.py / spikes/ untouched** — one-shot path intact; the bot
  ENGINE_MODE switch + decision-seam wiring are T7. (Comprehensive cross-check at the end-phase Verify+QA.)

### T5 — Decisions-in + async answer-hold + 60-min backstop + cancel
- **Goal:** Implement decisions-in (question answer via native `answers` map; plan verdict allow / deny+feedback; free-text reply; cancel) and the async answer-hold (hold a pending `ask`/`plan` request open, resolve it back into the live session, 60-min backstop auto-resolve+notify, `/cancel` clean abort) — per ADR-002.
- **Depends on:** T4
- **Files (expected):** `claude_tg/engine/engine.py` (+ a `pending.py` / answer-hold helper), `claude_tg/config.py` (backstop interval).
- **Acceptance:**
  - WHEN an `ask` event is pending and a question answer is injected, the engine SHALL resolve it via the native `answers`-map allow path and the session SHALL continue on that answer. **(unit, mock substrate)**
  - WHEN a `plan` event is pending and a plan verdict is injected, approve → allow (proceed); reject+feedback → deny(message) (revise) — honored.
  - WHEN a pending request exceeds the configured backstop, the engine SHALL auto-resolve (deny) + emit a notify `status`/`error` event and leave the session usable (RB4-shape).
  - WHEN `/cancel` is issued on a waiting run, the engine SHALL abort the pending request cleanly without wedging state (RB4).
  - A pending request SHALL be keyed so the answer routes to the correct request (`tool_use_id` / request id) — forward-compatible toward P4/P5 correlation.
- **Tests:** unit — answer routing, plan verdict honoring, backstop auto-resolve, cancel; mock substrate; deterministic timers (no real 60-min wait).
- **Status:** done (f050f57) — `claude_tg/engine/pending.py` (`PendingRegistry`): a per-request
  `asyncio.Future[Decision]` keyed by `tool_use_id`, awaited inside the decision callback, resolved by
  exactly one of (a) operator `resolve(tool_use_id, decision)`, (b) a per-request **backstop timer**
  (auto-resolve → DENY + notify; default 60 min via `ANSWER_BACKSTOP_SECONDS`, injectable for tests), or
  (c) `cancel(tool_use_id|None)` (clean deny abort). `engine.py`: `on_tool_request` routes ask/plan →
  `_answer_hold` (injects the `AskEvent`/`PlanEvent` with `tool_use_id` onto the turn's `asyncio.Queue`
  merge stream so the operator sees it, then awaits the pending), and **ordinary tools → auto-allow**
  (P1 interim per S3 — NO per-tool gating, NO bypass flag; P2 replaces this branch). `send()` merges the
  bounded substrate stream + injected events via a producer/consumer queue (no deadlock; producer cancelled
  in `finally`). Decisions mapped through the single `decision_to_substrate`. **Verified by me on 0.2.105:**
  pytest **123 passed** (+24), ruff clean, mypy clean (13 files), secret-scan clean; tests deterministic
  (0.05 s backstop, no real waits) and the end-to-end `HoldingSubstrate` would HANG if `resolve`/backstop
  failed to unblock (can't false-pass). **One fresh independent reviewer (concurrency-focused: 12 adversarial
  no-hang/no-leak/no-race probes + RB4 + contract) AGREES done, no required fixes.** RB4 honored
  (cancel + backstop don't wedge; session usable after); RB1 (stray/late `resolve` no-crash). Scope:
  adapter/substrate/types/bot/runner/main/spikes untouched; T4 seam signature unchanged.

### T6 — Render layer: events→Telegram, inline keyboards, coalesce/throttle (RB5)
- **Goal:** Map normalized events to Telegram output — verbatim for meaningful output (questions/plans/errors/results), one-liner/status otherwise; inline keyboards for `ask` (one button per option + "Other") and `plan` (`[Approve]`/`[Reject + feedback]`); coalesce + throttle updates (edit-in-place status message; respect ~1 msg/s/chat).
- **Depends on:** T4
- **Files (expected):** `claude_tg/render.py` (new/extracted).
- **Acceptance:**
  - WHEN an `ask` event renders, the system SHALL produce a message with one inline button per option plus an "Other" (free-text) affordance. **(unit)**
  - WHEN a `plan` event renders, the system SHALL produce the plan text (chunked ≤ Telegram limit) + `[Approve]` / `[Reject + feedback]` buttons.
  - WHEN a burst of `text`/`status` deltas arrives, the renderer SHALL coalesce/throttle (edit a status message rather than flood) and SHALL NOT exceed Telegram send limits (RB5).
  - WHEN rendering, sensitive content SHALL NOT be logged (SB3); long output SHALL chunk (preserve existing chunking behavior).
- **Tests:** unit — event→message mapping (verbatim vs one-liner), keyboard construction, coalesce/throttle under simulated burst (RB5), chunking.
- **Status:** done (6b9cf11) — `claude_tg/render.py` (pure logic; T7 executes the Telegram calls). `RenderAction`
  (op `new`/`edit_status`/`none`, pre-split chunks via `util.split_message`, optional `reply_markup`):
  **verbatim** (own message) for `ask`/`plan`/`error`/`result`/assembled-`text`; **one-liner edit-in-place**
  for `tool_use`/`status`/incremental-`text`. **Inline keyboards:** `ask` → one button per option + per-question
  "Other (free text)"; `plan` → [Approve]/[Reject+feedback]. **`callback_data` codec** `kind|tool_use_id|payload`
  (payload = option **index**, never the label; plan = approve/reject) — **≤64 bytes asserted at build**,
  round-trippable, and **`decode_callback` is defensive** (non-str/empty/>64B/wrong-arity/unknown-kind/foreign →
  `None`, never raises) which feeds **SB1** at T7; `answers_from_ask(ask, q_idx, o_idx)` reconstructs the native
  question-text→label answers-map. **Coalescer (RB5)** with an **injected clock** (leading + trailing edge,
  newest-wins): a 50-delta burst → **1 edit**, verbatim events force-flush in order; tests drive a `FakeClock`
  (no real sleeps). SB3: `tool_use` renders the event's `tool_input_summary` (lengths-not-bodies); render.py
  logs nothing. **Verified by me on 0.2.105:** pytest **182 passed** (+59), ruff clean, mypy clean (14 files),
  secret-scan clean; no test touches Telegram/network. Scope: engine/bot/util/config/main/spikes untouched.
  **Deferred to T7:** the actual send/edit/answer_callback calls + real rate-limit waiting, SB1 allowlist on
  decoded callbacks, routing decoded taps to the engine decision seam, "Other"/reject free-text prompting.
  (Comprehensive cross-check at the end-phase Verify+QA.)

### T7 — Wire bot.py: ENGINE_MODE switch + SB1 callback handler + /cancel + routing
- **Goal:** Integrate the engine into `bot.py`: select engine via `ENGINE_MODE`; add an inline-keyboard **callback handler that is allowlist-checked (SB1)**; route a button tap / "Other" reply / plan verdict back into the engine's pending request; add `/cancel`. One-shot path stays default.
- **Depends on:** T4, T5, T6
- **Files (expected):** `claude_tg/bot.py`.
- **Acceptance:**
  - WHEN a callback-query (button tap) arrives from a **non-allowlisted** chat, the system SHALL ignore it — it SHALL NOT answer a question or approve a plan (SB1). WHEN from the allowlisted operator, it SHALL route to the correct pending request. **(unit)**
  - WHEN `ENGINE_MODE=streaming`, inbound messages/taps SHALL drive the streaming engine; WHEN `oneshot`/unset, behavior SHALL match today's one-shot bot (default preserved).
  - WHEN `/cancel` is sent, the waiting run SHALL abort cleanly (RB4).
  - No message text SHALL be interpolated into shell commands/args (SB4); no new bypass introduced (SB6).
- **Tests:** unit — callback allowlist enforcement (SB1), routing to pending request, ENGINE_MODE switch keeps one-shot default, /cancel; mock engine.
- **Status:** done (3b90693) — `bot.py` + `main.py` wire the streaming engine behind the
  **ENGINE_MODE switch** (oneshot DEFAULT unchanged — the 53 original tests pass byte-identical; streaming
  delegates to a new `claude_tg/stream_session.py` `StreamingSession`). **Streaming driver:** per-chat `Engine`
  (lazy start / resume from persisted `(session_id, cwd)` with fresh-start fallback), a per-chat `asyncio.Lock`
  guarding the TURN (not the resolve), a `_drive_turn` send/edit loop honoring the `Coalescer` (incremental/status
  → edit-in-place; verbatim ask/plan/error/result → own messages + keyboards), session_id persisted from the
  result event. **SB1 (the security boundary):** PTB 21.x `CallbackQueryHandler` can't be chat-filtered, so the
  authoritative gate is the explicit `_authorized(update)` recheck inside `on_callback` (reads the
  Telegram-delivered `effective_chat.id`, not anything in `callback_data`) — an unauthorized/no-streaming tap is
  answered + dropped BEFORE any engine touch (never resolves a decision); `allowed_updates` enables callback_query
  only in streaming mode. `resolve_callback` ignores `decode_callback`→None (foreign/stale/malformed) and any
  `tool_use_id`/index that doesn't match the held request (no resolve, RB1 try/except, query always answered).
  **Free-text state machine:** ask "Other" / plan "Reject+feedback" arm a per-chat marker → the NEXT message is
  captured as the answer/feedback (cleared first so a failure can't wedge). **/cancel** → `engine.cancel()`
  (RB4). SB4 (prompt verbatim, no shell interpolation) + SB6 (no bypass; `permission_mode=default`). **Verified
  on 0.2.105:** pytest **212 passed** (53 originals preserved + T4-T6 + 30 new T7), ruff/mypy/secret-scan clean.
  **Reviews:** correctness reviewer AGREES done (one-shot byte-identical, no lock/resolve deadlock, free-text
  ordering bug fixed + mutation-probed, tests proven non-false-passing). The dedicated **security-reviewer
  subagent was repeatedly blocked by a content/auto-mode filter false-positive**, so **SB1 was verified directly
  by the orchestrator** (read `on_callback`+`resolve_callback`: auth-before-engine-touch, decode→None ignored,
  id-match required, RB1) + the SB1 unit tests (unauthorized chat → resolve never called; malformed → ignored).
  **REMAINING FOLLOW-UP:** when the filter allows, run a fresh independent security review of the SB1 callback
  path for completeness (the implementation is verified; this is belt-and-suspenders). Scope:
  claude_runner/engine/render/config/session_store/spikes untouched; only bot.py + main.py modified + the driver
  + 2 test files new.

### T8 — /cd path policy (SB2) + SB/RB test suite (RB7)
- **Goal:** Harden `/cd` with canonicalization + `ALLOWED_ROOTS` containment (SB2), and add the dedicated SB/RB tests the cross-cutting baseline requires (RB7).
- **Depends on:** T7
- **Files (expected):** `claude_tg/bot.py` / a `paths.py` helper; `tests/` additions.
- **Acceptance:**
  - WHEN an operator-supplied path is given to `/cd`, the system SHALL canonicalize it (symlinks resolved) and reject it unless it is contained within an `ALLOWED_ROOTS` entry; `..`/symlink-escape/out-of-root SHALL be rejected; `ALLOW_ANY_PATH=true` is the explicit opt-out (SB2). **(unit)**
  - Dedicated tests SHALL exist for: SB1 (non-allowlisted message + callback ignored), SB2 (traversal/symlink-escape rejected), SB3 (no secrets in logs), SB4 (no shell injection from message text), RB1 (never crash on malformed input), RB2 (clean failure on engine/timeout error), RB5 (throttle/coalesce holds under burst). **(RB7: each has a test)**
  - The existing user-facing guarantees (allowlist ignore, chunking, command behavior) SHALL retain equivalent-or-stronger coverage.
- **Tests:** unit — the SB/RB matrix above; substrate/engine mocked.
- **Status:** todo

### T9 — Live end-to-end verify (programmatic, real Claude) + owner phone-verify checklist
- **Goal:** Prove the full streaming path works **live** end-to-end against real Claude with code-injected operator decisions (no Telegram tap needed), and hand the owner a concrete phone-verification checklist for final acceptance under `ENGINE_MODE=streaming`.
- **Depends on:** T7, T8
- **Files (expected):** `spikes/p1-async-latency/` or a `scripts/` live-verify harness (throwaway, contained); `docs/features/streaming-engine/verify.md` (owner checklist).
- **Acceptance:**
  - WHEN the engine runs a real session end-to-end (start → emit `ask`/`plan` from a real flow → inject answer/verdict from code → continue → result), it SHALL complete with retained context and clean stop — verdict + scrubbed transcript. **(live probe; not in CI)**
  - The owner checklist SHALL give exact steps to run `/grill` (or an AskUserQuestion-emitting flow) from a real chat under `ENGINE_MODE=streaming` and confirm: option buttons + answer + continuation, plan approve/reject+feedback, live coalesced rendering, `/cancel`.
  - No API key; effects contained; evidence scrubbed (SB3).
- **Tests:** none — live verdict + transcript + owner checklist are the artifacts.
- **Status:** todo

## Rules
- **Flag-gated, branch-only.** All work on `feat/streaming-engine`; **never merge to main**; the live
  bot keeps running one-shot (`ENGINE_MODE=oneshot` default) — it is the owner's only remote access.
- **Anti-goals:** no per-tool permission gating/bypass-removal (P2); no multi-project (P4)/concurrency
  (P5)/crash-recovery (P4); no Substrate-B adapter build (slot only); no live Claude/network in CI.
- **T1 gates T4+.** A FAIL/PARTIAL de-risk spike stops the build and escalates to the owner.
- **Substrate mocked in unit tests;** live probes (T1, T9) are isolated, contained, scrubbed, no API key.
- **`progress.md` is the single source of task truth;** `state.json` tracks the phase.
