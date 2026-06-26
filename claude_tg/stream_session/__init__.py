"""Streaming-mode driver package (the collaborator ``bot.py`` delegates to when
``ENGINE_MODE=streaming``).

This package replaces the former single-file ``claude_tg/stream_session.py`` (a
behavior-preserving relocation — see ``docs/features/core-refactor/design.md``). The import
path ``claude_tg.stream_session`` is preserved EXACTLY: this ``__init__`` re-exports every
symbol that ``bot.py``, ``app.py``, or any test previously imported from the module, so every
``from claude_tg.stream_session import …`` line keeps working unchanged.

Layout:

* ``types.py``   — leaf types/constants/pure helpers (no ``self``, no runtime-dataclass dep).
* ``runtime.py`` — runtime dataclasses + module-level pure helpers + the engine factory.
* ``core.py``    — :class:`StreamingSession`, the orchestration root.

The re-exports below are deliberately unused *in this file* (they exist only to republish the
moved names at the package's public path); the per-file ``# noqa: F401`` keeps ruff quiet.
"""

from __future__ import annotations

from .core import StreamingSession
from .runtime import (  # noqa: F401  (re-export hub — see module docstring)
    EngineFactory,
    _ChatState,
    _default_engine_factory,
    _footer_only_result,
    _is_resume_failure_event,
    _pending_kind_of,
    _PendingRef,
    _ProjectRuntime,
    _QueuedTurn,
    _resume_failure_text,
    _TurnDedup,
)
from .types import (  # noqa: F401  (re-export hub — see module docstring)
    _AWAITING_STATUS,
    _PERMISSION_NOTES,
    _PERMISSION_VERDICTS,
    AttachOutcome,
    CallbackOutcome,
    DeleteFn,
    EditFn,
    PendingKind,
    PermissionVerdictName,
    PinFn,
    SendFn,
    StreamingBusy,
    UnpinFn,
    WatchOutcome,
    _basename_of,
    _sanitize_attach_name,
)

__all__ = [
    "StreamingSession",
    "StreamingBusy",
    "AttachOutcome",
    "WatchOutcome",
    "CallbackOutcome",
    "EngineFactory",
    "SendFn",
    "EditFn",
    "DeleteFn",
]
