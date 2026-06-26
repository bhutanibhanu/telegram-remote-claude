# core-refactor — design

**Slug:** `core-refactor` · **Branch:** `feat/core-refactor` (off `main` `f0dc73c`)
**Kind:** behavior-PRESERVING refactor (investigation + design here; build is a separate phase)

## 0. Goal & non-negotiable invariant

Two readability refactors surfaced by a 3-reviewer architecture review:

1. **Split the ~5,500-line `StreamingSession` god-class** (`claude_tg/stream_session.py`,
   ~100 methods, one class) into a `claude_tg/stream_session/` **package**.
2. **Collapse the duplicated per-project "knob" pattern** (model / effort / thinking) into
   ONE declarative registry.

**The invariant that dominates every decision below: NO behavior change.** The codebase is
fully green at `f0dc73c` (1663 tests; `ruff check .`, `mypy`, `python scripts/secret_scan.py`
all clean — verified `ruff` clean on `stream_session.py` and the baseline collects). Every
task must keep all four gates green AND the full suite passing — this is a pure
relocation/dedup, not a redesign. The concurrency design, the engine, render.py, the leaf
modules are **relocated intact, not re-thought** (see §7 OUT OF SCOPE).

The gate commands (from `.github/workflows/ci.yml`, run from repo root):

```
python -m pytest          # full suite
ruff check .
mypy
python scripts/secret_scan.py
```

Commits are clean single-line, **NO `Co-Authored-By`** (inherited repo convention). The SDK
pin is untouched.

---

## 1. The central design decision — Mixins, not collaborators

A class's methods share `self`. The question is the SAFEST behavior-preserving way to split
~100 methods that all read/write `self.config`, `self.store`, `self._chats`, `self._running`,
`self._engine_factory`, … and call one another freely.

I analyzed the coupling (every `self.X` and every cross-method call, per group). The verdict:

> **Use MIXINS for the method-groups, and a clean MODULE move for the pure
> dataclasses/helpers + the knob registry.** Recommend AGAINST extracting true collaborators
> in this pass.

### Why mixins (the coupling evidence)

`StreamingSession` is a *coordinator*: nearly every method bottoms out in a tiny set of
foundational helpers — `_chat`, `_active_runtime`/`_override_runtime`/`_runtime`,
`_is_foreground`, `_gate`/`_gated_send`/`_gated_edit`, `_resolve_runtime_key`, `_persist`,
`_resolve_project_model`/`_resolve_project_effort` — plus the instance fields. Concretely:

- **statusline** (`_statusline_text`, `_update_statusline`, `_statusline_gated_edit`, …)
  calls `_active_runtime`, `_resolve_project_model`, `_resolve_project_effort`,
  `_is_foreground`, `_chat`, `_gate`, `_gated_edit`, `_sleep`, and reads
  `_ProjectRuntime`/`_ChatState` fields directly.
- **callbacks/resolve** (`resolve_callback` + `_resolve_*` family) calls into the
  pending-index helpers (`_drop_pending`, `_resume_pending_status`, `_runtime_for_pending`,
  `_clear_project_pending`, `_next_armed_seq`, `_clear_runtime_text`), `_engine_for_pending`,
  `_resolve_runtime_key`, `_active_runtime`, `_chat`, and `_drain_queued` (a concurrency
  method) — and `_resolve_free_text` is called straight from `handle_message`.
- **concurrency** (`_acquire_slot`/`_release_slot`/`_pop_next_waiter`/`_remove_queued`/
  `_is_queued`) touches `self._running`, `self.config.max_concurrent_runs`, `self._chats`,
  `self._gated_send`, `self._sleep`.
- **core** (`handle_message`/`_drive_turn`/`_ensure_engine`) touches **all of the above**.

Extracting these as collaborator objects with their own state would mean threading
`self`/`state`/`runtime` into every method (they already take `state`/`rt` params in many
cases, but they also call ~8 sibling `self._*` helpers each), re-wiring ~40 call sites, and
moving shared state (`_running`, `_chats`) behind an interface. That is an invasive rewrite
with real behavior-drift risk (ordering of the slot/lock/abort dance in `handle_message`, the
B2 foreground re-check windows in the statusline) — exactly what a behavior-preserving pass
must avoid.

A **mixin** is a near-pure *relocation*: cut a method-group into `class StatuslineMixin:` in
its own file, and `class StreamingSession(RuntimeMixin, StatuslineMixin, CallbacksMixin,
ConcurrencyMixin, CoreMixin)` composes them onto one shared `self`. Every `self.X` keeps
resolving through the MRO regardless of which file the method physically lives in. No logic
changes, no call-site rewrites, no state threading.

### Why this is near-zero-risk here specifically (the test-shape proof)

I parsed the whole `tests/` tree for how the private members are reached:

- **No test monkeypatches the module** — grep for `monkeypatch.setattr("claude_tg.stream_session…")`
  and `patch("claude_tg.stream_session…")` returns **zero** hits. So no test pins a method to a
  specific module path; moving a method between files is invisible to them.
- The private *methods* (`_ensure_engine` ×21, `_register_pending` ×11, `_drive_turn` ×8,
  `_acquire_slot` ×3, `_persist`, `_resolve_free_text`, …) are referenced **only as instance
  attributes** (`session._ensure_engine(...)`). Instance-attribute access resolves through the
  MRO — a mixin method is reached identically to a method defined on the class. **No test
  changes for any method call site.**
- The only import-level coupling is `from claude_tg.stream_session import <Name>` for a small
  set of **symbols** (classes/functions/type-aliases), enumerated in §4. The package
  `__init__.py` re-exports every one, so those import lines keep working **unchanged**.

Net: the test blast-radius of the entire split is **zero edited test lines** — the re-export
keeps every import valid and instance-method access is MRO-transparent.

### The one honest caveat (and why it's a non-issue)

Splitting one class across mixin files means `mypy` and `ruff` see each method in a file that
does **not** define the fields/siblings it uses (`self._running` lives on `__init__` in
`core.py`; `StatuslineMixin._update_statusline` calls `self._active_runtime` defined in
`RuntimeMixin`). Two mitigations, both standard and both behavior-neutral:

- Each mixin declares the **attributes/sibling methods it consumes** as bare annotations under
  an `if TYPE_CHECKING:` block (e.g. `config: Config`, `store: Any`, `_chats: dict[int,
  _ChatState]`, `_running: int`, and the foundational method signatures) — OR, simpler and what
  the repo's lenient mypy baseline already tolerates, the mixins inherit a thin
  **`_SessionProto` Protocol/base** that declares the shared surface. The build task picks
  whichever keeps `mypy` green with the fewest annotations; the existing baseline is lenient
  (`pyproject.toml` notes "green on the current codebase"), so untyped `self.x` access on a
  mixin is already acceptable — the annotations are belt-and-braces for readability, not a gate
  requirement.
- `ruff` is unaffected (it does not require a name to be defined in-file for attribute access).

This caveat is purely about *type-checker visibility*, never about *runtime behavior* — at
runtime the composed class has every attribute. It is the known, accepted cost of the mixin
pattern and is the lightest of the available costs.

### Recommendation per group (lowest-risk that still wins readability)

| Group | Approach | Rationale |
|---|---|---|
| dataclasses + pure helpers (`_ProjectRuntime`, `_ChatState`, `_PendingRef`, `_QueuedTurn`, `_TurnDedup`, `_sanitize_attach_name`, `_basename_of`, `_pending_kind_of`, `_footer_only_result`, `_resume_failure_text`, `_is_resume_failure_event`, the constant tables, the `AttachOutcome`/`WatchOutcome`/`CallbackOutcome` result dataclasses, the `EngineFactory` Protocol + `_default_engine_factory`) | **clean MODULE move** | No `self`; pure data/functions. They move to `runtime.py`/`types.py` and are imported normally. Zero MRO concern. |
| statusline, callbacks/resolve+pending-index, concurrency | **MIXINS** | Tightly self-coupled method-groups; relocation only. |
| core (`__init__`, foundational helpers, engine lifecycle, `handle_message`/`_drive_turn`, recovery, notify/attach/watch/fire feature methods, the public command surface) | **stays as the base class in `core.py`** (the `StreamingSession` that inherits the mixins) | It owns `__init__` + all instance state and is the orchestration root; everything depends *on it*, so it is the natural MRO tail. |
| knob pattern (model/effort/thinking) | **declarative registry + module move** (refactor #2) | A data-table + 2 helpers replacing parallel methods; see §5. |

---

## 2. Package layout

Replace the single file `claude_tg/stream_session.py` with a package
`claude_tg/stream_session/`. The module's import path (`claude_tg.stream_session`) is preserved
exactly, so `bot.py`, `app.py`, and all tests keep importing from `claude_tg.stream_session`.

```
claude_tg/stream_session/
├── __init__.py        # re-export hub: `from .core import StreamingSession`, etc. (§4)
├── types.py           # leaf types/constants, NO self, imported by everything below
├── runtime.py         # runtime dataclasses + module-level pure helpers (clean move)
├── knobs.py           # PROJECT_KNOBS registry + _resolve_knob (refactor #2; clean move)
├── statusline.py      # class StatuslineMixin
├── callbacks.py       # class CallbacksMixin   (resolve path + pending-index helpers)
├── concurrency.py     # class ConcurrencyMixin (slot/queue/cap)
└── core.py            # class StreamingSession(RuntimeMixin?, Statusline, Callbacks,
                        #   Concurrency): __init__ + foundation + engine + handle_message/
                        #   _drive_turn + recovery + notify/attach/watch/fire
```

Notes on the layout choice:

- **`types.py`** holds the things with no `self` and no dependency on the runtime dataclasses:
  the type aliases (`SendFn`/`EditFn`/`DeleteFn`/`PinFn`/`UnpinFn`, `PermissionVerdictName`,
  `PendingKind`), the constant tables (`_PERMISSION_VERDICTS`, `_PERMISSION_NOTES`,
  `_AWAITING_STATUS`, `_ATTACH_NAME_SANITIZE_RE`), the `StreamingBusy` exception, the three
  frozen result dataclasses (`AttachOutcome`/`WatchOutcome`/`CallbackOutcome`), and the small
  pure functions that don't touch runtime state (`_sanitize_attach_name`, `_basename_of`).
  Splitting these out first means `runtime.py` and the mixins can import from `types.py` with
  no cycle.
- **`runtime.py`** holds `_ProjectRuntime`, `_ChatState`, `_PendingRef`, `_QueuedTurn`,
  `_pending_kind_of`, `_TurnDedup`, `_footer_only_result`, `_resume_failure_text`,
  `_is_resume_failure_event`, the `EngineFactory` Protocol + `_default_engine_factory`. (These
  depend on engine/render/permissions types but **not** on `StreamingSession`.)
- **`RuntimeMixin` is optional.** The foundational resolution helpers (`_chat`,
  `_active_runtime`, `_override_runtime`, `_ensure_default_active`, `_runtime`, `get_cwd`,
  `_is_foreground`, `_gate`, `_gated_send`, `_gated_edit`, `_resolve_runtime_key`, `_persist`,
  `is_busy`, `project_status`, the yolo getters/setters) are the layer **every** mixin calls.
  Two valid placements:
  - **(A, recommended) leave them in `core.py`** as methods on `StreamingSession` itself. They
    are few (~15) relative to core's size, they sit naturally next to `__init__` (which builds
    the state they read), and keeping them on the base class means the mixins depend "up" onto
    the class they're mixed into (normal for mixins). This is the simplest and is what the task
    breakdown assumes.
  - (B) extract a `RuntimeMixin` in `runtime_mixin.py` if `core.py` is still uncomfortably
    large after T1–T4. This is a pure follow-on relocation with the same zero-risk profile; it
    is listed as an OPTIONAL step in T6, not required for the readability win.

### Dependency direction (acyclic — verified against the call graph)

```
types.py        →  (stdlib + telegram + engine/render/permissions leaf types only)
runtime.py      →  types.py            (+ engine/render/permissions)
knobs.py        →  (config types; session_store at call time)   — leaf, no session import
statusline.py   →  types, runtime                 [mixin: assumes core's foundation via self]
concurrency.py  →  types, runtime                 [mixin]
callbacks.py    →  types, runtime                 [mixin]
core.py         →  types, runtime, knobs, statusline, concurrency  (imports the mixins to compose)
__init__.py     →  core (+ re-exports from types/runtime/knobs)
```

The mixins import only **leaf** modules (`types`, `runtime`) at module scope — never `core`.
They reach `StreamingSession`'s foundation through `self` at **runtime** (resolved by the MRO),
which is not an import, so there is no module-level cycle. `core.py` imports the mixins (to
list them as bases) — a one-way edge. `__init__.py` imports `core`. **No cycles.**

---

## 3. Circular-import analysis (the explicit risk the brief asked about)

The danger in a god-class split is a *module-level* import cycle. I checked each suspect edge:

- **Does statusline need callbacks?** No. `_update_statusline` & co. call only foundation
  helpers + read runtime fields. `statusline.py` imports `types`/`runtime` only.
- **Does callbacks need concurrency?** At *runtime* yes — `_cancel_project` calls
  `self._drain_queued` (a concurrency method). But that is an MRO call through `self`, **not** a
  module import. `callbacks.py` does not `import` `concurrency.py`. No cycle.
- **Does the core need all of them?** Yes — and it gets them by importing the mixin
  *modules* to list as bases. That edge is one-way (`core → {statusline, callbacks,
  concurrency}`); none of those import `core`. No cycle.
- **Do the mixins need core's symbols at module scope?** No. They need core's *methods/fields*,
  reached via `self` at runtime. The only module-scope names they need (the dataclasses, the
  constant tables, `_pending_kind_of`, `_AWAITING_STATUS`, the type aliases) live in
  `types`/`runtime`, which are leaves.

**How `__init__.py` resolves it:** `__init__.py` imports `core` **last** (after — or rather,
`core` itself imports the leaves it needs). Because every cross-cut runs through `self` rather
than a module import, the import graph is a DAG rooted at `types`. The one rule the build must
respect: **a mixin must never `import` `core` at module scope** (only `TYPE_CHECKING` imports
of `StreamingSession` for annotations are allowed, since those are not evaluated at runtime).

The only residual risk is *accidental* — a stray top-level `from .core import …` added to a
mixin during the cut. T2–T4 each end with a full `pytest` + `python -c "import
claude_tg.stream_session"` so an import cycle is caught the instant it's introduced (it
manifests as an `ImportError` at collection time, not a silent regression).

---

## 4. `__init__.py` re-export list (the make-or-break for the test/back-compat surface)

`bot.py` imports `StreamingBusy, StreamingSession` (line 68); `app.py` imports
`StreamingSession` (line 25). The tests import (verified by AST-parsing every `tests/*.py`
`ImportFrom` whose module is `claude_tg.stream_session`):

`AttachOutcome`, `CallbackOutcome`, `StreamingBusy`, `StreamingSession`, `WatchOutcome`,
`_ProjectRuntime`, `_QueuedTurn`, `_default_engine_factory`.

The current single module also declares `__all__ = [StreamingSession, StreamingBusy,
AttachOutcome, WatchOutcome, CallbackOutcome, EngineFactory, SendFn, EditFn, DeleteFn]`. The
brief flags `_ChatState`, `_drive_turn`, `_ensure_engine`, `_acquire_slot`, `_PendingRef` as
imported by ~92 sites — confirmed: those are reached as **instance attributes / bare names
after import**, not all as distinct `import` lines, but to be safe the `__init__` re-exports
**every moved public + private symbol** so any import form keeps working.

The package `__init__.py` MUST re-export (superset of "what's imported today" ∪ "`__all__`" ∪
"every private symbol a test could import"):

```python
# claude_tg/stream_session/__init__.py
from .core import StreamingSession
from .types import (
    StreamingBusy,
    AttachOutcome, WatchOutcome, CallbackOutcome,
    SendFn, EditFn, DeleteFn, PinFn, UnpinFn,
    PermissionVerdictName, PendingKind,
    _sanitize_attach_name, _basename_of,
    _PERMISSION_VERDICTS, _PERMISSION_NOTES, _AWAITING_STATUS,
)
from .runtime import (
    _ProjectRuntime, _ChatState, _PendingRef, _QueuedTurn,
    _TurnDedup, _pending_kind_of, _footer_only_result,
    _resume_failure_text, _is_resume_failure_event,
    EngineFactory, _default_engine_factory,
)
from .knobs import PROJECT_KNOBS  # refactor #2 (optional to export, useful for tests)

__all__ = [
    "StreamingSession", "StreamingBusy",
    "AttachOutcome", "WatchOutcome", "CallbackOutcome",
    "EngineFactory", "SendFn", "EditFn", "DeleteFn",
]
```

Minimum *required* by today's imports: `StreamingSession`, `StreamingBusy`, `AttachOutcome`,
`WatchOutcome`, `CallbackOutcome`, `_ProjectRuntime`, `_QueuedTurn`, `_default_engine_factory`
(tests) + the `__all__` set (`EngineFactory`/`SendFn`/`EditFn`/`DeleteFn`, kept for back-compat
even though no current site imports them). The fuller list above re-exports the rest of the
private/leaf symbols too, so a future test that imports `_ChatState`/`_PendingRef`/
`_pending_kind_of`/`_AWAITING_STATUS` from the package keeps working — cheap insurance, exactly
the "keep the blast-radius to import lines" guarantee. (Re-exporting private names triggers a
ruff F401-unused-import; suppress with a per-file `# noqa: F401` on `__init__.py` or list them
in `__all__`-adjacent `_REEXPORT` — the build picks whichever the repo's ruff config prefers;
the existing per-file-ignores section in `pyproject.toml` is the place.)

**Acceptance for the re-export task:** after the move,
`python -c "from claude_tg.stream_session import StreamingSession, StreamingBusy,
AttachOutcome, WatchOutcome, CallbackOutcome, _ProjectRuntime, _QueuedTurn,
_default_engine_factory"` succeeds, and the full suite passes with **zero** edits to any test
import line.

---

## 5. Knob registry (refactor #2)

### The duplication today

Three per-project knobs — **model**, **effort**, **thinking** — each carry a parallel,
near-identical implementation spread across two files:

- `StreamingSession.set_model` / `set_effort` / (`set_thinking`) — the command setters.
- `StreamingSession._resolve_project_model` / `_resolve_project_effort` — "override → config
  default → None" resolution at `_ensure_engine` time. (Thinking has no resolver — it's read
  straight off the runtime as `rt.thinking`.)
- `session_store.set_model` / `get_model` and `set_effort` / `get_effort` — the persistence
  pair (model + effort persist; thinking is transient-only). These two `set_*` store methods
  are byte-for-byte the same shape: `_load_raw` → `_resolve` → validate/normalize →
  `record.pop(field)` or `record[field] = value` → bump `last_active` → `_save_raw`. They
  differ ONLY in the field name (`"model"`/`"effort"`) and the validation (model: non-empty
  str; effort: membership in `_EFFORT_LEVELS`).
- `_ensure_engine`'s warm-engine match key: `rt.engine_permission_mode == permission_mode and
  rt.engine_thinking == thinking and rt.engine_effort == effort` — three parallel "built-with"
  comparisons.
- `_ProjectRuntime`'s per-runtime "built-with" markers: `engine_permission_mode`,
  `engine_thinking`, `engine_effort` (model is re-resolved each build, not stored as a marker —
  an asymmetry the registry can normalize or leave; see below).

### The registry

A single declarative table describes each knob; one resolver + one persist helper replace the
parallel methods. **Behavior-identical** — the table's rows encode exactly today's
per-knob behavior (default source, validation, stickiness, persistence).

```python
# claude_tg/stream_session/knobs.py
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass(frozen=True)
class Knob:
    name: str                       # "model" | "effort" | "thinking"
    field: str                      # the session_store record field ("model"/"effort") or "" if transient
    persisted: bool                 # model/effort True; thinking False (in-memory only, RB3)
    config_default: Callable[["Config"], Optional[object]]  # model→config.model; effort→None; thinking→False
    normalize: Callable[[object], Optional[object]]         # model: strip-or-None; effort: lower∩_EFFORT_LEVELS-or-None; thinking: bool
    sticky: bool                    # all three are sticky per project (model/effort persist; thinking sticky in-memory).

PROJECT_KNOBS: dict[str, Knob] = {
    "model":    Knob("model",    "model",  True,  lambda c: c.model, _norm_model,    sticky=True),
    "effort":   Knob("effort",   "effort", True,  lambda c: None,    _norm_effort,   sticky=True),
    "thinking": Knob("thinking", "",       False, lambda c: False,   bool,           sticky=True),
}
```

Then:

- **`session_store._persist_field(chat_id, name, field, value)`** — the single generalized
  setter the store gains: `_load_raw` → `_resolve` → if `value is None`: `record.pop(field,
  None)` else `record[field] = value` → `record["last_active"] = _now()` → `_save_raw`. Raises
  `UnknownProject` like the existing setters. `set_model`/`set_effort` become **2-line wrappers**
  that normalize then call `_persist_field` (keep the wrappers as the public store API so
  `session_store`'s own tests are unchanged — the dedup is behind them). A matching
  `_read_field` could back `get_model`/`get_effort`, but those are already trivial reads;
  collapsing them is optional and lower-value (leave or fold per the build's discretion).
- **`StreamingSession._resolve_knob(chat_id, name, knob_name)`** — the single resolver
  replacing `_resolve_project_model` + `_resolve_project_effort` (and giving thinking a uniform
  resolution): read the persisted override via the store (for `persisted` knobs) or the runtime
  field (for transient ones), normalize, fall back to `config_default`. `_ensure_engine` calls
  `self._resolve_knob(chat_id, name, "model")` etc. The setter side —
  `set_model`/`set_effort`/`set_thinking` — becomes a single `_set_knob(chat_id, knob_name,
  value)` that resolves the active runtime, normalizes, and either persists (via the store's
  `set_*`/`_persist_field`) or writes the runtime field; the three public methods stay as thin
  wrappers (they have distinct signatures/return contracts and the bot + tests call them by
  name, so keep the names, dedup the bodies).
- **Warm-engine match key:** replace the three hand-written comparisons with a loop over the
  knobs that participate in session identity: `all(rt.engine_built_with.get(k) ==
  resolved[k] for k in SESSION_KEY_KNOBS)` plus the existing `permission_mode` term
  (permission_mode is a per-turn one-shot, not a project knob — it stays its own term, OR is
  modeled as a fourth "turn knob" row; recommend keeping it explicit since it's one-shot, not
  per-project-sticky). The per-runtime built-with markers collapse from three named fields to
  one small `engine_built_with: dict[str, object]` (or stay as named fields if mypy/readability
  prefers; the dict is what removes the parallelism). Either is behavior-identical; the dict is
  the cleaner endpoint.

### Behavior-identity checklist for #2 (what the build must preserve exactly)

- model: override → `config.model` → `None`; persisted; applies next fresh session.
- effort: override → `None` (NO config default — the asymmetry is encoded as
  `config_default = lambda c: None`); validated against `_EFFORT_LEVELS`; persisted.
- thinking: `rt.thinking` sticky in-memory, default `False`; NOT persisted; applies next fresh
  session. The registry must keep thinking **transient** (`persisted=False`) — persisting it
  would be a behavior change (RB3: the supervision posture must not survive a restart).
- The warm fast-path must reuse the engine **iff** every session-identity knob's built-with
  value matches the freshly-resolved value (so a back-to-back no-override turn still reuses
  byte-for-byte, and any knob change rebuilds on the next turn) — exactly today's
  `engine_permission_mode == … and engine_thinking == … and engine_effort == …`.
- `session_store.set_model`/`set_effort`/`get_model`/`get_effort` keep their exact public
  signatures + `UnknownProject`-on-missing + atomic-0600 + `last_active`-bump semantics (the
  dedup is internal).

Refactor #2 is **smaller and more localized** than #1 and is independently verifiable (its own
task, T5) — it does not depend on the package split and could even land first, but the task
order below does it after the split so each task's diff is small and reviewable.

---

## 6. Safety strategy & task breakdown

### Safety strategy (how "tests-green-throughout" is guaranteed)

- **Every task keeps all four gates green and the full suite passing** — a task is not "done"
  until `python -m pytest && ruff check . && mypy && python scripts/secret_scan.py` is clean.
- **One commit per task** (clean single-line message, no `Co-Authored-By`). A task that can't
  go green is reverted, not patched forward.
- **The re-export keeps the blast-radius to import lines** — because no test monkeypatches the
  module and method access is MRO-transparent, the *only* thing that can break a test is a
  missing re-export, which §4's `__init__` covers and each task's `pytest` run catches
  immediately.
- **Per task: add a one-liner import smoke** (`python -c "import claude_tg.stream_session"` +
  the §4 multi-name import) so an import cycle / missing re-export fails loudly at the task
  boundary, not three tasks later.
- **Mechanical moves only.** Each move task is cut-paste of a contiguous method-group into a
  mixin + add the base to the `class StreamingSession(...)` line + (if needed) the
  `TYPE_CHECKING` attribute annotations. No method body is edited (T1–T4). If a move would
  require editing a body, that's a signal the group boundary is wrong — re-slice, don't rewrite.

### Ordered tasks (each independently verifiable; suite stays green after each)

- **T1 — `types.py` + `runtime.py` (the leaf module move).** Create the package skeleton
  (`stream_session/` dir, move the file to `core.py`, add `__init__.py` re-exporting everything
  from `core` so the import path is preserved and the suite is green *before any split*). Then
  extract the no-`self` symbols (type aliases, constant tables, `StreamingBusy`, the three
  result dataclasses, `_sanitize_attach_name`/`_basename_of` → `types.py`; the runtime
  dataclasses + pure helpers + `EngineFactory`/`_default_engine_factory` → `runtime.py`).
  `core.py` imports them back; `__init__` re-exports per §4. **Verify:** full suite + import
  smoke; this is the riskiest *packaging* step so it's first and isolated.
- **T2 — `concurrency.py` (`ConcurrencyMixin`).** Move `_acquire_slot`, `_release_slot`,
  `_pop_next_waiter`, `_remove_queued`, `_is_queued`, `_queued_waiting`, `queued_waiting`,
  `active_run_count`, `_drain_queued`. Add `ConcurrencyMixin` to the bases. Smallest, most
  self-contained group → proves the mixin pattern end-to-end. **Verify:** full suite +
  `test_concurrency_matrix.py` green; import smoke.
- **T3 — `statusline.py` (`StatuslineMixin`).** Move `_edit_status`, `_maybe_update_statusline`,
  `_statusline_text`, `_update_statusline`, `_statusline_gated_edit`,
  `_statusline_send_and_pin`, `_statusline_pin`. **Verify:** full suite (statusline B1/B2/B3
  behavior unchanged — the foreground re-check windows must be byte-identical); import smoke.
- **T4 — `callbacks.py` (`CallbacksMixin`).** Move `resolve_callback` + the `_resolve_*` family
  (`_resolve_switch`/`_resolve_attach`/`_resolve_ask_option`/`_record_ask_answer`/
  `_arm_ask_other`/`_resolve_plan`/`_resolve_permission`/`_resolve_free_text`/`resolve_to`),
  `_engine_for_pending`, and the pending-index helpers (`_register_pending`/`_drop_pending`/
  `_clear_project_pending`/`_prune_reply_to`/`_runtime_for_pending`/`_resume_pending_status`/
  `_armed_text_runtime`/`_next_armed_seq`/`_route_free_text_target`/`_runtime_armed_for_id`/
  `_clear_runtime_text`/`_clear_runtime_turn_state`). (`handle_cancel`/`_cancel_project` may go
  here OR stay in core next to the lifecycle — recommend `callbacks.py` since they're part of
  the lock-free decision-resolution surface; the build picks the placement that keeps `core.py`
  cohesive.) **Verify:** full suite; import smoke.
- **T5 — knob registry (refactor #2).** Add `knobs.py` (`PROJECT_KNOBS` + `Knob`), the
  `session_store._persist_field` helper (rewire `set_model`/`set_effort` as wrappers), the
  `StreamingSession._resolve_knob`/`_set_knob`, the warm-engine match-key collapse, and the
  per-runtime built-with-markers normalization. **Verify:** full suite incl.
  `test_session_store.py`, `test_multi_project.py`, the statusline knob tests; assert the
  warm-reuse byte-identity tests still pass; import smoke.
- **T6 — final tidy + verify.** Confirm `core.py` is now cohesive (`__init__` + foundation +
  engine lifecycle + `handle_message`/`_drive_turn` + recovery + notify/attach/watch/fire). If
  desired, the OPTIONAL `RuntimeMixin` extraction (placement B in §2) lands here. Tighten
  `__init__.py`'s `__all__`, ensure the `# noqa`/re-export hygiene is clean for ruff, do a final
  4-gate run + the full import smoke. No behavior touched.

Each Tn is a single commit. The build can stop after any Tn with a green, shipped tree (the
split is incremental — every task leaves a smaller-but-working `core.py`).

---

## 7. OUT OF SCOPE (explicit)

These are **relocated intact, not re-thought** — or deferred entirely:

- **The concurrency DESIGN** — the slot/queue/cap/abort/inflight lifecycle, the lock-free
  resolve path, the FIFO drain, the slot-transfer-window handling. These methods MOVE to
  `concurrency.py` unchanged; their *logic* is not touched (it carries hard-won
  cross-model-QA fixes — D6/D9, the TOCTOU/zombie-run/slot-leak guards).
- **`engine/`** (the substrate-neutral engine + adapters), **`render.py`**, and the leaf
  modules (`audit.py`, `bash_policy.py`, `permissions.py`, `paths.py`, `session_mirror.py`,
  `sessions_discovery.py`, `scheduler*.py`, `util.py`, `voice.py`, `tg_html.py`). Untouched.
- **`session_store.py` beyond the `_persist_field` dedup** — no schema change, no new fields,
  no migration. Only the internal dedup of the two `set_*` methods (their public API is
  preserved).
- **`bot.py`** — untouched except that nothing changes for it (it imports `StreamingBusy,
  StreamingSession` from `claude_tg.stream_session`, which the package preserves). The deferred
  lighter items below are explicitly NOT in this refactor.
- **Deferred lighter items (named, not done here):**
  - **Comment-thinning.** `stream_session.py` carries very long explanatory comments (the
    ADR/QF/RB rationale). They MOVE with their methods verbatim; trimming them is a separate
    pass (and risky to do blind — they encode why concurrency fixes are shaped as they are).
  - **`bot.py` command-table** refactor (collapsing the `cmd_*` handler registration). Separate.
  - **`_drive_turn` `TurnContext`** — bundling the `(state, chat_id, engine, send/edit/delete/
    pin/unpin, target, images, proactive, plan_turn)` parameter blob into a context object.
    This is a *signature* change to the hottest method and a behavior-risk surface; explicitly
    deferred. `_drive_turn` moves to `core.py` with its current signature.

---

## 8. Feasibility risks found (honest accounting)

1. **Mixin type-checker visibility (LOW).** `self._running` / `self._active_runtime` accessed in
   a mixin file aren't defined there. Mitigated by the lenient mypy baseline + optional
   `TYPE_CHECKING` attribute annotations / a `_SessionProto`. Behavior-neutral. Covered in §1.
2. **Re-exporting private names trips ruff F401 (LOW, mechanical).** The `__init__` re-exports
   `_ProjectRuntime` etc.; ruff flags unused imports. Mitigated by a per-file `# noqa: F401` or
   the `pyproject.toml` per-file-ignores. Pure hygiene.
3. **`handle_cancel`/`_cancel_project`/`_drain_queued` straddle callbacks↔concurrency (LOW).**
   `_cancel_project` (decision surface) calls `_drain_queued` (concurrency). They land in
   different mixins but call each other via `self` — fine at runtime, no import edge. The only
   choice is *which file* `handle_cancel`/`_cancel_project` live in; either works (§6 T4
   recommends `callbacks.py`). No method resists grouping — it's a placement preference, not a
   blocker.
4. **`core.py` is still large after the split (ACCEPTED).** The foundation + engine lifecycle +
   `handle_message`/`_drive_turn` (two ~330/~360-line methods) + the notify/attach/watch/fire
   feature methods are the irreducible orchestration core and stay together. The split takes the
   file from ~5,500 lines / one class to ~6 focused files; `core.py` will still be the biggest
   (~2,500–3,000 lines) but is now *just* the orchestrator. The OPTIONAL `RuntimeMixin` (T6) and
   the deferred notify/attach/watch extraction could shrink it further later — out of scope here.
5. **No genuine snag for the split itself.** The decisive facts — zero module monkeypatching,
   instance-attribute (MRO-transparent) method access, every cross-cut running through `self`
   rather than a module import — mean the mixin split has **no behavior-drift surface** and
   **no import-cycle surface** if the one rule (no `core` import in a mixin) is followed. The
   knob registry (#2) is a localized data-table dedup with an explicit behavior-identity
   checklist (§5). Both are feasible at low risk.
