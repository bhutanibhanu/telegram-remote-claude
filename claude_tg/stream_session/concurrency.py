"""Concurrency mixin — the process-global run-slot cap + per-chat FIFO queue (P5 / ADR-005 D6).

This is a behavior-preserving relocation of the slot/queue/cap method-group out of the former
single-file ``StreamingSession`` (see ``docs/features/core-refactor/design.md`` §6 T2). The
ADR-005 concurrency DESIGN is **unchanged** — the slot-transfer window, the FIFO drain, the
TOCTOU/zombie-run/slot-leak guards (D6/D9) all move here verbatim. :class:`ConcurrencyMixin`
holds the methods; they reach the foundation (``self._running``/``self.config``/``self._chats``/
``self._gated_send``) through ``self`` at runtime, resolved by the composed
:class:`~claude_tg.stream_session.core.StreamingSession`'s MRO — so there is no module-level
import of ``core`` (no cycle). It imports only the leaf ``runtime`` types.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from .runtime import _ChatState, _ProjectRuntime, _QueuedTurn
from .types import SendFn

if TYPE_CHECKING:
    # The foundation surface these methods consume — defined on ``StreamingSession.__init__``
    # (``core.py``), not in this file. Declared here as bare attribute annotations (``Callable``
    # for the consumed methods, NOT ``def`` stubs — those would create spurious override-
    # compatibility checks against core's real signatures) so the type-checker resolves
    # ``self._running`` etc. on the mixin without runtime cost (the composed instance carries
    # them via the MRO). Behavior-neutral; see design.md §1 "the one honest caveat".
    from collections.abc import Awaitable, Callable

    from ..config import Config

log = logging.getLogger(__name__)


class ConcurrencyMixin:
    """Slot-cap + FIFO-queue coordination (P5 / ADR-005 D6), relocated intact from ``core.py``.

    Mixed into :class:`~claude_tg.stream_session.core.StreamingSession` ahead of the base in the
    MRO. Every method here references the orchestration root's state/foundation through ``self``;
    the annotations below exist only for the type-checker.
    """

    if TYPE_CHECKING:
        config: Config
        _chats: dict[int, _ChatState]
        _running: int
        _gated_send: Callable[..., Awaitable[Optional[int]]]

    def active_run_count(self) -> int:
        """The number of turns currently RUNNING across the whole process (T2 /status).

        Mirrors the concurrency counter the queue/cap logic (D6) maintains — read-only. The
        cap is :attr:`config.max_concurrent_runs`; this is the live numerator the operator
        sees as ``N active / M max``. Process-global (the cap is per-deployment), matching how
        the queue admission is accounted.
        """
        return self._running

    def _queued_waiting(self, state: _ChatState) -> int:
        """The number of turns parked behind the cap in THIS chat's run queue (T6/P9).

        Pulled straight from the per-chat FIFO :attr:`~_ChatState.run_queue` (D6): the count
        of still-pending waiters (a drained/transferred entry has a done future, so it is
        excluded). The notification builders append a ``" (N more waiting)"`` counter when
        this is ≥1 so the operator knows work is backed up; 0 → no suffix. Read-only / pure
        (never mutates the queue, never raises) so it is safe to call on any send path. The
        counter is per-chat (the queue is per-chat — D6); the global RUNNING count is
        :meth:`active_run_count`.
        """
        return sum(1 for q in state.run_queue if not q.future.done())

    def queued_waiting(self, chat_id: int) -> int:
        """Public read-only view of :meth:`_queued_waiting` for a chat (T6/P9; ``/status``).

        Returns 0 for a chat with no state yet (RB1 — never creates anything). The bot's
        ``/status`` runs line uses this to show ``" (N more waiting)"`` alongside the
        ``N active / M max`` counts.
        """
        state = self._chats.get(chat_id)
        return self._queued_waiting(state) if state is not None else 0

    async def _acquire_slot(
        self, state: _ChatState, target_rt: _ProjectRuntime, *, send: SendFn
    ) -> None:
        """Acquire one process-global run slot — run now if under the cap, else QUEUE.

        The concurrency cap (``config.max_concurrent_runs``, D6) bounds how many turns RUN
        at once across the whole process. When the global :attr:`_running` count is below
        the cap, increment it and return immediately (run now). When AT the cap, the turn
        is **accepted and queued** (never refused / dropped — SB6 fail-closed → queue):

        * mark this project ``queued`` for ``/projects`` (T4 status / T7 render),
        * send a **one-time** ``⏳ queued behind N run(s)`` notice (D6 — N is the number of
          slot-holders ahead, i.e. the cap; richer live position is deferrable),
        * append a waiter :class:`asyncio.Future` to this chat's FIFO :attr:`run_queue` and
          ``await`` it. A finishing run pops the OLDEST waiter and **transfers** it the freed
          slot via :meth:`_release_slot` (which re-increments :attr:`_running` and resolves
          the future) — so on wake the slot is already counted as held and this turn just
          proceeds. FIFO order is preserved (``popleft`` of the oldest).

        Returns once a slot is held; the caller MUST release it exactly once (the
        ``handle_message`` ``finally`` → :meth:`_release_slot`). The counter is global; the
        queue is per-chat (no cross-chat semantics — D6).
        """
        cap = self.config.max_concurrent_runs
        if self._running < cap:
            self._running += 1
            return
        # At the cap → queue this turn (FIFO) and park until a slot is transferred to it.
        # Mark the project queued so /projects shows it (the turn has not started running).
        target_rt.status = "queued"
        # NB2: turns AHEAD of this one = the slot-holders RUNNING (== the cap when full) PLUS
        # any turns already QUEUED ahead of it (counted BEFORE this turn's entry is appended
        # below). Counting only ``_running`` would tell a turn queued behind other queued
        # turns the wrong position (always "behind <cap>"). The queue is per-chat, so only
        # this chat's already-queued turns precede it.
        ahead = self._running + len(state.run_queue)
        waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        # Record the parked turn WITH its target runtime so /cancel + /rm can drain it (T9).
        queued = _QueuedTurn(runtime=target_rt, future=waiter)
        state.run_queue.append(queued)
        # One-time queued notice (D6). Best-effort: a failed notice must not strand the turn
        # in the queue (the wait below is what actually gates it), so swallow a send error.
        # Operator-facing → verbatim priority through the D8 send gate.
        try:
            await self._gated_send(
                state, send, verbatim=True,
                text=f"⏳ Queued behind {ahead} run(s) — it'll start when a slot frees.",
                reply_markup=None,
                parse_mode=None,
            )
        except Exception:
            log.debug("queued-notice send failed (turn still queued)", exc_info=True)
        # Park until a finishing run hands us the slot (it re-incremented _running for us).
        # If the wait is cancelled — shutdown, the awaiting task torn down, OR a /cancel|/rm
        # DRAIN of this queued project (T9: handle_cancel/_drain_queued cancels this future
        # before the turn ever runs, so no zombie run when a slot frees) — we must not leak:
        # either we were still queued (drop our entry — we never held a slot), or a
        # _release_slot had ALREADY transferred us the slot (our future is resolved, the
        # counter holds it for us) — in which case hand that slot straight back on
        # (_release_slot transfers it to the next waiter or decrements). Either way the
        # global count stays correct; the CancelledError then propagates (the turn is gone).
        try:
            await waiter
        except asyncio.CancelledError:
            removed = self._remove_queued(state, waiter)
            if not removed and waiter.done() and not waiter.cancelled():
                # Not in the queue → a transfer resolved our future a tick before the cancel
                # landed; that slot is counted as held for us, so release it (not leak it).
                self._release_slot(state)
            raise

    def _release_slot(self, state: _ChatState) -> None:
        """Release the current turn's run slot — decrement, or TRANSFER to the next waiter.

        Called exactly once per running turn from ``handle_message``'s ``finally`` (every
        exit path — normal end, mid-stream raise, cancel, resume-failure). The slot-leak
        safety contract (the flagged D6 hazard): a turn that consumed a slot ALWAYS reaches
        here, so capacity can never permanently shrink; and it adjusts :attr:`_running` by
        exactly one net step (either ``-1`` to free, or ``0`` because the slot is handed
        straight to a waiter), so the counter never drifts.

        FIFO dequeue: look for the OLDEST queued waiter — this chat's queue first, then any
        other chat's (the cap is global, so a slot freed here may unblock a turn queued in
        another chat; per-chat queues keep order within a chat). If one exists, **transfer**
        the slot to it: keep :attr:`_running` as-is (the slot stays held, now by the waiter)
        and resolve its future (waking the parked :meth:`_acquire_slot`). If none, simply
        decrement (the slot is now free). Pure + non-awaiting + never raises, so it cannot
        itself leak a slot or mask the turn's exception.
        """
        queued = self._pop_next_waiter(state)
        if queued is not None:
            # Transfer: the freed slot stays counted (now held by the woken turn). Do NOT
            # decrement — set the waiter's result so its parked _acquire_slot returns.
            queued.future.set_result(None)
            return
        # No one waiting → the slot is free. Decrement, clamped at 0 (defensive: a double
        # release must never drive the count negative and wrongly grant extra capacity).
        if self._running > 0:
            self._running -= 1

    def _pop_next_waiter(self, state: _ChatState) -> "Optional[_QueuedTurn]":
        """Pop the oldest still-pending queued turn (this chat first, then any), FIFO.

        Skips any already-cancelled/done futures (a queued turn whose task was torn down or
        DRAINED by /cancel|/rm — its CancelledError handler removes it, but a race could
        leave a settled future), so a transferred slot always goes to a LIVE waiter. Returns
        ``None`` when no chat has a pending waiter (the slot is then freed by the caller).
        """
        # This chat's queue first (preserve its FIFO order), then every other chat's.
        queues = [state.run_queue]
        queues.extend(s.run_queue for s in self._chats.values() if s is not state)
        for q in queues:
            while q:
                queued = q.popleft()
                if not queued.future.done():
                    return queued
        return None

    @staticmethod
    def _remove_queued(
        state: _ChatState, waiter: "asyncio.Future[None]"
    ) -> bool:
        """Remove the queue entry whose future is ``waiter``; return whether one was found.

        Used by :meth:`_acquire_slot`'s CancelledError handler (the parked turn was torn
        down / drained) to drop its own entry. ``False`` means it was not queued (a transfer
        already popped it), telling the caller to release the slot it now implicitly holds.
        """
        for i, queued in enumerate(state.run_queue):
            if queued.future is waiter:
                del state.run_queue[i]
                return True
        return False

    @staticmethod
    def _is_queued(state: _ChatState, rt: _ProjectRuntime) -> bool:
        """Whether ``rt`` already has a turn WAITING in the run queue (BLOCKER 2 guard).

        A turn that queued behind the cap parks on a waiter in :meth:`_acquire_slot` and
        holds NO lock until its slot is granted, so :meth:`is_busy` (lock-based) reports it
        idle. The pre-slot busy-guard uses this so a SECOND message to an already-queued
        project is refused (``StreamingBusy``) rather than appending a second
        :class:`_QueuedTurn` — one pending turn per project (D6). A finished/cancelled
        waiter (``future.done()``) does not count: its turn is no longer pending (its
        CancelledError handler removes the entry, but a settled-but-not-yet-popped future
        must not block a fresh turn).
        """
        return any(
            q.runtime is rt and not q.future.done() for q in state.run_queue
        )

    @staticmethod
    def _drain_queued(state: _ChatState, rt: _ProjectRuntime) -> int:
        """Cancel every QUEUED (not-yet-running) waiter belonging to ``rt`` (T9 drain).

        A queued turn parks on its waiter inside :meth:`_acquire_slot` before it ever
        reaches ``_drive_turn`` — it has no live engine and no pending-index entries yet, so
        the ONLY thing holding it is the future. Cancelling that future wakes its
        ``_acquire_slot`` into the CancelledError path, which removes the entry from the
        queue and releases any slot already transferred to it — so a cancelled/removed
        queued project can never spring to a "zombie run" when a slot frees (the T6-review
        hazard). We cancel the future and leave the queue mutation to that handler (so the
        slot-accounting stays in one place); a defensive ``status`` reset to ``idle`` covers
        the case where the parked task has not yet been scheduled to run its handler.

        Returns the number of queued waiters drained (NB1: the caller counts these as
        cancelled units so a queued-only ``/cancel`` reports the turn it really aborted).
        Normally 0 or 1 (one pending turn per project — BLOCKER 2), but it drains every
        matching waiter defensively.
        """
        drained = 0
        for queued in list(state.run_queue):
            if queued.runtime is rt and not queued.future.done():
                queued.future.cancel()
                drained += 1
        if drained and rt.status == "queued":
            rt.status = "idle"
        return drained
