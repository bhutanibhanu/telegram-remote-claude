"""The proactive-scheduler DRIVER — the single asyncio firing task (P14 T-FIRE).

This is the runtime half of the proactive scheduler. The pure model + math live in
:mod:`claude_tg.scheduler` (``Schedule`` / ``parse_interval`` / ``due_schedules`` /
``next_wake`` / ``Schedule.compute_next_run``); persistence lives in
:class:`~claude_tg.session_store.JsonSessionStore` (``all_schedules`` / ``add_schedule``
/ ``rearm_all_schedules``); and the actual gated turn is driven by
:meth:`~claude_tg.stream_session.StreamingSession.fire_schedule`. THIS module owns ONLY
the loop that decides *when* and calls a ``fire`` callback — so it is trivially
unit-testable with an injected clock + sleep + a fake fire (no PTB, no SDK, no real time),
mirroring the injected-clock pattern the rest of the project already uses
(``StreamingSession``'s ``clock``/``sleep``, the live-mirror loop).

**The loop (design §3.2).** On :meth:`start` it FIRST re-arms every persisted schedule's
``next_run`` to ``now + interval`` (``store.rearm_all_schedules`` — design §5.5: a window
that elapsed while the bot was DOWN is NEVER replayed as a burst). Then it loops: read the
current schedules, find the due (non-paused) ones (:func:`~claude_tg.scheduler.due_schedules`),
fire each, reschedule each (``next_run = now + interval`` via
:meth:`~claude_tg.scheduler.Schedule.compute_next_run`, persisted), and sleep until the next
wake (:func:`~claude_tg.scheduler.next_wake`) — or until a poll fallback when nothing is
scheduled (so a schedule created AFTER the loop slept is still noticed without an explicit
wake-up).

**⭐ RB1-TOTAL (the make-or-break robustness invariant).** A fire that raises — or one bad
schedule, or a store hiccup — is caught, logged body-free, and SKIPPED; it NEVER kills the
loop or the bot. The fire callback (``StreamingSession.fire_schedule``) is itself RB1-total
(it catches its own turn errors + audits a skip), and this loop wraps every fire AND the
per-tick body in its own guard as belt-and-braces, so a single misbehaving schedule can never
take the scheduler down. Cancellation (:meth:`stop`, fired from ``post_shutdown``) unwinds the
loop cleanly with no orphaned task.

**The security posture is NOT here.** The force-gate (an unattended turn can never inherit
``/yolo``) lives in the engine + ``fire_schedule``; this driver only decides timing. It
deliberately holds NO permission state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from .scheduler import Schedule, due_schedules, next_wake

log = logging.getLogger(__name__)

#: The default poll cadence (seconds) the loop falls back to when it cannot compute a
#: precise next-wake — i.e. when there are NO enabled schedules (``next_wake`` is ``None``)
#: OR after a tick, so a schedule CREATED while the loop slept is still picked up within one
#: poll. Small enough to feel responsive, large enough to be nearly free when idle. The fire
#: path itself is gated + per-chat rate-limited, so this is not a flood lever.
DEFAULT_POLL_INTERVAL_SECONDS = 30.0

#: A hard floor on any computed sleep so a due/overdue schedule (``next_wake <= now``) yields
#: a tiny non-negative wait rather than a busy-spin, and a clock skew can't ask for a negative
#: sleep. The loop fires the due tasks on the NEXT iteration regardless; this just bounds the
#: wait between iterations.
_MIN_SLEEP_SECONDS = 0.0


# The fire seam: given a schedule, drive it as a proactive turn (RB1-total — never raises).
FireFn = Callable[[Schedule], Awaitable[bool]]


class Scheduler:
    """The single asyncio task that fires due proactive schedules (P14 T-FIRE).

    Construct ONE per bot (wired in ``bot.py``: started in ``post_init``, cancelled in
    ``post_shutdown``). Collaborators are injected so the loop is pure-timing + testable:

    * ``store`` — anything exposing ``all_schedules()`` (the cross-chat due view),
      ``add_schedule(schedule)`` (used to persist a reschedule — it overwrites by name) and
      ``rearm_all_schedules(now)`` (the start-time no-replay re-arm). In production this is the
      :class:`~claude_tg.session_store.JsonSessionStore`.
    * ``fire`` — an ``async (Schedule) -> bool`` that drives ONE schedule as a gated proactive
      turn. In production this is :meth:`~claude_tg.stream_session.StreamingSession.fire_schedule`
      (already RB1-total). Tests pass a recorder.
    * ``clock`` / ``sleep`` — the time source + awaitable wait (default real); injected so tests
      drive the loop deterministically with no real time (the project-wide pattern).
    * ``poll_interval`` — the idle/poll fallback cadence (see
      :data:`DEFAULT_POLL_INTERVAL_SECONDS`).

    The driver holds NO schedule state of its own — the store is the source of truth, read
    fresh each tick — so a ``/every`` / ``/pause`` / ``/unschedule`` between ticks is honored
    on the next tick with no cache to invalidate.
    """

    def __init__(
        self,
        *,
        store,
        fire: FireFn,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._fire = fire
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = float(poll_interval) if poll_interval and poll_interval > 0 else DEFAULT_POLL_INTERVAL_SECONDS
        self._task: Optional[asyncio.Task[None]] = None

    # -- lifecycle (wired into bot.py post_init / post_shutdown) --------------

    def start(self) -> None:
        """Re-arm persisted schedules, then start the firing loop as a background task.

        Idempotent: a second call while the loop is live is a no-op (one driver per bot). The
        re-arm (``store.rearm_all_schedules(now)``) happens HERE, BEFORE the loop's first tick,
        so a window that elapsed while the bot was down is never replayed as a burst (design
        §5.5). RB1: a re-arm failure is logged + swallowed (the loop still starts — a schedule
        that couldn't re-arm simply fires on its stale-then-next cadence; it never blocks
        startup). The task is created on the CURRENT running loop (``post_init`` runs inside
        ``app.run_polling``'s loop), mirroring the live-mirror task pattern.
        """
        if self._task is not None and not self._task.done():
            return
        try:
            self._store.rearm_all_schedules(self._clock())
        except Exception:  # a re-arm hiccup must never block the bot from starting (RB1)
            log.warning("scheduler re-arm on start failed (ignored)", exc_info=True)
        self._task = asyncio.ensure_future(self._run())
        log.info("proactive scheduler started")

    async def stop(self) -> None:
        """Cancel the firing loop cleanly (idempotent) — wired into ``post_shutdown``.

        Cancels the background task (if any) and awaits its unwind so no orphaned task
        outlives the bot (RB1). Best-effort: a cancellation/await error is swallowed so a
        crash on exit never blocks shutdown. Safe to call when never started (no-op).
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - the loop already swallows its own errors
            log.debug("scheduler task raised during shutdown (ignored)", exc_info=True)
        log.info("proactive scheduler stopped")

    @property
    def running(self) -> bool:
        """Whether the firing loop task is live (read-only; for tests / introspection)."""
        return self._task is not None and not self._task.done()

    # -- the loop -------------------------------------------------------------

    async def _run(self) -> None:
        """The firing loop: fire due schedules, reschedule them, sleep to the next wake.

        RB1-TOTAL: the per-tick body is wrapped so a fire/reschedule/read error is logged +
        swallowed and the loop CONTINUES — one bad schedule (or a transient store error) can
        never kill the loop. Only :class:`asyncio.CancelledError` (from :meth:`stop`) breaks
        out, re-raised so the task unwinds cleanly. The sleep between ticks is the
        ``next_wake``-derived wait (or the poll fallback), so the loop is idle-cheap.
        """
        while True:
            try:
                wait = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - _tick is itself guarded; belt-and-braces
                log.exception("scheduler tick failed (loop continues)")
                wait = self._poll_interval
            try:
                await self._sleep(max(_MIN_SLEEP_SECONDS, wait))
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> float:
        """Run ONE scheduling tick; return the seconds to sleep before the next.

        Reads the schedules fresh from the store (the source of truth — so a CRUD change
        between ticks is honored), fires every due (non-paused) one, reschedules each, and
        returns the wait until the next wake. RB1: the read + each fire + each reschedule is
        individually guarded; a failure on one schedule never stops the others or the tick.
        """
        now = self._clock()
        try:
            schedules = list(self._store.all_schedules())
        except Exception:  # a store read hiccup → treat as "nothing due", poll again (RB1)
            log.warning("scheduler could not read schedules this tick (ignored)", exc_info=True)
            return self._poll_interval

        for schedule in due_schedules(schedules, now):
            # Fire (RB1-total in fire_schedule; guard here too as belt-and-braces) THEN
            # reschedule — the reschedule must happen even if the fire skipped (busy) / errored,
            # so a due schedule always advances to its next interval and is never re-fired in a
            # tight loop. Anchored on NOW (compute_next_run), so no burst catch-up (design §5.5).
            try:
                await self._fire(schedule)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - fire_schedule is already RB1-total
                log.exception("scheduler fire raised for %s (loop continues)", schedule.name)
            self._reschedule(schedule, now)

        # Recompute the next wake from the FRESH post-reschedule view so a just-fired schedule's
        # advanced next_run is reflected; fall back to the poll cadence when nothing is enabled
        # (or to pick up a schedule created while we sleep).
        try:
            updated = list(self._store.all_schedules())
        except Exception:
            updated = []
        wake_at = next_wake(updated, now)
        if wake_at is None:
            return self._poll_interval
        # Cap the wait at the poll interval so a far-future single schedule doesn't blind the
        # loop to a NEWLY created near-term one (the store has no change signal — we re-poll).
        return min(self._poll_interval, max(_MIN_SLEEP_SECONDS, wake_at - now))

    def _reschedule(self, schedule: Schedule, now: float) -> None:
        """Persist ``schedule``'s next ``next_run`` (``now + interval``) — RB1, best-effort.

        Anchored on the moment of firing (``compute_next_run(now)``) so the gap to the next
        fire is exactly one interval and a window that elapsed mid-tick never produces a burst
        of catch-up fires (design §5.5). Overwrites the SAME-named schedule in place
        (``add_schedule`` resolves the name case-insensitively + overwrites — it does not grow
        the count, so the per-chat cap is never tripped by a reschedule). A persist failure is
        logged + swallowed: the schedule degrades to its in-memory cadence for this process
        (it re-arms on the next restart) — a write never wedges the loop (RB1, design §5.5).
        """
        try:
            rearmed = schedule.with_next_run(schedule.compute_next_run(now))
            self._store.add_schedule(rearmed)
        except Exception:
            log.warning(
                "scheduler could not persist reschedule for %s (ignored)",
                schedule.name,
                exc_info=True,
            )


__all__ = ["Scheduler", "FireFn", "DEFAULT_POLL_INTERVAL_SECONDS"]
