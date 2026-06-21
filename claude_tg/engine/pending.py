"""The async answer-hold — a pending-decision registry per ADR-002.

This is the mechanism the T1 de-risk spike proved and ADR-002 decided: an
engine-side ``PendingDecision`` :class:`asyncio.Future` per interactive request
(keyed by ``tool_use_id``), **awaited inside the substrate's** ``can_use_tool``
**callback**, resolved by exactly one of:

* **(a)** the operator's decision routed in from Telegram (T7 calls
  :meth:`PendingRegistry.resolve`),
* **(b)** a harness-side **backstop timer** (auto-resolve → DENY + notify; default
  60 min, configurable), or
* **(c)** ``/cancel`` (:meth:`PendingRegistry.cancel`, a clean abort → deny).

The engine **never relies on the SDK to bound the hold** — the backstop is the
engine's own :class:`asyncio` timer racing the operator :class:`asyncio.Future`
(ADR-002 §Decision; the SDK ``await``s the inbound ``can_use_tool`` control request
with no ``fail_after``). The whole module is substrate-neutral: it deals only in
:class:`~claude_tg.engine.types.Decision` (the resolution) and never touches an SDK
or CLI shape.

Concurrency model (single asyncio loop, mirrors the rest of the engine):

* :meth:`await_decision` registers a Future and awaits it bounded by the backstop —
  it runs *inside* the decision callback, which is itself a task the SDK spawns.
* :meth:`resolve` / :meth:`cancel` are called from "outside" (the bot's callback
  handler, T7) on the **same loop**; setting the Future unblocks the awaiting
  callback. Both are no-ops on an unknown / already-resolved id so a stray tap can
  **never crash** the engine (RB1).
* The backstop is a child task created per pending request; whichever of (operator
  Future / backstop) completes first wins, and the loser is cancelled. There is no
  busy-wait and no real long sleep on the hot path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .types import Cancel, Decision, PermissionVerdict

log = logging.getLogger(__name__)

#: Default backstop: 60 minutes (decision-log #4 / ADR-002). Configurable via
#: ``config.py`` (``ANSWER_BACKSTOP_SECONDS``); injectable per-request in tests so a
#: backstop firing is exercised with a SHORT interval — never a real 60-min wait.
DEFAULT_BACKSTOP_SECONDS: float = 3600.0

#: A "notify" sink the registry calls when the backstop fires (so the engine can
#: surface a status/error event to the operator). Given the pending key + reason.
NotifyHook = Callable[[str, str], Awaitable[None]]


def _backstop_decision(reason: str) -> Decision:
    """The decision a backstop expiry resolves to: a DENY carrying the reason.

    Per ADR-002 the backstop auto-resolves the pending request as **DENY + notify**
    and leaves the session usable (proven in T1.5). A plain ``Cancel`` would also map
    to a deny, but a ``PermissionVerdict(deny)`` lets us carry an explicit operator-
    facing reason through the single ``decision_to_substrate`` mapper.
    """
    return PermissionVerdict(behavior="deny", message=reason)


@dataclass
class _Pending:
    """One in-flight request awaiting a decision (internal registry record)."""

    tool_use_id: str
    tool_name: str
    future: "asyncio.Future[Decision]"
    backstop_task: Optional["asyncio.Task[None]"] = field(default=None)


class PendingRegistry:
    """Registry of pending interactive decisions, keyed by ``tool_use_id``.

    One instance per :class:`~claude_tg.engine.engine.Engine`. Drive from a single
    asyncio task/loop (the engine is single-session, not thread-safe — mirrors the
    substrate contract). The registry owns the Future lifecycle and the per-request
    backstop timer; it does **not** map decisions to substrate verdicts (that stays in
    the single ``decision_to_substrate`` mapper, called by the engine).
    """

    def __init__(
        self,
        *,
        backstop_seconds: float = DEFAULT_BACKSTOP_SECONDS,
        notify: Optional[NotifyHook] = None,
    ) -> None:
        if backstop_seconds <= 0:
            raise ValueError("backstop_seconds must be positive")
        self._backstop_seconds = backstop_seconds
        self._notify = notify
        self._pending: dict[str, _Pending] = {}

    # -- the hold (called from inside the decision callback) -----------------

    async def await_decision(
        self,
        tool_use_id: str,
        tool_name: str,
        *,
        backstop_seconds: Optional[float] = None,
    ) -> Decision:
        """Register a pending decision for ``tool_use_id`` and await its resolution.

        Returns the :class:`~claude_tg.engine.types.Decision` produced by exactly one
        of: the operator (:meth:`resolve`), the backstop timer, or :meth:`cancel`.
        Bounded by the backstop (``backstop_seconds`` overrides the registry default —
        tests pass a SHORT interval so the backstop path runs with no real wait).

        The operator Future and the backstop timer are raced; whichever resolves first
        wins and the other is cancelled. The pending entry is always cleared on the way
        out (success, backstop, or cancel) so a key never leaks.
        """
        loop = asyncio.get_running_loop()
        timeout = self._backstop_seconds if backstop_seconds is None else backstop_seconds
        if timeout <= 0:
            raise ValueError("backstop_seconds must be positive")

        future: asyncio.Future[Decision] = loop.create_future()
        pending = _Pending(tool_use_id=tool_use_id, tool_name=tool_name, future=future)
        # Last-writer-wins on a duplicate id would orphan an earlier waiter; in the
        # single-session model ids are unique, but guard anyway (RB1): cancel any prior.
        prior = self._pending.get(tool_use_id)
        if prior is not None and not prior.future.done():
            prior.future.set_result(Cancel())
        self._pending[tool_use_id] = pending

        pending.backstop_task = loop.create_task(
            self._backstop(tool_use_id, timeout), name=f"backstop:{tool_use_id}"
        )
        try:
            return await future
        finally:
            # Whoever resolved us, stop the backstop and drop the entry.
            self._cancel_backstop(pending)
            # Only remove if it's still *our* entry (a re-register replaced it).
            if self._pending.get(tool_use_id) is pending:
                del self._pending[tool_use_id]

    # -- resolution from "outside" (bot callback handler, T7) ----------------

    def resolve(self, tool_use_id: str, decision: Decision) -> bool:
        """Resolve a pending decision (operator's answer/verdict). RB1-safe.

        Routes ``decision`` to the request keyed by ``tool_use_id``; the awaiting
        callback in :meth:`await_decision` unblocks and returns it. An **unknown or
        already-resolved id is a no-op** that returns ``False`` and logs at debug —
        it never raises, so a stray/late button tap cannot crash the engine (RB1).
        Returns ``True`` iff a waiting request was resolved by this call.
        """
        pending = self._pending.get(tool_use_id)
        if pending is None:
            log.debug("resolve(): no pending decision for id=%s (ignored)", tool_use_id)
            return False
        if pending.future.done():
            log.debug("resolve(): id=%s already resolved (ignored)", tool_use_id)
            return False
        pending.future.set_result(decision)
        return True

    def cancel(self, tool_use_id: Optional[str] = None) -> int:
        """Cancel a specific pending request, or ALL of them, as a clean abort (RB4).

        Resolves the pending Future(s) with :class:`~claude_tg.engine.types.Cancel`, so
        the awaiting callback returns a clean *deny* (``decision_to_substrate`` maps
        Cancel → deny) and the session is **not wedged** — the SDK gets a prompt deny
        for that request rather than a hung callback. With ``tool_use_id=None`` every
        pending request is cancelled (used by the engine's turn-level ``cancel()``).
        Unknown id is a no-op. Returns the number of requests resolved.
        """
        if tool_use_id is not None:
            pending = self._pending.get(tool_use_id)
            if pending is None or pending.future.done():
                log.debug("cancel(): nothing to cancel for id=%s", tool_use_id)
                return 0
            pending.future.set_result(Cancel())
            return 1

        count = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(Cancel())
                count += 1
        return count

    # -- introspection (engine/tests) ----------------------------------------

    def has_pending(self, tool_use_id: str) -> bool:
        p = self._pending.get(tool_use_id)
        return p is not None and not p.future.done()

    @property
    def pending_ids(self) -> list[str]:
        return [k for k, p in self._pending.items() if not p.future.done()]

    # -- internals -----------------------------------------------------------

    async def _backstop(self, tool_use_id: str, timeout: float) -> None:
        """Per-request timer: after ``timeout`` seconds, auto-resolve DENY + notify.

        Sleeps the backstop interval, then resolves the pending request as
        ``DENY`` (the awaiting callback returns it) and fires the notify hook so the
        engine can emit an operator-facing event. Cancelled (no-op) when the operator
        or a cancel resolves first. The sleep is the ONLY wait; tests inject a tiny
        ``timeout`` so it fires deterministically with no real delay.
        """
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return  # operator (or cancel) won the race; nothing to do
        pending = self._pending.get(tool_use_id)
        if pending is None or pending.future.done():
            return
        reason = f"answer backstop reached ({timeout:.0f}s); auto-denied"
        pending.future.set_result(_backstop_decision(reason))
        if self._notify is not None:
            try:
                await self._notify(tool_use_id, reason)
            except Exception:  # noqa: BLE001 - a notify failure must not wedge the hold
                log.exception("backstop notify hook failed for id=%s", tool_use_id)

    @staticmethod
    def _cancel_backstop(pending: _Pending) -> None:
        task = pending.backstop_task
        if task is not None and not task.done():
            task.cancel()


__all__ = ["PendingRegistry", "DEFAULT_BACKSTOP_SECONDS", "NotifyHook"]
