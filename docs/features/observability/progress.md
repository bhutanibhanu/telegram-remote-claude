# Progress: observability

_Plan generated 2026-06-30 from design.md · 6 tasks · supervised build._
_Two capabilities: live activity line (agents) + 🪙 limit field & warning (tokens). Both telemetry
spikes (T1, T2) are front-loaded so each data source is proven before any UI commits to it._

## Task list
- [x] T1 — Adapter limit telemetry + spike (precise % available via RateLimitInfo.utilization)
- [x] T2 — Adapter activity telemetry + spike (Task* emitted; task_type first-class; SB3 names-only; lifecycle reconcile)
- [x] T3 — Statusline 🪙 limit field (🪙 <pct>% / 🟢🟡🔴 badge / omit; byte-for-byte unchanged when None)
- [ ] T4 — Proactive limit warning
- [ ] T5 — Live activity line (ActivityMixin)
- [ ] T6 — ADR-010 + docs + phone-verify

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked

## Global acceptance (EVERY task)
- All 4 gates green from the worktree `.venv`: `pytest` (floor **1670**, only grows), `ruff check .`,
  `mypy claude_tg`, `python scripts/secret_scan.py`.
- `main` stays runnable; the public import path `claude_tg.stream_session` unchanged.
- Invariants hold: **SB1** (authn on commands/sends), **SB3** (body-free — names/numbers only, never
  tool args/bodies/output), **RB1** (every new render/observer is best-effort OFF the turn's critical
  path — any failure degrades silently, never breaks a turn), **foreground-only** rendering.
- Clean single-line commit, **NO Co-Authored-By**.

## Tasks

### T1 — Adapter limit telemetry + spike
- **Goal:** capture the SDK's rolling-limit signal and expose it for the statusline + warning.
- **Depends on:** none
- **Files (expected):** `claude_tg/engine/adapter_sdk.py`, `claude_tg/engine/engine.py`, `tests/test_engine.py`
- **Acceptance:**
  - WHEN a `RateLimitEvent`/`RateLimitInfo` is received, the substrate SHALL capture a normalized limit
    **status** (`ok` / `approaching` / `limited`) AND a precise **percent** IF the SDK exposes one
    (`RateLimitInfo` fields/`raw`), else `None` for the percent.
  - The task SHALL document (code comment + the commit msg) whether a precise % is available — the
    **spike answer**; if not, `limit_status()` returns status-only and the UI uses the badge.
  - WHEN no limit signal has been seen, `Engine.limit_status()` SHALL return `None` (never fabricated).
  - `Engine.limit_status()` SHALL be a pure in-memory read that NEVER raises (best-effort, mirroring
    `context_percentage()`/`last_model()`); reset on `stop()` (RB3).
  - Capture SHALL be body-free (SB3): only status / percent / reset-time, never request content.
- **Tests:** fake `RateLimitInfo` with a precise-% shape → `(status, pct)`; status-only shape →
  `(status, None)`; no-signal → `None`; odd/garbage shape → unchanged, no raise (RB1). Behavior, not internals.
- **Status:** todo

### T2 — Adapter activity telemetry + spike
- **Goal:** capture current-tool + active-subagent activity from the stream for the activity line.
- **Depends on:** none
- **Files (expected):** `claude_tg/engine/adapter_sdk.py`, `claude_tg/engine/engine.py`, `tests/test_engine.py`
- **Acceptance:**
  - WHEN a `Task*` message (`TaskStarted`/`TaskUpdated`/`TaskUsage`) is received, the substrate SHALL
    record the subagent **name** + **status**; the task SHALL document whether `Task*` is actually
    emitted for the bot's sessions — the **spike answer**.
  - IF `Task*` is NOT emitted, the substrate SHALL fall back to inferring subagent activity from
    `tool_use` blocks + `parent_tool_use_id` (a sub-agent tool_use carries a parent id).
  - WHEN a `tool_use` is received, the substrate SHALL record the current tool **NAME only** (SB3 —
    never the tool input/args).
  - `Engine.last_activity()` SHALL return a body-free snapshot (current tool name; active-subagent
    names/count) or `None` when idle; pure in-memory read, never raises (RB1); reset on `stop()`.
- **Tests:** fake `Task*` → snapshot with subagent names; fake `tool_use`(+parent) → current tool +
  inferred subagent; SB3 (snapshot carries NO tool args); idle → `None`; garbage → no raise (RB1).
- **Status:** todo

### T3 — Statusline 🪙 limit field
- **Goal:** render the limit %/badge on the pinned statusline.
- **Depends on:** T1
- **Files (expected):** `claude_tg/render.py`, `claude_tg/stream_session/statusline.py`, `tests/test_render.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN `limit_status()` yields a precise %, `format_statusline` SHALL render a `🪙 <pct>%` field;
    WHEN only a status is available, it SHALL render the `🟢/🟡/🔴` badge; WHEN `None`, it SHALL OMIT
    the field entirely (no fabricated value), mirroring the `ctx —` discipline.
  - The field SHALL sit consistently in the bar (after `🧠 ctx`) and be HTML-escaped once (SB3); the
    line stays valid `parse_mode="HTML"`.
  - The statusline builder SHALL read the limit from the FOREGROUND project's engine only, best-effort
    (RB1 — a failing/absent read omits the field, never breaks the line).
- **Tests:** render with %, with badge, with `None` (field absent); `_statusline_text` includes the
  field when the engine reports it and omits it otherwise; never raises. Existing statusline tests stay green.
- **Status:** todo

### T4 — Proactive limit warning
- **Goal:** one-time heads-up as the cap approaches.
- **Depends on:** T1
- **Files (expected):** `claude_tg/stream_session/core.py` (+ a small helper / state on the runtime), `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN the limit signal first crosses `approaching` (🟡 / ≥ threshold) during a foreground turn, the
    bot SHALL post EXACTLY ONE warning message suggesting wrap-up (+ the reset hint if known).
  - The warning SHALL be de-duped per limit-window: NOT repeated while still approaching; re-armed only
    after the status returns to `ok` (reset/recovery).
  - SB1 (only the authorized chat) + SB3 (body-free — status + reset hint, no request content).
  - Best-effort (RB1): a send failure is swallowed and never breaks the turn (observer off the critical path).
- **Tests:** warns once on crossing; does NOT warn again while approaching; re-arms after `ok`→approach;
  never warns when `ok`; send failure swallowed (RB1); authorized-chat-only (SB1).
- **Status:** todo

### T5 — Live activity line (ActivityMixin)
- **Goal:** the transient edit-in-place activity message.
- **Depends on:** T2
- **Files (expected):** `claude_tg/stream_session/activity.py` (NEW), `claude_tg/stream_session/core.py` (+ `__init__.py` re-export), `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN a foreground turn starts, the bot SHALL post a SINGLE activity message; WHEN activity changes
    (tool/subagent), it SHALL EDIT that same message in place (NEVER a new message per change).
  - Edits SHALL be throttled (≲1 edit/sec OR only on tool/subagent change), coalescing rapid changes,
    within Telegram edit limits + the existing send-gate.
  - The line SHALL show tool/subagent NAMES only (SB3 — never args/bodies).
  - WHEN the turn ends, the line SHALL collapse to a one-line summary OR be removed (no lingering ⚙️).
  - Foreground-only (a background project's turn never writes it); best-effort (RB1 — any send/edit
    failure swallowed). `StreamingSession` composes `ActivityMixin`; `__init__` re-exports unchanged.
- **Tests:** posts one message on first activity; edits in place on change (asserts EDIT not new send);
  throttles (rapid changes → coalesced); SB3 (text carries no args); collapses/removes at end;
  foreground-only; RB1 (send error swallowed).
- **Status:** todo

### T6 — ADR-010 + docs + phone-verify
- **Goal:** record decisions, document, and run Verify+QA + live phone-verify.
- **Depends on:** T1–T5
- **Files (expected):** `docs/adr/ADR-010-observability.md`, `README.md`, `docs/features/observability/{handoff,qa}.md`
- **Acceptance:**
  - ADR-010 SHALL record: the activity line + the limit signal; SB3 names-only; the throttle/edit-in-place
    discipline; the RB1 observer-off-critical-path stance; the precise-%-or-badge fallback; the
    one-time-per-window warning de-dup; AND the two spike findings (Task* availability, precise-% availability).
  - README SHALL accurately document the 🪙 field, the activity line, and the warning.
  - The pipeline Verify+QA (Verifier subagent + cross-model Codex) SHALL run; blockers fixed; Codex → SHIP.
  - Live phone-verify SHALL confirm the activity line edits in place + the 🪙 field renders (the warning
    path exercised or its non-exercise documented, e.g. limit not currently near).
- **Tests:** none new (docs + verify); the full suite stays green.
- **Status:** todo
