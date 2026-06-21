"""The ``Substrate`` adapter seam — one logical contract, swappable backends.

`normalized_interface.md` §4 establishes that both candidate substrates expose the
**same logical contract**: a ``start/resume/send/stop`` lifecycle, an outbound
stream of normalized events, and a permission/decision seam (a ``can_use_tool``-style
callback the engine supplies). This module is that seam as a typed
:class:`typing.Protocol`, so the engine drives any backend uniformly and a fake
substrate satisfies it for unit tests.

ADR-001 (and design S1) decide: **Substrate A (`claude-agent-sdk`) is built**
(:mod:`claude_tg.engine.adapter_sdk`); **Substrate B (raw CLI ``stream-json``) is a
documented slot only** — proven as a fallback in P0 but NOT implemented in P1
(YAGNI; add only if A regresses). The slot lives here, at the bottom, as
:class:`SubstrateBAdapter` (raises ``NotImplementedError``).

The permission seam is deliberately substrate-neutral: the engine hands the adapter
a callback that, given ``(tool_name, tool_input, tool_use_id)``, returns a
:class:`~claude_tg.engine.types.SubstrateDecision` (the allow/deny triple). Each
adapter renders that triple to its own wire type — no SDK or CLI shape appears in
this Protocol.
"""

from __future__ import annotations

from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
    Protocol,
    runtime_checkable,
)

from .types import Event, SubstrateDecision

#: The permission/decision seam. The engine supplies this; the adapter calls it
#: when the substrate asks to use a tool (or raises an interactive AskUserQuestion /
#: ExitPlanMode, which arrive through the same per-request channel). It returns the
#: neutral allow/deny triple, which the adapter renders to its wire type.
#:
#: In T4 the engine's default implementation is synchronous-ish (returns promptly so
#: the lifecycle is testable). T5 replaces the body with the async answer-hold
#: (a pending-decision Future awaited here), per ADR-002 — but the *seam shape* (this
#: signature) does not change, which is the point of defining it now.
DecisionCallback = Callable[
    [str, dict[str, Any], Optional[str]],
    Awaitable[SubstrateDecision],
]


@runtime_checkable
class Substrate(Protocol):
    """A persistent, single-active-run Claude session backend.

    One instance owns one session (one underlying connection) between
    ``start()``/``resume()`` and ``stop()``. Not thread-safe; drive from one
    asyncio task (mirrors the proven harness contract).

    ``session_id`` is populated during/after the first ``send`` (captured from the
    substrate's init/result frames) and is the value the engine persists with the
    cwd (the ``(session_id, cwd)`` coupling, ADR-001 / C6).
    """

    #: The Claude session id, or None before the substrate reports one.
    session_id: Optional[str]

    async def start(self) -> None:
        """Establish a fresh persistent session (host CLI auth; no API key)."""
        ...

    async def resume(self, session_id: str) -> None:
        """Re-attach to an existing session by id.

        Per ADR-001 / C6 the id is **cwd/project-scoped** — the engine must resume
        only from the original cwd (it owns the ``(session_id, cwd)`` coupling and
        the double-attach guard; the substrate does not enforce either).
        """
        ...

    def send(self, prompt: str, *, timeout: float = 120.0) -> AsyncIterator[Event]:
        """Send one operator turn; async-yield **normalized events** out.

        Returns an async iterator (so the engine can ``async for`` it). Iteration
        ends after the turn's terminal result event. The turn is **bounded** by
        ``timeout`` — on timeout/failure the adapter yields a ``driver_error``
        :class:`~claude_tg.engine.types.ErrorEvent` and stops, never hanging (RB2).
        """
        ...

    async def stop(self) -> None:
        """Disconnect and tear down the session. Idempotent (RB2/cancel)."""
        ...


# ---------------------------------------------------------------------------
# Substrate B — DOCUMENTED SLOT ONLY (NOT built in P1; design S1 / ADR-001)
# ---------------------------------------------------------------------------


class SubstrateBAdapter:
    """SLOT: the raw ``claude`` CLI ``stream-json`` control-protocol substrate.

    **Intentionally NOT implemented in P1.** Substrate B was proven in P0 as a
    credible fallback for the make-or-break C2/C3/C4 (`evidence/c2_cli.*`,
    `c3_cli.*`, `c4_cli.*`), but ADR-001 chose A as primary on maintainability, and
    design S1 scopes P1 to the A adapter with B as a documented slot (YAGNI — add it
    only if A becomes unavailable or regresses).

    A real implementation would (per `normalized_interface.md` / `harness_cli.py`):

    * spawn ``claude --output-format stream-json --verbose -p --input-format
      stream-json [--permission-mode …] --permission-prompt-tool stdio …`` and run
      the ``initialize`` control handshake before the first ``send``;
    * parse NDJSON ``system``/``assistant``/``user``/``stream_event``/``result``
      frames into the same :class:`~claude_tg.engine.types.Event` types ``normalize``
      produces for A (the contract is substrate-neutral);
    * answer ``can_use_tool`` **control_requests** by writing a ``control_response``
      built from the engine's :class:`~claude_tg.engine.types.SubstrateDecision`
      — and on **allow MUST set ``updatedInput`` to a record** (the ZodError gotcha,
      [FLAG]); a missing/changed ``--permission-prompt-tool stdio`` flag (undocumented)
      must **fail closed** (SB6), never fall open to auto-allow;
    * bound every turn by total + idle timeout and fail clean (RB2).

    It conforms to the :class:`Substrate` Protocol so it can drop in behind the same
    engine with no engine change — that is the whole point of the seam.
    """

    session_id: Optional[str] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "Substrate B (raw CLI stream-json) is a documented slot only in P1 "
            "(design S1 / ADR-001): A is primary, B is the proven fallback added "
            "only if A regresses. See this class's docstring for the build sketch."
        )

    async def start(self) -> None:  # pragma: no cover - slot
        raise NotImplementedError

    async def resume(self, session_id: str) -> None:  # pragma: no cover - slot
        raise NotImplementedError

    def send(  # pragma: no cover - slot
        self, prompt: str, *, timeout: float = 120.0
    ) -> AsyncIterator[Event]:
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - slot
        raise NotImplementedError


__all__ = ["Substrate", "DecisionCallback", "SubstrateBAdapter"]
