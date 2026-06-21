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
    DENIED_MESSAGE,
    AskEvent,
    Cancel,
    PermissionDecision,
    PermissionEvent,
    PermissionVerdict,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    StatusEvent,
)
from claude_tg.permissions import PermissionPolicy


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


# ---- policy-allowed ordinary tools never hold (no operator decision) --------


async def test_engine_policy_allowed_tool_does_not_hold_or_inject():
    # An ordinary tool the policy ALLOWS (here Read — a safe read in the allowlist) is
    # allowed immediately, with no pending entry created and nothing injected — it never
    # waits on an operator. (Under the P2 gated default a RISKY tool would HOLD instead;
    # that path is covered by the permission-gate tests below — false-pass note: were
    # the gate missing/auto-allow left in, the RISKY-hold tests would not hold and the
    # gated-by-default test would see an allow, not a PermissionEvent.)
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="Read",
        tool_input={"file_path": "/a"},
        tool_use_id="tu-read",
        post_factory=lambda d: TextEvent(text="read", session_id="S1"),
    )
    eng = _wire(sub)
    await eng.start()
    # No operator task at all — if this held, drain() would block forever.
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)

    assert sub.last_decision.allow is True
    assert sub.last_decision.updated_input == {"file_path": "/a"}
    # No prompt of any kind injected for a policy-allowed tool.
    assert not any(
        isinstance(e, (AskEvent, PlanEvent, PermissionEvent)) for e in collected
    )
    assert eng._pending.pending_ids == []
    assert any(getattr(e, "text", "") == "read" for e in collected)


# ===========================================================================
# P2 / T3: the permission gate over the ordinary-tool branch (ADR-003 §2/§4).
#
# A risky, not-granted tool HOLDS for an operator PermissionDecision exactly as an
# ask/plan holds — reusing the SAME PendingRegistry (so RB4 backstop/cancel apply for
# free). These use the HoldingSubstrate (one request/turn) and a TwoToolSubstrate
# (two sequential requests/turn, sharing the engine's policy) to prove allow-once
# RE-asks, allow-session SUPPRESSES (per tool NAME), deny carries the canned message,
# /yolo runs free, and the ADR-001 caveat (one grant frees nothing else).
# Substrate mocked; no live Claude/network; deterministic (resolve / short backstop).
# ===========================================================================


class TwoToolSubstrate:
    """Mock substrate whose ``send`` fires TWO sequential tool requests in one turn.

    Each request blocks on ``decision_callback`` (the SDK's ``can_use_tool``) before the
    next is issued, so an operator resolves them in order — and the SECOND request goes
    through the engine's gate AFTER any allow-session grant from the first is recorded.
    Records every returned :class:`SubstrateDecision` in ``decisions``. The two requests
    may be the same tool name (re-ask / suppress tests) or different (ADR-001 caveat).
    """

    def __init__(self, *, requests):
        # requests: list of (tool_name, tool_input, tool_use_id)
        self._requests = requests
        self.session_id = "S1"
        self.decision_callback = None
        self.decisions = []
        self.calls = []

    async def start(self):
        self.calls.append(("start",))

    async def send(self, prompt, *, timeout=120.0):
        from claude_tg.engine.types import TextEvent

        self.calls.append(("send", prompt, timeout))
        for (name, tool_input, tuid) in self._requests:
            decision = await self.decision_callback(name, tool_input, tuid)
            self.decisions.append(decision)
            # A post-event per request so a test can see the turn kept streaming.
            yield TextEvent(
                text=f"{tuid}:{'allow' if decision.allow else 'deny'}", session_id="S1"
            )

    async def stop(self):
        self.calls.append(("stop",))


async def _resolve_when_pending(eng, tool_use_id, decision):
    """Poll the registry (same loop, no real wait) then resolve — deterministic."""
    for _ in range(2000):
        if eng._pending.has_pending(tool_use_id):
            break
        await asyncio.sleep(0)
    return eng.resolve(tool_use_id, decision)


# ---- risky tool HOLDS, emits a body-free PermissionEvent, allow-once allows -


async def test_engine_risky_tool_holds_and_emits_permission_event_then_allow_once():
    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/a.py", "content": "x"},
        tool_use_id="tu-w1",
    )
    eng = _wire(sub)  # fresh default policy -> Write gates
    await eng.start()

    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-w1", PermissionDecision("allow_once"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()

    # A PermissionEvent reached the operator (held), correlated by id.
    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    assert len(perms) == 1
    assert perms[0].tool_name == "Write"
    assert perms[0].tool_use_id == "tu-w1"
    # allow_once -> substrate allow (this request only).
    assert sub.last_decision.allow is True


async def test_engine_risky_permission_event_summary_is_body_free_sb3():
    # SB3: the emitted PermissionEvent summary carries a LENGTH for a body field, never
    # the raw contents (a Write's long `content` shows `<N chars>`, not the text).
    big = "S3CR3T" * 100  # 600 chars; must NOT appear verbatim in the summary
    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/a.py", "content": big},
        tool_use_id="tu-sb3",
    )
    eng = _wire(sub)
    await eng.start()

    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-sb3", PermissionDecision("deny"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    assert len(perms) == 1
    summary = perms[0].tool_input_summary
    assert big not in summary  # the raw body is absent
    assert "<600 chars>" in summary  # a length is present instead
    assert "/a.py" in summary  # the (safe) path is shown


async def test_engine_allow_once_does_not_grant_second_use_re_holds():
    # allow-once allows THIS request only: a SECOND use of the same tool still HOLDS
    # (no grant recorded). False-pass guard: if allow_once wrongly recorded a grant,
    # the second request would auto-allow and emit no second PermissionEvent.
    sub = TwoToolSubstrate(
        requests=[
            ("Write", {"file_path": "/a"}, "tu-1"),
            ("Write", {"file_path": "/b"}, "tu-2"),
        ]
    )
    eng = _wire(sub)
    await eng.start()

    async def operator():
        await _resolve_when_pending(eng, "tu-1", PermissionDecision("allow_once"))
        # The SECOND use must hold again (allow-once granted nothing).
        await _resolve_when_pending(eng, "tu-2", PermissionDecision("allow_once"))

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    # TWO prompts: the second use re-asked (allow-once did NOT grant the session).
    assert [p.tool_use_id for p in perms] == ["tu-1", "tu-2"]
    assert all(d.allow for d in sub.decisions)  # both allowed (once each)


async def test_engine_allow_session_grants_and_suppresses_second_prompt():
    # allow-session records the per-NAME grant so the SECOND use auto-allows with NO
    # second prompt. False-pass guard: if the grant were NOT recorded (drop
    # _verdict_for's grant_session call), the second use would HOLD and emit a second
    # PermissionEvent — this test would then see two prompts and FAIL.
    sub = TwoToolSubstrate(
        requests=[
            ("Write", {"file_path": "/a"}, "tu-1"),
            ("Write", {"file_path": "/b"}, "tu-2"),
        ]
    )
    eng = _wire(sub)
    await eng.start()

    async def operator():
        # Only the FIRST use needs an operator verdict; the second auto-allows.
        await _resolve_when_pending(eng, "tu-1", PermissionDecision("allow_session"))

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    # EXACTLY ONE prompt: the second use was auto-allowed by the session grant.
    assert [p.tool_use_id for p in perms] == ["tu-1"]
    assert all(d.allow for d in sub.decisions)  # both allowed
    assert eng._policy.is_granted("Write")  # the grant is recorded on the policy


async def test_engine_allow_session_is_per_tool_name_other_risky_tool_still_holds():
    # ADR-001 caveat: granting Write does NOT free a DIFFERENT risky tool (Bash) — each
    # gates independently. The second (Bash) request must still HOLD and prompt.
    sub = TwoToolSubstrate(
        requests=[
            ("Write", {"file_path": "/a"}, "tu-write"),
            ("Bash", {"command": "rm -rf /"}, "tu-bash"),
        ]
    )
    eng = _wire(sub)
    await eng.start()

    async def operator():
        await _resolve_when_pending(eng, "tu-write", PermissionDecision("allow_session"))
        # Bash is a DIFFERENT tool — it must still hold despite the Write grant.
        await _resolve_when_pending(eng, "tu-bash", PermissionDecision("allow_once"))

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    # BOTH tools prompted: the Write grant greenlit nothing about Bash.
    assert [p.tool_name for p in perms] == ["Write", "Bash"]
    assert eng._policy.is_granted("Write")
    assert not eng._policy.is_granted("Bash")  # never granted


async def test_engine_deny_maps_to_substrate_deny_with_canned_message():
    # deny -> a substrate deny carrying the canned DENIED_MESSAGE (D5; no free text).
    sub = HoldingSubstrate(
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tu-deny",
    )
    eng = _wire(sub)
    await eng.start()

    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-deny", PermissionDecision("deny"))
    )
    await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    assert sub.last_decision.allow is False
    assert sub.last_decision.message == DENIED_MESSAGE


async def test_engine_yolo_allows_risky_tool_with_no_prompt():
    # /yolo (policy.set_yolo(True)) makes every tool run free: a risky Bash is allowed
    # with NO prompt and no hold. False-pass guard: without the gate consulting the
    # policy, this would still pass — but the gated-by-default + deny tests would fail;
    # this isolates the yolo bypass specifically.
    from claude_tg.engine.types import TextEvent

    policy = PermissionPolicy()
    policy.set_yolo(True)
    sub = HoldingSubstrate(
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/x"},
        tool_use_id="tu-yolo",
        post_factory=lambda d: TextEvent(text="ran", session_id="S1"),
    )
    eng = Engine(sub, permission_policy=policy)
    sub.decision_callback = eng.on_tool_request
    await eng.start()
    # No operator at all — if it held, drain would block forever.
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)

    assert sub.last_decision.allow is True
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # no prompt
    assert eng._pending.pending_ids == []
    assert any(getattr(e, "text", "") == "ran" for e in collected)


async def test_engine_live_grant_allows_risky_tool_with_no_prompt():
    # A pre-existing allow-session grant (e.g. from an earlier turn) makes that tool run
    # free with no prompt — the engine consults the injected policy.
    policy = PermissionPolicy()
    policy.grant_session("Write")
    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/a", "content": "x"},
        tool_use_id="tu-granted",
    )
    eng = Engine(sub, permission_policy=policy)
    sub.decision_callback = eng.on_tool_request
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)

    assert sub.last_decision.allow is True
    assert not any(isinstance(e, PermissionEvent) for e in collected)
    assert eng._pending.pending_ids == []


# ---- RB4: backstop + cancel on a PERMISSION hold resolve to DENY (no wedge) -


async def test_engine_permission_backstop_auto_denies_and_session_usable_rb4():
    # RB4: nobody answers a permission prompt; a SHORT backstop fires, the held request
    # resolves to a substrate DENY (NOT auto-allow), a notify status reaches the
    # operator, and the turn completes — the session stays usable. No real wait.
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tu-bs-perm",
        post_factory=lambda d: TextEvent(text="post", session_id="S1"),
    )
    eng = Engine(sub, backstop_seconds=0.05)  # short, injected; fresh default policy
    sub.decision_callback = eng.on_tool_request
    await eng.start()

    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()

    # Backstop auto-DENIED the permission hold (never auto-allowed).
    assert sub.last_decision.allow is False
    assert "backstop" in (sub.last_decision.message or "")
    # A notify status reached the operator and the turn still completed (usable).
    statuses = [e for e in collected if isinstance(e, StatusEvent)]
    assert any("backstop" in (s.detail or "") for s in statuses)
    assert any(getattr(e, "text", "") == "post" for e in collected)
    assert eng._pending.pending_ids == []


async def test_engine_permission_cancel_aborts_held_request_to_deny_rb4():
    # RB4: /cancel on a pending PERMISSION request resolves it to a clean deny (NOT an
    # allow); the turn unwinds cleanly and keeps streaming (no wedge).
    from claude_tg.engine.types import TextEvent

    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/a", "content": "x"},
        tool_use_id="tu-cancel-perm",
        post_factory=lambda d: TextEvent(text="resumed-after-cancel", session_id="S1"),
    )
    eng = Engine(sub, backstop_seconds=100)  # long: prove cancel (not backstop) wins
    sub.decision_callback = eng.on_tool_request
    await eng.start()

    async def operator():
        for _ in range(2000):
            if eng._pending.has_pending("tu-cancel-perm"):
                break
            await asyncio.sleep(0)
        assert eng.cancel("tu-cancel-perm") == 1

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    # Cancel resolved the hold to a clean abort -> deny (never auto-allow).
    assert sub.last_decision.allow is False
    assert sub.last_decision.message == "cancelled"
    assert any(
        getattr(e, "text", "") == "resumed-after-cancel" for e in collected
    )


async def test_engine_ask_plan_route_to_answer_hold_not_permission_gate():
    # The ask/plan branch is UNCHANGED by P2: even with a default (gating) policy
    # present, AskUserQuestion/ExitPlanMode route to the answer-hold and emit their OWN
    # event — never a PermissionEvent (they are answered, not permission-gated).
    for tool_name, tool_input, tuid, ev_type, verdict in [
        ("AskUserQuestion", {"questions": [{"question": "Pick?"}]}, "tu-a",
         AskEvent, QuestionAnswer({"Pick?": "Bravo"})),
        ("ExitPlanMode", {"plan": "do X"}, "tu-p", PlanEvent, PlanVerdict(approve=True)),
    ]:
        sub = HoldingSubstrate(tool_name=tool_name, tool_input=tool_input, tool_use_id=tuid)
        eng = _wire(sub)  # fresh default policy: gates risky tools, but NOT ask/plan
        await eng.start()
        op = asyncio.create_task(_resolve_when_pending(eng, tuid, verdict))
        collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
        await op
        await eng.stop()

        assert any(isinstance(e, ev_type) for e in collected)  # its own prompt
        assert not any(isinstance(e, PermissionEvent) for e in collected)  # NOT gated
        assert sub.last_decision.allow is True


# ===========================================================================
# T10: dedup the DOUBLE ask/plan path (engine policy in _drain_substrate).
#
# A live run (spikes/p1-live-verify/evidence/v3_plan_reject.transcript.txt) showed
# the interactive ask/plan reaching the operator TWICE for one tool_use_id:
#   1. the adapter maps the assistant-message ToolUseBlock(Ask/Plan) onto the
#      substrate event stream — and this arrives FIRST, BEFORE can_use_tool fires,
#      so NO pending is registered yet (a decision against it -> resolve() -> False,
#      and is lost — in the live run v3's plan-reject was dropped);
#   2. the engine injects the authoritative Ask/Plan from the permission channel in
#      _answer_hold, synced with registering the pending (resolvable).
# The fix drops the substrate-stream copy; exactly ONE (the injected, resolvable)
# event must reach the operator.
# ===========================================================================


class DoublePathSubstrate:
    """Mock substrate that reproduces the LIVE double-path for one ask/plan.

    Mirrors the real adapter+SDK timing for an interactive tool:
      1. ``send`` yields a **substrate-origin** Ask/PlanEvent for ``tool_use_id``
         (what ``adapter_sdk._normalize_block`` emits from the assistant ToolUseBlock)
         — this is the PREMATURE copy that arrives before any pending exists;
      2. THEN it calls ``decision_callback(tool_name, tool_input, tool_use_id)`` for the
         SAME id and blocks on it (the SDK's ``can_use_tool``), which is what makes the
         engine inject its authoritative copy + register the pending;
      3. records the returned decision and yields a post-event reflecting allow/deny.

    Wired with ``decision_callback = engine.on_tool_request`` (as the SDK adapter does).
    """

    def __init__(self, *, tool_name, tool_input, tool_use_id, stream_event, post_factory=None):
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._tool_use_id = tool_use_id
        self._stream_event = stream_event  # the substrate-origin Ask/PlanEvent (path 1)
        self._post_factory = post_factory
        self.session_id = "S1"
        self.decision_callback = None
        self.last_decision = None
        self.calls = []

    async def start(self):
        self.calls.append(("start",))

    async def send(self, prompt, *, timeout=120.0):
        self.calls.append(("send", prompt, timeout))
        # (1) the adapter's copy comes off the substrate stream FIRST — before the
        #     permission channel fires, mirroring the live ordering (no pending yet).
        yield self._stream_event
        # (2) now the SDK raises can_use_tool -> engine injects + registers + holds.
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


async def test_engine_dedups_double_ask_path_keeps_only_injected_resolvable():
    # The substrate stream yields a premature AskEvent (adapter copy) for tu-dup AND
    # the SDK then fires can_use_tool for the same id. The engine must drop the
    # substrate copy and surface ONLY the injected one — which is resolvable.
    from claude_tg.engine.types import QuestionAnswer, TextEvent

    sub = DoublePathSubstrate(
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Pick?"}]},
        tool_use_id="tu-dup",
        stream_event=AskEvent(
            questions=[{"question": "Pick?"}], tool_use_id="tu-dup", session_id="S1"
        ),
        post_factory=lambda d: TextEvent(
            text=f"answered={d.updated_input.get('answers')}", session_id="S1"
        ),
    )
    eng = _wire(sub)
    await eng.start()

    resolved: list[bool] = []

    async def operator():
        # The pending only exists once the INJECTED copy registers it. Poll, then
        # resolve. (The premature substrate copy registers no pending — see false-pass
        # note: without the fix the operator would also see that copy, and resolving
        # against it first would return False.)
        for _ in range(1000):
            if eng._pending.has_pending("tu-dup"):
                break
            await asyncio.sleep(0)
        resolved.append(eng.resolve("tu-dup", QuestionAnswer({"Pick?": "Bravo"})))

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    # EXACTLY ONE AskEvent reaches the operator (the injected, resolvable one) — the
    # substrate-stream duplicate was dropped. Without the fix there would be TWO.
    asks = [e for e in collected if isinstance(e, AskEvent)]
    assert len(asks) == 1
    assert asks[0].tool_use_id == "tu-dup"
    # The decision was honored (pending was registered when the injected copy fired).
    assert resolved == [True]
    assert sub.last_decision.allow is True
    assert sub.last_decision.updated_input["answers"] == {"Pick?": "Bravo"}
    texts = [e.text for e in collected if isinstance(e, TextEvent)]
    assert any("Bravo" in t for t in texts)


async def test_engine_dedups_double_plan_path_keeps_only_injected_resolvable():
    # Same double-path, for ExitPlanMode + a plan REJECT-with-feedback (the v3 case the
    # live run dropped). The reject must be honored against the single injected copy.
    sub = DoublePathSubstrate(
        tool_name="ExitPlanMode",
        tool_input={"plan": "do X then Y"},
        tool_use_id="tu-dup-plan",
        stream_event=PlanEvent(
            plan="do X then Y", tool_use_id="tu-dup-plan", session_id="S1"
        ),
    )
    eng = _wire(sub)
    await eng.start()

    resolved: list[bool] = []

    async def operator():
        for _ in range(1000):
            if eng._pending.has_pending("tu-dup-plan"):
                break
            await asyncio.sleep(0)
        resolved.append(
            eng.resolve("tu-dup-plan", PlanVerdict(approve=False, feedback="revise it"))
        )

    op = asyncio.create_task(operator())
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()

    # EXACTLY ONE PlanEvent (the injected one); the substrate duplicate was dropped.
    plans = [e for e in collected if isinstance(e, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].tool_use_id == "tu-dup-plan"
    # The reject + feedback was honored (resolvable because the pending was registered).
    assert resolved == [True]
    assert sub.last_decision.allow is False
    assert sub.last_decision.message == "revise it"
