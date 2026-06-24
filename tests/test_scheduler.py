"""Pure scheduler core (P14 T1) — interval parse + the due/next-run math.

The module is pure (no telegram, no SDK, no real time): every test injects ``now`` and
asserts deterministic arithmetic. Covers :func:`parse_interval` (valid + each failure
mode + the floor), :func:`format_interval` (round-trip), the :class:`Schedule` record +
its validation, and :meth:`Schedule.due` / :meth:`Schedule.compute_next_run` + the
``due_schedules`` / ``next_wake`` helpers with a fake clock.
"""

from __future__ import annotations

import pytest

from claude_tg.scheduler import (
    InvalidInterval,
    InvalidScheduleName,
    Schedule,
    due_schedules,
    format_interval,
    next_wake,
    parse_interval,
    validate_schedule_name,
)

# ---- parse_interval --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("90s", 90),
        ("30m", 1800),
        ("1h", 3600),
        ("2d", 172800),
        ("  5 H ", 18000),  # whitespace + case tolerated
        ("1S", 1),
        ("1440m", 86400),
    ],
)
def test_parse_interval_valid(raw, expected):
    # The floor defaults to 1 (the absolute minimum), so a 1s interval parses here.
    assert parse_interval(raw, minimum_seconds=1) == expected


@pytest.mark.parametrize("raw", ["0s", "0h", "-1m", "abc", "", "30", "5x", "h", "1.5h", "1m30s"])
def test_parse_interval_invalid_raises(raw):
    with pytest.raises(InvalidInterval):
        parse_interval(raw)


def test_parse_interval_non_string_raises():
    with pytest.raises(InvalidInterval):
        parse_interval(None)  # type: ignore[arg-type]


def test_parse_interval_below_floor_raises():
    # 1s is a valid token but below a 60 s floor → reject (the SCHEDULE_MIN_INTERVAL guard).
    with pytest.raises(InvalidInterval):
        parse_interval("1s", minimum_seconds=60)
    with pytest.raises(InvalidInterval):
        parse_interval("59s", minimum_seconds=60)
    # Exactly at the floor is allowed.
    assert parse_interval("60s", minimum_seconds=60) == 60
    assert parse_interval("1m", minimum_seconds=60) == 60


# ---- format_interval -------------------------------------------------------


@pytest.mark.parametrize("tok", ["90s", "30m", "1h", "2d", "45m", "6h", "7d"])
def test_format_interval_round_trips(tok):
    assert format_interval(parse_interval(tok, minimum_seconds=1)) == tok


def test_format_interval_largest_exact_unit():
    assert format_interval(90 * 60) == "90m"  # 90 min is not whole hours
    assert format_interval(3600) == "1h"
    assert format_interval(86400) == "1d"
    assert format_interval(3661) == "3661s"  # not a whole minute/hour


def test_format_interval_defensive_non_positive():
    # RB1: a non-positive / odd value floors at "0s" rather than raising (a listing line
    # must never crash on an odd stored value).
    assert format_interval(0) == "0s"
    assert format_interval(-5) == "0s"


# ---- name validation -------------------------------------------------------


def test_validate_schedule_name_accepts_sb4():
    for ok in ("ci", "nightly-build", "a_b", "X" * 32, "1"):
        validate_schedule_name(ok)  # no raise


@pytest.mark.parametrize("bad", ["", "a b", "../x", "x" * 33, "a/b", "naïve", "."])
def test_validate_schedule_name_rejects_non_sb4(bad):
    with pytest.raises(InvalidScheduleName):
        validate_schedule_name(bad)


# ---- Schedule record + validation ------------------------------------------


def _mk(**kw) -> Schedule:
    base = dict(
        name="ci",
        interval_seconds=3600,
        prompt="run tests",
        chat_id=7,
        next_run=1000.0,
        created_at=500.0,
    )
    base.update(kw)
    return Schedule(**base)  # type: ignore[arg-type]


def test_schedule_construction_validates_name():
    with pytest.raises(InvalidScheduleName):
        _mk(name="bad name")


def test_schedule_construction_validates_interval():
    with pytest.raises(InvalidInterval):
        _mk(interval_seconds=0)
    with pytest.raises(InvalidInterval):
        _mk(interval_seconds=-1)


def test_schedule_fields_default():
    s = _mk()
    assert s.project is None
    assert s.paused is False


# ---- due / compute_next_run (injected clock) -------------------------------


def test_due_at_or_after_next_run():
    s = _mk(next_run=1000.0)
    assert s.due(999.999) is False
    assert s.due(1000.0) is True  # exactly due
    assert s.due(5000.0) is True


def test_due_false_when_paused():
    s = _mk(next_run=1000.0, paused=True)
    # Even well past next_run, a paused schedule is never due (its next_run is preserved).
    assert s.due(1_000_000.0) is False


def test_compute_next_run_anchors_on_now_not_old_next_run():
    # The recurrence anchors on the moment of firing (now + interval), NOT on the old
    # next_run — so a window that elapsed while busy/asleep never produces a catch-up burst.
    s = _mk(interval_seconds=3600, next_run=1000.0)
    # Fired late at now=9999 (8999 s past the due time): the next run is now+interval,
    # a single clean interval ahead — not 1000+3600 (which would still be in the past).
    assert s.compute_next_run(9999.0) == 9999.0 + 3600


def test_with_next_run_and_with_paused_are_pure_copies():
    s = _mk(next_run=1000.0, paused=False)
    s2 = s.with_next_run(2000.0)
    assert s2.next_run == 2000.0
    assert s.next_run == 1000.0  # original untouched (frozen)
    s3 = s.with_paused(True)
    assert s3.paused is True
    assert s3.next_run == s.next_run  # pause doesn't disturb next_run
    assert s.paused is False


# ---- due_schedules / next_wake helpers -------------------------------------


def test_due_schedules_filters_and_preserves_order():
    a = _mk(name="a", next_run=100.0)
    b = _mk(name="b", next_run=50.0, paused=True)  # paused → never due
    c = _mk(name="c", next_run=200.0)  # not yet due at now=150
    assert [s.name for s in due_schedules([a, b, c], 150.0)] == ["a"]
    # All due → all returned in input order.
    assert [s.name for s in due_schedules([a, c], 1000.0)] == ["a", "c"]
    assert due_schedules([], 0.0) == []


def test_next_wake_is_earliest_enabled_next_run():
    a = _mk(name="a", next_run=100.0)
    b = _mk(name="b", next_run=50.0, paused=True)  # paused → ignored for wake
    c = _mk(name="c", next_run=200.0)
    assert next_wake([a, b, c], 0.0) == 100.0  # b paused → not the min
    # Only paused tasks → no wake.
    assert next_wake([b], 0.0) is None
    # Empty → None.
    assert next_wake([], 0.0) is None


def test_module_source_imports_no_telegram_or_sdk():
    # Mirrors permissions.py's import-light posture: the pure core must not depend on telegram
    # or the SDK (so it stays unit-testable + cheap to import). session_store is the only
    # allowed sibling dependency (the SB4 name rule lives there). Guard via the source so the
    # check is robust to what OTHER tests in the session have imported into sys.modules.
    import ast
    from pathlib import Path

    import claude_tg.scheduler as sched

    tree = ast.parse(Path(sched.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "telegram" not in imported
    assert "claude_agent_sdk" not in imported
