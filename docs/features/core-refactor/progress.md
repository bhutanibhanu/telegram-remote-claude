# Progress: core-refactor

_From design.md · behavior-PRESERVING refactor · supervised build. Baseline **1663 tests green**, ruff/mypy/secret_scan clean. Approach: split the `StreamingSession` god-class into a `stream_session/` mixin package (pure relocation — `self` shared via MRO) + a `PROJECT_KNOBS` registry. **Every task keeps all 1663 tests green + all 4 gates clean + an import smoke-test; zero test-logic edits (the package `__init__` re-exports every moved symbol).**_

## Acceptance for EVERY task (it's a refactor)
- `pytest` still **1663 passed** (no test logic changed — only `from claude_tg.stream_session import …` keeps resolving via the `__init__` re-export).
- `ruff check .` + `mypy claude_tg` + `python scripts/secret_scan.py` all green.
- Import smoke-test: `python -c "import claude_tg.bot, claude_tg.stream_session"` + the re-exported private symbols still importable.
- No behavior change (it's relocation/consolidation, not logic).

## Task list
- [ ] T1 — `stream_session/` package skeleton + leaf modules: `types.py` (StreamingBusy/Attach/Watch/Callback outcomes + constants) + `runtime.py` (`_ProjectRuntime`/`_ChatState`/`_PendingRef`/`_QueuedTurn`/`_TurnDedup` + `_sanitize_attach_name`/`_basename_of` + `_default_engine_factory`), and `__init__.py` re-exporting EVERY symbol `bot.py` + the tests import (the riskiest packaging step — isolate first). `core.py` initially holds the rest of `StreamingSession`.
- [ ] T2 — extract `ConcurrencyMixin` (`concurrency.py`): the slot cap + FIFO queue (`_acquire_slot`/`_release_slot`/`_pop_next_waiter`/`_drain_queued` + lock coordination). Relocate INTACT (no redesign).
- [ ] T3 — extract `StatuslineMixin` (`statusline.py`): `_statusline_*`/`_update_statusline`/`_maybe_update_statusline`/`_statusline_text` + the gated-edit/send helpers.
- [ ] T4 — extract `CallbacksMixin` (`callbacks.py`): `resolve_callback` + the `_resolve_*` family + the pending-index helpers.
- [ ] T5 — `PROJECT_KNOBS` registry: collapse model/effort/thinking into one registry + `_resolve_knob` + `session_store._persist_field`; fold the warm-engine match-key + the per-runtime built-with markers. Preserve the two asymmetries (thinking transient/RB3; effort no config-default).
- [ ] T6 — tidy + verify: confirm `core.py` is the clean coordinator (`__init__`/foundation/engine-lifecycle/`handle_message`/`_drive_turn`/`_ensure_engine`); finalize `__init__` `__all__`; a final whole-suite + gates pass.

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (sha) · `[!]` blocked

## Tasks

### T1 — package skeleton + leaf modules (`types.py`, `runtime.py`, `__init__.py`)
- **What:** convert `claude_tg/stream_session.py` → a `claude_tg/stream_session/` package. Move the no-`self` leaf types/constants/result-dataclasses into `types.py`; the runtime dataclasses (`_ProjectRuntime`/`_ChatState`/`_PendingRef`/`_QueuedTurn`/`_TurnDedup`) + pure module helpers (`_sanitize_attach_name`/`_basename_of`) + `_default_engine_factory` into `runtime.py`. `core.py` holds the (still-whole) `StreamingSession` + everything else, importing from the leaf modules. `__init__.py` re-exports the full symbol set (`StreamingSession`, `StreamingBusy`, `AttachOutcome`, `WatchOutcome`, `CallbackOutcome`, `_ProjectRuntime`, `_ChatState`, `_PendingRef`, `_QueuedTurn`, `_default_engine_factory`, the `EngineFactory`/`SendFn`/… aliases, + the existing `__all__`).
- **Accept:** all gates + 1663 tests green; `bot.py` + every test import resolves unchanged; no logic touched. (Riskiest step — the packaging move — so it lands first + alone.)

### T2 — `ConcurrencyMixin` (`concurrency.py`)
- **What:** relocate the slot/queue/lock methods into a mixin class; `StreamingSession(ConcurrencyMixin, …)`. INTACT relocation — the ADR-005 concurrency design is unchanged. A mixin never imports `core` at module scope (TYPE_CHECKING only) → no cycle.
- **Accept:** all gates + 1663 green; concurrency tests (`test_concurrency_matrix`) unchanged + passing.

### T3 — `StatuslineMixin` (`statusline.py`)
- **What:** relocate the statusline methods (incl. the B2 foreground-re-check gated-edit/send helpers) into a mixin. Preserve the post-await `_is_foreground(built_for)` invariant exactly.
- **Accept:** all gates + 1663 green; statusline tests unchanged + passing.

### T4 — `CallbacksMixin` (`callbacks.py`)
- **What:** relocate `resolve_callback` + `_resolve_switch/attach/ask/plan/permission/free_text` + the pending-index helpers into a mixin. (`handle_cancel`/`_cancel_project`/`_drain_queued` straddle callbacks↔concurrency — place per the design; they call each other via `self`, no import edge.)
- **Accept:** all gates + 1663 green; the answer-hold/callback tests unchanged + passing.

### T5 — `PROJECT_KNOBS` registry (knob consolidation)
- **Files:** `stream_session/knobs.py` (the registry + `_resolve_knob`), `session_store.py` (`_persist_field` helper; `set_model`/`set_effort`/`set_thinking` → thin wrappers), `core.py`/`_ensure_engine` (warm-match-key loop + built-with markers from the registry).
- **Accept:** behavior-identical — model/effort/thinking still resolve, persist, and rebuild-on-change exactly as before; **thinking stays transient (RB3), effort keeps NO config default**; all gates + 1663 green (the existing knob tests are the proof).

### T6 — tidy + verify
- **What:** confirm the final package shape, `__init__` `__all__`, no dead re-exports; a final full `pytest` + all 4 gates + the import smoke-test. Update README/docs only if a path/structure reference is now stale (the public import path is unchanged, so likely none).
- **Accept:** 1663 green, all gates clean, `core.py` materially smaller, no behavior change.

## T-VERIFY (the pipeline Verify+QA phase, after T1–T6)
Cross-model Codex QA (focus: did ANY behavior change? any moved method that lost a `self`-binding / changed an order / dropped a guard? import-cycle/`__all__` correctness) + a Verifier (same) → since it's behavior-preserving, the bar is **"prove nothing changed"** + the suite green → merge to `main`.
