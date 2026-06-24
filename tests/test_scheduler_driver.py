"""P14 T-FIRE — the proactive-scheduler DRIVER (the asyncio firing loop).

Mock-only, NO PTB / NO SDK / NO real time: the driver's collaborators (a store with
``all_schedules``/``add_schedule``/``rearm_all_schedules``, a ``fire`` callback, the
clock + sleep) are all injected, so every test is deterministic. Covers:

* a due schedule FIRES exactly once + is rescheduled (next_run = now + interval);
* a NOT-due schedule does not fire; a PAUSED schedule does not fire;
* ⭐ RB1-TOTAL: a fire that RAISES is caught — the loop survives + continues to the next
  schedule (one bad schedule never kills the driver or the bot);
* ``start`` RE-ARMS every schedule (no missed-fire replay) BEFORE the first tick;
* ``stop`` cancels the loop cleanly (no orphaned task), idempotent + safe-when-unstarted;
* a reschedule is ANCHORED on now (no burst catch-up) + persisted via the store;
* a store-read error degrades to "nothing due" (RB1) — never raises out of the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_tg.scheduler import Schedule
from claude_tg.scheduler_driver import DEFAULT_POLL_INTERVAL_SECONDS, Scheduler

# ---------------------------------------------------------------------------
# A fake store: holds schedules in a list; records rearm + add (reschedule).
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, schedules=None, *, raise_on_read=False):
        self._schedules = list(schedules or [])
        self.raise_on_read = raise_on_read
        self.rearm_calls: list[float] = []
        self.added: list[Schedule] = []

    def all_schedules(self):
        if self.raise_on_read:
            raise RuntimeError("store read boom")
        return list(self._schedules)

    def add_schedule(self, schedule, *, max_per_chat=None):
        # Overwrite by name (mirrors JsonSessionStore.add_schedule) so a reschedule replaces.
        self.added.append(schedule)
        self._schedules = [s for s in self._schedules if s.name != schedule.name]
        self._schedules.append(schedule)

    def rearm_all_schedules(self, now):
        self.rearm_calls.append(now)
        self._schedules = [s.with_next_run(now + s.interval_seconds) for s in self._schedules]


def _sched(name, *, interval=60, next_run=0.0, paused=False, chat_id=1, project=None, prompt="do it"):
    return Schedule(
        name=name,
        interval_seconds=interval,
        prompt=prompt,
        chat_id=chat_id,
        next_run=next_run,
        project=project,
        paused=paused,
    )


def _make_driver(store, *, fire=None, now=1000.0, fires=None):
    """A Scheduler with an injected clock fixed at ``now`` and a recording fire callback."""
    fires = fires if fires is not None else []

    async def _record_fire(schedule):
        fires.append(schedule)
        return True

    return Scheduler(
        store=store,
        fire=fire or _record_fire,
        clock=lambda: now,
        sleep=lambda _w: asyncio.sleep(0),
    ), fires


# ---------------------------------------------------------------------------
# _tick — the fire/skip/reschedule decision (driven directly, deterministic)
# ---------------------------------------------------------------------------


async def test_tick_fires_due_schedule_once_and_reschedules():
    store = FakeStore([_sched("ci", interval=60, next_run=500.0)])  # due (next_run <= now=1000)
    driver, fires = _make_driver(store, now=1000.0)

    wait = await driver._tick()

    assert [s.name for s in fires] == ["ci"]  # fired exactly once
    # Rescheduled, anchored on NOW (1000 + 60), persisted via add_schedule.
    assert len(store.added) == 1 and store.added[0].name == "ci"
    assert store.added[0].next_run == 1060.0
    # The next wake reflects the advanced next_run (capped at the poll interval).
    assert wait == pytest.approx(min(DEFAULT_POLL_INTERVAL_SECONDS, 60.0))


async def test_tick_does_not_fire_not_due_schedule():
    store = FakeStore([_sched("later", interval=60, next_run=5000.0)])  # not due
    driver, fires = _make_driver(store, now=1000.0)

    wait = await driver._tick()

    assert fires == []  # nothing fired
    assert store.added == []  # nothing rescheduled
    # Sleep until the (capped) next wake.
    assert wait == pytest.approx(min(DEFAULT_POLL_INTERVAL_SECONDS, 4000.0))


async def test_tick_skips_paused_schedule():
    store = FakeStore([_sched("paused", interval=60, next_run=500.0, paused=True)])  # due but paused
    driver, fires = _make_driver(store, now=1000.0)

    wait = await driver._tick()

    assert fires == []  # a paused schedule is never due → never fires
    assert store.added == []
    # No enabled schedule → poll fallback (paused schedules don't hold the loop awake).
    assert wait == pytest.approx(DEFAULT_POLL_INTERVAL_SECONDS)


async def test_tick_fires_multiple_due_in_stable_order():
    store = FakeStore([
        _sched("a", interval=60, next_run=100.0),
        _sched("b", interval=120, next_run=200.0),
        _sched("c", interval=60, next_run=9999.0),  # not due
    ])
    driver, fires = _make_driver(store, now=1000.0)

    await driver._tick()

    assert [s.name for s in fires] == ["a", "b"]  # both due, input order; c skipped
    assert {s.name for s in store.added} == {"a", "b"}


async def test_tick_rb1_a_raising_fire_does_not_stop_the_others_and_still_reschedules():
    """⭐ RB1: a fire that RAISES is caught — the OTHER due schedules still fire, and the
    raising one is STILL rescheduled (so it advances, never re-fires in a tight loop)."""
    fires: list[str] = []

    async def _fire(schedule):
        fires.append(schedule.name)
        if schedule.name == "boom":
            raise RuntimeError("fire boom")
        return True

    store = FakeStore([
        _sched("boom", interval=60, next_run=100.0),
        _sched("ok", interval=60, next_run=100.0),
    ])
    driver, _ = _make_driver(store, fire=_fire, now=1000.0)

    # The tick itself must NOT raise despite the fire raising (RB1).
    wait = await driver._tick()

    assert fires == ["boom", "ok"]  # both attempted; the raise did not stop "ok"
    # BOTH were rescheduled (the raising one too) — it advances to its next interval.
    assert {s.name for s in store.added} == {"boom", "ok"}
    assert isinstance(wait, float)


async def test_tick_store_read_error_degrades_to_poll_rb1():
    store = FakeStore([_sched("x")], raise_on_read=True)
    driver, fires = _make_driver(store, now=1000.0)

    wait = await driver._tick()  # must not raise

    assert fires == []  # nothing read → nothing fired
    assert wait == pytest.approx(DEFAULT_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# start / stop — the loop lifecycle (real loop, injected sleep)
# ---------------------------------------------------------------------------


async def _noop_fire(_schedule):
    return True


async def test_start_rearms_all_schedules_before_running():
    """``start`` calls ``store.rearm_all_schedules(now)`` (no missed-fire replay) BEFORE the
    loop is launched — so a window that elapsed while the bot was DOWN never replays as a
    burst (design §5.5). The re-arm is synchronous in ``start`` (before ``ensure_future``), so
    it is observable immediately, with the loop parked in a long sleep (never ticking here)."""
    store = FakeStore([_sched("ci", interval=60, next_run=0.0)])
    driver = Scheduler(
        store=store,
        fire=_noop_fire,
        clock=lambda: 1000.0,
        sleep=lambda _w: asyncio.sleep(3600),  # park immediately so no tick fires here
    )
    driver.start()
    # rearm_all_schedules was called at start with the injected now (BEFORE the loop ticked).
    assert store.rearm_calls == [1000.0]
    # And it actually re-armed: the schedule's next_run advanced to now + interval.
    assert store.all_schedules()[0].next_run == 1060.0
    await driver.stop()  # cancel cleanly
    assert not driver.running


async def test_start_is_idempotent():
    store = FakeStore([])
    driver = Scheduler(
        store=store, fire=_noop_fire, clock=lambda: 0.0, sleep=lambda _w: asyncio.sleep(3600)
    )
    driver.start()
    first = driver._task
    driver.start()  # second start while live → no-op (same task)
    assert driver._task is first
    await driver.stop()


async def test_stop_cancels_loop_cleanly_no_orphan_task():
    store = FakeStore([])

    async def _block(_wait):
        await asyncio.sleep(3600)  # park so the loop is genuinely running when we cancel

    driver = Scheduler(store=store, fire=_noop_fire, clock=lambda: 0.0, sleep=_block)
    driver.start()
    assert driver.running
    task = driver._task
    await driver.stop()
    assert not driver.running
    assert task.done()  # the task actually finished (cancelled), not orphaned


async def test_stop_is_safe_when_never_started():
    store = FakeStore([])
    driver = Scheduler(store=store, fire=_noop_fire)
    await driver.stop()  # no task → clean no-op, no raise
    assert not driver.running


async def test_start_rearm_failure_does_not_block_start_rb1():
    class BadRearmStore(FakeStore):
        def rearm_all_schedules(self, now):
            raise RuntimeError("rearm boom")

    store = BadRearmStore([])
    driver = Scheduler(
        store=store, fire=_noop_fire, clock=lambda: 0.0, sleep=lambda _w: asyncio.sleep(3600)
    )
    driver.start()  # must not raise despite the rearm failure
    assert driver.running  # the loop still started
    await driver.stop()


class AlwaysDueStore(FakeStore):
    """A store whose reschedule is a no-op, so the schedule stays DUE every tick (the loop
    fires it on every iteration) — lets the RB1-survival test count repeated fires
    deterministically without relying on clock advancement."""

    def add_schedule(self, schedule, *, max_per_chat=None):
        self.added.append(schedule)  # record, but DON'T advance next_run → still due next tick


async def test_loop_survives_a_raising_fire_and_keeps_running():
    """⭐ RB1 end-to-end: with the REAL loop body, a fire that raises EVERY time does NOT kill
    the loop — it keeps ticking + firing across iterations, proving a perpetually-failing fire
    never takes the driver (or the bot) down.

    Deterministic: we drive ``_run()`` directly (the mirror-test pattern) and STOP it from
    inside the injected ``sleep`` after several fires by raising ``CancelledError`` (which the
    loop re-raises to exit cleanly — exactly what :meth:`Scheduler.stop` does). The schedule
    stays due every tick (the no-op reschedule store), so each tick re-fires."""
    seen = {"fires": 0}

    async def _always_raise(_schedule):
        seen["fires"] += 1
        raise RuntimeError("always boom")

    async def _sleep_then_stop(_wait):
        # After the loop has survived several raising fires, end it like a shutdown cancel.
        if seen["fires"] >= 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)  # yield, no real wait

    store = AlwaysDueStore([_sched("boom", interval=60, next_run=0.0)])
    driver = Scheduler(
        store=store, fire=_always_raise, clock=lambda: 1_000_000.0, sleep=_sleep_then_stop
    )

    # Drive the loop body directly; it must survive every raising fire and only exit on the
    # CancelledError (NEVER on the RuntimeError the fire keeps raising).
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(driver._run(), timeout=5)

    assert seen["fires"] >= 3  # the (always-raising) fire was re-attempted across ticks
