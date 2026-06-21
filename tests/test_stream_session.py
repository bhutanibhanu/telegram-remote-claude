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
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.render import encode_callback
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
    def __init__(self, script: list, *, session_id="sess-1"):
        self._script = script
        self.session_id = session_id
        self.resolve_calls: list[tuple[str, object]] = []
        self.cancel_calls: list = []
        self.started = False
        self.resumed: str | None = None
        self.stopped = False
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
        return True

    def cancel(self, tool_use_id=None) -> int:
        self.cancel_calls.append(tool_use_id)
        self._gate.set()
        return 1


class Recorder:
    """Captures the send/edit calls the driver performs."""

    def __init__(self):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self._next_id = 100

    async def send(self, *, text, reply_markup=None, parse_mode=None) -> int:
        self.sends.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        self._next_id += 1
        return self._next_id

    async def edit(self, *, message_id, text, parse_mode=None) -> None:
        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})


def make_session(engine: FakeEngine, *, config=None, store=None, clock=None) -> StreamingSession:
    """A StreamingSession whose factory always returns ``engine`` (no SDK, no network)."""
    return StreamingSession(
        config or make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds: engine,
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
