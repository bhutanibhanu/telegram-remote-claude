# Feature Handoff: core-refactor

## Goal
A behavior-PRESERVING refactor: split the ~5,500-line `StreamingSession` god-class into a `stream_session/` mixin package, and collapse the model/effort/thinking knob duplication into one `PROJECT_KNOBS` registry. NO behavior change — the 1663-test suite stayed green throughout with ZERO test edits.

## Files changed
```
 README.md                                  |   4 +-   (stale stream_session.py file-ref → package)
 claude_tg/session_store.py                 |  62 +-   (T5: _persist_field helper; set_model/set_effort → wrappers)
 claude_tg/stream_session/__init__.py       |  66 +    (re-export hub — keeps every import path valid)
 claude_tg/stream_session/types.py          | 193 +    (T1: leaf — outcomes/aliases/constants/StreamingBusy/helpers)
 claude_tg/stream_session/runtime.py        | 661 +    (T1: _ProjectRuntime/_ChatState/_PendingRef/_QueuedTurn/_TurnDedup/_default_engine_factory)
 claude_tg/stream_session/concurrency.py    | 260 +    (T2: ConcurrencyMixin — slot/queue/lock)
 claude_tg/stream_session/statusline.py     | 451 +    (T3: StatuslineMixin)
 claude_tg/stream_session/callbacks.py      | 838 +    (T4: CallbacksMixin — resolve_* + cancel + pending-index)
 claude_tg/stream_session/knobs.py          | 162 +    (T5: PROJECT_KNOBS registry + _resolve_knob)
 {stream_session.py => stream_session/core.py} | -2312  (git rename; StreamingSession core, 5487→3369 lines)
 ZERO test files changed.
```

## How to run
- Gates (worktree `.venv`): `pytest -q` (**1663**, unchanged), `ruff check .`, `mypy claude_tg`, `python scripts/secret_scan.py`.
- Smoke: `python -c "import claude_tg.bot, claude_tg.stream_session; from claude_tg.stream_session import StreamingSession, _ProjectRuntime, _ChatState, _PendingRef, _QueuedTurn, _default_engine_factory"`.
- The bot runs exactly as before — the public import path `claude_tg.stream_session` is unchanged (now a package).

## Expected behavior
- **Identical to before.** This is relocation (T1–T4) + a behavior-identical knob consolidation (T5). No feature, flow, ordering, or guard changed.
- `StreamingSession(StatuslineMixin, CallbacksMixin, ConcurrencyMixin)` — methods relocated into mixins, `self` shared via the MRO (no logic rewrite). Leaf dataclasses/helpers in `types.py`/`runtime.py`.
- `model`/`effort`/`thinking` now flow through one registry: same resolve (override→default), same persistence (model+effort to disk 0600; **thinking transient/RB3**; **effort no config-default**), same warm-engine rebuild-on-change (model is NOT a session-identity knob — unchanged), same `_build_options` kwargs.

## Test plan
- **The 1663-test suite is the proof** — it passed at every task with ZERO edits. The relocation is provably behavior-preserving for everything the tests cover (concurrency matrix, answer-hold/callbacks, statusline, knobs/rebuild, session-store, security/reliability). The QA bar here is "confirm nothing changed," not "test new behavior."
- Import smoke-test (above) + the package `__init__` re-export keeps `bot.py`/`app.py`/tests importing unchanged.

## Known risks
- **Mixin `self.x` resolution:** methods reference foundation attrs not defined in their own file; resolved at runtime via the composed `StreamingSession` MRO. mypy satisfied via `TYPE_CHECKING` attribute annotations (behavior-neutral). Where to look if something's off: the MRO line in `core.py` + the `__init__` re-export set.
- **Import cycles:** mixins import only leaf modules (`.types`/`.runtime`); none import `core` at module scope. A new module-scope `core` import in a mixin would cycle.
- **Knob registry (T5):** the only logic-touching task — the asymmetries (thinking transient, effort no-default, model not-session-identity) are the behavior-identity hotspots; the existing knob/rebuild tests pin them.

## Open questions
- Deferred (noted, not done): comment-thinning in `core.py`, `bot.py` command-table single-sourcing, `_drive_turn` TurnContext (the 14-param signature). Optional follow-ups.
