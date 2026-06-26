"""The per-project *knob* registry (model / effort / thinking) — refactor #2.

Three per-project knobs steer how a project's NEXT fresh session is built:

* **model** (``/fast``·``/deep``·``/auto``) — the Claude model id baked into
  ``ClaudeAgentOptions(model=…)``. PERSISTED on the project; default ``config.model``
  (``CLAUDE_MODEL``); else ``None`` (the SDK default).
* **effort** (``/effort low…max``) — the reasoning-EFFORT level baked into
  ``ClaudeAgentOptions(effort=…)``. PERSISTED on the project; **NO global default** — an
  unset effort resolves to ``None`` so the kwarg is omitted and the SDK's own default
  (``high``) applies.
* **thinking** (``/thinking on|off``) — the live-reasoning VISIBILITY flag. **Transient**
  (in-memory on the runtime, NEVER persisted — RB3: the supervision posture must not survive
  a restart); default ``False``.

Before this registry each knob carried a near-identical, hand-written copy of the same dance
(``set_*``/``get_*`` on the store, ``_resolve_project_*`` on the session, and a per-knob term
in ``_ensure_engine``'s warm-engine match-key). This table encodes — once, declaratively —
exactly what each knob did: its store field, whether it persists, its default source, its
normalization, and whether it participates in **session identity** (a change forces a fresh
session to be built on the NEXT turn — it can't be hot-swapped). The session's ``_resolve_knob``
+ the store's ``_persist_field`` + the looped match-key are the single generalized
implementations the rows drive.

**Behavior-IDENTICAL by construction.** Every row reproduces today's per-knob behavior
byte-for-byte (default chain, validation/normalization, persistence, stickiness); the registry
is a dedup, not a redesign. The companion ``test_stream_session.py`` / ``test_session_store.py``
knob + warm-reuse tests are the proof (they call ``set_model``/``set_effort``/
``_resolve_project_model``/``_resolve_project_effort`` and read ``rt.engine_effort`` by name —
all preserved).

This module is a LEAF in the import graph (it imports only ``Config`` for the type annotation,
under ``TYPE_CHECKING``): it never imports ``core``/``session_store``, so it can be imported by
both without a cycle. The store field/normalize and the session resolution read the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from ..session_store import _EFFORT_LEVELS

if TYPE_CHECKING:  # annotation-only — avoids importing Config at runtime (leaf module)
    from ..config import Config


def _norm_model(value: object) -> Optional[str]:
    """Normalize a model override: a non-empty (stripped) string, else ``None``.

    Byte-for-byte the rule the store's ``set_model``/``get_model`` and the bot's ``/auto``
    used inline — a non-string / empty / whitespace value clears the override (RB1: a bad
    value can never wedge the project on an unusable id; the turn falls back to the default).
    """
    return value.strip() if isinstance(value, str) and value.strip() else None


def _norm_effort(value: object) -> Optional[str]:
    """Normalize an effort override: a recognized level (lowercased), else ``None``.

    Validates against ``{low, medium, high, xhigh, max}`` case-insensitively and returns the
    lowercased canonical level; a non-string / empty / unrecognized value clears the override
    (RB1) — exactly the rule the store's ``set_effort``/``get_effort`` applied inline, so a
    garbage level can never wedge the project on an effort the SDK would reject.
    """
    return (
        value.strip().lower()
        if isinstance(value, str) and value.strip().lower() in _EFFORT_LEVELS
        else None
    )


def _norm_thinking(value: object) -> bool:
    """Normalize the thinking flag to a plain ``bool`` (the inline ``bool(on)`` rule)."""
    return bool(value)


@dataclass(frozen=True)
class Knob:
    """One declarative per-project knob (see the module docstring for the three rows).

    * ``name`` — the knob's stable identifier (``"model"``/``"effort"``/``"thinking"``); the
      key into :data:`PROJECT_KNOBS`.
    * ``field`` — the ``session_store`` record field a PERSISTED knob reads/writes
      (``"model"``/``"effort"``); ``""`` for a transient knob (``thinking`` — never on disk).
    * ``persisted`` — ``True`` iff the override is stored on the project (model/effort);
      ``False`` for the in-memory-only ``thinking`` (RB3 — the asymmetry the table must keep).
    * ``config_default`` — ``Config -> value | None``: the fallback when no override is set.
      model → ``config.model``; effort → ``None`` (NO global default); thinking → ``False``.
    * ``normalize`` — the value-normalizer (model: strip-or-None; effort: lower∩levels-or-None;
      thinking: bool). The SAME callable validates a set value and a read value.
    * ``session_identity`` — ``True`` iff the knob's freshly-resolved value is COMPARED in the
      warm-engine match-key, so a change forces a FRESH session to be built on the NEXT turn.
      ``effort`` and ``thinking`` are session-identity knobs (each baked at session-creation,
      not hot-swappable, and each has a built-with marker on the runtime). ``model`` is NOT:
      historically it is re-resolved at every fresh build but is **absent from the match-key**
      (a warm engine is reused regardless of a model change), so it carries no built-with marker
      — preserving that asymmetry byte-for-byte (design §5: "model is re-resolved each build,
      not stored as a marker").
    * ``runtime_marker`` — for a session-identity knob, the ``_ProjectRuntime`` attribute that
      records the value the live engine was BUILT with (``engine_effort``/``engine_thinking``);
      the looped match-key compares the freshly-resolved value to it. ``""`` for ``model``
      (no marker — it does not participate in the match-key).
    """

    name: str
    field: str
    persisted: bool
    config_default: Callable[["Config"], Optional[object]]
    normalize: Callable[[object], Optional[object]]
    session_identity: bool
    runtime_marker: str


#: The three per-project knobs, keyed by name. Each row reproduces today's exact behavior
#: (default source, normalization/validation, persistence, session-identity). ``model`` and
#: ``effort`` PERSIST (model defaults to ``config.model``, effort has NO default); ``thinking``
#: is TRANSIENT (``persisted=False`` — RB3, never written to disk). ``effort`` and ``thinking``
#: are session-identity knobs (a change rebuilds the session on the next turn); ``model`` is NOT
#: in the match-key (it is re-resolved each fresh build but never compared — the warm engine is
#: reused across a model change, as today).
PROJECT_KNOBS: dict[str, Knob] = {
    "model": Knob(
        name="model",
        field="model",
        persisted=True,
        config_default=lambda c: c.model,
        normalize=_norm_model,
        session_identity=False,  # NOT in the warm match-key (re-resolved per build, never compared)
        runtime_marker="",
    ),
    "effort": Knob(
        name="effort",
        field="effort",
        persisted=True,
        config_default=lambda c: None,  # NO CLAUDE_* global default for effort (the asymmetry)
        normalize=_norm_effort,
        session_identity=True,
        runtime_marker="engine_effort",
    ),
    "thinking": Knob(
        name="thinking",
        field="",  # transient — never persisted to the store (RB3)
        persisted=False,
        config_default=lambda c: False,
        normalize=_norm_thinking,
        session_identity=True,
        runtime_marker="engine_thinking",
    ),
}

#: The knobs that participate in SESSION IDENTITY, in a stable order (``effort`` then
#: ``thinking`` — insertion order over :data:`PROJECT_KNOBS`). The loop ``_ensure_engine`` runs
#: to decide warm-engine reuse-vs-rebuild compares each project-resolved value to the live
#: engine's built-with marker (``getattr(rt, knob.runtime_marker)``). A change to any of these
#: forces a fresh session on the NEXT turn (neither is hot-swappable). ``model`` is excluded
#: (``session_identity=False``) so a model change does NOT rebuild the warm engine — preserving
#: today's behavior exactly. The per-turn one-shot ``permission_mode`` is NOT a per-project knob
#: (it is not sticky), so it stays an explicit term in the match-key rather than a row here.
SESSION_KEY_KNOBS: tuple[Knob, ...] = tuple(
    knob for knob in PROJECT_KNOBS.values() if knob.session_identity
)
