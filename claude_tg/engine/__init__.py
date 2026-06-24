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
from .pending import DEFAULT_BACKSTOP_SECONDS, PendingRegistry
from .substrate import DecisionCallback, Substrate, SubstrateBAdapter
from .types import (
    DENIED_MESSAGE,
    AskEvent,
    Cancel,
    Decision,
    ErrorEvent,
    Event,
    FreeTextReply,
    ImageInput,
    ImageMediaType,
    PermissionDecision,
    PermissionEvent,
    PermissionVerdict,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
    decision_to_substrate,
    safe_input_summary,
)

__all__ = [
    "Engine",
    "PendingRegistry",
    "DEFAULT_BACKSTOP_SECONDS",
    "Substrate",
    "SubstrateBAdapter",
    "DecisionCallback",
    # events
    "TextEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "AskEvent",
    "PlanEvent",
    "PermissionEvent",
    "ErrorEvent",
    "ResultEvent",
    "StatusEvent",
    "Event",
    "safe_input_summary",
    # decisions
    "PermissionVerdict",
    "PermissionDecision",
    "QuestionAnswer",
    "PlanVerdict",
    "FreeTextReply",
    "Cancel",
    "Decision",
    "DENIED_MESSAGE",
    "SubstrateDecision",
    "decision_to_substrate",
    # multimodal input (P10 T1)
    "ImageInput",
    "ImageMediaType",
]
