"""T7 streaming-driver + SB1 callback tests.

Everything here is mock-only — NO live Telegram, NO live Claude, NO network. The
engine is a scripted fake (``FakeEngine``) whose ``send()`` can *hold* until
``resolve()`` is called, so we exercise the real concurrency the live path has: the
turn loop iterates ``engine.send`` while the callback handler calls ``engine.resolve``
on the same loop to unblock it. Every hold is bounded by ``asyncio.wait_for`` so a
wiring bug fails fast rather than hanging the suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_tg.config import Config
from claude_tg.engine.types import (
    AskEvent,
    ErrorEvent,
    PermissionDecision,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.render import RenderAction, encode_callback
from claude_tg.stream_session import StreamingBusy, StreamingSession


def make_config(allowed=(1,), engine_mode="streaming", state_file=None):
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset(allowed),
        workdir=Path("/work"),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=state_file,
        engine_mode=engine_mode,
        answer_backstop_seconds=3600,
    )


# ---------------------------------------------------------------------------
# A scripted fake Engine. resolve()/cancel() record calls; send() yields the
# scripted events and (optionally) PARKS on a "hold" sentinel until resolve fires.
# ---------------------------------------------------------------------------

HOLD = object()  # sentinel in a script: park send() here until a resolve/cancel arrives


class FakeEngine:
    def __init__(self, script: list, *, session_id="sess-1", resolve_result=True):
        self._script = script
        self.session_id = session_id
        self.resolve_calls: list[tuple[str, object]] = []
        self.cancel_calls: list = []
        self.started = False
        self.resumed: str | None = None
        self.stopped = False
        # What resolve() returns — True = a pending request was resolved (the live
        # path); False simulates a stale/already-decided id (nothing pending).
        self._resolve_result = resolve_result
        # Set when send() parks on a HOLD; resolve()/cancel() set it to release.
        self._gate = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def resume(self, session_id: str) -> None:
        self.resumed = session_id
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, prompt: str, *, timeout=None):
        for item in self._script:
            if item is HOLD:
                # Park until the operator resolves (mirrors the held can_use_tool).
                await self._gate.wait()
                self._gate.clear()
                continue
            yield item

    def resolve(self, tool_use_id: str, decision) -> bool:
        self.resolve_calls.append((tool_use_id, decision))
        self._gate.set()
        return self._resolve_result

    def cancel(self, tool_use_id=None) -> int:
        self.cancel_calls.append(tool_use_id)
        self._gate.set()
        return 1


class Recorder:
    """Captures the send/edit/delete calls the driver performs.

    ``fail_html`` (default off) makes ``send`` raise on a ``parse_mode=="HTML"`` call —
    simulating Telegram rejecting a bad HTML entity — so the driver's plain-text fallback
    can be exercised. The raising send is still recorded (so the attempt is observable).
    """

    def __init__(self, *, fail_html: bool = False):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.deletes: list[dict] = []
        self._next_id = 100
        self._fail_html = fail_html

    async def send(self, *, text, reply_markup=None, parse_mode=None) -> int:
        self.sends.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        if self._fail_html and parse_mode == "HTML":
            raise RuntimeError("Telegram BadRequest: can't parse entities")
        self._next_id += 1
        return self._next_id

    async def edit(self, *, message_id, text, parse_mode=None) -> None:
        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})

    async def delete(self, *, message_id) -> None:
        self.deletes.append({"message_id": message_id})


def make_session(engine: FakeEngine, *, config=None, store=None, clock=None) -> StreamingSession:
    """A StreamingSession whose factory always returns ``engine`` (no SDK, no network)."""
    return StreamingSession(
        config or make_config(),
        session_store=store,
        # The factory accepts the chat's shared permission_policy (P2) but the scripted
        # FakeEngine ignores it — the policy mutations under test act on state.policy
        # directly (the SAME object the real engine would receive).
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=clock or (lambda: 0.0),  # frozen clock: every status edit is "due"
    )


# ---------------------------------------------------------------------------
# Streaming on_message drives the engine: scripted events -> sends + keyboard.
# ---------------------------------------------------------------------------


async def test_streaming_turn_renders_events_and_keyboard():
    ask = AskEvent(
        questions=[{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}]}],
        tool_use_id="tid-ask",
    )
    engine = FakeEngine(
        [
            TextEvent(text="Working on it.", incremental=False),
            ask,
            HOLD,  # park until the operator answers
            ResultEvent(session_id="sess-1", is_error=False, subtype="success", result_text="done!"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()

    async def drive():
        await session.handle_message(1, "go", send=rec.send, edit=rec.edit)

    turn = asyncio.create_task(drive())
    # Let the turn run up to the HOLD (the ask is sent with a keyboard, then it parks).
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The verbatim text + the ask (with a keyboard) have been sent.
    texts = [s["text"] for s in rec.sends]
    assert any("Working on it." in t for t in texts)
    ask_send = next(s for s in rec.sends if s["reply_markup"] is not None)
    assert "Color?" in ask_send["text"]
    # The turn is still parked (no result yet).
    assert not turn.done()

    # Operator answers via the callback path (resolve unblocks the held send()).
    outcome = session.resolve_callback(1, encode_callback("a", "tid-ask", question_index=0, option_index=0))
    assert outcome.handled is True
    assert engine.resolve_calls and isinstance(engine.resolve_calls[0][1], QuestionAnswer)
    assert engine.resolve_calls[0][1].answers == {"Color?": "Red"}

    # The turn now continues to completion (bounded so a wiring bug fails fast).
    await asyncio.wait_for(turn, timeout=2.0)
    assert any("done!" in s["text"] for s in rec.sends)


async def test_streaming_status_coalesced_into_edit():
    # tool_use + incremental text are status noise -> one send (create) then edits.
    engine = FakeEngine(
        [
            ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)"),
            TextEvent(text="thinking", incremental=True),
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok"),
        ]
    )
    session = make_session(engine)  # frozen clock => each status flush is due
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The first status line is a send; the second status update edits it in place.
    assert any("Bash" in s["text"] for s in rec.sends)
    assert rec.edits, "second status update should edit the existing status line"
    # The verbatim result is its own new message.
    assert any("ok" in s["text"] for s in rec.sends)


async def test_streaming_busy_second_message_raises():
    engine = FakeEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    rec = Recorder()

    turn = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # A second concurrent turn for the same chat is rejected (harvested ClaudeBusy).
    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "second", send=rec.send, edit=rec.edit)
    # Release + finish the first turn.
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


# ---------------------------------------------------------------------------
# Callback routing (the decision plumbing).
# ---------------------------------------------------------------------------


async def test_resolve_ask_option_maps_to_question_answer():
    ask = AskEvent(
        questions=[{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}],
        tool_use_id="tid",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    # Prime the engine + the held ask (as a live turn would).
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = ask

    outcome = session.resolve_callback(1, encode_callback("a", "tid", question_index=0, option_index=1))
    assert outcome.handled is True
    assert engine.resolve_calls == [("tid", QuestionAnswer(answers={"Pick": "B"}))]


async def test_plan_approve_maps_to_plan_verdict():
    plan = PlanEvent(plan="the plan", tool_use_id="pid")
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_plan = plan

    outcome = session.resolve_callback(1, encode_callback("p", "pid", plan_action="a"))
    assert outcome.handled is True
    assert engine.resolve_calls == [("pid", PlanVerdict(approve=True))]


async def test_plan_reject_then_free_text_resolves_with_feedback():
    plan = PlanEvent(plan="the plan", tool_use_id="pid")
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_plan = plan

    # Reject arms free-text capture (no resolve yet).
    outcome = session.resolve_callback(1, encode_callback("p", "pid", plan_action="r"))
    assert outcome.handled is True and outcome.expects_text is True
    assert engine.resolve_calls == []
    assert session._chat(1).awaiting_text_for == "pid"

    # The NEXT message is captured as the reject feedback (NOT a new turn).
    rec = Recorder()
    await session.handle_message(1, "use a different approach", send=rec.send, edit=rec.edit)
    assert engine.resolve_calls == [
        ("pid", PlanVerdict(approve=False, feedback="use a different approach"))
    ]
    # No new turn was started (no sends), and capture is cleared.
    assert rec.sends == []
    assert session._chat(1).awaiting_text_for is None


async def test_ask_other_then_free_text_resolves_with_answer():
    ask = AskEvent(
        questions=[{"question": "Name?", "options": [{"label": "A"}]}],
        tool_use_id="tid",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = ask

    outcome = session.resolve_callback(1, encode_callback("o", "tid", question_index=0))
    assert outcome.handled is True and outcome.expects_text is True
    assert engine.resolve_calls == []

    rec = Recorder()
    await session.handle_message(1, "Charlie", send=rec.send, edit=rec.edit)
    assert engine.resolve_calls == [("tid", QuestionAnswer(answers={"Name?": "Charlie"}))]


# ---------------------------------------------------------------------------
# Multi-question AskUserQuestion — one AskUserQuestion carries SEVERAL questions under a
# single tool_use_id; the relay must accumulate per-question answers and resolve ONCE all
# are answered. Regression for the live phone-verify bug where the first option tap
# resolved the whole ask with a 1-of-N answer map, stranding the remaining questions and
# making grill error out. (Neither the P1 nor P2 live probe caught this — both injected
# answers via engine.resolve directly, bypassing the per-tap path.)
# ---------------------------------------------------------------------------


async def test_multi_question_ask_resolves_only_after_all_answered():
    """A 3-question ask holds open until every question is answered, then resolves ONCE
    with the full native map. The first two taps accumulate without resolving."""
    ask = AskEvent(
        questions=[
            {"question": "Q1", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2", "options": [{"label": "A2"}, {"label": "B2"}]},
            {"question": "Q3", "options": [{"label": "A3"}, {"label": "B3"}]},
        ],
        tool_use_id="multi",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = ask

    out0 = session.resolve_callback(1, encode_callback("a", "multi", question_index=0, option_index=0))
    assert out0.handled is True  # accepted, but...
    assert engine.resolve_calls == []  # ...NOT resolved yet
    assert session._chat(1).pending_ask is ask  # the ask is still held

    out1 = session.resolve_callback(1, encode_callback("a", "multi", question_index=1, option_index=1))
    assert out1.handled is True
    assert engine.resolve_calls == []  # still holding (2 of 3)

    out2 = session.resolve_callback(1, encode_callback("a", "multi", question_index=2, option_index=0))
    assert out2.handled is True
    # Resolved exactly once, with ALL three answers keyed by question text.
    assert engine.resolve_calls == [
        ("multi", QuestionAnswer(answers={"Q1": "A1", "Q2": "B2", "Q3": "A3"}))
    ]
    assert session._chat(1).pending_ask is None  # cleared after the full resolve


async def test_multi_question_ask_retap_overwrites_choice():
    """Re-tapping a question before completion overwrites that answer (count unchanged),
    so the operator can change a choice before the final tap resolves the ask."""
    ask = AskEvent(
        questions=[
            {"question": "Q1", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2", "options": [{"label": "A2"}, {"label": "B2"}]},
        ],
        tool_use_id="multi2",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = ask

    session.resolve_callback(1, encode_callback("a", "multi2", question_index=0, option_index=0))  # Q1=A1
    session.resolve_callback(1, encode_callback("a", "multi2", question_index=0, option_index=1))  # Q1=B1 (overwrite)
    assert engine.resolve_calls == []  # only ONE distinct question answered so far
    session.resolve_callback(1, encode_callback("a", "multi2", question_index=1, option_index=0))  # Q2=A2
    assert engine.resolve_calls == [
        ("multi2", QuestionAnswer(answers={"Q1": "B1", "Q2": "A2"}))  # the overwrite stuck
    ]


async def test_multi_question_ask_mixed_option_and_free_text():
    """A multi-question ask can be completed by mixing an option tap and an "Other"
    free-text answer; it resolves only when the LAST question is answered."""
    ask = AskEvent(
        questions=[
            {"question": "Q1", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2", "options": [{"label": "A2"}]},
        ],
        tool_use_id="multi3",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = ask

    # Answer Q2 by tapping its option — not complete yet (Q1 still open).
    session.resolve_callback(1, encode_callback("a", "multi3", question_index=1, option_index=0))  # Q2=A2
    assert engine.resolve_calls == []
    # Answer Q1 via "Other" → free text; the typed answer completes the ask → one resolve.
    out = session.resolve_callback(1, encode_callback("o", "multi3", question_index=0))
    assert out.expects_text is True
    assert engine.resolve_calls == []
    rec = Recorder()
    await session.handle_message(1, "custom answer", send=rec.send, edit=rec.edit)
    assert engine.resolve_calls == [
        ("multi3", QuestionAnswer(answers={"Q2": "A2", "Q1": "custom answer"}))
    ]
    assert session._chat(1).pending_ask is None


async def test_multi_question_ask_renders_one_message_per_question():
    """A multi-question ask is sent as ONE message per question, each with its OWN keyboard
    — not a single stacked wall of buttons (the live phone-verify UX complaint)."""
    ask = AskEvent(
        questions=[
            {"question": "Storage?", "options": [{"label": "JSON"}, {"label": "SQLite"}]},
            {"question": "CLI?", "options": [{"label": "argparse"}, {"label": "Typer"}]},
        ],
        tool_use_id="tid-multi",
    )
    engine = FakeEngine(
        [ask, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done")]
    )
    session = make_session(engine)
    rec = Recorder()

    async def drive():
        await session.handle_message(1, "go", send=rec.send, edit=rec.edit)

    turn = asyncio.create_task(drive())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Two keyboarded messages — one per question — each numbered, each carrying ONLY its options.
    keyboarded = [s for s in rec.sends if s["reply_markup"] is not None]
    assert len(keyboarded) == 2
    assert "(1/2)" in keyboarded[0]["text"] and "Storage?" in keyboarded[0]["text"]
    assert "(2/2)" in keyboarded[1]["text"] and "CLI?" in keyboarded[1]["text"]
    labels_q1 = [b.text for row in keyboarded[1]["reply_markup"].inline_keyboard for b in row]
    assert "argparse" in labels_q1 and "JSON" not in labels_q1  # only Q1's own options

    # Answering one question does NOT resolve; the second (final) tap resolves the whole ask.
    session.resolve_callback(1, encode_callback("a", "tid-multi", question_index=0, option_index=0))
    assert engine.resolve_calls == []
    session.resolve_callback(1, encode_callback("a", "tid-multi", question_index=1, option_index=1))
    assert engine.resolve_calls == [
        ("tid-multi", QuestionAnswer(answers={"Storage?": "JSON", "CLI?": "Typer"}))
    ]
    await asyncio.wait_for(turn, timeout=2.0)


async def test_identical_status_line_is_not_resent():
    """An unchanged status line is skipped — editing a Telegram message to identical text
    errors, and the old fallback re-sent a duplicate message (the status-line spam)."""
    engine = FakeEngine([])
    session = make_session(engine)
    state = session._chat(1)
    rec = Recorder()
    thinking = RenderAction(op="edit_status", chunks=("💭 Claude is thinking…",))
    await session._perform(state, thinking, send=rec.send, edit=rec.edit)  # first → one send
    await session._perform(state, thinking, send=rec.send, edit=rec.edit)  # identical → skip
    await session._perform(state, thinking, send=rec.send, edit=rec.edit)  # identical → skip
    assert len(rec.sends) == 1  # ONE status message, not three
    assert rec.edits == []  # no edit attempted for identical text
    # A CHANGED line edits the existing message in place (no new message).
    await session._perform(
        state, RenderAction(op="edit_status", chunks=("⏳ rate limited",)), send=rec.send, edit=rec.edit
    )
    assert len(rec.sends) == 1 and len(rec.edits) == 1


# ---------------------------------------------------------------------------
# Permission taps (P2) — m|tid|o / |s / |d -> the engine's PermissionDecision verdict.
# ---------------------------------------------------------------------------


async def test_permission_allow_once_maps_to_decision():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)

    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="o"))
    assert outcome.handled is True
    assert outcome.note == "Allowed once"
    assert engine.resolve_calls == [("tid", PermissionDecision(verdict="allow_once"))]


async def test_permission_allow_session_maps_to_decision():
    # The SESSION only routes the verdict; the GRANT is recorded by the engine on
    # resolve (T3 _verdict_for), so the session must NOT touch the policy here.
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)

    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="s"))
    assert outcome.handled is True
    assert outcome.note == "Allowed for session"
    assert engine.resolve_calls == [("tid", PermissionDecision(verdict="allow_session"))]
    # No grant recorded by the session itself (the engine owns that — fake doesn't).
    assert session._chat(1).policy.granted_tools() == frozenset()


async def test_permission_deny_maps_to_decision():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)

    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="d"))
    assert outcome.handled is True
    assert outcome.note == "Denied"
    assert engine.resolve_calls == [("tid", PermissionDecision(verdict="deny"))]


async def test_permission_tap_with_no_active_engine_is_ignored():
    engine = FakeEngine([])
    session = make_session(engine)  # engine not started for this chat
    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="o"))
    assert outcome.handled is False
    assert engine.resolve_calls == []


async def test_permission_tap_for_stale_request_returns_not_handled():
    # A well-formed permission tap whose id has nothing pending (already decided /
    # backstopped): the engine's resolve() returns False -> handled=False, benign note.
    engine = FakeEngine([], resolve_result=False)
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    outcome = session.resolve_callback(1, encode_callback("m", "gone", payload="o"))
    assert outcome.handled is False
    assert outcome.note == "no pending request"
    # resolve() WAS attempted (id alone routes a permission verdict) but found nothing.
    assert engine.resolve_calls == [("gone", PermissionDecision(verdict="allow_once"))]


# ---------------------------------------------------------------------------
# /yolo + /reset policy state (P2, D6/D7) on the shared per-chat policy.
# ---------------------------------------------------------------------------


async def test_set_yolo_flips_chat_policy():
    engine = FakeEngine([])
    session = make_session(engine)
    assert session._chat(1).policy.yolo is False
    session.set_yolo(1, True)
    assert session._chat(1).policy.yolo is True
    session.set_yolo(1, False)
    assert session._chat(1).policy.yolo is False


async def test_reset_clears_policy_grants_and_yolo():
    # D7: /reset must drop allow-session grants AND turn /yolo off so the next session
    # starts fail-closed. False-pass guard: if reset() skipped policy.clear() this fails.
    engine = FakeEngine([])
    session = make_session(engine)
    state = session._chat(1)
    state.policy.set_yolo(True)
    state.policy.grant_session("Bash")
    assert state.policy.yolo is True and state.policy.granted_tools() == frozenset({"Bash"})

    session.reset(1)
    assert state.policy.yolo is False
    assert state.policy.granted_tools() == frozenset()


async def test_driven_turn_shows_loud_yolo_indicator():
    # D6 "loud throughout": with /yolo on, a driven turn leads with a ⚠️ marker so an
    # in-progress allow-all session is never silent.
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_session(engine)
    session.set_yolo(1, True)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("⚠️" in s["text"] for s in rec.sends), "yolo turn must carry a loud ⚠️"


async def test_driven_turn_has_no_yolo_indicator_when_off():
    # Inverse: with the gate on (yolo off) no ⚠️ marker is prepended to the turn.
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert not any("⚠️ YOLO" in s["text"] for s in rec.sends)


# ---------------------------------------------------------------------------
# SB1 / RB1: malformed / stale / no-session callbacks NEVER resolve.
# ---------------------------------------------------------------------------


async def test_malformed_callback_never_resolves():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="tid"
    )
    for bad in ["garbage", "a|tid", "x|tid|0.0", 12345, None, "a|other|0.0", ""]:
        outcome = session.resolve_callback(1, bad)
        assert outcome.handled is False
    assert engine.resolve_calls == [], "no malformed/foreign callback may resolve a decision"


async def test_callback_for_unknown_id_does_not_resolve():
    # A well-formed callback whose tool_use_id does not match the held ask is ignored.
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    session._chat(1).pending_ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="held-id"
    )
    outcome = session.resolve_callback(1, encode_callback("a", "stale-id", question_index=0, option_index=0))
    assert outcome.handled is False
    assert engine.resolve_calls == []


async def test_callback_with_no_active_engine_is_ignored():
    engine = FakeEngine([])
    session = make_session(engine)  # engine not started, no pending ask
    outcome = session.resolve_callback(1, encode_callback("a", "tid", question_index=0, option_index=0))
    assert outcome.handled is False
    assert engine.resolve_calls == []


async def test_stale_option_index_does_not_crash_or_resolve():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(session._chat(1), 1)
    # Held ask has a single option; a tap for option 9 is stale -> ignored, no crash.
    session._chat(1).pending_ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="tid"
    )
    outcome = session.resolve_callback(1, encode_callback("a", "tid", question_index=0, option_index=9))
    assert outcome.handled is False
    assert engine.resolve_calls == []


# ---------------------------------------------------------------------------
# /cancel + RB2 (error event renders cleanly).
# ---------------------------------------------------------------------------


async def test_handle_cancel_calls_engine_cancel():
    engine = FakeEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    aborted = session.handle_cancel(1)
    assert aborted == 1
    assert engine.cancel_calls == [None]
    await asyncio.wait_for(turn, timeout=2.0)


async def test_handle_cancel_idle_chat_is_noop():
    engine = FakeEngine([])
    session = make_session(engine)
    assert session.handle_cancel(1) == 0  # no engine started for this chat
    assert engine.cancel_calls == []


async def test_error_event_renders_clean_message():
    engine = FakeEngine(
        [
            ErrorEvent(kind_of_error="tool_error", message="it broke"),
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The error is a clean verbatim message (no traceback), its own send.
    err_send = next(s for s in rec.sends if "it broke" in s["text"])
    assert err_send["text"].startswith("⚠️")


# ---------------------------------------------------------------------------
# session_id persistence from the result event.
# ---------------------------------------------------------------------------


async def test_result_event_persists_session_id(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    engine = FakeEngine(
        [ResultEvent(session_id="abc-123", is_error=False, subtype="success", result_text="ok")],
        session_id="abc-123",
    )
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    saved = store.load()
    assert saved["1"]["session_id"] == "abc-123"


async def test_resume_uses_persisted_session_id(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.update(7, session_id="prev-sess", cwd="/work")

    engine = FakeEngine([ResultEvent(session_id="prev-sess", is_error=False, subtype="success")])
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(7, "hi", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.resumed == "prev-sess"  # resumed, not started fresh
    assert engine.started is True


# ---------------------------------------------------------------------------
# CommonMark -> HTML rendering of prose + the HTML->plain send fallback.
# ---------------------------------------------------------------------------


async def test_prose_is_sent_as_html():
    # An assembled Claude reply with markdown is sent with parse_mode="HTML" and the
    # markdown is converted (**x** -> <b>x</b>).
    engine = FakeEngine(
        [
            TextEvent(text="A **bold** answer.", incremental=False),
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    prose = next(s for s in rec.sends if "bold" in s["text"])
    assert prose["parse_mode"] == "HTML"
    assert "<b>bold</b>" in prose["text"]


async def test_html_send_failure_falls_back_to_raw_markdown():
    # CRITICAL: if Telegram rejects the HTML chunk, the driver resends the ORIGINAL raw
    # markdown for that chunk with parse_mode=None — never a dropped message, worst case
    # equals today's behavior (raw markdown).
    engine = FakeEngine(
        [
            TextEvent(text="A **bold** answer.", incremental=False),
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_session(engine)
    rec = Recorder(fail_html=True)  # every HTML send raises
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The HTML attempt was made AND a plain resend followed with the RAW markdown.
    html_attempt = next(s for s in rec.sends if s["parse_mode"] == "HTML" and "bold" in s["text"])
    assert "<b>bold</b>" in html_attempt["text"]
    plain_resend = next(
        s for s in rec.sends if s["parse_mode"] is None and "**bold**" in s["text"]
    )
    assert plain_resend["text"] == "A **bold** answer."  # raw, NOT the HTML


async def test_html_fallback_preserves_keyboard_on_first_chunk():
    # The plan keyboard must still ride the (plain) fallback message when HTML is rejected.
    engine = FakeEngine(
        [
            PlanEvent(plan="Do **step one**", tool_use_id="pid"),
            HOLD,
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_session(engine)
    rec = Recorder(fail_html=True)

    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The plan was attempted as HTML (failed) then resent plain — WITH the keyboard.
    plain_plan = next(
        s for s in rec.sends if s["parse_mode"] is None and "step one" in s["text"]
    )
    assert plain_plan["reply_markup"] is not None
    assert "**step one**" in plain_plan["text"]  # raw markdown
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_plain_send_failure_is_not_swallowed():
    # A genuine plain-text send failure (parse_mode=None) has nothing left to fall back
    # to, so it must propagate (not be silently swallowed by the HTML-fallback path).
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=1)]
    )
    session = make_session(engine)

    class BoomRecorder(Recorder):
        async def send(self, *, text, reply_markup=None, parse_mode=None):
            raise RuntimeError("network down")

    rec = BoomRecorder()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
        )


async def test_ask_question_sent_as_html_with_keyboard():
    ask = AskEvent(
        questions=[{"question": "Pick **A** or B?", "options": [{"label": "A"}, {"label": "B"}]}],
        tool_use_id="tid",
    )
    engine = FakeEngine([ask, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    q = next(s for s in rec.sends if s["reply_markup"] is not None)
    assert q["parse_mode"] == "HTML"
    assert "<b>A</b>" in q["text"]
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_ask_question_html_failure_falls_back_to_plain():
    ask = AskEvent(
        questions=[{"question": "Pick **A**?", "options": [{"label": "A"}]}],
        tool_use_id="tid",
    )
    engine = FakeEngine([ask, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    rec = Recorder(fail_html=True)
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # HTML attempt failed -> plain resend carrying the raw question + the keyboard.
    plain_q = next(
        s for s in rec.sends if s["parse_mode"] is None and s["reply_markup"] is not None
    )
    assert "**A**" in plain_q["text"]  # raw markdown question
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


# ---------------------------------------------------------------------------
# Status-line cleanup at turn end (delete the transient "thinking…" message).
# ---------------------------------------------------------------------------


async def test_status_message_deleted_at_turn_end():
    # After a turn completes, the transient status line is deleted (delete closure called
    # with its id) and status_message_id is reset to None.
    engine = FakeEngine(
        [
            TextEvent(text="thinking", incremental=True),  # creates the status line
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done"),
        ]
    )
    session = make_session(engine)  # frozen clock => the status edit is "due"
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
        timeout=2.0,
    )
    # A status line was created, then deleted at the end of the turn.
    assert rec.deletes, "the transient status line must be deleted at turn end"
    assert session._chat(1).status_message_id is None
    assert session._chat(1).status_text is None


async def test_no_status_message_means_no_delete():
    # A turn with no status line (no incremental/tool_use noise) has nothing to delete.
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done")]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
        timeout=2.0,
    )
    assert rec.deletes == []


async def test_status_delete_is_optional():
    # Existing callers that do NOT pass a delete closure still work (the status line just
    # stays, as before) — no crash, no requirement.
    engine = FakeEngine(
        [
            TextEvent(text="thinking", incremental=True),
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    # No delete= passed.
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("done" in s["text"] for s in rec.sends)


async def test_status_delete_failure_does_not_kill_turn():
    # RB1: a failing delete (message gone / too old) must never kill the turn — the real
    # content is already sent. The status id is still reset.
    engine = FakeEngine(
        [
            TextEvent(text="thinking", incremental=True),
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()

    async def boom_delete(*, message_id):
        raise RuntimeError("message to delete not found")

    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=boom_delete),
        timeout=2.0,
    )
    assert any("done" in s["text"] for s in rec.sends)  # turn completed cleanly
    assert session._chat(1).status_message_id is None  # still reset


async def test_keyboard_attaches_to_first_non_empty_chunk():
    """If the head chunk is whitespace-only it's skipped — but the keyboard must still ride
    the first REAL chunk, else an ask/plan whose body chunked with a blank head would lose
    its buttons entirely (audit P0)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    engine = FakeEngine([])
    session = make_session(engine)
    state = session._chat(1)
    rec = Recorder()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="x")]])
    action = RenderAction(op="new", chunks=("   ", "real content"), reply_markup=kb)
    await session._perform(state, action, send=rec.send, edit=rec.edit)
    # Only the non-empty chunk is sent, and it carries the keyboard.
    assert [s["text"] for s in rec.sends] == ["real content"]
    assert rec.sends[0]["reply_markup"] is kb
