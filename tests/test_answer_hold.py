"""Unit tests for the async answer-hold (T5) — ADR-002, mock substrate only.

These exercise the engine-side ``PendingDecision`` registry + the 60-min backstop +
``/cancel`` + answer routing, with **NO live Claude, NO network, and NO real waits**
(the backstop is driven with a tiny injected interval or by resolving the Future
directly). Two test surfaces:

* :class:`PendingRegistry` directly — routing, backstop, cancel, RB1 (unknown id).
* the full :class:`Engine` answer-hold end-to-end against a ``HoldingSubstrate`` whose
  ``send()`` calls the engine's ``on_tool_request`` mid-turn (simulating the SDK's
  ``can_use_tool``), so we prove the operator's :meth:`Engine.resolve` unblocks the
  held callback and the injected ``ask``/``plan`` reaches the outgoing stream.

Determinism: the registry's only real wait is ``asyncio.sleep(timeout)`` in the
backstop; tests pass ``backstop_seconds`` ≈ 0.05 s (or much larger when proving the
operator wins), and assert via the resolved decision — never a wall-clock sleep ≥ 1 s.
"""

import asyncio

import pytest

from claude_tg.engine import Engine, PendingRegistry
from claude_tg.engine.types import (
    AskEvent,
    Cancel,
    PermissionVerdict,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    StatusEvent,
)


async def drain(aiter):
    return [ev async for ev in aiter]


# ===========================================================================
# PendingRegistry — the mechanism in isolation
# ===========================================================================


async def test_registry_resolve_routes_decision_to_awaiter():
    reg = PendingRegistry(backstop_seconds=100)  # large: operator must win
    task = asyncio.create_task(reg.await_decision("tu1", "AskUserQuestion"))
    await asyncio.sleep(0)  # let the awaiter register before we resolve

    assert reg.has_pending("tu1")
    ok = reg.resolve("tu1", QuestionAnswer({"Pick?": "Bravo"}))
    assert ok is True

    decision = await task
    assert isinstance(decision, QuestionAnswer)
    assert decision.answers == {"Pick?": "Bravo"}
    # the entry is cleared once resolved (no leak)
    assert not reg.has_pending("tu1")
    assert reg.pending_ids == []


async def test_registry_wrong_id_is_ignored_correct_id_resolves():
    reg = PendingRegistry(backstop_seconds=100)
    task = asyncio.create_task(reg.await_decision("right", "AskUserQuestion"))
    await asyncio.sleep(0)

    # A resolve for an id that isn't pending is a no-op (RB1) and does NOT resolve ours.
    assert reg.resolve("wrong", QuestionAnswer({"Q?": "X"})) is False
    assert reg.has_pending("right")  # still waiting

    assert reg.resolve("right", QuestionAnswer({"Q?": "Y"})) is True
    decision = await task
    assert decision.answers == {"Q?": "Y"}


async def test_registry_two_concurrent_pendings_route_by_id():
    reg = PendingRegistry(backstop_seconds=100)
    t1 = asyncio.create_task(reg.await_decision("tuA", "AskUserQuestion"))
    t2 = asyncio.create_task(reg.await_decision("tuB", "ExitPlanMode"))
    await asyncio.sleep(0)
    assert set(reg.pending_ids) == {"tuA", "tuB"}

    # Resolve out of order; each answer must reach its own request.
    reg.resolve("tuB", PlanVerdict(approve=True))
    reg.resolve("tuA", QuestionAnswer({"Q?": "A"}))

    dA = await t1
    dB = await t2
    assert isinstance(dA, QuestionAnswer) and dA.answers == {"Q?": "A"}
    assert isinstance(dB, PlanVerdict) and dB.approve is True


async def test_registry_backstop_auto_denies_deterministically():
    # SHORT injected interval: the backstop fires with no real wait and resolves DENY.
    notes: list[tuple[str, str]] = []

    async def notify(tool_use_id, reason):
        notes.append((tool_use_id, reason))

    reg = PendingRegistry(backstop_seconds=0.05, notify=notify)
    decision = await reg.await_decision("tuX", "AskUserQuestion")  # nobody resolves

    assert isinstance(decision, PermissionVerdict)
    assert decision.behavior == "deny"
    assert "backstop" in (decision.message or "")
    assert notes and notes[0][0] == "tuX"  # notify fired for the right id
    assert not reg.has_pending("tuX")  # cleared


async def test_registry_per_call_backstop_override_beats_default():
    # The per-call interval overrides the registry default (tests drive it short).
    reg = PendingRegistry(backstop_seconds=10_000)  # default huge
    decision = await reg.await_decision("tu", "ExitPlanMode", backstop_seconds=0.05)
    assert isinstance(decision, PermissionVerdict) and decision.behavior == "deny"


async def test_registry_operator_wins_race_against_backstop():
    # With a comfortably long backstop the operator's resolve wins; backstop is moot.
    reg = PendingRegistry(backstop_seconds=100)
    task = asyncio.create_task(reg.await_decision("tu", "AskUserQuestion"))
    await asyncio.sleep(0)
    reg.resolve("tu", QuestionAnswer({"Q?": "fast"}))
    decision = await task
    assert isinstance(decision, QuestionAnswer)  # NOT a backstop deny


async def test_registry_cancel_specific_resolves_clean_abort():
    reg = PendingRegistry(backstop_seconds=100)
    task = asyncio.create_task(reg.await_decision("tu", "AskUserQuestion"))
    await asyncio.sleep(0)
    n = reg.cancel("tu")
    assert n == 1
    decision = await task
    assert isinstance(decision, Cancel)  # clean abort -> Cancel (maps to deny)


async def test_registry_cancel_all_resolves_every_pending():
    reg = PendingRegistry(backstop_seconds=100)
    t1 = asyncio.create_task(reg.await_decision("a", "AskUserQuestion"))
    t2 = asyncio.create_task(reg.await_decision("b", "ExitPlanMode"))
    await asyncio.sleep(0)
    n = reg.cancel()  # None -> cancel all
    assert n == 2
    assert isinstance(await t1, Cancel)
    assert isinstance(await t2, Cancel)


async def test_registry_resolve_unknown_id_does_not_crash_rb1():
    reg = PendingRegistry(backstop_seconds=100)
    # No pending at all: resolve / cancel are clean no-ops, never raise (RB1).
    assert reg.resolve("ghost", QuestionAnswer({"Q?": "X"})) is False
    assert reg.cancel("ghost") == 0
    assert reg.cancel() == 0  # cancel-all with nothing pending


async def test_registry_double_resolve_second_is_noop():
    reg = PendingRegistry(backstop_seconds=100)
    task = asyncio.create_task(reg.await_decision("tu", "AskUserQuestion"))
    await asyncio.sleep(0)
    assert reg.resolve("tu", QuestionAnswer({"Q?": "first"})) is True
    # The id is gone after the first resolve; a second is a no-op (RB1), no crash.
    assert reg.resolve("tu", QuestionAnswer({"Q?": "second"})) is False
    assert (await task).answers == {"Q?": "first"}


def test_registry_rejects_nonpositive_backstop():
    with pytest.raises(ValueError):
        PendingRegistry(backstop_seconds=0)
    with pytest.raises(ValueError):
        PendingRegistry(backstop_seconds=-1)


# ===========================================================================
# A substrate that calls the engine's decision callback mid-turn (the SDK's
# can_use_tool, faked) so the FULL answer-hold is exercised end-to-end.
# ===========================================================================


class HoldingSubstrate:
    """Mock substrate whose ``send`` raises ONE interactive request mid-turn.

    Wired with the engine's ``on_tool_request`` as ``decision_callback`` (mirrors how
    the SDK adapter calls it). On ``send`` it:
      1. yields a pre-event (so the stream is live),
      2. calls ``decision_callback(tool_name, tool_input, tool_use_id)`` and BLOCKS on
         it (exactly like ``receive_response`` blocking while ``can_use_tool`` holds),
      3. records the returned :class:`SubstrateDecision`, then yields a post-event
         reflecting whether the tool was allowed (carrying the applied ``updated_input``)
         or denied — so a test can assert the session "continued on the answer".
    """

    def __init__(self, *, tool_name, tool_input, tool_use_id, pre=None, post_factory=None):
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._tool_use_id = tool_use_id
        self._pre = pre
        self._post_factory = post_factory
        self.session_id = "S1"
        self.decision_callback = None  # set by the test to engine.on_tool_request
        self.last_decision = None
        self.calls = []

    async def start(self):
        self.calls.append(("start",))

    async def resume(self, session_id):
        self.calls.append(("resume", session_id))
        self.session_id = session_id

    async def send(self, prompt, *, timeout=120.0):
        self.calls.append(("send", prompt, timeout))
        if self._pre is not None:
            yield self._pre
        # Block on the engine seam exactly as the SDK blocks on can_use_tool.
        decision = await self.decision_callback(
            self._tool_name, self._tool_input, self._tool_use_id
        )
        self.last_decision = decision
        if self._post_factory is not None:
            ev = self._post_factory(decision)
            if ev is not None:
                yield ev

    async def stop(self):
        self.calls.append(("stop",))


def _wire(sub) -> Engine:
    """Build an Engine over the mock and wire the mock's callback to its seam."""
    eng = Engine(sub)
    sub.decision_callback = eng.on_tool_request
    return eng


# ---- end-to-end: question answer routes through the native answers-map -----


async def test_engine_ask_hold_injects_event_and_resolves_native_answer():
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Pick?"}]},
        tool_use_id="tu-ask",
        post_factory=lambda d: TextEvent(
            text=f"answered={d.updated_input.get('answers')}", session_id="S1"
        ),
    )
    eng = _wire(sub)
    await eng.start()

    collected = []

    async def operator():
        # Wait until the engine has injected the AskEvent + registered the pending,
        # then answer it (no real delay — poll the registry on the same loop).
        for _ in range(1000):
            if eng._pending.has_pending("tu-ask"):
                break
            await asyncio.sleep(0)
        eng.resolve("tu-ask", QuestionAnswer({"Pick?": "Bravo"}))

    op = asyncio.create_task(operator())
    async for ev in eng.send("go"):
        collected.append(ev)
    await op
    await eng.stop()

    # The operator SAW the ask (injected into the outgoing stream) with its id.
    asks = [e for e in collected if isinstance(e, AskEvent)]
    assert len(asks) == 1 and asks[0].tool_use_id == "tu-ask"
    # The held callback resolved via the NATIVE answers-map allow path.
    assert sub.last_decision.allow is True
    assert sub.last_decision.updated_input == {
        "questions": [{"question": "Pick?"}],
        "answers": {"Pick?": "Bravo"},
    }
    # And the session continued on that answer (post-event reflects it).
    texts = [e.text for e in collected if isinstance(e, TextEvent)]
    assert any("Bravo" in t for t in texts)


async def test_engine_plan_hold_approve_allows():
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="ExitPlanMode",
        tool_input={"plan": "do X then Y"},
        tool_use_id="tu-plan",
        post_factory=lambda d: TextEvent(text="allow" if d.allow else "deny", session_id="S1"),
    )
    eng = _wire(sub)
    await eng.start()

    async def operator():
        for _ in range(1000):
            if eng._pending.has_pending("tu-plan"):
                break
            await asyncio.sleep(0)
        eng.resolve("tu-plan", PlanVerdict(approve=True))

    op = asyncio.create_task(operator())
    collected = await drain(eng.send("go"))
    await op

    plans = [e for e in collected if isinstance(e, PlanEvent)]
    assert len(plans) == 1 and plans[0].plan == "do X then Y"
    assert sub.last_decision.allow is True  # approve -> allow
    assert any(getattr(e, "text", "") == "allow" for e in collected)


async def test_engine_plan_hold_reject_denies_with_feedback():
    sub = HoldingSubstrate(
        tool_name="ExitPlanMode",
        tool_input={"plan": "risky plan"},
        tool_use_id="tu-plan2",
    )
    eng = _wire(sub)
    await eng.start()

    async def operator():
        for _ in range(1000):
            if eng._pending.has_pending("tu-plan2"):
                break
            await asyncio.sleep(0)
        eng.resolve("tu-plan2", PlanVerdict(approve=False, feedback="add a test step"))

    op = asyncio.create_task(operator())
    await drain(eng.send("go"))
    await op

    # Reject -> deny carrying the feedback on the message channel (no native field).
    assert sub.last_decision.allow is False
    assert sub.last_decision.message == "add a test step"


# ---- end-to-end: backstop fires while the turn is held (deterministic) -----


async def test_engine_backstop_auto_denies_and_emits_status_then_usable():
    # Nobody answers; a SHORT backstop fires, auto-denies, emits a notify status, and
    # the turn completes cleanly (session stays usable). No real wait.
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Pick?"}]},
        tool_use_id="tu-bs",
        post_factory=lambda d: TextEvent(text="post", session_id="S1"),
    )
    eng = Engine(sub, backstop_seconds=0.05)  # short, injected
    sub.decision_callback = eng.on_tool_request
    await eng.start()

    collected = await drain(eng.send("go"))
    await eng.stop()

    # The pending was auto-denied by the backstop...
    assert sub.last_decision.allow is False
    assert "backstop" in (sub.last_decision.message or "")
    # ...a notify status event reached the operator...
    statuses = [e for e in collected if isinstance(e, StatusEvent)]
    assert any("backstop" in (s.detail or "") for s in statuses)
    # ...and the turn still completed (post-event present) — session usable (RB4-shape).
    assert any(getattr(e, "text", "") == "post" for e in collected)


# ---- end-to-end: /cancel aborts the held request cleanly (no wedge) --------


async def test_engine_cancel_aborts_held_request_cleanly():
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Pick?"}]},
        tool_use_id="tu-cancel",
        post_factory=lambda d: TextEvent(text="resumed-after-cancel", session_id="S1"),
    )
    eng = Engine(sub, backstop_seconds=100)  # long: prove cancel (not backstop) wins
    sub.decision_callback = eng.on_tool_request
    await eng.start()

    async def operator():
        for _ in range(1000):
            if eng._pending.has_pending("tu-cancel"):
                break
            await asyncio.sleep(0)
        assert eng.cancel("tu-cancel") == 1

    op = asyncio.create_task(operator())
    collected = await drain(eng.send("go"))  # must NOT hang / raise
    await op
    await eng.stop()

    # Cancel resolved the hold as a clean abort -> deny; the callback returned.
    assert sub.last_decision.allow is False
    assert sub.last_decision.message == "cancelled"
    # The turn unwound cleanly and kept streaming (no wedge).
    assert any(getattr(e, "text", "") == "resumed-after-cancel" for e in collected)


async def test_engine_turn_level_cancel_aborts_all_pending():
    sub = HoldingSubstrate(
        tool_name="ExitPlanMode",
        tool_input={"plan": "p"},
        tool_use_id="tu-turn",
    )
    eng = Engine(sub, backstop_seconds=100)
    sub.decision_callback = eng.on_tool_request
    await eng.start()

    async def operator():
        for _ in range(1000):
            if eng._pending.has_pending("tu-turn"):
                break
            await asyncio.sleep(0)
        assert eng.cancel() >= 1  # turn-level cancel (no id)

    op = asyncio.create_task(operator())
    await drain(eng.send("go"))
    await op
    assert sub.last_decision.allow is False and sub.last_decision.message == "cancelled"


async def test_engine_resolve_unknown_id_is_noop_rb1():
    # Calling resolve()/cancel() on the engine with no in-flight hold never raises.
    eng = Engine(HoldingSubstrate(tool_name="X", tool_input={}, tool_use_id="z"))
    assert eng.resolve("nope", QuestionAnswer({"Q?": "A"})) is False
    assert eng.cancel("nope") == 0
    assert eng.cancel() == 0


# ---- ordinary tools never hold (auto-allow, no operator decision) ----------


async def test_engine_ordinary_tool_does_not_hold_or_inject():
    # An ordinary (non ask/plan) tool request is auto-allowed immediately, with no
    # pending entry created and nothing injected — it never waits on an operator.
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/a", "content": "x"},
        tool_use_id="tu-write",
        post_factory=lambda d: TextEvent(text="wrote", session_id="S1"),
    )
    eng = _wire(sub)
    await eng.start()
    # No operator task at all — if this held, drain() would block forever.
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)

    assert sub.last_decision.allow is True
    assert sub.last_decision.updated_input == {"file_path": "/a", "content": "x"}
    # No ask/plan injected for an ordinary tool.
    assert not any(isinstance(e, (AskEvent, PlanEvent)) for e in collected)
    assert eng._pending.pending_ids == []
    assert any(getattr(e, "text", "") == "wrote" for e in collected)
