"""P1 interactive streaming session engine (Substrate A, normalized interface).

The engine replaces the one-shot ``claude -p`` runner with a persistent, streaming
session behind the contract drafted in
`spikes/session-substrate/normalized_interface.md` and decided in ADR-001 (Substrate
A = ``claude-agent-sdk`` primary; B = documented slot). Selected at runtime by
``ENGINE_MODE`` (``oneshot`` default -> ``streaming``); wiring into ``bot.py`` is T7.

Importing this package is **SDK-free**: ``claude_agent_sdk`` is imported lazily inside
:mod:`claude_tg.engine.adapter_sdk`, so the package (and the mock-based unit tests)
import without the SDK installed or a CLI present.

Public surface:

* types        — :mod:`claude_tg.engine.types` (events out, decisions in, the
                 ``decision_to_substrate`` mapping)
* the seam     — :class:`claude_tg.engine.substrate.Substrate` (+ the B slot)
* adapter A    — :class:`claude_tg.engine.adapter_sdk.SdkSubstrate` + ``normalize``
* the engine   — :class:`claude_tg.engine.engine.Engine`
"""

from __future__ import annotations

from .engine import Engine
from .substrate import DecisionCallback, Substrate, SubstrateBAdapter
from .types import (
    AskEvent,
    Cancel,
    Decision,
    ErrorEvent,
    Event,
    FreeTextReply,
    PermissionVerdict,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ToolUseEvent,
    decision_to_substrate,
)

__all__ = [
    "Engine",
    "Substrate",
    "SubstrateBAdapter",
    "DecisionCallback",
    # events
    "TextEvent",
    "ToolUseEvent",
    "AskEvent",
    "PlanEvent",
    "ErrorEvent",
    "ResultEvent",
    "StatusEvent",
    "Event",
    # decisions
    "PermissionVerdict",
    "QuestionAnswer",
    "PlanVerdict",
    "FreeTextReply",
    "Cancel",
    "Decision",
    "SubstrateDecision",
    "decision_to_substrate",
]
