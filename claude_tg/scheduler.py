"""Proactive scheduler — the PURE model + math (P14 T-SCHED / design §6).

This module is the **pure-ish core** of the proactive scheduler (the design's
``schedule.py``): a frozen :class:`Schedule`, an interval parser
(:func:`parse_interval`), and the pure scheduling math (:meth:`Schedule.due` /
:meth:`Schedule.compute_next_run`). It mirrors the isolation of
:mod:`claude_tg.permissions` / :mod:`claude_tg.bash_policy` — **no telegram, no
SDK, no real time, no I/O** — so the whole thing is unit-testable with an injected
clock.

**Scope (this module = T1+T2+T3+T7 of the design — the data + math, NOT the firing).**
A :class:`Schedule` is a pure data record: it carries a ``next_run`` epoch and the math
to decide when it is due, but **nothing here fires it** — it is persisted (see
:mod:`claude_tg.session_store`) and the asyncio driver that drives a turn through the
engine (T-FIRE) consumes it, firing a due schedule on its interval.

The security model (design §5) lives with the firing driver (T-FIRE); this module only
owns the data shape + the interval/due arithmetic. The two facts it DOES encode that matter for
safety/robustness:

* **SB4 name rule** — a schedule name is the same ``^[A-Za-z0-9_-]{1,32}$`` shape a
  project / macro name uses (:func:`~claude_tg.session_store.validate_project_name`
  is reused), so a name is always safe to interpolate body-free into a listing.
* **A clear interval floor + ceiling on the *parse*** — :func:`parse_interval`
  rejects ``0`` / negatives / garbage with a loud :class:`InvalidInterval` (a typo
  ``/every 0s`` can never become a hammer-the-host schedule). The per-chat-cap floor
  on the *number* of schedules lives in the store (T2) + config (T3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .session_store import InvalidProjectName, validate_project_name

#: SB4 schedule-name rule — identical to the project/macro name rule
#: (``session_store._NAME_RE``): non-empty, ≤32 chars, ASCII letters/digits/``_``/``-``.
#: A schedule name is the operator's own label; constraining it to this safe charset
#: means it is always inert to interpolate into a body-free listing (SB3) and into a
#: ``callback_data`` field, exactly like a project name.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: The interval-token grammar: an integer count followed by a single unit suffix.
#: ``90s`` / ``30m`` / ``1h`` / ``2d`` — seconds / minutes / hours / days. Case-
#: insensitive on the suffix. NO compound forms (``1h30m``) in v1 — a single
#: count+unit covers every headline cadence ("every hour", "every 30 minutes",
#: "every 2 days") without a duration mini-language.
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)

#: Unit suffix → seconds-per-unit. Used by :func:`parse_interval`.
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


class InvalidInterval(ValueError):
    """An interval string is not a positive ``<count><unit>`` (``30m``/``1h``/``2d``/``90s``).

    Raised by :func:`parse_interval` on garbage (``abc``, empty), a non-positive count
    (``0s`` / ``-1m`` — a zero/negative interval would be a hammer-the-host schedule),
    a missing/unknown unit, or a value below an enforced floor. Fail-loud (mirrors
    :class:`~claude_tg.config` parse errors and
    :class:`~claude_tg.session_store.InvalidProjectName`) so a typo is rejected at
    create time with a clear message, never silently coerced into a surprising cadence.
    """


class InvalidScheduleName(InvalidProjectName):
    """A schedule name fails the SB4 rule (``^[A-Za-z0-9_-]{1,32}$``).

    A thin alias of :class:`~claude_tg.session_store.InvalidProjectName` (the schedule
    name rule IS the project/macro name rule) so the bot can ``except`` either uniformly
    while the message names *schedule* specifically. :func:`validate_schedule_name`
    raises this.
    """


def validate_schedule_name(name: str) -> None:
    """Raise :class:`InvalidScheduleName` unless ``name`` satisfies the SB4 rule.

    Delegates to :func:`~claude_tg.session_store.validate_project_name` (the same
    ``^[A-Za-z0-9_-]{1,32}$`` rule) and re-raises as :class:`InvalidScheduleName` so a
    caller catching the schedule-specific type gets a schedule-worded error, while the
    underlying rule stays in ONE place (no second regex to drift). Pure + side-effect
    free, so the bot command (T7) and the store CRUD (T2) both reuse it.
    """
    try:
        validate_project_name(name)
    except InvalidProjectName as exc:
        raise InvalidScheduleName(str(exc)) from exc


def parse_interval(raw: str, *, minimum_seconds: int = 1) -> int:
    """Parse an interval token (``30m``/``1h``/``2d``/``90s``) → seconds (fail-loud).

    Accepts a single ``<count><unit>`` where ``<count>`` is a non-negative integer and
    ``<unit>`` is ``s``/``m``/``h``/``d`` (case-insensitive, surrounding whitespace
    tolerated). Returns the interval in **seconds**.

    Fail-loud on anything else — raises :class:`InvalidInterval` for:

    * garbage / empty / non-``str`` (``"abc"``, ``""``, ``None``);
    * a missing or unknown unit (``"30"``, ``"5x"``);
    * a non-positive count (``"0s"``, ``"0h"``) — a zero interval would fire forever;
    * a result below ``minimum_seconds`` (the configured ``SCHEDULE_MIN_INTERVAL_SECONDS``
      floor, T3) — so ``/every 1s`` can't hammer the host when the floor is, say, 60 s.

    ``minimum_seconds`` defaults to ``1`` (the absolute floor — a sub-second interval is
    never valid) so the pure function is usable on its own in tests; the bot passes the
    config floor. Pure (no I/O, no clock).
    """
    if not isinstance(raw, str):
        raise InvalidInterval(f"interval must be a string like '30m'/'1h'/'2d', got {raw!r}")
    match = _INTERVAL_RE.match(raw)
    if match is None:
        raise InvalidInterval(
            f"invalid interval {raw!r}: use a count + unit like '30m', '1h', '2d', or '90s'"
        )
    count = int(match.group(1))
    unit = match.group(2).lower()
    if count <= 0:
        raise InvalidInterval(f"interval must be positive, got {raw!r}")
    seconds = count * _UNIT_SECONDS[unit]
    floor = max(1, int(minimum_seconds))
    if seconds < floor:
        raise InvalidInterval(
            f"interval {raw!r} ({seconds}s) is below the minimum of {floor}s"
        )
    return seconds


def format_interval(seconds: int) -> str:
    """Render an interval (seconds) back to a compact ``2d``/``1h``/``30m``/``90s`` token.

    The inverse of :func:`parse_interval` for the LARGEST exact unit: a value that is a
    whole number of days renders as ``Nd``, else whole hours → ``Nh``, else whole
    minutes → ``Nm``, else seconds → ``Ns``. A non-exact value (e.g. 90 minutes) keeps
    the largest unit that divides it (``90m`` here, since 90 is not a whole number of
    hours) so the rendering round-trips through :func:`parse_interval`. Pure; defensive
    (RB1): a non-positive / non-int value floors at ``0s`` rather than raising — a
    listing line must never crash on an odd stored value.
    """
    total = int(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else 0
    if total <= 0:
        return "0s"
    for suffix, unit in (("d", _UNIT_SECONDS["d"]), ("h", _UNIT_SECONDS["h"]), ("m", _UNIT_SECONDS["m"])):
        if total % unit == 0:
            return f"{total // unit}{suffix}"
    return f"{total}s"


@dataclass(frozen=True)
class Schedule:
    """One proactive scheduled task — a frozen, pure data record (P14 T1).

    A :class:`Schedule` is a **pure data record**: it carries everything needed to know
    WHEN it should fire (:attr:`next_run`, an epoch) and the math to decide that
    (:meth:`due` / :meth:`compute_next_run`), but it does **not** fire anything itself —
    the runtime driver (T-FIRE) consumes it and fires it. Fields (design §3.1):

    * ``name``            — SB4 label (``^[A-Za-z0-9_-]{1,32}$``); the operator's own
      handle for the task (used in ``/unschedule``/``/pause``/``/resume`` and the
      ``/schedules`` listing). Validated by :func:`validate_schedule_name`.
    * ``interval_seconds``— the recurrence in seconds (from :func:`parse_interval`).
    * ``prompt``          — the turn text fired as a normal turn (the same kind of
      operator-authored prompt a macro stores — SB4: it is the engine's prompt, NEVER
      interpolated into a shell command). Stored verbatim.
    * ``project``         — the target project NAME the turn runs against (pinned at
      create so a later ``/switch`` doesn't silently retarget it), or ``None`` to use
      the chat's active project at fire time (T-FIRE resolves it).
    * ``chat_id``         — the chat the task is bound to + fires INTO (SB1/§5.4: a task
      can never target another chat).
    * ``next_run``        — the epoch (seconds) of the next fire. Re-armed from *now* on
      load (the store, T2) so a missed window while the bot was down is never replayed.
    * ``paused``          — when ``True`` the task is skipped by :meth:`due` (the
      ``/pause`` state) without losing the definition.
    * ``created_at``      — the epoch the task was created (informational; stable across
      re-arms).

    Frozen + ``__post_init__`` validates the name (fail-loud) so an invalid
    :class:`Schedule` can never be constructed — every instance is well-formed. The
    pure math takes an **injected** ``now`` (a float epoch); the module never reads the
    real clock.
    """

    name: str
    interval_seconds: int
    prompt: str
    chat_id: int
    next_run: float
    project: str | None = None
    paused: bool = False
    created_at: float = 0.0

    def __post_init__(self) -> None:
        # Validate the name (SB4) at construction so an invalid Schedule cannot exist —
        # every persisted / in-flight instance is well-formed (fail-loud, mirrors the
        # store's create-time validation). The interval is validated upstream by
        # parse_interval; defend the invariant here too so a hand-built bad value can't
        # produce a never-firing/always-firing record.
        validate_schedule_name(self.name)
        if not isinstance(self.interval_seconds, int) or self.interval_seconds <= 0:
            raise InvalidInterval(
                f"interval_seconds must be a positive int, got {self.interval_seconds!r}"
            )

    def due(self, now: float) -> bool:
        """Whether this task is due to fire at ``now`` (epoch seconds) — pure.

        ``True`` iff the task is **not paused** AND ``now >= next_run``. A paused task is
        never due (it keeps its ``next_run`` so a later ``/resume`` resumes the cadence).
        Takes the clock as an argument — no real time is read here (deterministic tests).
        """
        if self.paused:
            return False
        return now >= self.next_run

    def compute_next_run(self, now: float) -> float:
        """The next ``next_run`` epoch after firing at ``now`` — pure.

        The recurrence is **anchored on the moment of firing** (``now + interval``), not
        on the old ``next_run``. Rationale (RB3/RB6, design §5.5): anchoring on *now*
        guarantees a clean single-interval gap to the next fire and never produces a
        burst of back-to-back fires to "catch up" a window that elapsed while the bot was
        busy or asleep — the same abandon-and-lazy-resume posture the store uses when it
        re-arms on load. ``now`` is injected (no real clock).
        """
        return now + self.interval_seconds

    def with_next_run(self, next_run: float) -> "Schedule":
        """A copy of this schedule with a new ``next_run`` (the frozen-record updater).

        :class:`Schedule` is frozen, so re-arming / rescheduling produces a NEW instance.
        Used by the store's re-arm-on-load (T2) and (later) the firing loop's reschedule
        (T-FIRE). Pure.
        """
        return Schedule(
            name=self.name,
            interval_seconds=self.interval_seconds,
            prompt=self.prompt,
            chat_id=self.chat_id,
            next_run=next_run,
            project=self.project,
            paused=self.paused,
            created_at=self.created_at,
        )

    def with_paused(self, paused: bool) -> "Schedule":
        """A copy with the ``paused`` flag set/cleared (the ``/pause`` · ``/resume`` updater).

        Frozen-record updater (pure). The bot's ``/pause`` / ``/resume`` go through the
        store, which uses this to flip the flag without disturbing ``next_run`` so the
        cadence resumes where it left off.
        """
        return Schedule(
            name=self.name,
            interval_seconds=self.interval_seconds,
            prompt=self.prompt,
            chat_id=self.chat_id,
            next_run=self.next_run,
            project=self.project,
            paused=paused,
            created_at=self.created_at,
        )


def due_schedules(schedules: list[Schedule], now: float) -> list[Schedule]:
    """The subset of ``schedules`` that are :meth:`Schedule.due` at ``now`` — pure helper.

    A stable-ordered filter (input order preserved) over :meth:`Schedule.due`, so the
    firing loop (T-FIRE) gets the due tasks deterministically. Paused tasks are excluded
    (``due`` returns ``False`` for them). Pure (no clock, no I/O); returns a NEW list.
    """
    return [s for s in schedules if s.due(now)]


def next_wake(schedules: list[Schedule], now: float) -> float | None:
    """The earliest future ``next_run`` across the **enabled** tasks, or ``None`` — pure.

    The wake-time the firing loop (T-FIRE) sleeps until: the minimum ``next_run`` of all
    not-paused tasks (a value ``<= now`` means "fire immediately"). Returns ``None`` when
    there are no enabled tasks (nothing to wake for). Paused tasks are ignored (a
    ``/pause``d task must not hold the loop awake). Pure (no clock, no I/O).
    """
    candidates = [s.next_run for s in schedules if not s.paused]
    if not candidates:
        return None
    return min(candidates)
