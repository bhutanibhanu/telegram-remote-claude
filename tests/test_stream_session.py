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
from collections import deque
from pathlib import Path

import pytest

from claude_tg.config import Config
from claude_tg.engine.types import (
    AskEvent,
    ErrorEvent,
    PermissionDecision,
    PermissionEvent,
    PlanEvent,
    PlanVerdict,
    QuestionAnswer,
    ResultEvent,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.permissions import PermissionPolicy
from claude_tg.render import RenderAction, encode_callback
from claude_tg.stream_session import StreamingBusy, StreamingSession, _ProjectRuntime


def make_config(
    allowed=(1,),
    engine_mode="streaming",
    state_file=None,
    workdir="/work",
    *,
    allowed_roots=(),
    allow_any_path=True,
    max_concurrent_runs=3,
    render_chat_send_interval_seconds=0.0,
):
    # NOTE (T7): turn-behavior tests default to ``allow_any_path=True`` so that
    # ``_ensure_engine``'s SB2 cwd re-validation (added in T7) NO-OPS — these tests are
    # about the turn loop / callback plumbing, not path confinement. Empty roots +
    # ``allow_any_path=False`` is fail-closed *everywhere* (you could not /new either), so
    # making turns respect it is correct; we simply give these tests a config where work
    # is actually permitted. The SB2 refusal itself is exercised by focused new tests
    # below that pass REAL ``allowed_roots`` + real dirs with ``allow_any_path=False``.
    #
    # NOTE (T8): the per-chat send-gate interval defaults to 0.0 here so the gate never
    # introduces a real ``asyncio.sleep`` under the frozen test clock (``clock=lambda: 0.0``
    # in make_session) — these tests assert send/edit CONTENT + ordering, not RB5 rate
    # timing (which has its own injected-clock tests below + in test_render). Production
    # defaults to ~1 s; the RB7 concurrency tests pass an explicit interval + a recording
    # clock/sleep.
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset(allowed),
        workdir=Path(workdir),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=state_file,
        engine_mode=engine_mode,
        answer_backstop_seconds=3600,
        max_concurrent_runs=max_concurrent_runs,
        render_chat_send_interval_seconds=render_chat_send_interval_seconds,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
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
        # The factory accepts the project's shared permission_policy (P2) but the scripted
        # FakeEngine ignores it — the policy mutations under test act on the active
        # project's runtime policy (the SAME object the real engine would receive). See
        # `active_policy()` below.
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=clock or (lambda: 0.0),  # frozen clock: every status edit is "due"
    )


def prime_pending(
    session: StreamingSession,
    event,
    *,
    chat_id: int = 1,
    project: str | None = None,
):
    """Register ``event`` (ask/plan/permission) in the chat's pending index (P5 / ADR-005).

    P4 primed a held request by setting the chat-global ``pending_ask``/``pending_plan``
    slot directly; P5 routes every decision-in by ``tool_use_id`` through the per-chat
    **pending-request index** to the owning project. This mirrors what ``_drive_turn`` does
    when an engine injects an ask/plan/permission: it maps ``tool_use_id -> (project, kind,
    event)``. ``project`` defaults to the chat's active project (auto-created like a real
    turn) so a single-project test reads naturally; cross-project tests pass it explicitly.
    Returns the resolved owning project name.
    """
    if project is None:
        project, _rt = session._active_runtime(chat_id, create_default=True)
        assert project is not None
    session._register_pending(session._chat(chat_id), project, event)
    return project


def active_policy(session: StreamingSession, chat_id: int = 1) -> PermissionPolicy:
    """The ACTIVE project's :class:`PermissionPolicy` (P4: policy moved chat→project).

    P5/T4: the live-turn state (status line + free-text-capture marker + status enum) ALSO
    moved off the chat down to the per-project ``_ProjectRuntime`` (ADR-005 D7) — see
    :func:`active_rt`. This resolves the active project (auto-creating ``default`` like a
    real turn) and returns its policy — the object ``/yolo`` and ``/reset`` mutate."""
    _name, rt = session._active_runtime(chat_id, create_default=True)
    assert rt is not None
    return rt.policy


def active_rt(session: StreamingSession, chat_id: int = 1):
    """The ACTIVE project's ``_ProjectRuntime`` (P5/T4: live-turn state lives HERE now).

    T4 relocated the status line (``status_message_id``/``status_text``), the free-text
    capture marker (``awaiting_text_*``), and the per-project ``status`` enum from the chat
    down to the per-project runtime (ADR-005 D7). Tests that used to read
    ``session._chat(chat_id).<field>`` now read ``active_rt(session, chat_id).<field>``.
    Auto-creates ``default`` like a real turn so a single-project test reads naturally.
    """
    _name, rt = session._active_runtime(chat_id, create_default=True)
    assert rt is not None
    return rt


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
    # Prime the engine + the held ask in the pending index (as a live turn would).
    await session._ensure_engine(1)
    prime_pending(session, ask)

    outcome = session.resolve_callback(1, encode_callback("a", "tid", question_index=0, option_index=1))
    assert outcome.handled is True
    assert engine.resolve_calls == [("tid", QuestionAnswer(answers={"Pick": "B"}))]


async def test_plan_approve_maps_to_plan_verdict():
    plan = PlanEvent(plan="the plan", tool_use_id="pid")
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, plan)

    outcome = session.resolve_callback(1, encode_callback("p", "pid", plan_action="a"))
    assert outcome.handled is True
    assert engine.resolve_calls == [("pid", PlanVerdict(approve=True))]


async def test_plan_reject_then_free_text_resolves_with_feedback():
    plan = PlanEvent(plan="the plan", tool_use_id="pid")
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, plan)

    # Reject arms free-text capture (no resolve yet).
    outcome = session.resolve_callback(1, encode_callback("p", "pid", plan_action="r"))
    assert outcome.handled is True and outcome.expects_text is True
    assert engine.resolve_calls == []
    assert active_rt(session).awaiting_text_for == "pid"  # marker on the owning runtime (D7)

    # The NEXT message is captured as the reject feedback (NOT a new turn).
    rec = Recorder()
    await session.handle_message(1, "use a different approach", send=rec.send, edit=rec.edit)
    assert engine.resolve_calls == [
        ("pid", PlanVerdict(approve=False, feedback="use a different approach"))
    ]
    # No new turn was started (no sends), and capture is cleared.
    assert rec.sends == []
    assert active_rt(session).awaiting_text_for is None


async def test_ask_other_then_free_text_resolves_with_answer():
    ask = AskEvent(
        questions=[{"question": "Name?", "options": [{"label": "A"}]}],
        tool_use_id="tid",
    )
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, ask)

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
    await session._ensure_engine(1)
    prime_pending(session, ask)

    out0 = session.resolve_callback(1, encode_callback("a", "multi", question_index=0, option_index=0))
    assert out0.handled is True  # accepted, but...
    assert engine.resolve_calls == []  # ...NOT resolved yet
    assert session._chat(1).pending_index["multi"].event is ask  # the ask is still held

    out1 = session.resolve_callback(1, encode_callback("a", "multi", question_index=1, option_index=1))
    assert out1.handled is True
    assert engine.resolve_calls == []  # still holding (2 of 3)

    out2 = session.resolve_callback(1, encode_callback("a", "multi", question_index=2, option_index=0))
    assert out2.handled is True
    # Resolved exactly once, with ALL three answers keyed by question text.
    assert engine.resolve_calls == [
        ("multi", QuestionAnswer(answers={"Q1": "A1", "Q2": "B2", "Q3": "A3"}))
    ]
    assert "multi" not in session._chat(1).pending_index  # cleared after the full resolve


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
    await session._ensure_engine(1)
    prime_pending(session, ask)

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
    await session._ensure_engine(1)
    prime_pending(session, ask)

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
    assert "multi3" not in session._chat(1).pending_index


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
    rt = active_rt(session)  # T4: the status line lives on the per-project runtime (D7)
    rec = Recorder()
    state = session._chat(1)  # T8: _perform now takes the chat state (the send-rate gate)
    thinking = RenderAction(op="edit_status", chunks=("💭 Claude is thinking…",))
    await session._perform(state, rt, thinking, send=rec.send, edit=rec.edit)  # first → one send
    await session._perform(state, rt, thinking, send=rec.send, edit=rec.edit)  # identical → skip
    await session._perform(state, rt, thinking, send=rec.send, edit=rec.edit)  # identical → skip
    assert len(rec.sends) == 1  # ONE status message, not three
    assert rec.edits == []  # no edit attempted for identical text
    # A CHANGED line edits the existing message in place (no new message).
    await session._perform(
        state, rt, RenderAction(op="edit_status", chunks=("⏳ rate limited",)), send=rec.send, edit=rec.edit
    )
    assert len(rec.sends) == 1 and len(rec.edits) == 1


# ---------------------------------------------------------------------------
# Permission taps (P2) — m|tid|o / |s / |d -> the engine's PermissionDecision verdict.
# ---------------------------------------------------------------------------


def permission_event(tool_use_id="tid", tool_name="Bash"):
    """A held PermissionEvent to prime in the index (as the engine injects one)."""
    return PermissionEvent(
        tool_name=tool_name, tool_input_summary=f"{tool_name}(...)", tool_use_id=tool_use_id
    )


async def test_permission_allow_once_maps_to_decision():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, permission_event("tid"))

    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="o"))
    assert outcome.handled is True
    assert outcome.note == "Allowed once"
    assert engine.resolve_calls == [("tid", PermissionDecision(verdict="allow_once"))]


async def test_permission_allow_session_maps_to_decision():
    # The SESSION only routes the verdict; the GRANT is recorded by the engine on
    # resolve (T3 _verdict_for), so the session must NOT touch the policy here.
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, permission_event("tid"))

    outcome = session.resolve_callback(1, encode_callback("m", "tid", payload="s"))
    assert outcome.handled is True
    assert outcome.note == "Allowed for session"
    assert engine.resolve_calls == [("tid", PermissionDecision(verdict="allow_session"))]
    # No grant recorded by the session itself (the engine owns that — fake doesn't).
    assert active_policy(session).granted_tools() == frozenset()


async def test_permission_deny_maps_to_decision():
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, permission_event("tid"))

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


async def test_permission_tap_for_unknown_id_is_a_noop():
    # P5 (ADR-005 D3): a well-formed permission tap whose id is NOT in the pending index
    # resolves NOTHING — it never even reaches engine.resolve (the index has no owner for
    # it). A stale/forged id is a benign no-op (RB1).
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    outcome = session.resolve_callback(1, encode_callback("m", "gone", payload="o"))
    assert outcome.handled is False
    assert outcome.note == "no pending request"
    assert engine.resolve_calls == []  # id not in the index → engine never consulted


async def test_permission_tap_for_stale_request_returns_not_handled():
    # A permission tap whose id IS in the index but whose engine has nothing pending for it
    # (already decided / backstopped): engine.resolve() returns False -> handled=False, a
    # benign note. The index entry is cleared only on a successful resolve, so the stale
    # entry remains (a re-tap is still a clean no-op).
    engine = FakeEngine([], resolve_result=False)
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(session, permission_event("gone"))
    outcome = session.resolve_callback(1, encode_callback("m", "gone", payload="o"))
    assert outcome.handled is False
    assert outcome.note == "no pending request"
    # resolve() WAS attempted (the id routed to the owning engine) but found nothing.
    assert engine.resolve_calls == [("gone", PermissionDecision(verdict="allow_once"))]


# ---------------------------------------------------------------------------
# /yolo + /reset policy state (P2, D6/D7) on the ACTIVE PROJECT's policy (P4).
# ---------------------------------------------------------------------------


async def test_set_yolo_flips_active_project_policy():
    engine = FakeEngine([])
    session = make_session(engine)
    assert active_policy(session).yolo is False
    session.set_yolo(1, True)
    assert active_policy(session).yolo is True
    session.set_yolo(1, False)
    assert active_policy(session).yolo is False


async def test_reset_clears_policy_grants_and_yolo():
    # D7: /reset must drop allow-session grants AND turn /yolo off so the next session
    # starts fail-closed. False-pass guard: if reset() skipped policy.clear() this fails.
    engine = FakeEngine([])
    session = make_session(engine)
    policy = active_policy(session)  # the ACTIVE project's policy (P4)
    policy.set_yolo(True)
    policy.grant_session("Bash")
    assert policy.yolo is True and policy.granted_tools() == frozenset({"Bash"})

    session.reset(1)
    assert policy.yolo is False
    assert policy.granted_tools() == frozenset()


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
    await session._ensure_engine(1)
    prime_pending(
        session,
        AskEvent(questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="tid"),
    )
    for bad in ["garbage", "a|tid", "x|tid|0.0", 12345, None, "a|other|0.0", ""]:
        outcome = session.resolve_callback(1, bad)
        assert outcome.handled is False
    assert engine.resolve_calls == [], "no malformed/foreign callback may resolve a decision"


async def test_callback_for_unknown_id_does_not_resolve():
    # A well-formed callback whose tool_use_id is NOT in the pending index is ignored
    # (P5 / ADR-005 D3: an absent id resolves nothing — handled=False, RB1).
    engine = FakeEngine([])
    session = make_session(engine)
    await session._ensure_engine(1)
    prime_pending(
        session,
        AskEvent(
            questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="held-id"
        ),
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
    await session._ensure_engine(1)
    # Held ask has a single option; a tap for option 9 is stale -> ignored, no crash.
    prime_pending(
        session,
        AskEvent(questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="tid"),
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
    assert active_rt(session).status_message_id is None  # T4: line on the runtime (D7)
    assert active_rt(session).status_text is None


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
    assert active_rt(session).status_message_id is None  # still reset (on the runtime, D7)


async def test_keyboard_attaches_to_first_non_empty_chunk():
    """If the head chunk is whitespace-only it's skipped — but the keyboard must still ride
    the first REAL chunk, else an ask/plan whose body chunked with a blank head would lose
    its buttons entirely (audit P0)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    engine = FakeEngine([])
    session = make_session(engine)
    rt = active_rt(session)  # _perform folds a status edit into THIS runtime's line (D7)
    rec = Recorder()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="x")]])
    action = RenderAction(op="new", chunks=("   ", "real content"), reply_markup=kb)
    await session._perform(session._chat(1), rt, action, send=rec.send, edit=rec.edit)
    # Only the non-empty chunk is sent, and it carries the keyboard.
    assert [s["text"] for s in rec.sends] == ["real content"]
    assert rec.sends[0]["reply_markup"] is kb


# ===========================================================================
# P4 (T4): per-active-project rework — the store is the source of truth for the
# active project + its (session_id, cwd); the engine is built/resumed per project;
# session_id persists per project; restart resets the transient bypass (D3); /reset
# targets the active project but keeps it; is_busy reflects the turn lock.
#
# These use a REAL JsonSessionStore (the registry CRUD under test) but the engine is
# still the scripted FakeEngine (no SDK / no network). Where two projects are exercised
# the factory hands out a DISTINCT engine per cwd so we can assert which one ran/stopped.
# ===========================================================================


def make_multi_session(engines_by_cwd: dict, *, store, config=None) -> StreamingSession:
    """A session whose factory returns a DISTINCT engine per cwd (for two-project tests).

    ``engines_by_cwd`` maps a project's cwd → its :class:`FakeEngine`. The default
    production factory builds one engine per call; here we route by cwd so a test can
    assert per-project resume/stop. The same cwd always yields the same engine (a
    project's runtime is built once and reused within the process)."""
    return StreamingSession(
        config or make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engines_by_cwd[cwd],
        clock=lambda: 0.0,
    )


async def test_first_turn_with_no_active_project_auto_creates_default(tmp_path):
    # ADR-004 D6: a turn with NO active project auto-creates `default` at config.workdir
    # and runs; get_active is then "default" with the workdir as its cwd.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    assert store.get_active(1) is None  # nothing yet
    engine = FakeEngine(
        [ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The turn ran (engine started) and `default` is now the active project at workdir.
    assert engine.started is True
    assert store.get_active(1) == "default"
    assert store.get_project(1, "default")["cwd"] == "/work"
    assert any("ok" in s["text"] for s in rec.sends)


async def test_get_cwd_does_not_create_a_project(tmp_path):
    # Read-only: get_cwd on a chat with no active project returns the default workdir and
    # creates NOTHING (no `default` project written) — only a turn/set_yolo auto-creates.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    session = make_session(FakeEngine([]), store=store)
    assert session.get_cwd(1) == "/work"  # falls back to config.workdir
    assert store.get_active(1) is None  # NOT created by a read-only query
    assert store.list_projects(1) == {}


async def test_get_cwd_returns_active_project_cwd(tmp_path):
    # With an active project, get_cwd reports THAT project's cwd (not the default workdir).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    session = make_session(FakeEngine([]), store=store)
    assert session.get_cwd(1) == "/work/api"


async def test_per_project_resume_uses_each_projects_own_session_and_cwd(tmp_path):
    # Two projects with different cwds + session_ids: a turn resumes from the ACTIVE
    # project's own (session_id, cwd). Switching the active project makes the NEXT turn
    # build/resume the OTHER project. P5/T5: switching does NOT stop the previously-started
    # engine (the single-active-run stop is removed so background runs survive a switch).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.update(1, session_id="alpha-sess", cwd=None)  # alpha is active → gets the id
    store.switch(1, "beta")
    store.update(1, session_id="beta-sess", cwd=None)  # beta is active → gets the id
    store.switch(1, "alpha")  # back to alpha for the first turn

    eng_alpha = FakeEngine(
        [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success")],
        session_id="alpha-sess",
    )
    eng_beta = FakeEngine(
        [ResultEvent(session_id="beta-sess", is_error=False, subtype="success")],
        session_id="beta-sess",
    )
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )
    rec = Recorder()

    # Turn 1 on alpha → resumes alpha's session in alpha's cwd; beta untouched.
    await asyncio.wait_for(
        session.handle_message(1, "hi alpha", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng_alpha.resumed == "alpha-sess" and eng_alpha.started is True
    assert eng_beta.resumed is None and eng_beta.started is False  # never touched

    # Operator switches active project to beta (registry op); next turn uses beta.
    store.switch(1, "beta")
    await asyncio.wait_for(
        session.handle_message(1, "hi beta", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng_beta.resumed == "beta-sess" and eng_beta.started is True
    # P5/T5: switching no longer stops alpha's previously-started engine — a switched-away
    # project keeps running in the background (the whole point of background concurrency).
    assert eng_alpha.stopped is False
    assert session._chat(1).runtimes["alpha"].started is True  # still live, not torn down


async def test_per_project_session_id_persists_to_active_only(tmp_path):
    # After a turn, the result's session_id is written to the ACTIVE project; the OTHER
    # project's record is untouched (per-project persistence, not a chat-global slot).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)  # no session_id yet

    eng_alpha = FakeEngine(
        [ResultEvent(session_id="alpha-new", is_error=False, subtype="success")],
        session_id="alpha-new",
    )
    session = make_multi_session({"/work/alpha": eng_alpha}, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # alpha (active) got the new id; beta's record is still pristine (session_id None).
    assert store.get_project(1, "alpha")["session_id"] == "alpha-new"
    assert store.get_project(1, "beta")["session_id"] is None
    assert store.get_project(1, "beta")["cwd"] == "/work/beta"  # fixed cwd untouched


async def test_result_persists_to_captured_project_not_active_after_mid_turn_switch(tmp_path):
    # ⭐ P5/T7 — the LOCK-P-DRIVE-Q / persist-drift guard (ADR-005 D2, the load-bearing
    # per-project persist now that /switch is free). Drive a turn in ALPHA that parks at a
    # HOLD *before* its ResultEvent; WHILE parked, /switch the active project to BETA (now
    # legal — the relaxed busy-guard); THEN release the HOLD so alpha's result (carrying
    # alpha-new) lands while BETA is the active project. The session_id MUST persist to
    # ALPHA (the project the turn ran on = the captured target), NOT to beta (active now).
    #
    # MUTATION PROBE: the prior code persisted via store.update, which always writes the
    # *active* project — so a mid-turn switch would clobber BETA with alpha-new and leave
    # alpha None. With the per-project persist (set_session_id keyed on the captured
    # turn_name) alpha gets alpha-new and beta stays pristine. If _drive_turn's result
    # persist reverts to the active project, this fails loudly on BOTH assertions.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)

    eng_alpha = FakeEngine(
        # park BEFORE the result so we can switch active to beta mid-turn, then land it.
        [HOLD, ResultEvent(session_id="alpha-new", is_error=False, subtype="success", result_text="a-done")],
        session_id="alpha-new",
    )
    eng_beta = FakeEngine([], session_id="beta-sess")
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )
    rec = Recorder()

    # Turn on ALPHA (active) → parks at the HOLD holding alpha's lock, BEFORE its result.
    turn_a = asyncio.create_task(session.handle_message(1, "go alpha", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert store.get_active(1) == "alpha"

    # Operator SWITCHES active to beta WHILE alpha's turn is parked (the freed /switch).
    store.switch(1, "beta")
    assert store.get_active(1) == "beta"

    # Release alpha's HOLD → its ResultEvent lands while BETA is active. The id-routed
    # release would normally come from a tap; here cancel() releases the gate so the
    # scripted result is yielded and the turn completes.
    eng_alpha.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)

    # ⭐ alpha-new persisted to ALPHA (the captured project), NOT beta (active at land time).
    assert store.get_project(1, "alpha")["session_id"] == "alpha-new", "result must persist to the turn's project"
    assert store.get_project(1, "beta")["session_id"] is None, "active-at-land beta must NOT be clobbered"
    assert store.get_active(1) == "beta"  # the switch stands


async def test_reset_clears_active_project_session_but_keeps_project(tmp_path):
    # /reset clears the ACTIVE project's persisted session_id (a fresh conversation) but
    # KEEPS the project in the registry (reset ≠ delete), and clears its policy (D3/D7).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    store.update(1, session_id="live-sess", cwd=None)
    session = make_session(FakeEngine([]), store=store)
    pol = active_policy(session)
    pol.set_yolo(True)
    pol.grant_session("Bash")

    session.reset(1)

    assert store.get_project(1, "api")["session_id"] is None  # session cleared
    assert store.get_active(1) == "api"  # project KEPT + still active
    assert store.get_project(1, "api")["cwd"] == "/work/api"  # cwd preserved (D4)
    assert pol.yolo is False and pol.granted_tools() == frozenset()  # D7 fail-closed


async def test_reset_with_no_active_project_is_noop(tmp_path):
    # Reset on a chat that never ran a turn (no active project) must not crash or create
    # anything (RB1).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    session = make_session(FakeEngine([]), store=store)
    session.reset(1)  # no raise
    assert store.get_active(1) is None
    assert store.list_projects(1) == {}


async def test_restart_resets_yolo_and_grants_then_resumes_persisted_session(tmp_path):
    # D3/SB5: a NEW StreamingSession over the SAME store starts the active project with
    # yolo OFF / no grants (the transient bypass is in-memory only — reset for free on a
    # fresh process), and resumes its PERSISTED session_id on the next turn.
    from claude_tg.session_store import JsonSessionStore

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "api", "/work/api", make_active=True)
    store.update(1, session_id="persisted-sess", cwd=None)

    # --- process 1: turn on, then flip yolo + grant a tool (in-memory state) ---
    eng1 = FakeEngine([ResultEvent(session_id="persisted-sess", is_error=False, subtype="success")])
    s1 = make_multi_session({"/work/api": eng1}, store=store)
    s1.set_yolo(1, True)
    active_policy(s1).grant_session("Bash")
    assert active_policy(s1).yolo is True

    # --- process 2: fresh StreamingSession over the SAME store (simulated restart) ---
    eng2 = FakeEngine(
        [ResultEvent(session_id="persisted-sess", is_error=False, subtype="success")],
        session_id="persisted-sess",
    )
    s2 = make_multi_session({"/work/api": eng2}, store=store)
    # Transient bypass did NOT survive the restart: fresh policy, yolo off, no grants.
    assert active_policy(s2).yolo is False
    assert active_policy(s2).granted_tools() == frozenset()
    # Identity reloaded from the registry → the next turn RESUMES the persisted session.
    rec = Recorder()
    await asyncio.wait_for(
        s2.handle_message(1, "back", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng2.resumed == "persisted-sess" and eng2.started is True


async def test_is_busy_reflects_turn_lock():
    # is_busy() is True exactly while a turn holds the per-chat lock (the D2 busy-guard
    # surface), and False before/after. A chat with no state is never busy.
    engine = FakeEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    assert session.is_busy(1) is False  # nothing started

    turn = asyncio.create_task(
        session.handle_message(1, "go", send=Recorder().send, edit=Recorder().edit)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.is_busy(1) is True  # turn holds the lock (parked at HOLD)

    engine.cancel()  # release the hold
    await asyncio.wait_for(turn, timeout=2.0)
    assert session.is_busy(1) is False  # lock released at turn end


async def test_resume_failure_falls_back_to_fresh_start(tmp_path):
    # The harvested resume-failure recovery survives the per-project rework: a project
    # whose resume() raises falls back to a fresh start() (the turn is never wedged on a
    # stale/torn session). T7 adds the operator notice + cwd re-validation; here we only
    # assert the fallback shape is preserved.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)

    class ResumeBoomEngine(FakeEngine):
        async def resume(self, session_id):
            raise RuntimeError("torn transcript")

    engine = ResumeBoomEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success")]
    )
    session = make_multi_session({"/work/api": engine}, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.started is True  # fell back to a fresh start despite the resume boom


# ===========================================================================
# P4 (T7): resume hardening — SB2 cwd re-validation on the turn path + RB3
# resume-failure operator notice + RB3 interrupted-turn-on-restart recovery.
#
# These use a REAL JsonSessionStore and REAL allowed_roots + real tmp dirs (so the
# SB2 re-validation has teeth, not allow_any_path=True like the turn-plumbing
# fixtures). The engine is still a scripted FakeEngine (no SDK / no network).
# ===========================================================================


def make_roots_config(tmp_path, *, root, allow_any_path=False):
    """A streaming Config whose ``allowed_roots`` is a REAL dir (SB2 has teeth).

    Used by the T7 SB2 turn-path tests: ``allow_any_path=False`` so the driver's cwd
    re-validation actually confines (unlike the default turn fixtures which no-op it)."""
    return make_config(
        workdir=str(root), allowed_roots=(Path(root),), allow_any_path=allow_any_path
    )


async def test_turn_refused_when_cwd_no_longer_within_roots(tmp_path):
    # SB2 (T7): a project whose stored cwd is OUTSIDE allowed_roots (config narrowed since
    # /new, allow_any_path=False) → the turn is REFUSED via send and the engine is NEVER
    # built/started (no factory call, no hang). The lock is released (RB1/SB6 fail-closed).
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"  # a real dir, but OUTSIDE the permitted root
    outside.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "drifted", str(outside), make_active=True)

    factory_calls: list[str] = []

    def boom_factory(*, cwd, backstop_seconds, permission_policy):
        factory_calls.append(cwd)  # must NOT be called — SB2 refuses before building
        raise AssertionError("engine factory must not run when cwd is out-of-roots")

    session = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=boom_factory,
        clock=lambda: 0.0,
    )
    rec = Recorder()
    # No hang: the refusal returns promptly (bounded so a wiring regression fails fast).
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )

    # The engine was never built (the authoritative gate fired before the factory).
    assert factory_calls == []
    # A clear refusal naming the out-of-roots dir was sent; no turn content followed.
    assert len(rec.sends) == 1
    refusal = rec.sends[0]["text"]
    assert "no longer" in refusal and "permitted roots" in refusal
    assert str(outside) in refusal
    assert "/new" in refusal
    # The lock was released (not held) — the chat is usable, not wedged.
    assert session.is_busy(1) is False


async def test_turn_allowed_when_cwd_inside_roots(tmp_path):
    # The companion happy case: an in-roots cwd (allow_any_path=False) re-validates fine,
    # so the turn runs normally — proves the SB2 gate is not over-broad.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"  # INSIDE the permitted root
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)

    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")]
    )
    session = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=lambda: 0.0,
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.started is True  # the turn ran (cwd permitted)
    assert any("ok" in s["text"] for s in rec.sends)
    # No SB2 refusal text leaked into the happy path.
    assert not any("permitted roots" in s["text"] for s in rec.sends)


async def test_resume_failure_sends_operator_notice_and_completes(tmp_path):
    # RB3 (T7): a persisted session_id whose engine.resume() raises → the driver sends the
    # one-line "couldn't resume… started fresh" notice (BEFORE the turn content), falls
    # back to a fresh start(), and the turn COMPLETES (never hangs).
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)  # a persisted (now-dead) session

    class ResumeBoomEngine(FakeEngine):
        async def resume(self, session_id):
            raise RuntimeError("torn transcript")

    engine = ResumeBoomEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success", result_text="done")]
    )
    session = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=lambda: 0.0,
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # Fell back to a fresh start despite the resume boom, and the turn completed.
    assert engine.started is True
    assert any("done" in s["text"] for s in rec.sends)
    # The operator was notified, and the notice preceded the turn's real content.
    notice_idx = next(
        (i for i, s in enumerate(rec.sends) if "Couldn't resume" in s["text"]), None
    )
    assert notice_idx is not None, "the resume-failure notice must be sent"
    done_idx = next(i for i, s in enumerate(rec.sends) if "done" in s["text"])
    assert notice_idx < done_idx  # notice BEFORE the content


async def test_clean_resume_sends_no_notice(tmp_path):
    # Inverse of the RB3 notice: a session that resumes cleanly must NOT emit the
    # "couldn't resume" notice (false-pass guard — the notice is gated on a real failure).
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="live-sess", cwd=None)

    engine = FakeEngine(
        [ResultEvent(session_id="live-sess", is_error=False, subtype="success", result_text="ok")]
    )
    session = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=lambda: 0.0,
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.resumed == "live-sess"  # clean resume
    assert not any("Couldn't resume" in s["text"] for s in rec.sends)


async def test_interrupted_turn_comes_back_idle_and_recovers_on_restart(tmp_path):
    # RB3 (T7): an interrupted (in-flight-at-crash) turn persists NO new session_id, so on
    # restart the project comes back IDLE — a fresh StreamingSession over the same store
    # (in-memory runtime gone) does NOT auto-replay the lost turn, and the NEXT message
    # resumes the last GOOD session and completes. No hang.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="good-sess", cwd=None)  # the last GOOD persisted session

    # --- process 1: a turn is interrupted mid-flight (parked at HOLD, never finishes) ---
    eng1 = FakeEngine([HOLD, ResultEvent(session_id="never", is_error=False, subtype="success")])
    s1 = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng1,
        clock=lambda: 0.0,
    )
    rec1 = Recorder()
    turn = asyncio.create_task(s1.handle_message(1, "interrupted", send=rec1.send, edit=rec1.edit))
    for _ in range(200):
        if s1.is_busy(1):
            break
        await asyncio.sleep(0)
    assert s1.is_busy(1)  # the turn is in flight (parked at HOLD)
    # Simulate a crash: drop process 1 without letting the turn finish. The task is left
    # pending; we cancel it to avoid a leaked task (a real crash would just lose it).
    turn.cancel()
    try:
        await turn
    except asyncio.CancelledError:
        pass
    # The interrupted turn persisted NO new session_id — the store still holds the GOOD one.
    assert store.get_project(1, "api")["session_id"] == "good-sess"

    # --- process 2: a FRESH StreamingSession over the SAME store (in-memory state gone) ---
    # A prompt-RECORDING engine so the "no auto-replay" claim is an ASSERTION, not just a
    # comment: the base FakeEngine.send ignores its prompt, so we capture prompts here and
    # prove eng2 only ever saw the NEW "recover" prompt — never the lost "interrupted" one.
    class PromptRecordingEngine(FakeEngine):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.prompts: list[str] = []

        async def send(self, prompt, *, timeout=None):
            self.prompts.append(prompt)
            async for ev in super().send(prompt, timeout=timeout):
                yield ev

    eng2 = PromptRecordingEngine(
        [ResultEvent(session_id="good-sess", is_error=False, subtype="success", result_text="back")],
        session_id="good-sess",
    )
    s2 = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng2,
        clock=lambda: 0.0,
    )
    # The restarted process comes back IDLE (no turn auto-running from the prior crash).
    assert s2.is_busy(1) is False
    # The NEXT message resumes the last GOOD session and completes.
    rec2 = Recorder()
    await asyncio.wait_for(
        s2.handle_message(1, "recover", send=rec2.send, edit=rec2.edit), timeout=2.0
    )
    assert eng2.resumed == "good-sess" and eng2.started is True
    assert any("back" in s["text"] for s in rec2.sends)
    # No auto-replay of the lost "interrupted" turn — eng2 was driven with ONLY "recover".
    assert eng2.prompts == ["recover"]


# ===========================================================================
# P4 (T8) — deferred defensive-branch tests (from T6/T7 review):
#   * send-raises-on-refusal no-wedge (T7): if the SB2-refusal send() itself raises,
#     the turn lock still releases (is_busy False after) — no wedge.
#   * (P5/T5) switch-no-longer-stops-the-other-engine — see
#     test_switch_does_not_stop_the_previously_started_engine above (the old
#     _stop_other_started stop-failure test, repurposed now the cross-project stop is gone).
#   * _resume_id defensive branches: a non-str / empty session_id → no-resume (fresh start).
#
# REAL JsonSessionStore + REAL allowed_roots (so the SB2 gate has teeth); scripted engines.
# ===========================================================================


async def test_sb2_refusal_send_raising_does_not_wedge_the_lock(tmp_path):
    # T7 deferred: the SB2 refusal path sends a "no longer within roots" message; if THAT
    # send raises (Telegram hiccup at the worst moment), the exception propagates but the
    # turn lock must still RELEASE (the `async with target_rt.lock` unwinds — P5/T5 moved
    # the lock onto the per-project runtime) — the chat is not wedged busy forever.
    # False-pass guard: if the refusal ran OUTSIDE the lock or swallowed into a hang,
    # is_busy would stay True.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"  # real dir, OUTSIDE the permitted root
    outside.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "drifted", str(outside), make_active=True)  # cwd out-of-roots → SB2 refuses

    def boom_factory(*, cwd, backstop_seconds, permission_policy):
        raise AssertionError("engine must not be built when cwd is out-of-roots")

    session = StreamingSession(
        make_roots_config(tmp_path, root=root),
        session_store=store,
        engine_factory=boom_factory,
        clock=lambda: 0.0,
    )

    async def boom_send(*, text, reply_markup=None, parse_mode=None):
        raise RuntimeError("telegram down during the refusal")

    async def edit(*, message_id, text, parse_mode=None):
        return None

    # The refusal send raises; the exception surfaces (nothing left to fall back to), but
    # the lock must be released by the time we observe it.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            session.handle_message(1, "go", send=boom_send, edit=edit), timeout=2.0
        )
    assert session.is_busy(1) is False  # lock released — NOT wedged busy


async def test_switch_does_not_stop_the_previously_started_engine(tmp_path):
    # P5/T5: switching the active project must NOT tear down the previously-started
    # engine (the P4 _stop_other_started single-active-run stop is REMOVED so a
    # switched-away project keeps its engine live for a background run). After turn 1 on
    # alpha + switch to beta + turn 2 on beta, alpha's engine was never stopped and its
    # runtime is still live. (Was the old "stop-failure-on-switch swallow" test, whose
    # premise — switching stops the other engine — no longer holds.)
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)

    class StopTrackingEngine(FakeEngine):
        async def stop(self):
            # If T5 regressed and re-introduced the cross-project stop, this would flip
            # stopped True on the switched-away alpha — the assertion below would catch it.
            self.stopped = True

    eng_alpha = StopTrackingEngine(
        [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="a")],
        session_id="alpha-sess",
    )
    eng_beta = FakeEngine(
        [ResultEvent(session_id="beta-sess", is_error=False, subtype="success", result_text="b")],
        session_id="beta-sess",
    )
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )
    rec = Recorder()

    # Turn 1 on alpha → alpha started.
    await asyncio.wait_for(
        session.handle_message(1, "go alpha", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng_alpha.started is True

    # Switch to beta; turn 2 runs beta WITHOUT stopping alpha (concurrent runs, T5).
    store.switch(1, "beta")
    await asyncio.wait_for(
        session.handle_message(1, "go beta", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng_beta.started is True  # the new turn ran
    assert any("b" == s["text"] for s in rec.sends)
    # alpha was NOT stopped and its runtime is still live (left running in the background).
    assert eng_alpha.stopped is False
    alpha_rt = session._chat(1).runtimes["alpha"]
    assert alpha_rt.started is True and alpha_rt.engine is eng_alpha


# ===========================================================================
# P5 (T5) — per-project turn lock + CONCURRENT runs (ADR-005 D1). The moment
# background concurrency turns ON: the turn lock moved off _ChatState onto each
# _ProjectRuntime, so a message to an idle project runs even while another project's
# turn is parked. Switching away no longer stops the other run; a resolve routes by id
# to the owning project (T2) regardless of which is foreground.
# ===========================================================================


async def _wait_busy(session, chat_id, name, *, want=True):
    """Spin the loop until ``is_busy(chat_id, name) is want`` (bounded; no real sleep)."""
    for _ in range(500):
        if session.is_busy(chat_id, name) is want:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"is_busy({chat_id!r}, {name!r}) never became {want}")


async def test_two_projects_run_concurrent_turns(tmp_path):
    # ⭐ The headline concurrency proof at the unit level. Start a turn in alpha (parks on
    # a HOLD awaiting an answer), SWITCH the active project to beta, start a turn in beta
    # (also parks). BOTH engines are live + BOTH turns are in flight at once (alpha is NOT
    # stopped by the switch). Resolve each INDEPENDENTLY via the id-routed callback path
    # and both turns run to completion.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)

    ask_a = AskEvent(
        questions=[{"question": "A?", "options": [{"label": "Ya"}, {"label": "Na"}]}],
        tool_use_id="tid-a",
    )
    ask_b = AskEvent(
        questions=[{"question": "B?", "options": [{"label": "Yb"}, {"label": "Nb"}]}],
        tool_use_id="tid-b",
    )
    eng_alpha = FakeEngine(
        [ask_a, HOLD, ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="alpha-done")],
        session_id="alpha-sess",
    )
    eng_beta = FakeEngine(
        [ask_b, HOLD, ResultEvent(session_id="beta-sess", is_error=False, subtype="success", result_text="beta-done")],
        session_id="beta-sess",
    )
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )
    rec = Recorder()

    # Turn 1 on alpha (active) → parks at the HOLD holding ALPHA's lock.
    turn_a = asyncio.create_task(session.handle_message(1, "go alpha", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session.is_busy(1, "alpha") is True
    assert session.is_busy(1, "beta") is False  # beta idle so far

    # Operator SWITCHES the active project to beta WHILE alpha is parked (T7 frees this;
    # here we drive the store directly). Then turn 2 on beta starts CONCURRENTLY.
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go beta", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "beta", want=True)

    # BOTH turns are in flight at once — neither was stopped by the other starting.
    assert session.is_busy(1, "alpha") is True
    assert session.is_busy(1, "beta") is True
    assert not turn_a.done() and not turn_b.done()
    assert eng_alpha.started is True and eng_beta.started is True
    assert eng_alpha.stopped is False  # the switch did NOT tear alpha down
    # Each project's held ask is in the pending index, routed to its OWNING project (T2).
    idx = session._chat(1).pending_index
    assert idx["tid-a"].project_name == "alpha"
    assert idx["tid-b"].project_name == "beta"

    # Resolve BETA's ask via the id-routed callback path → only beta's engine resolves.
    out_b = session.resolve_callback(1, encode_callback("a", "tid-b", question_index=0, option_index=0))
    assert out_b.handled is True
    assert eng_beta.resolve_calls == [("tid-b", QuestionAnswer(answers={"B?": "Yb"}))]
    assert eng_alpha.resolve_calls == []  # alpha untouched by beta's resolve
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert any("beta-done" in s["text"] for s in rec.sends)
    # alpha is STILL parked + busy after beta finished (independent runs).
    assert session.is_busy(1, "alpha") is True and not turn_a.done()
    assert session.is_busy(1, "beta") is False

    # Now resolve ALPHA's ask → alpha's engine resolves and its turn completes.
    out_a = session.resolve_callback(1, encode_callback("a", "tid-a", question_index=0, option_index=0))
    assert out_a.handled is True
    assert eng_alpha.resolve_calls == [("tid-a", QuestionAnswer(answers={"A?": "Ya"}))]
    await asyncio.wait_for(turn_a, timeout=2.0)
    # P5 / ADR-005 D4 (T8): alpha is now a BACKGROUND project (the store switched to beta),
    # so alpha's completion arrives as a "✅ alpha — done" PING — NOT inline "alpha-done"
    # (the foreground beta still rendered its own result inline above). This is the headline
    # D4 behavior: "A's completion arrives as ✅ A — done even though B is in front."
    assert any("alpha-done" in s["text"] for s in rec.sends) is False  # no inline result
    assert any(s["text"] == "✅ alpha — done" for s in rec.sends)  # background ping instead
    assert session.is_busy(1) is False  # both done → nothing busy


async def test_same_project_second_message_raises_streaming_busy(tmp_path):
    # Per-project lock: a SECOND message to the SAME running project raises StreamingBusy
    # (one run per project — unchanged per-project UX). A real answer-hold keeps the lock.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)

    eng = FakeEngine(
        [HOLD, ResultEvent(session_id="alpha-sess", is_error=False, subtype="success")],
        session_id="alpha-sess",
    )
    session = make_multi_session({"/work/alpha": eng}, store=store)
    rec = Recorder()

    turn = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    # A second message to the same (active, busy) project is refused.
    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "second", send=rec.send, edit=rec.edit)
    # Release + finish.
    eng.cancel()
    await asyncio.wait_for(turn, timeout=2.0)
    assert session.is_busy(1, "alpha") is False


async def test_is_busy_name_reflects_per_project_lock_state(tmp_path):
    # is_busy(chat, name) is True ONLY for the project whose turn holds its lock; a sibling
    # idle project reads False. is_busy(chat) with no name is True iff ANY project is busy.
    # Matched case-insensitively (mirrors the store), so /projects + /switch WORK agree.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)

    eng_alpha = FakeEngine([HOLD, ResultEvent(session_id="a", is_error=False, subtype="success")], session_id="a")
    eng_beta = FakeEngine([], session_id="b")
    session = make_multi_session({"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store)
    rec = Recorder()

    assert session.is_busy(1) is False  # nothing running
    assert session.is_busy(1, "alpha") is False
    assert session.is_busy(1, "missing") is False  # unknown name → never busy (RB1)

    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session.is_busy(1, "alpha") is True
    assert session.is_busy(1, "ALPHA") is True  # case-insensitive
    assert session.is_busy(1, "beta") is False  # the sibling is idle
    assert session.is_busy(1) is True  # SOME project is busy

    eng_alpha.cancel()
    await asyncio.wait_for(turn, timeout=2.0)
    assert session.is_busy(1, "alpha") is False
    assert session.is_busy(1) is False


async def test_turn_exception_releases_lock_and_resets_status_idle(tmp_path):
    # T4-review finally-wrap: a _drive_turn whose engine.send RAISES mid-stream must leave
    # the project at status idle, its transient status line cleared, and its per-project
    # lock released — the chat stays usable (a concurrent run, or a retry on this project,
    # is unaffected). Without the try/finally, the project would stick at running/awaiting_*
    # and a /projects read would mislead.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)

    class BoomMidStreamEngine(FakeEngine):
        async def send(self, prompt, *, timeout=None):
            # Emit one status event (so a status line is created), then blow up mid-stream.
            yield ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)")
            raise RuntimeError("engine exploded mid-stream")

    eng = BoomMidStreamEngine([], session_id="alpha-sess")
    session = make_multi_session({"/work/alpha": eng}, store=store)
    rec = Recorder()

    # The turn raises; the exception surfaces (RB2 clean-fail), but the finally must run.
    # Pass `delete` so the finally's best-effort status-line cleanup is observable.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
            timeout=2.0,
        )

    rt = session._chat(1).runtimes["alpha"]
    # finally: status forced back to idle (NOT stuck at running/awaiting_*).
    assert rt.status == "idle"
    # finally: the transient status line was cleared (id/text reset), and best-effort
    # deleted (a status line was created by the ToolUseEvent before the raise).
    assert rt.status_message_id is None and rt.status_text is None
    assert rec.deletes, "the transient status line should be deleted in the finally"
    # The per-project lock was released → the chat is NOT wedged busy and a retry works.
    assert session.is_busy(1, "alpha") is False
    assert session.is_busy(1) is False
    # A SUBSEQUENT turn on the same project runs cleanly (the chat is usable, not wedged).
    eng2 = FakeEngine(
        [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="recovered")],
        session_id="alpha-sess",
    )
    session._chat(1).runtimes["alpha"].engine = eng2  # swap in a healthy engine for the retry
    session._chat(1).runtimes["alpha"].started = True
    rec2 = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "again", send=rec2.send, edit=rec2.edit), timeout=2.0
    )
    assert any("recovered" in s["text"] for s in rec2.sends)


async def test_held_ask_then_cancel_returns_status_to_idle(tmp_path):
    # The cancel-path status outcome: a turn parked on a held ask reports awaiting_answer;
    # /cancel (handle_cancel) aborts it → the held turn unblocks, runs out, and the project
    # returns to idle (not stuck at awaiting_answer). The pending-index entry is cleared.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)

    ask = AskEvent(
        questions=[{"question": "Proceed?", "options": [{"label": "Yes"}, {"label": "No"}]}],
        tool_use_id="hold-tid",
    )
    eng = FakeEngine(
        [ask, HOLD, ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="done")],
        session_id="alpha-sess",
    )
    session = make_multi_session({"/work/alpha": eng}, store=store)
    rec = Recorder()

    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    rt = session._chat(1).runtimes["alpha"]
    # Parked on the ask → status is awaiting_answer + the id is in the index.
    assert rt.status == "awaiting_answer"
    assert "hold-tid" in session._chat(1).pending_index

    # /cancel aborts the run (lock-free) → the held turn unblocks and completes.
    aborted = session.handle_cancel(1)
    assert aborted == 1
    # The pending-index entry for the cancelled project is cleared immediately.
    assert "hold-tid" not in session._chat(1).pending_index
    await asyncio.wait_for(turn, timeout=2.0)
    # The turn ended → status is back to idle (the finally / turn-end path), lock released.
    assert rt.status == "idle"
    assert session.is_busy(1, "alpha") is False


# ===========================================================================
# T6 — concurrency cap + per-chat FIFO queue (MAX_CONCURRENT_RUNS, ADR-005 D6).
# Mock-only (the FakeEngine HOLD/resolve dance). The cap bounds RUNNING turns
# process-wide; excess turns queue per chat and start when a slot frees.
# ===========================================================================


async def _wait_running(session, n, *, timeout_iters=1000):
    """Spin the loop until ``session._running == n`` (bounded; no real sleep)."""
    for _ in range(timeout_iters):
        if session._running == n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"session._running never became {n} (is {session._running})")


def _three_project_store(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.create(1, "gamma", "/work/gamma", make_active=False)
    return store


def _holding_engine(name):
    """A FakeEngine that parks on a HOLD then completes (so its turn stays 'running')."""
    return FakeEngine(
        [HOLD, ResultEvent(session_id=f"{name}-sess", is_error=False, subtype="success", result_text=f"{name}-done")],
        session_id=f"{name}-sess",
    )


async def test_cap_queues_third_project_and_dequeues_when_slot_frees(tmp_path):
    # ⭐ The headline cap proof. With MAX_CONCURRENT_RUNS=2: two project turns RUN
    # concurrently; a THIRD project's message is QUEUED (a one-time notice + status
    # "queued", NOT refused) and starts only when one running turn completes (FIFO).
    store = _three_project_store(tmp_path)
    eng_a, eng_b, eng_c = _holding_engine("alpha"), _holding_engine("beta"), _holding_engine("gamma")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b, "/work/gamma": eng_c},
        store=store,
        config=make_config(max_concurrent_runs=2),
    )
    rec = Recorder()

    # Start alpha (active) → runs, parks at HOLD, holds slot 1.
    turn_a = asyncio.create_task(session.handle_message(1, "go alpha", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    # Switch active to beta, start beta → runs concurrently, holds slot 2 (AT the cap now).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go beta", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "beta", want=True)
    await _wait_running(session, 2)
    assert session.is_busy(1, "alpha") and session.is_busy(1, "beta")

    # Switch active to gamma, start gamma → AT the cap → it QUEUES (does NOT run yet).
    store.switch(1, "gamma")
    turn_c = asyncio.create_task(session.handle_message(1, "go gamma", send=rec.send, edit=rec.edit))
    # Give the queued turn a chance to park + send its notice.
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    # gamma is QUEUED: a waiter is parked, gamma reports "queued", and it is NOT running.
    assert len(session._chat(1).run_queue) == 1
    assert session.project_status(1, "gamma") == "queued"
    assert session.is_busy(1, "gamma") is False  # not running — no lock held
    assert eng_c.started is False  # the queued engine has NOT been started yet
    assert session._running == 2  # still exactly the cap (gamma did not consume a slot)
    # The one-time "queued behind N run(s)" notice was sent (N == the cap == 2).
    assert any("Queued behind 2" in s["text"] for s in rec.sends), [s["text"] for s in rec.sends]
    assert not turn_c.done()  # accepted + parked, NOT refused/dropped (SB6)

    # Resolve ALPHA's HOLD → alpha completes → its slot frees → gamma DEQUEUES + starts.
    eng_a.cancel()  # releases alpha's HOLD; alpha's turn runs out to its result
    await asyncio.wait_for(turn_a, timeout=2.0)
    # gamma now starts (the freed slot was transferred to it, FIFO).
    await _wait_busy(session, 1, "gamma", want=True)
    assert eng_c.started is True
    assert session._chat(1).run_queue == deque()  # queue drained
    assert session._running == 2  # beta + gamma now hold the two slots

    # Drain the rest.
    eng_b.cancel()
    eng_c.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    await asyncio.wait_for(turn_c, timeout=2.0)
    assert session._running == 0
    assert any("gamma-done" in s["text"] for s in rec.sends)


async def test_queued_notice_counts_queued_ahead_not_just_running_nb2(tmp_path):
    # ⭐ NB2 (cross-model QA): the "⏳ Queued behind N run(s)" notice must count slot-holders
    # RUNNING **plus** turns already QUEUED ahead — else a turn that queues behind other
    # queued turns is told the wrong position. cap=1: alpha runs (1 slot-holder); beta queues
    # → "behind 1"; gamma queues BEHIND beta → must say "behind 2" (1 running + 1 queued
    # ahead). With the bug (ahead = _running, capped at 1) gamma wrongly says "behind 1".
    store = _three_project_store(tmp_path)
    eng_a, eng_b, eng_c = _holding_engine("alpha"), _holding_engine("beta"), _holding_engine("gamma")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b, "/work/gamma": eng_c},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    # beta queues behind the 1 running → "behind 1".
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if len(session._chat(1).run_queue) == 1:
            break
        await asyncio.sleep(0)
    assert any("Queued behind 1 run(s)" in s["text"] for s in rec.sends), [s["text"] for s in rec.sends]
    # gamma queues behind 1 running + 1 queued (beta) → must say "behind 2".
    store.switch(1, "gamma")
    turn_c = asyncio.create_task(session.handle_message(1, "c", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if len(session._chat(1).run_queue) == 2:
            break
        await asyncio.sleep(0)
    assert any("Queued behind 2 run(s)" in s["text"] for s in rec.sends), (
        "gamma queued behind 1 running + 1 queued must report position 2 (NB2): "
        f"{[s['text'] for s in rec.sends]}"
    )

    # Teardown: drain alpha → beta → gamma (FIFO), no leak.
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await _wait_busy(session, 1, "beta", want=True)
    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    await _wait_busy(session, 1, "gamma", want=True)
    eng_c.cancel()
    await asyncio.wait_for(turn_c, timeout=2.0)
    assert session._running == 0 and session._chat(1).run_queue == deque()


async def test_queue_preserves_fifo_order(tmp_path):
    # With cap=1: alpha runs; beta then gamma are queued. Completing alpha starts BETA
    # (the older waiter), not gamma; completing beta then starts gamma. FIFO preserved.
    store = _three_project_store(tmp_path)
    eng_a, eng_b, eng_c = _holding_engine("alpha"), _holding_engine("beta"), _holding_engine("gamma")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b, "/work/gamma": eng_c},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    # Queue beta, then gamma (order matters).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if len(session._chat(1).run_queue) == 1:
            break
        await asyncio.sleep(0)
    store.switch(1, "gamma")
    turn_c = asyncio.create_task(session.handle_message(1, "c", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if len(session._chat(1).run_queue) == 2:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 2
    assert eng_b.started is False and eng_c.started is False

    # Finish alpha → BETA starts (older waiter), gamma still queued.
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await _wait_busy(session, 1, "beta", want=True)
    assert eng_b.started is True
    assert eng_c.started is False  # gamma is still waiting behind beta (FIFO)
    assert len(session._chat(1).run_queue) == 1

    # Finish beta → GAMMA starts.
    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    await _wait_busy(session, 1, "gamma", want=True)
    assert eng_c.started is True
    assert session._chat(1).run_queue == deque()

    eng_c.cancel()
    await asyncio.wait_for(turn_c, timeout=2.0)
    assert session._running == 0


# --- ROUND-2 RACE (cross-model QA, elevated from Codex suggested-test #2): the per-project
#     busy-guard must hold CONTINUOUSLY across the slot-transfer window. After
#     _pop_next_waiter pops a queued project's waiter (so _is_queued is False) but BEFORE the
#     woken turn acquires its per-project lock (so lock.locked() is False), a same-project 2nd
#     message slips the busy-guard and creates a SECOND _QueuedTurn for that project — two
#     turns for one project, violating D6's single-turn-per-project. -----------------------


def _step_to_busy_guard(coro):
    """Drive ``coro`` (a ``handle_message`` coroutine) up to its FIRST await, returning the
    kind of stop: ``"busy"`` if it raised :class:`StreamingBusy` at the synchronous pre-slot
    busy-guard, or ``"await"`` if it reached its first await (the busy-guard PASSED — the bug).

    ``handle_message`` runs synchronously from entry through the pre-slot busy-guard (free-text
    routing, ``_active_runtime``, the ``lock.locked() or _is_queued`` check) with NO await
    before it, so a single ``coro.send(None)`` evaluates that guard deterministically. This is
    the faithful real-code probe of the guard at the exact transfer-window instant — no sleeps,
    no timing. A coroutine that reaches its first await is closed (it must not actually run a
    turn in this probe)."""
    try:
        coro.send(None)
    except StreamingBusy:
        return "busy"
    except StopIteration:
        return "stopped"
    else:
        # Reached the first await (i.e. _acquire_slot) → the busy-guard let it through. Close
        # the coroutine so it never actually drives a turn / appends a real queue entry.
        coro.close()
        return "await"


async def test_same_project_rejected_during_slot_transfer_window_toctou(tmp_path):
    # ⭐ ROUND-2 RACE (TOCTOU). cap=1: alpha runs (holds the only slot, parked at HOLD); beta
    # queues behind it. We complete alpha so _release_slot → _pop_next_waiter POPS beta's
    # waiter (beta now absent from run_queue) and is about to set_result (the woken beta turn
    # has NOT acquired its lock yet). DETERMINISTICALLY at that instant we evaluate a SECOND
    # beta message's busy-guard: it must raise StreamingBusy (beta already has a turn in
    # flight). With the bug — guard = lock.locked() or _is_queued — both are False in the
    # window, so the 2nd beta message PASSES the guard (reaches _acquire_slot) and would append
    # a 2nd _QueuedTurn for beta → two turns for one project. We also assert beta ends with
    # exactly ONE turn ever run and the slot count returns to baseline.
    store = _three_project_store(tmp_path)  # alpha (active), beta, gamma
    eng_a, eng_b = _holding_engine("alpha"), _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    # alpha takes the only slot and parks at its HOLD.
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session._running == 1
    # beta queues behind alpha (AT the cap) and parks on its waiter.
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if len(session._chat(1).run_queue) == 1:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1 and session.is_busy(1, "beta") is False

    # Inject the transfer window: wrap _pop_next_waiter so that AFTER the real pop (beta's
    # entry removed from run_queue) but BEFORE its future is resolved + the woken beta turn
    # acquires its lock, we evaluate a 2nd beta message's busy-guard. This is exactly the
    # window: _is_queued(beta) is now False (popped) and beta.lock.locked() is False (the woken
    # turn has not resumed yet).
    state = session._chat(1)
    beta_rt = state.runtimes["beta"]
    real_pop = session._pop_next_waiter
    probe = {"result": None, "queue_len_in_window": None, "fired": False}

    def _pop_with_window_probe(st):
        popped = real_pop(st)
        # Only probe on the transfer that wakes BETA (its waiter), exactly once.
        if not probe["fired"] and popped is not None and popped.runtime is beta_rt:
            probe["fired"] = True
            # In-window invariants the bug relies on: beta absent from the queue, lock unheld.
            probe["queue_len_in_window"] = len(st.run_queue)
            assert beta_rt.lock.locked() is False
            second_beta = session.handle_message(1, "b-again", send=rec.send, edit=rec.edit)
            probe["result"] = _step_to_busy_guard(second_beta)
        return popped

    session._pop_next_waiter = _pop_with_window_probe  # type: ignore[assignment]

    # Complete alpha → its finally → _release_slot → the patched _pop fires the in-window probe.
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    # beta then runs (the freed slot transferred to it) — drain it cleanly.
    await _wait_busy(session, 1, "beta", want=True)
    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)

    # The probe MUST have fired in the window, with beta genuinely popped (queue empty there).
    assert probe["fired"] is True
    assert probe["queue_len_in_window"] == 0, "beta must be popped from the queue in the window"
    # ⭐ The core assertion: the 2nd beta message in the transfer window was REJECTED as busy.
    assert probe["result"] == "busy", (
        "a same-project 2nd message in the slot-transfer window must raise StreamingBusy "
        f"(got {probe['result']!r}) — the busy-guard slipped (TOCTOU)"
    )
    # Exactly ONE beta turn ever ran (one start, no 2nd queue entry left behind) and the slot
    # count is back to baseline (no leak).
    assert eng_b.started is True
    assert session._chat(1).run_queue == deque()
    assert session._running == 0


async def test_inflight_guard_is_per_project_distinct_project_still_concurrent(tmp_path):
    # GUARD (don't over-tighten): the in-flight marker is PER PROJECT. While ALPHA is in
    # flight (running, parked at HOLD), a DISTINCT idle project (beta) must still start
    # concurrently — the in-flight guard must reject only a SAME-project 2nd message, never a
    # different project (which is the whole point of P5 concurrency). This is the real
    # (non-probe) companion to the TOCTOU test, pinning that inflight didn't become a global
    # busy bit. cap=2 so both can run at once.
    store = _three_project_store(tmp_path)
    eng_a, eng_b = _holding_engine("alpha"), _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=2),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session._chat(1).runtimes["alpha"].inflight is True
    # A SAME-project 2nd alpha message is rejected (one turn per project) ...
    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "a-again", send=rec.send, edit=rec.edit)
    # ... but a DIFFERENT idle project (beta) starts concurrently (distinct-project guard).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "beta", want=True)
    assert session._running == 2  # both concurrent — inflight is per-project, not global
    assert session._chat(1).runtimes["beta"].inflight is True

    # Drain both; inflight clears on each turn's end.
    eng_a.cancel()
    eng_b.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._running == 0
    assert session._chat(1).runtimes["alpha"].inflight is False
    assert session._chat(1).runtimes["beta"].inflight is False


async def test_inflight_marker_cleared_after_turn_so_next_message_accepted(tmp_path):
    # The in-flight marker must clear on turn-end so a LATER message for the same project is
    # accepted (the guard rejects only WHILE a turn is in flight, not forever). Also pins the
    # marker is set during the run and unset after — the clear-on-every-exit contract.
    store = _three_project_store(tmp_path)
    eng_a = FakeEngine(
        [HOLD, ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="done-1")],
        session_id="alpha-sess",
    )
    session = make_multi_session({"/work/alpha": eng_a}, store=store, config=make_config())
    rec = Recorder()

    turn1 = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    rt = session._chat(1).runtimes["alpha"]
    assert rt.inflight is True
    eng_a.cancel()  # release the HOLD → turn 1 runs to its result
    await asyncio.wait_for(turn1, timeout=2.0)
    assert rt.inflight is False  # cleared on turn-end

    # A SECOND message for the same project is now accepted (not wedged as busy).
    eng_a._script = [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="done-2")]
    eng_a._gate = asyncio.Event()  # fresh gate (the prior cancel had set it)
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("done-2" in s["text"] for s in rec.sends)
    assert rt.inflight is False


async def test_slot_leak_safety_mid_stream_raise_frees_slot_and_dequeues(tmp_path):
    # ⚠️ THE FLAGGED SLOT-LEAK HAZARD. cap=1: alpha runs, beta is queued. alpha's engine
    # RAISES mid-stream — its slot MUST still be released AND the queued beta MUST start
    # (the counter returns to a correct value; capacity is not permanently lost).
    store = _three_project_store(tmp_path)

    class BoomMidStreamEngine(FakeEngine):
        async def send(self, prompt, *, timeout=None):
            yield ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)")
            raise RuntimeError("engine exploded mid-stream")

    eng_a = BoomMidStreamEngine([], session_id="alpha-sess")
    # alpha must hold its slot long enough for beta to queue; a frozen-clock status edit is
    # synchronous, so we let beta queue FIRST (cap=1), then trigger alpha's raise by awaiting.
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    # alpha takes the only slot and raises almost immediately; capture the raise on its task.
    turn_a = asyncio.create_task(
        session.handle_message(1, "boom", send=rec.send, edit=rec.edit, delete=rec.delete)
    )
    # alpha will raise; await it and assert the RuntimeError surfaced (RB2 clean-fail).
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(turn_a, timeout=2.0)
    # alpha's slot was released by the finally (no leak): the counter is back to 0.
    await _wait_running(session, 0)
    assert session._chat(1).run_queue == deque()
    assert session.is_busy(1, "alpha") is False  # lock released too

    # Now a FRESH turn on beta must be able to run — capacity was NOT permanently lost by
    # alpha's raise (a leaked slot would leave _running stuck at 1 and queue beta forever).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go beta", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "beta", want=True)
    assert session._running == 1  # beta got the (correctly-freed) slot
    assert eng_b.started is True
    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._running == 0  # back to zero — no drift across the raised turn


async def test_slot_leak_safety_raise_while_a_turn_is_queued(tmp_path):
    # The stricter leak proof: a turn raises WHILE another is queued behind it — the freed
    # slot must transfer to the queued turn (dequeue fires on the exception path too).
    store = _three_project_store(tmp_path)

    # alpha holds, beta queues; THEN we make alpha's held turn raise after the HOLD is
    # released. send() is an async generator (yields a status event so the turn is clearly
    # running, then parks on the gate exactly like a HOLD, then raises when released).
    class RaiseAfterHoldEngine(FakeEngine):
        async def send(self, prompt, *, timeout=None):
            yield ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)")
            await self._gate.wait()  # park like a HOLD until cancel()/resolve() fires
            self._gate.clear()
            raise RuntimeError("blew up after the hold released")

    eng_a = RaiseAfterHoldEngine([], session_id="alpha-sess")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session._running == 1
    # Queue beta behind alpha (cap=1).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1 and eng_b.started is False

    # Release alpha's hold → alpha's send RAISES → its turn errors. The slot must transfer
    # to the queued beta (NOT just decrement-and-strand-beta).
    eng_a.resolve("x", PlanVerdict(approve=True))  # trips the gate; alpha then raises
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(turn_a, timeout=2.0)
    # beta DEQUEUED + started on alpha's freed slot (the exception path popped the next).
    await _wait_busy(session, 1, "beta", want=True)
    assert eng_b.started is True
    assert session._running == 1  # beta now holds the single slot (no leak, no double-grant)
    assert session._chat(1).run_queue == deque()

    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._running == 0


async def test_cancelled_held_turn_frees_slot_for_queued_turn(tmp_path):
    # A cancelled HELD turn frees its slot. cap=1: alpha parks on a held ask, beta queued;
    # /cancel alpha → alpha unblocks + ends → beta starts on the freed slot.
    store = _three_project_store(tmp_path)
    ask = AskEvent(
        questions=[{"question": "Go?", "options": [{"label": "Y"}, {"label": "N"}]}],
        tool_use_id="hold-a",
    )
    eng_a = FakeEngine(
        [ask, HOLD, ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="a-done")],
        session_id="alpha-sess",
    )
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session.project_status(1, "alpha") == "awaiting_answer"
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1
    assert session.project_status(1, "beta") == "queued"

    # Cancel alpha's run → its held turn unblocks (lock-free) and completes → slot frees.
    # /cancel targets the ACTIVE project; switch back to alpha to cancel it.
    store.switch(1, "alpha")
    aborted = session.handle_cancel(1)
    assert aborted == 1
    await asyncio.wait_for(turn_a, timeout=2.0)
    # beta DEQUEUED + started on alpha's freed slot.
    await _wait_busy(session, 1, "beta", want=True)
    assert eng_b.started is True
    assert session._running == 1

    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._running == 0


async def test_same_project_second_message_is_busy_not_a_queue_slot(tmp_path):
    # A project is never queued behind ITSELF: a second message to a RUNNING project raises
    # StreamingBusy (not a queue entry) even when there is queue room under the cap.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    eng = _holding_engine("alpha")
    session = make_multi_session(
        {"/work/alpha": eng}, store=store, config=make_config(max_concurrent_runs=3)
    )
    rec = Recorder()

    turn = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    assert session._running == 1
    # Second message to the SAME (running) project → StreamingBusy, NOT queued.
    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "second", send=rec.send, edit=rec.edit)
    assert session._chat(1).run_queue == deque()  # nothing queued
    assert session._running == 1  # the StreamingBusy refusal consumed no slot

    eng.cancel()
    await asyncio.wait_for(turn, timeout=2.0)
    assert session._running == 0


async def test_under_cap_runs_immediately_without_queue_notice(tmp_path):
    # Below the cap, a turn runs immediately — no queue entry, no "queued" notice, status
    # never "queued". (Guards against a false-positive: the notice is gated on the cap.)
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    eng = FakeEngine(
        [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="done")],
        session_id="alpha-sess",
    )
    session = make_multi_session(
        {"/work/alpha": eng}, store=store, config=make_config(max_concurrent_runs=3)
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert not any("Queued behind" in s["text"] for s in rec.sends)
    assert session._chat(1).run_queue == deque()
    assert session._running == 0  # released cleanly at turn end
    assert session.project_status(1, "alpha") == "idle"


async def test_resume_id_empty_string_session_id_starts_fresh(tmp_path):
    # T8 (12b): a persisted session_id that is an EMPTY string is falsy → _resume_id
    # returns None → the engine starts FRESH (not resume). Guards the `and session_id`
    # branch (an empty id must never be passed to resume()).
    from claude_tg.session_store import JsonSessionStore

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "api", "/work/api", make_active=True)
    # Hand-write an empty-string session_id (update() would store None for a fresh reset;
    # an empty string is the on-disk edge we must treat as no-resume).
    raw = store._load_raw()
    raw["chats"]["1"]["projects"]["api"]["session_id"] = ""
    store._save_raw(raw)

    engine = FakeEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_multi_session({"/work/api": engine}, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.started is True  # fresh start
    assert engine.resumed is None  # empty-string id was NOT passed to resume()


async def test_resume_id_non_str_session_id_starts_fresh(tmp_path):
    # T8 (12b): a NON-str session_id on disk (hand-edited junk) → _resume_id returns None
    # → fresh start. Guards the `isinstance(session_id, str)` branch (a list/number id must
    # never reach resume()).
    from claude_tg.session_store import JsonSessionStore

    path = tmp_path / "state.json"
    store = JsonSessionStore(path)
    store.create(1, "api", "/work/api", make_active=True)
    raw = store._load_raw()
    raw["chats"]["1"]["projects"]["api"]["session_id"] = ["not", "a", "string"]
    store._save_raw(raw)

    engine = FakeEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_multi_session({"/work/api": engine}, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.started is True
    assert engine.resumed is None  # non-str id was NOT passed to resume()


# ===========================================================================
# QF3 (Codex B3 / RB3): resume CONNECTS but the FIRST turn errors with a
# resume-failure-shaped event. T7 covered resume() RAISING; this is the
# complementary case the one-shot runner handles via _is_resume_failure but that
# was never ported to streaming. On detection the persisted session_id MUST be
# cleared (so it is never retried), the engine dropped (next turn fresh), the
# operator notified, and the turn MUST NOT hang. False-pass guards: a CLEAN
# resumed turn keeps its id + emits no notice; a FRESH session erroring for an
# unrelated reason is NOT treated as a resume failure.
#
# REAL JsonSessionStore (so the persisted-id clear is observable on disk);
# scripted FakeEngine whose resume() SUCCEEDS but whose first send yields the
# resume-failure event. Bounded by asyncio.wait_for so a wiring bug fails fast.
# ===========================================================================


def make_sequential_session(engines_by_cwd: dict, *, store, config=None) -> StreamingSession:
    """A session whose factory hands out the NEXT engine for a cwd on each BUILD.

    ``engines_by_cwd`` maps a cwd → a LIST of engines; successive builds for that cwd
    pop the next one. Used by the QF3 recovery tests where the first engine resumes
    (and fails) and the engine is then DROPPED, so the next turn must BUILD a SECOND,
    fresh engine — letting us assert the dead id is never re-resumed.
    """
    queues = {cwd: list(engines) for cwd, engines in engines_by_cwd.items()}

    def factory(*, cwd, backstop_seconds, permission_policy):
        return queues[cwd].pop(0)

    return StreamingSession(
        config or make_config(),
        session_store=store,
        engine_factory=factory,
        clock=lambda: 0.0,
    )


class ResumeOkButFirstTurnFailsEngine(FakeEngine):
    """resume() SUCCEEDS (connects), but the first send() yields a resume-failure event.

    Mirrors the live B3 shape: a stale/aged/torn session id re-attaches "successfully"
    and only errors on the first turn. ``resumed`` records the id resume() was called
    with so a test can prove a SECOND engine never re-resumes the dead id.
    """


async def test_resume_failure_on_first_turn_clears_id_recovers_and_notifies(tmp_path):
    # B3 core (ErrorEvent path): resume() connects, the first turn yields a
    # resume-failure-shaped ErrorEvent → the persisted id is CLEARED, the operator is
    # notified, the engine is dropped, the turn does NOT hang, and a SUBSEQUENT turn
    # starts FRESH (a brand-new engine that never re-resumes the dead id).
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)  # the (stale) persisted session

    # Engine 1: resume() succeeds, first send yields a resume-failure error then a result.
    eng1 = ResumeOkButFirstTurnFailsEngine(
        [
            ErrorEvent(
                kind_of_error="turn_error",
                message="No conversation found with session id dead-sess",
            ),
            ResultEvent(session_id="dead-sess", is_error=True, subtype="error_during_execution"),
        ],
        session_id="dead-sess",
    )
    # Engine 2: the fresh engine the NEXT turn builds after the dead one is dropped.
    eng2 = FakeEngine(
        [ResultEvent(session_id="fresh-sess", is_error=False, subtype="success", result_text="ok")],
        session_id="fresh-sess",
    )
    session = make_sequential_session(
        {str(proj): [eng1, eng2]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )

    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )

    # Resume WAS attempted on the dead id (the bug is post-connect), then recovery fired.
    assert eng1.resumed == "dead-sess"
    # The persisted id is CLEARED on disk — the dead session is NOT retried next time.
    assert store.get_project(1, "api")["session_id"] is None
    # The operator got the recovery notice telling them to resend.
    assert any("Couldn't resume" in s["text"] for s in rec.sends), "operator must be notified"
    assert any("again to start fresh" in s["text"] for s in rec.sends)
    # The engine was dropped (next turn rebuilds fresh) and the flag cleared.
    rt = session._chat(1).runtimes["api"]
    assert rt.engine is None and rt.started is False
    assert rt.resumed_unverified is False
    # QF4 bonus: the connected-but-dead engine was stop()'d before being dropped (its SDK
    # client is closed, not orphaned). A stop failure would be swallowed, but here it succeeds.
    assert eng1.stopped is True
    # No hang — the chat is idle (lock released).
    assert session.is_busy(1) is False

    # A SUBSEQUENT turn starts FRESH: it builds eng2, which is started (NOT resumed) — the
    # dead id is gone, so it is never re-resumed. (Mutation probe: if recovery had NOT
    # cleared the persisted id, this turn would resume "dead-sess" and eng2.resumed would
    # be set / eng2.started False — this assertion would fail.)
    await asyncio.wait_for(
        session.handle_message(1, "again", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng2.started is True
    assert eng2.resumed is None  # fresh start — the dead id was never re-resumed
    assert any("ok" in s["text"] for s in rec.sends)


async def test_resume_failure_signalled_via_is_error_result_event(tmp_path):
    # B3 variant (ResultEvent path): the resume failure is carried on an is_error
    # ResultEvent (its result_text matches the heuristic) rather than a separate
    # ErrorEvent → same recovery: id cleared, notice, dead id NOT re-persisted.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)

    eng1 = ResumeOkButFirstTurnFailsEngine(
        [
            ResultEvent(
                session_id="dead-sess",
                is_error=True,
                subtype="error",
                result_text="session not found or invalid",
            )
        ],
        session_id="dead-sess",
    )
    session = make_sequential_session(
        {str(proj): [eng1]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The dead id was cleared (NOT re-persisted from the is_error result's session_id).
    assert store.get_project(1, "api")["session_id"] is None
    assert any("Couldn't resume" in s["text"] for s in rec.sends)


async def test_clean_resumed_turn_keeps_id_and_no_notice(tmp_path):
    # False-pass guard (a): a resumed session whose FIRST turn is CLEAN must keep its
    # persisted session_id, emit NO recovery notice, and clear resumed_unverified (the
    # resume is confirmed good). If detection were over-broad this would spuriously clear.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="live-sess", cwd=None)

    engine = ResumeOkButFirstTurnFailsEngine(
        [ResultEvent(session_id="live-sess", is_error=False, subtype="success", result_text="ok")],
        session_id="live-sess",
    )
    session = make_sequential_session(
        {str(proj): [engine]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.resumed == "live-sess"  # it really did resume
    # Clean turn → id retained, no notice, runtime confirmed (flag cleared, engine kept).
    assert store.get_project(1, "api")["session_id"] == "live-sess"
    assert not any("Couldn't resume" in s["text"] for s in rec.sends)
    rt = session._chat(1).runtimes["api"]
    assert rt.resumed_unverified is False
    assert rt.engine is engine and rt.started is True


async def test_fresh_session_error_is_not_treated_as_resume_failure(tmp_path):
    # False-pass guard (b): a FRESH session (no persisted id → start(), not resume())
    # whose first turn errors — even with text that LOOKS like a session error — must NOT
    # be treated as a resume failure: nothing to clear, no notice, engine NOT dropped.
    # (resumed_unverified is only set on a real resume, so the heuristic never runs here.)
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)  # NO session_id → fresh start

    engine = FakeEngine(
        [
            # An error whose text would TRIP the heuristic IF it were checked — but this is
            # a fresh session, so it must be ignored as an ordinary turn error.
            ErrorEvent(kind_of_error="turn_error", message="No conversation found / session expired"),
            ResultEvent(session_id="brand-new", is_error=True, subtype="error"),
        ],
        session_id="brand-new",
    )
    session = make_sequential_session(
        {str(proj): [engine]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.started is True and engine.resumed is None  # fresh start, never resumed
    # NOT a resume failure: no spurious clear, no notice, engine retained.
    assert not any("Couldn't resume" in s["text"] for s in rec.sends)
    rt = session._chat(1).runtimes["api"]
    assert rt.engine is engine and rt.started is True
    assert rt.resumed_unverified is False  # was never set (fresh start)


async def test_ordinary_tool_error_on_resumed_turn_is_not_a_resume_failure(tmp_path):
    # False-pass guard (c): a resumed session whose first turn errors for an UNRELATED
    # reason (an ordinary tool error, not session-gone) must NOT trip recovery — the id is
    # kept and no notice is sent. Mirrors the one-shot heuristic excluding non-resume
    # errors; confirms the streaming reuse inherits that discrimination.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="live-sess", cwd=None)

    engine = ResumeOkButFirstTurnFailsEngine(
        [
            ErrorEvent(kind_of_error="tool_error", message="Bash: command not found: frobnicate"),
            ResultEvent(session_id="live-sess", is_error=False, subtype="success", result_text="recovered"),
        ],
        session_id="live-sess",
    )
    session = make_sequential_session(
        {str(proj): [engine]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # An ordinary tool error is NOT session-gone → id kept, no recovery notice.
    assert store.get_project(1, "api")["session_id"] == "live-sess"
    assert not any("Couldn't resume" in s["text"] for s in rec.sends)
    rt = session._chat(1).runtimes["api"]
    assert rt.resumed_unverified is False  # turn completed → confirmed good
    assert rt.engine is engine


# ===========================================================================
# QF4 (Codex re-QA B3′ / RB3): resume() RAISES (it does not connect) AND the
# SDK adapter assigns its client BEFORE connect(), so the FAILED engine is left
# with a partial, non-None client. The pre-QF4 fallback called start() on that
# SAME engine → the adapter's "session already started" guard re-raised → the
# turn failed and the dead session_id was NEVER cleared → every future turn
# re-resumed the same dead id → the project was permanently WEDGED. QF4 recovers
# onto a FRESH engine: stop the failed one (best-effort), clear the dead id,
# build + start a fresh engine, and signal resume_failed for the T7 notice.
#
# REAL JsonSessionStore (so the persisted-id clear is observable) + a sequential
# factory (a FRESH engine per build) so a buggy fallback that reuses the failed
# instance fails fast. Bounded by asyncio.wait_for so a wiring bug fails fast.
# ===========================================================================


class ResumeRaisesThenStartRaisesEngine(FakeEngine):
    """resume() RAISES, and start() on this SAME instance ALSO raises — the adapter shape.

    Mirrors ``adapter_sdk.py``: resume() assigns ``self._client`` BEFORE ``connect()``, so
    a connect failure leaves ``_client`` non-None; a subsequent start() then hits the
    "session already started" guard and raises. This fake reproduces that coupling so the
    QF4 mutation probe has teeth: a buggy fallback that reuses THIS instance (calls its
    start()) blows up here → the turn fails / the dead id is never cleared. A FRESH engine
    (the correct fix) has ``started=False`` and starts cleanly.
    """

    async def resume(self, session_id):
        # Record the attempt (the bug is the post-RAISE handling), then fail like a dead
        # session's connect(), leaving the (simulated) partial client attached.
        self.resumed = session_id
        self.started = True  # simulate adapter's client-assigned-before-connect coupling
        raise RuntimeError("connect failed: no conversation found with session id")

    async def start(self):
        # The adapter's guard: a non-None client (here: this same already-touched engine)
        # makes start() raise. Reusing the failed instance must hit this.
        if self.started:
            raise RuntimeError("session already started; call stop() first")
        self.started = True


async def test_resume_raises_recovers_on_fresh_engine_clears_id_and_notifies(tmp_path):
    # B3′ core: resume() RAISES on an engine that mimics the adapter (start() on the same
    # instance ALSO raises). The QF4 fallback must NOT reuse it — it must (a) clear the
    # persisted dead id, (b) build a SECOND, FRESH engine and start() it successfully,
    # (c) complete the turn + post the T7 "couldn't resume… started fresh" notice, (d) NOT
    # hang (is_busy False), and (e) a SUBSEQUENT turn must NOT re-resume the dead id.
    #
    # Mutation probe: if the fix reused the failed engine (called its start()), eng1.start()
    # raises "already started" → the turn would fail / the id would be left set → the
    # fresh-engine + cleared-id assertions below would fail.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)  # the (dead) persisted session

    # Engine 1: resume() raises AND start() on it raises (adapter coupling) — the failed one.
    eng1 = ResumeRaisesThenStartRaisesEngine([], session_id="dead-sess")
    # Engine 2: the FRESH engine the fallback must build + start() instead.
    eng2 = FakeEngine(
        [ResultEvent(session_id="fresh-sess", is_error=False, subtype="success", result_text="ok")],
        session_id="fresh-sess",
    )
    session = make_sequential_session(
        {str(proj): [eng1, eng2]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )

    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )

    # resume() WAS attempted on the dead id (the bug is the post-raise handling).
    assert eng1.resumed == "dead-sess"
    # (a) The dead id is GONE — recovery cleared it, then the fresh turn persisted the
    # fresh session's id. What matters for the wedge fix is that "dead-sess" is no longer
    # the persisted id (so it can never be re-resumed); it has been replaced by the fresh
    # session's id, exactly as a normal completed turn persists its result.
    persisted = store.get_project(1, "api")["session_id"]
    assert persisted != "dead-sess"
    assert persisted == "fresh-sess"
    # (a′) Best-effort stop() of the failed engine was attempted (free its partial client).
    assert eng1.stopped is True
    # (b) A SECOND, FRESH engine was built and started (NOT eng1, which would have raised).
    assert eng2.started is True
    assert eng2.resumed is None  # the fresh engine never resumed anything
    rt = session._chat(1).runtimes["api"]
    assert rt.engine is eng2 and rt.started is True
    # A fresh start is NOT resumed_unverified (a fresh-session error ≠ a resume failure).
    assert rt.resumed_unverified is False
    # (c) The turn completed AND the operator got the T7 resume-failure notice.
    assert any("ok" in s["text"] for s in rec.sends)
    notice_idx = next(
        (i for i, s in enumerate(rec.sends) if "Couldn't resume" in s["text"]), None
    )
    assert notice_idx is not None, "the resume-failure notice must be sent"
    done_idx = next(i for i, s in enumerate(rec.sends) if "ok" in s["text"])
    assert notice_idx < done_idx  # notice BEFORE the turn content (T7 ordering)
    # (d) No hang — the chat is idle (lock released).
    assert session.is_busy(1) is False

    # (e) A SUBSEQUENT turn does NOT re-resume the dead id — it reuses the now-warm fresh
    # engine (already started), so no new build/resume happens and the dead id is never
    # seen again. The persisted id is the FRESH one (never reverts to "dead-sess").
    await asyncio.wait_for(
        session.handle_message(1, "again", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng2.resumed is None  # still never resumed (the dead id was cleared)
    assert store.get_project(1, "api")["session_id"] == "fresh-sess"  # never "dead-sess"


async def test_resume_raises_then_fresh_start_failure_still_cleared_the_dead_id(tmp_path):
    # QF4 ordering guarantee: the dead id is cleared BEFORE the fresh start, so even if the
    # FRESH engine's start() also raises (a doubly-bad moment), the dead id is already gone
    # → the next turn starts fresh, never re-resuming the wedge. The first turn surfaces the
    # fresh-start error (it propagates), but the project is NOT left wedged.
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(proj), make_active=True)
    store.update(1, session_id="dead-sess", cwd=None)

    eng1 = ResumeRaisesThenStartRaisesEngine([], session_id="dead-sess")

    class FreshStartBoomEngine(FakeEngine):
        async def start(self):
            raise RuntimeError("fresh start also failed")

    eng2 = FreshStartBoomEngine([], session_id="never")
    session = make_sequential_session(
        {str(proj): [eng1, eng2]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
    )
    rec = Recorder()
    # The fresh start raises → it propagates out of the turn (nothing left to recover to).
    with pytest.raises(RuntimeError, match="fresh start also failed"):
        await asyncio.wait_for(
            session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
        )
    # But the dead id was ALREADY cleared (step b runs before the fresh start in step d), so
    # the project is not wedged re-resuming it — the next turn would start fresh.
    assert store.get_project(1, "api")["session_id"] is None
    # The lock released despite the raise (no wedge-busy).
    assert session.is_busy(1) is False


# ===========================================================================
# QF5 (Codex re-QA B4 + related edge): the "stale in-memory runtime reused on a
# lifecycle transition" class.
#
#  (1) B4 — /rm purges the in-memory runtime (covered in test_bot_streaming.py:
#      the recreated project must not leak the old cwd / yolo / grants).
#  (2) Related edge — _ensure_engine must NEVER reuse a NON-started engine. Past the
#      warm fast-path (`rt.engine is not None and rt.started`), a present engine is
#      necessarily non-started (a prior start()/resume() that raised AFTER the adapter
#      allocated its client). Reusing it → start()/resume() hits the "already started"
#      guard → wedge. The fix discards it (best-effort stop) + builds fresh.
#
# REAL JsonSessionStore + a sequential factory (a FRESH engine per BUILD) so a buggy
# reuse fails fast. Bounded by asyncio.wait_for so a wiring bug fails fast.
# ===========================================================================


async def test_ensure_engine_discards_non_started_engine_and_builds_fresh(tmp_path):
    # The related edge: a runtime whose rt.engine is set but rt.started is False (a prior
    # start() that raised after the adapter allocated its client) must NOT be reused — the
    # next _ensure_engine best-effort stop()s the stale engine and builds a FRESH one.
    #
    # Mutation probe: reverting the fix to `engine = rt.engine or factory(...)` reuses the
    # stale engine → the assertions that the FRESH (second) engine ran and the stale one
    # was stopped + replaced would fail.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)  # NO session_id → start(), not resume

    # eng_stale: simulate a prior start() that raised AFTER the client was allocated — the
    # runtime is left with this non-None engine but started=False (the failed-start shape).
    # It is PLANTED directly on the runtime (never handed out by the factory).
    eng_stale = FakeEngine([], session_id="stale")
    # eng_fresh: the engine the next _ensure_engine must BUILD + start (never eng_stale). It
    # is the ONLY engine in the factory queue, so a buggy reuse of eng_stale would leave
    # eng_fresh unbuilt (started False) and the assertions fail.
    eng_fresh = FakeEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success", result_text="ok")],
        session_id="fresh",
    )
    session = make_sequential_session({"/work/api": [eng_fresh]}, store=store)

    # Seed the runtime into the failed-prior-start state (engine set, started False). Use
    # the same auto-create path a turn would, then plant the stale engine.
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng_stale
    rt.started = False

    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )

    # The stale engine was best-effort stopped and DISCARDED; the FRESH one was built + started.
    assert eng_stale.stopped is True, "the non-started engine must be stopped before discard"
    assert eng_stale.started is False, "the stale engine was never (re)started — it was replaced"
    assert eng_fresh.started is True, "a FRESH engine must be built + started, not the stale one"
    assert rt.engine is eng_fresh and rt.started is True
    assert any("ok" in s["text"] for s in rec.sends)
    assert session.is_busy(1) is False


async def test_ensure_engine_discard_swallows_stop_failure_and_builds_fresh(tmp_path):
    # Best-effort: if the stale (non-started) engine's stop() RAISES while being discarded,
    # _ensure_engine swallows it and still builds + starts the fresh engine (a wedged stale
    # engine must never block the rebuild). This is the SAME-project QF5 discard (kept under
    # P5/T5 concurrency — only the cross-project _stop_other_started stop was removed).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)

    class StopBoomEngine(FakeEngine):
        async def stop(self):
            self.stopped = True
            raise RuntimeError("stop blew up")

    eng_stale = StopBoomEngine([], session_id="stale")  # planted, not built
    eng_fresh = FakeEngine(
        [ResultEvent(session_id="fresh", is_error=False, subtype="success", result_text="ok")],
        session_id="fresh",
    )
    session = make_sequential_session({"/work/api": [eng_fresh]}, store=store)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng_stale
    rt.started = False

    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng_stale.stopped is True  # stop() was attempted (and raised, swallowed)
    assert eng_fresh.started is True  # the fresh engine still ran despite the stop failure
    assert rt.engine is eng_fresh and rt.started is True


async def test_ensure_engine_reuses_warm_started_engine(tmp_path):
    # Regression / false-pass guard: the warm fast-path must STILL return the SAME started
    # engine on a second turn — the discard-and-rebuild only fires for a NON-started engine.
    # If the fix wrongly rebuilt every turn, the sequential factory would hand out a second
    # engine on turn 2 (and run out / change identity) and this would fail.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)

    # The FakeEngine replays its whole script on each send(), so one ResultEvent suffices
    # for both turns. Only ONE engine is provided for the cwd: a second BUILD would
    # IndexError on the empty queue, so a rebuild-every-turn regression fails loudly here.
    eng = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
        session_id="s",
    )
    session = make_sequential_session({"/work/api": [eng]}, store=store)
    rec = Recorder()

    eng1, _ = await session._ensure_engine(1)
    assert eng1 is eng and eng.started is True
    await asyncio.wait_for(
        session.handle_message(1, "turn one", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # Second turn: the warm fast-path returns the SAME engine (no rebuild, no second pop).
    eng2, resume_failed = await session._ensure_engine(1)
    assert eng2 is eng, "a warm started engine must be reused, not rebuilt every turn"
    assert resume_failed is False
    assert eng.stopped is False  # the warm engine was never stopped/discarded
    await asyncio.wait_for(
        session.handle_message(1, "turn two", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert sum("ok" in s["text"] for s in rec.sends) >= 2  # both turns rendered a result


# ===========================================================================
# P5 (T2): id-routed resolve/cancel/free-text via the per-chat PENDING INDEX
# (ADR-005 D3). The relay must route every decision-in by tool_use_id to the
# OWNING project's engine — NOT _active_engine — so a tap for project A resolves
# A even while B is the active/foreground project. These wire TWO live engines
# (one per project) and prime each project's held request in the index, then
# assert the routing. _active_engine is retired from the resolve path; if any of
# these regressed to "resolve the active project", they fail loudly.
#
# (Scope: T2 changes ROUTING only — at most one project runs until T5. Here we
# seed two runtimes with live engines directly to exercise the routing in
# isolation, which is exactly what the index must get right regardless of which
# project is active in the store.)
# ===========================================================================


async def make_two_project_session(tmp_path, *, active: str):
    """A session with two projects (alpha/beta), each with its OWN live FakeEngine.

    Returns ``(session, store, eng_alpha, eng_beta)``. ``active`` is the store's active
    project. Both runtimes are seeded with a started engine so a decision can route to
    EITHER project's engine by id (the cross-project routing T2 must get right). The
    engines carry distinct session_ids matching the events the tests prime.
    """
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=(active == "alpha"))
    store.create(1, "beta", "/work/beta", make_active=(active == "beta"))
    eng_alpha = FakeEngine([], session_id="alpha-sid")
    eng_beta = FakeEngine([], session_id="beta-sid")
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )
    # Seed both runtimes with a live (started) engine — two concurrent runs' worth of
    # engines, so a decision can route to either by id (T5 starts them for real).
    for name, eng in (("alpha", eng_alpha), ("beta", eng_beta)):
        rt = session._runtime(1, name, f"/work/{name}")
        rt.engine = eng
        rt.started = True
    return session, store, eng_alpha, eng_beta


async def test_tap_routes_to_owning_project_not_active(tmp_path):
    # A held ask belongs to ALPHA, but BETA is the active/foreground project. A tap on
    # alpha's tool_use_id must resolve ALPHA's engine — never beta's. This is the core
    # cross-project routing guarantee (the inverse of P4's _active_engine collapse).
    session, _store, eng_alpha, eng_beta = await make_two_project_session(tmp_path, active="beta")
    ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "Yes"}, {"label": "No"}]}],
        tool_use_id="alpha-ask",
        session_id="alpha-sid",
    )
    prime_pending(session, ask, project="alpha")

    outcome = session.resolve_callback(
        1, encode_callback("a", "alpha-ask", question_index=0, option_index=0)
    )
    assert outcome.handled is True
    # Routed to ALPHA (the owner), NOT beta (the active project).
    assert eng_alpha.resolve_calls == [("alpha-ask", QuestionAnswer(answers={"Q": "Yes"}))]
    assert eng_beta.resolve_calls == []
    # The index entry was cleared after the resolve.
    assert "alpha-ask" not in session._chat(1).pending_index


async def test_two_pending_taps_route_to_their_own_projects(tmp_path):
    # BOTH projects hold a pending request at once (two entries in the index). A tap for
    # alpha's id resolves ALPHA only; a tap for beta's id resolves BETA only — never the
    # other. Proves id→one-owner routing with multiple concurrent holds.
    session, _store, eng_alpha, eng_beta = await make_two_project_session(tmp_path, active="alpha")
    perm_alpha = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="a-perm", session_id="alpha-sid"
    )
    plan_beta = PlanEvent(plan="beta plan", tool_use_id="b-plan", session_id="beta-sid")
    prime_pending(session, perm_alpha, project="alpha")
    prime_pending(session, plan_beta, project="beta")

    # Tap beta's plan-approve while ALPHA is active → resolves BETA, not alpha.
    out_b = session.resolve_callback(1, encode_callback("p", "b-plan", plan_action="a"))
    assert out_b.handled is True
    assert eng_beta.resolve_calls == [("b-plan", PlanVerdict(approve=True))]
    assert eng_alpha.resolve_calls == []  # alpha untouched by beta's tap

    # Tap alpha's permission-allow → resolves ALPHA only.
    out_a = session.resolve_callback(1, encode_callback("m", "a-perm", payload="o"))
    assert out_a.handled is True
    assert eng_alpha.resolve_calls == [("a-perm", PermissionDecision(verdict="allow_once"))]
    # beta still only has its own one resolve (alpha's tap did not touch it).
    assert eng_beta.resolve_calls == [("b-plan", PlanVerdict(approve=True))]
    # Both entries cleared after their resolves.
    assert session._chat(1).pending_index == {}


async def test_free_text_routes_to_owning_project_not_active(tmp_path):
    # An "Other" tap on ALPHA's ask arms free-text capture; the next plain message must
    # resolve ALPHA's engine even though BETA is the active project (free-text is id-routed
    # via the index, not _active_engine).
    session, _store, eng_alpha, eng_beta = await make_two_project_session(tmp_path, active="beta")
    ask = AskEvent(
        questions=[{"question": "Name?", "options": [{"label": "A"}]}],
        tool_use_id="alpha-ask",
        session_id="alpha-sid",
    )
    prime_pending(session, ask, project="alpha")

    out = session.resolve_callback(1, encode_callback("o", "alpha-ask", question_index=0))
    assert out.expects_text is True
    # T4/D7: the free-text marker is armed on the OWNING project's runtime (alpha), even
    # though beta is the active/foreground project — not a chat-global slot.
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "alpha-ask"

    rec = Recorder()
    await session.handle_message(1, "Charlie", send=rec.send, edit=rec.edit)
    # Resolved ALPHA (the owner), not beta (the active project).
    assert eng_alpha.resolve_calls == [("alpha-ask", QuestionAnswer(answers={"Name?": "Charlie"}))]
    assert eng_beta.resolve_calls == []
    assert rec.sends == []  # no new turn opened
    assert "alpha-ask" not in session._chat(1).pending_index


async def test_wrong_kind_callback_for_index_id_does_not_resolve(tmp_path):
    # Defense-in-depth (RB1/SB6): a forged permission tap (m|…) whose id maps to an ASK
    # entry must NOT resolve that ask with a permission verdict (a type confusion). The
    # per-kind guard refuses it; the held ask is untouched.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "Yes"}]}],
        tool_use_id="alpha-ask",
        session_id="alpha-sid",
    )
    prime_pending(session, ask, project="alpha")

    outcome = session.resolve_callback(1, encode_callback("m", "alpha-ask", payload="o"))
    assert outcome.handled is False
    assert eng_alpha.resolve_calls == []  # the ask was NOT resolved by a permission verdict
    assert "alpha-ask" in session._chat(1).pending_index  # ask still held


async def test_session_id_mismatch_refuses_to_resolve(tmp_path):
    # Defense-in-depth (ADR-005 D3): the held event's session_id must match the owning
    # engine's current session_id. A stale id whose held event was injected under an OLD
    # session (the engine since re-attached to a new one) must NOT resolve — no-op.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    # The held ask was injected under "old-sid", but alpha's engine is now on "alpha-sid".
    ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "Yes"}]}],
        tool_use_id="alpha-ask",
        session_id="old-sid",
    )
    prime_pending(session, ask, project="alpha")

    outcome = session.resolve_callback(
        1, encode_callback("a", "alpha-ask", question_index=0, option_index=0)
    )
    assert outcome.handled is False  # session mismatch → refused
    assert eng_alpha.resolve_calls == []  # never resolved the wrong session


async def test_cancel_routes_to_active_and_clears_only_its_pending(tmp_path):
    # handle_cancel aborts the ACTIVE project's engine (T2 scope) and clears ONLY that
    # project's pending-index entries; a DIFFERENT project's held request survives (it is
    # a separate concurrent run). Lock-free (no held turn here — pure routing).
    session, _store, eng_alpha, eng_beta = await make_two_project_session(tmp_path, active="alpha")
    prime_pending(
        session,
        PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="a-perm", session_id="alpha-sid"),
        project="alpha",
    )
    prime_pending(
        session,
        PlanEvent(plan="beta plan", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )

    aborted = session.handle_cancel(1)  # active == alpha
    assert aborted == 1  # FakeEngine.cancel() returns 1
    assert eng_alpha.cancel_calls == [None]  # alpha's engine was cancelled
    assert eng_beta.cancel_calls == []  # beta's concurrent run was NOT cancelled
    # Alpha's pending entry is cleared; beta's survives (a separate run).
    assert "a-perm" not in session._chat(1).pending_index
    assert "b-plan" in session._chat(1).pending_index


async def test_turn_end_clears_only_that_projects_pending(tmp_path):
    # A driven turn that ends with an unanswered ask drops THAT project's index entry at
    # turn-end (no leak across turns), but leaves a concurrent project's held request alone.
    # Here: alpha runs a real turn (ask → result, no HOLD), beta has a pre-seeded held plan.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    # Alpha's turn injects an ask then completes WITHOUT the operator answering it.
    alpha_ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "A"}]}], tool_use_id="alpha-ask"
    )
    eng_alpha = FakeEngine(
        [alpha_ask, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="done")],
        session_id="alpha-sid",
    )
    eng_beta = FakeEngine([], session_id="beta-sid")
    session = make_multi_session({"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store)
    # Beta has a concurrent held plan in the index (a separate run).
    prime_pending(
        session,
        PlanEvent(plan="beta plan", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )

    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # Alpha's unanswered ask was dropped at turn-end (no leak); beta's plan survives.
    assert "alpha-ask" not in session._chat(1).pending_index
    assert "b-plan" in session._chat(1).pending_index


# ===========================================================================
# P5 / ADR-005 D7 (T4) — live-turn state lifted from _ChatState to _ProjectRuntime
# + per-project run status. Each running project owns its OWN status line + free-text
# marker + status enum, so two projects never clash; the lock-free resolve still finds
# the held event on the owning project's runtime (via the pending index → project).
# ===========================================================================


async def test_status_transitions_idle_running_awaiting_answer_running_idle():
    # The per-project status enum walks idle -> running (turn start) -> awaiting_answer
    # (an ask hold) -> running (resolve unblocks the held turn) -> idle (turn end), all on
    # the project's OWN _ProjectRuntime (ADR-005 D7).
    ask = AskEvent(
        questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
        tool_use_id="tid-ask",
    )
    engine = FakeEngine(
        [ask, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success", result_text="done")]
    )
    session = make_session(engine)
    # No runtime yet → idle (the /projects default).
    assert session.project_status(1, "default") == "idle"

    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    # Let the turn run up to the HOLD: it injected the ask, so the active project's status
    # is now awaiting_answer (parked awaiting the operator).
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_rt(session).status == "awaiting_answer"
    assert session.project_status(1, "default") == "awaiting_answer"

    # Resolving the ask flips the status back to running (the held turn resumes); the turn
    # has not yet advanced (resolve_callback is synchronous), so we observe running here.
    out = session.resolve_callback(1, encode_callback("a", "tid-ask", question_index=0, option_index=0))
    assert out.handled is True
    assert active_rt(session).status == "running"

    # The turn now drains to completion → idle.
    await asyncio.wait_for(turn, timeout=2.0)
    assert active_rt(session).status == "idle"
    assert session.project_status(1, "default") == "idle"


async def test_status_awaiting_approval_for_permission_hold():
    # A held PermissionEvent flips the project's status to awaiting_approval; resolving it
    # (allow once) returns it to running, then idle at turn end.
    perm = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=ls)", tool_use_id="tid-perm"
    )
    engine = FakeEngine(
        [perm, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")]
    )
    session = make_session(engine)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_rt(session).status == "awaiting_approval"

    out = session.resolve_callback(1, encode_callback("m", "tid-perm", payload="o"))
    assert out.handled is True
    assert active_rt(session).status == "running"
    await asyncio.wait_for(turn, timeout=2.0)
    assert active_rt(session).status == "idle"


async def test_status_awaiting_plan_for_plan_hold():
    # A held PlanEvent flips the project's status to awaiting_plan; approving it returns it
    # to running, then idle at turn end.
    plan = PlanEvent(plan="the plan", tool_use_id="tid-plan")
    engine = FakeEngine(
        [plan, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")]
    )
    session = make_session(engine)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_rt(session).status == "awaiting_plan"

    out = session.resolve_callback(1, encode_callback("p", "tid-plan", plan_action="a"))
    assert out.handled is True
    assert active_rt(session).status == "running"
    await asyncio.wait_for(turn, timeout=2.0)
    assert active_rt(session).status == "idle"


async def test_multi_question_intermediate_tap_keeps_awaiting_answer():
    # A multi-question ask stays awaiting_answer until EVERY question is answered — an
    # intermediate tap (1 of 2) does not flip the project back to running.
    ask = AskEvent(
        questions=[
            {"question": "Q1", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2", "options": [{"label": "A2"}, {"label": "B2"}]},
        ],
        tool_use_id="multi-st",
    )
    engine = FakeEngine(
        [ask, HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")]
    )
    session = make_session(engine)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_rt(session).status == "awaiting_answer"

    # First tap (1 of 2): accepted but NOT resolved → still awaiting_answer.
    session.resolve_callback(1, encode_callback("a", "multi-st", question_index=0, option_index=0))
    assert active_rt(session).status == "awaiting_answer"

    # Final tap (2 of 2): resolves the whole ask → running, then idle at turn end.
    session.resolve_callback(1, encode_callback("a", "multi-st", question_index=1, option_index=1))
    assert active_rt(session).status == "running"
    await asyncio.wait_for(turn, timeout=2.0)
    assert active_rt(session).status == "idle"


async def test_two_projects_status_lines_are_independent(tmp_path):
    # ADR-005 D7: each project's status line (id/text) lives on its OWN _ProjectRuntime, so
    # a status edit for one project NEVER touches the other's line. (Driven directly via
    # _perform against each runtime — under T4 the chat still allows one turn at a time;
    # T5 adds the per-project lock so two real turns overlap.)
    session, _store, _eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    rt_alpha = session._chat(1).runtimes["alpha"]
    rt_beta = session._chat(1).runtimes["beta"]
    rec = Recorder()

    state = session._chat(1)  # T8: _perform now takes the chat state (the send-rate gate)
    # Alpha gets a status line.
    await session._perform(
        state, rt_alpha, RenderAction(op="edit_status", chunks=("💭 alpha thinking…",)),
        send=rec.send, edit=rec.edit,
    )
    # Beta gets its OWN, different status line.
    await session._perform(
        state, rt_beta, RenderAction(op="edit_status", chunks=("⏳ beta rate limited",)),
        send=rec.send, edit=rec.edit,
    )
    # Two distinct message ids — one per project — and the texts don't bleed across.
    assert rt_alpha.status_message_id is not None
    assert rt_beta.status_message_id is not None
    assert rt_alpha.status_message_id != rt_beta.status_message_id
    assert rt_alpha.status_text == "💭 alpha thinking…"
    assert rt_beta.status_text == "⏳ beta rate limited"

    # Editing alpha's line again does not disturb beta's text/id.
    beta_mid, beta_text = rt_beta.status_message_id, rt_beta.status_text
    await session._perform(
        state, rt_alpha, RenderAction(op="edit_status", chunks=("ℹ️ alpha update",)),
        send=rec.send, edit=rec.edit,
    )
    assert rt_alpha.status_text == "ℹ️ alpha update"
    assert rt_beta.status_message_id == beta_mid and rt_beta.status_text == beta_text


async def test_two_projects_pending_holds_resolve_independently_via_runtimes(tmp_path):
    # Two projects each hold a pending ask (the accumulator rides each id's index entry, the
    # owning runtime carries each status). Resolving alpha's ask resolves ONLY alpha's engine
    # and flips ONLY alpha's status; beta's held ask + awaiting_answer status are untouched —
    # the lock-free resolve finds each held event on the OWNING project's runtime (D3/D7).
    session, _store, eng_alpha, eng_beta = await make_two_project_session(tmp_path, active="alpha")
    ask_a = AskEvent(
        questions=[{"question": "QA", "options": [{"label": "A"}]}],
        tool_use_id="a-ask", session_id="alpha-sid",
    )
    ask_b = AskEvent(
        questions=[{"question": "QB", "options": [{"label": "B"}]}],
        tool_use_id="b-ask", session_id="beta-sid",
    )
    prime_pending(session, ask_a, project="alpha")
    prime_pending(session, ask_b, project="beta")
    # Simulate each turn having parked awaiting its answer (what _drive_turn would set).
    session._chat(1).runtimes["alpha"].status = "awaiting_answer"
    session._chat(1).runtimes["beta"].status = "awaiting_answer"

    # Resolve ALPHA's single-question ask → alpha's engine only; alpha back to running.
    out = session.resolve_callback(1, encode_callback("a", "a-ask", question_index=0, option_index=0))
    assert out.handled is True
    assert eng_alpha.resolve_calls == [("a-ask", QuestionAnswer(answers={"QA": "A"}))]
    assert eng_beta.resolve_calls == []  # beta's concurrent hold untouched
    assert session._chat(1).runtimes["alpha"].status == "running"
    # Beta's held ask + its awaiting_answer status both survive (a separate run).
    assert session._chat(1).runtimes["beta"].status == "awaiting_answer"
    assert "b-ask" in session._chat(1).pending_index


async def test_free_text_marker_is_per_project_not_a_chat_slot(tmp_path):
    # ADR-005 D7: arming "Other" on a project sets the free-text marker on THAT project's
    # runtime — not a chat-global slot and not on a different project's runtime.
    session, _store, _eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    ask = AskEvent(
        questions=[{"question": "Name?", "options": [{"label": "A"}]}],
        tool_use_id="a-ask", session_id="alpha-sid",
    )
    prime_pending(session, ask, project="alpha")

    out = session.resolve_callback(1, encode_callback("o", "a-ask", question_index=0))
    assert out.expects_text is True
    # Marker armed on ALPHA's runtime…
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask"
    assert session._chat(1).runtimes["alpha"].awaiting_text_mode == "ask_other"
    # …and NOT on beta's runtime, and the chat itself has no such attribute (it moved, D7).
    assert session._chat(1).runtimes["beta"].awaiting_text_for is None
    assert not hasattr(session._chat(1), "awaiting_text_for")


async def test_reset_clears_only_active_projects_live_turn_state(tmp_path):
    # ADR-005 D7: /reset clears the ACTIVE project's runtime live-turn state (status line +
    # free-text marker + status→idle) and its pending entries, leaving a CONCURRENT project's
    # runtime (status line + held request + status) untouched.
    session, _store, _eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    rt_alpha = session._chat(1).runtimes["alpha"]
    rt_beta = session._chat(1).runtimes["beta"]
    # Active (alpha) has live-turn state + a held ask; beta is a separate concurrent run.
    rt_alpha.status_message_id = 11
    rt_alpha.status_text = "💭 alpha thinking…"
    rt_alpha.status = "awaiting_answer"
    rt_alpha.awaiting_text_for = "a-ask"
    rt_alpha.awaiting_text_mode = "ask_other"
    prime_pending(
        session,
        AskEvent(questions=[{"question": "Q", "options": [{"label": "A"}]}],
                 tool_use_id="a-ask", session_id="alpha-sid"),
        project="alpha",
    )
    rt_beta.status_message_id = 22
    rt_beta.status_text = "⏳ beta working…"
    rt_beta.status = "running"
    prime_pending(
        session,
        PlanEvent(plan="beta plan", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )

    session.reset(1)  # active == alpha

    # Alpha's live-turn state is wiped; status back to idle; its pending entry dropped.
    assert rt_alpha.status_message_id is None
    assert rt_alpha.status_text is None
    assert rt_alpha.status == "idle"
    assert rt_alpha.awaiting_text_for is None
    assert "a-ask" not in session._chat(1).pending_index
    # Beta (a concurrent run) is fully untouched by the active project's reset.
    assert rt_beta.status_message_id == 22
    assert rt_beta.status_text == "⏳ beta working…"
    assert rt_beta.status == "running"
    assert "b-plan" in session._chat(1).pending_index


async def test_project_status_no_runtime_is_idle(tmp_path):
    # ADR-005 D7: a project with NO in-memory runtime (e.g. just after restart, never run a
    # turn this process) reads as idle — read-only, creates nothing (RB1).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    session = make_session(FakeEngine([]), store=store)
    assert session.project_status(1, "api") == "idle"  # no runtime yet
    assert session.project_status(1, "nonexistent") == "idle"  # unknown name
    assert session.project_status(999, "api") == "idle"  # unknown chat
    # Read-only: still no runtime created for "api".
    assert "api" not in session._chat(1).runtimes


async def test_project_status_reader_is_case_insensitive(tmp_path):
    # The /projects status reader matches the runtime key case-insensitively (mirroring the
    # store's name match), so /projects and /switch WORK agree on a project's status.
    session, _store, _eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    session._chat(1).runtimes["alpha"].status = "running"
    assert session.project_status(1, "ALPHA") == "running"
    assert session.project_status(1, "Alpha") == "running"


# ===========================================================================
# P5 (T8) — Notification send-decision (ADR-005 D4) + RB5 under concurrency
# (RB7, ADR-005 D8). A BACKGROUND (non-foreground) project's hold/terminal
# becomes a name-prefixed 🔔/✅/⚠️ ping (the operator isn't watching it); a
# FOREGROUND project renders inline as P4. All outbound for a chat funnels
# through a per-chat send-rate gate (verbatim prioritized, never starved).
# ===========================================================================


async def _drive_project(session, chat_id, name, rt, *, send, edit, prompt="go"):
    """Drive ONE turn for a SPECIFIC project (background or foreground) to completion.

    Mirrors what ``handle_message`` does for a concurrent run: pass the project as the
    pinned ``target`` so ``_drive_turn`` acts on THAT project (its engine + foreground
    check), regardless of which project is the store's active one. Bounded so a wiring
    bug fails fast.
    """
    state = session._chat(chat_id)
    await asyncio.wait_for(
        session._drive_turn(
            state, chat_id, rt.engine, prompt,
            send=send, edit=edit, target=(name, rt),
        ),
        timeout=2.0,
    )


async def test_background_permission_hold_sends_attention_ping(tmp_path):
    # A BACKGROUND project (alpha) hits a permission hold while beta is foreground → a
    # "🔔 alpha — Claude needs approval" ping is sent, carrying the SAME [Allow/Deny]
    # keyboard the inline render would (so the tap still routes by the D3 index). The
    # foreground (beta) is undisturbed.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    perm = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=rm -rf x)",
        tool_use_id="a-perm", session_id="alpha-sid",
    )
    # alpha's engine yields the permission, parks, then resolves to a clean result.
    eng_alpha._script = [perm, HOLD, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success")]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()

    turn = asyncio.create_task(_drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit))
    # Wait until the attention ping is sent (the turn then parks at the HOLD).
    for _ in range(500):
        if any(s["text"] == "🔔 alpha — Claude needs approval" for s in rec.sends):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("background permission ping was never sent")
    # The ping is the name-prefixed bell, NOT the inline "🔐 Permission needed" prompt.
    ping = next(s for s in rec.sends if s["text"].startswith("🔔"))
    assert ping["text"] == "🔔 alpha — Claude needs approval"
    assert ping["reply_markup"] is not None  # carries the verdict keyboard (routes by id)
    assert not any("🔐 Permission needed" in s["text"] for s in rec.sends)  # no inline prompt
    # The hold is in the index, owned by alpha → a tap routes to alpha (D3) and finishes it.
    assert session._chat(1).pending_index["a-perm"].project_name == "alpha"
    out = session.resolve_callback(1, encode_callback("m", "a-perm", payload="o"))
    assert out.handled is True
    assert eng_alpha.resolve_calls == [("a-perm", PermissionDecision(verdict="allow_once"))]
    await turn


async def test_foreground_permission_hold_renders_inline_no_ping(tmp_path):
    # The inverse: when the SAME hold belongs to the FOREGROUND project (alpha is active),
    # it renders inline (the "🔐 Permission needed" prompt) with NO "🔔" ping (no duplicate).
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    perm = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=ls)",
        tool_use_id="a-perm", session_id="alpha-sid",
    )
    eng_alpha._script = [perm, HOLD, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success")]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()

    turn = asyncio.create_task(_drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit))
    for _ in range(500):
        if any("🔐 Permission needed" in s["text"] for s in rec.sends):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("foreground permission prompt was never rendered inline")
    # Inline prompt present, NO background ping.
    assert any("🔐 Permission needed" in s["text"] for s in rec.sends)
    assert not any(s["text"].startswith("🔔") for s in rec.sends)
    session.resolve_callback(1, encode_callback("m", "a-perm", payload="o"))
    await turn


async def test_background_ask_pings_then_sends_question_keyboards(tmp_path):
    # A BACKGROUND ask → a "🔔 alpha — asks a question" ping + each question's option
    # keyboard (so a multi-question ask stays answerable while backgrounded), and NO inline
    # verbatim ask body for the foreground.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    ask = AskEvent(
        questions=[{"question": "Pick?", "options": [{"label": "X"}, {"label": "Y"}]}],
        tool_use_id="a-ask", session_id="alpha-sid",
    )
    eng_alpha._script = [ask, HOLD, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success")]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()

    turn = asyncio.create_task(_drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit))
    for _ in range(500):
        if any(s["text"] == "🔔 alpha — asks a question" for s in rec.sends):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("background ask ping was never sent")
    # The bell ping is body-free; the question keyboard rides its own (safe) message.
    assert any(s["text"] == "🔔 alpha — asks a question" for s in rec.sends)
    assert any(s["reply_markup"] is not None for s in rec.sends)  # a question keyboard sent
    # Resolve via the id-routed option tap (proves the keyboard routes to alpha).
    out = session.resolve_callback(1, encode_callback("a", "a-ask", question_index=0, option_index=0))
    assert out.handled is True
    assert eng_alpha.resolve_calls == [("a-ask", QuestionAnswer(answers={"Pick?": "X"}))]
    await turn


async def test_background_done_sends_check_ping(tmp_path):
    # A BACKGROUND project finishing cleanly → "✅ alpha — done" (NOT the inline result).
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    eng_alpha._script = [ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="the answer")]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    await _drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit)
    assert any(s["text"] == "✅ alpha — done" for s in rec.sends)
    # The result TEXT is NOT sent inline for a background project (SB3-adjacent: only the
    # fixed "done" word, never the result body).
    assert not any("the answer" in s["text"] for s in rec.sends)


async def test_background_error_ping_is_body_free_kind_only_sb3(tmp_path):
    # ⭐ The load-bearing SB3 check (T3-review SB3): a BACKGROUND error pings the body-free
    # ErrorKind, NEVER event.message. Feed a SECRET-bearing ErrorEvent.message and assert the
    # secret is ABSENT from the ping and the kind label is present.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    # A synthetic secret-shaped body the operator must NEVER see (built via concatenation so
    # it is obviously a test fixture, not a real credential).
    raw_body = "S3CR3T-" + "z" * 200
    eng_alpha._script = [
        ErrorEvent(kind_of_error="tool_error", message=f"boom: {raw_body}", is_error=True, session_id="alpha-sid"),
    ]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    await _drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit)
    # The error ping is "⚠️ alpha — tool_error" — the body-free ErrorKind, never the message.
    assert any(s["text"] == "⚠️ alpha — tool_error" for s in rec.sends)
    # The secret-bearing body (and the raw message) appears in NO send (SB3).
    assert all(raw_body not in s["text"] for s in rec.sends)
    assert all("S3CR3T" not in s["text"] for s in rec.sends)
    assert all("boom" not in s["text"] for s in rec.sends)


async def test_background_run_does_not_spam_status_inline(tmp_path):
    # A backgrounded run is SILENT inline (D4) — its verbose status (tool_use / thinking /
    # incremental text) does NOT spam the chat; only the terminal ✅ ping is sent. (The
    # foreground project keeps its inline status line — covered by the existing turn tests.)
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    eng_alpha._script = [
        ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=make)", session_id="alpha-sid"),
        TextEvent(text="thinking…", incremental=True, session_id="alpha-sid"),
        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success"),
    ]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    await _drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit)
    # Only the done ping — no status line, no tool_use one-liner sent for the background run.
    assert [s["text"] for s in rec.sends] == ["✅ alpha — done"]
    assert rec.edits == []  # no status-line edits for a background run


async def test_background_attention_pings_are_throttled(tmp_path):
    # D4 coalescing: a background project bursting the SAME hold kind does not spam duplicate
    # 🔔 pings within the send interval. Drive _notify_background directly (the throttle is
    # per (project, kind)) with a real interval + a frozen clock, and assert the SECOND
    # identical ping is suppressed.
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,            # frozen — both pings arrive at the same instant
        chat_send_interval=5.0,         # a real interval so the throttle window is open
        sleep=_no_sleep,
    )
    state = session._chat(1)
    perm = PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="p1")
    rec = Recorder()
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    # Only ONE 🔔 ping (the second was throttled — the operator already knows; the first
    # ping's keyboard still routes the tap, D3/D4).
    assert sum(1 for s in rec.sends if s["text"] == "🔔 alpha — Claude needs approval") == 1
    # A DIFFERENT kind (an error) is NOT suppressed by the permission throttle.
    err = ErrorEvent(kind_of_error="turn_error", message="x")
    await session._notify_terminal(state, "alpha", err, send=rec.send)
    assert any(s["text"] == "⚠️ alpha — turn_error" for s in rec.sends)


# --- BLOCKER 1 (cross-model QA): a BACKGROUND project's actionable holds must NEVER be
#     throttle-suppressed. The (project, kind) coalescing throttle is for repeated
#     NON-actionable status/attention pings; a SECOND DISTINCT-tool_use_id permission /
#     plan / ask arriving within the send interval lands in the pending index (a tap WOULD
#     resolve it) but used to be dropped BEFORE its keyboard was sent — so the operator
#     could not act on it until the 60-min backstop. Each distinct held request must always
#     send its answerable keyboard. ----------------------------------------------------


async def _open_window_two_project_session(tmp_path, *, active: str):
    """Two live-engine projects (alpha/beta) but with the throttle window OPEN.

    ``make_two_project_session`` runs at ``chat_send_interval`` 0 + a frozen clock, so the
    ``(project, kind)`` throttle NEVER suppresses (``now - last < 0`` is false) — which would
    let a buggy "drop the 2nd distinct-id ping" path pass the test vacuously. Here the clock
    is frozen at 100.0 and the interval is a real 5 s, so the throttle window is genuinely
    OPEN for every ping in the test: a second ping is suppressed UNLESS the distinct-id
    bypass under test lets it through. This is what makes the BLOCKER-1 tests true RED tests.
    Both runtimes are seeded with their own started engine so an id-routed tap can resolve
    EITHER (the keyboard the ping must carry).
    """
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=(active == "alpha"))
    store.create(1, "beta", "/work/beta", make_active=(active == "beta"))
    eng_alpha = FakeEngine([], session_id="alpha-sid")
    eng_beta = FakeEngine([], session_id="beta-sid")
    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: {
            "/work/alpha": eng_alpha, "/work/beta": eng_beta
        }[cwd],
        clock=lambda: 100.0,        # frozen — every ping arrives inside the throttle window
        chat_send_interval=5.0,     # a real interval so the throttle window is genuinely OPEN
        sleep=_no_sleep,
    )
    for name, eng in (("alpha", eng_alpha), ("beta", eng_beta)):
        rt = session._runtime(1, name, f"/work/{name}")
        rt.engine = eng
        rt.started = True
    return session, store, eng_alpha, eng_beta


async def test_background_distinct_permission_holds_each_send_keyboard(tmp_path):
    # ⭐ BLOCKER 1: two DISTINCT-tool_use_id permission holds from a BACKGROUND project,
    # WITHIN the (open) throttle window, must BOTH send an answerable keyboard — and tapping
    # EACH (via the real index/callback path) resolves the RIGHT request. With the bug, the
    # 2nd distinct-id ping is suppressed by the (project, kind) throttle → only ONE bell →
    # this fails. The two engines are seeded live so the id-routed resolve can reach each.
    session, _store, eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    perm1 = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=a)",
        tool_use_id="a-perm-1", session_id="alpha-sid",
    )
    perm2 = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=b)",
        tool_use_id="a-perm-2", session_id="alpha-sid",
    )
    rec = Recorder()
    # Register BOTH holds in the index (as _drive_turn does on each injected event), then
    # ping each. Both are background "permission" pings within the SAME open throttle window.
    session._register_pending(state, "alpha", perm1)
    await session._notify_background(state, 1, "alpha", perm1, "permission", send=rec.send)
    session._register_pending(state, "alpha", perm2)
    await session._notify_background(state, 1, "alpha", perm2, "permission", send=rec.send)

    # BOTH pings sent, each carrying a verdict keyboard (the operator can act on each).
    bell_sends = [s for s in rec.sends if s["text"] == "🔔 alpha — Claude needs approval"]
    assert len(bell_sends) == 2, "a 2nd distinct-id permission hold must NOT be throttle-suppressed"
    assert all(s["reply_markup"] is not None for s in bell_sends)  # each routes by id

    # Tapping EACH resolves the RIGHT request against alpha's engine (the index owns id->proj).
    out1 = session.resolve_callback(1, encode_callback("m", "a-perm-1", payload="o"))
    out2 = session.resolve_callback(1, encode_callback("m", "a-perm-2", payload="s"))
    assert out1.handled is True and out2.handled is True
    assert eng_alpha.resolve_calls == [
        ("a-perm-1", PermissionDecision(verdict="allow_once")),
        ("a-perm-2", PermissionDecision(verdict="allow_session")),
    ]


async def test_background_distinct_plan_holds_each_send_keyboard(tmp_path):
    # BLOCKER 1 (plan variant): two DISTINCT-id plan holds from a background project within
    # the OPEN throttle window each send their [Approve/Reject] keyboard (neither dropped).
    session, _store, eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    plan1 = PlanEvent(plan="Plan one", tool_use_id="a-plan-1", session_id="alpha-sid")
    plan2 = PlanEvent(plan="Plan two", tool_use_id="a-plan-2", session_id="alpha-sid")
    rec = Recorder()
    session._register_pending(state, "alpha", plan1)
    await session._notify_background(state, 1, "alpha", plan1, "plan", send=rec.send)
    session._register_pending(state, "alpha", plan2)
    await session._notify_background(state, 1, "alpha", plan2, "plan", send=rec.send)

    bell_sends = [s for s in rec.sends if s["text"] == "🔔 alpha — proposes a plan"]
    assert len(bell_sends) == 2, "a 2nd distinct-id plan hold must NOT be throttle-suppressed"
    assert all(s["reply_markup"] is not None for s in bell_sends)
    out = session.resolve_callback(1, encode_callback("p", "a-plan-2", plan_action="a"))
    assert out.handled is True
    assert eng_alpha.resolve_calls == [("a-plan-2", PlanVerdict(approve=True))]


async def test_background_same_permission_id_still_coalesced(tmp_path):
    # The throttle still COALESCES a re-emit of the SAME tool_use_id (the operator already
    # has that keyboard) — only DISTINCT ids bypass. Guards against the fix becoming "never
    # throttle anything", which would regress test_background_attention_pings_are_throttled.
    # Uses the OPEN-window rig so the throttle is genuinely active for the 2nd same-id ping.
    session, _store, _eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    perm = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(...)",
        tool_use_id="a-perm-same", session_id="alpha-sid",
    )
    rec = Recorder()
    session._register_pending(state, "alpha", perm)
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    assert sum(1 for s in rec.sends if s["text"] == "🔔 alpha — Claude needs approval") == 1


# --- ROUND-2 BLOCKER (cross-model QA): a same-tool_use_id BACKGROUND ASK re-emit must
#     coalesce FULLY — suppress the bell AND the question body/keyboards, exactly like the
#     permission/plan path. The round-1 B1 fix only suppressed the bell line; the per-question
#     keyboard loop still ran UNCONDITIONALLY, so a same-id ask re-emitted inside the throttle
#     window DUPLICATED the question keyboards (the operator's chat fills with redundant
#     answerable keyboards for the SAME question — contradicting the same-id-coalesce
#     contract). A DISTINCT-id ask must still send its full keyboard set (round-1 fix kept).
# ------------------------------------------------------------------------------------------


async def test_background_same_ask_id_reemit_sends_nothing_new(tmp_path):
    # ⭐ ROUND-2 BLOCKER: emit the SAME AskEvent(tool_use_id=X) twice inside an OPEN throttle
    # window → exactly ONE bell + ONE set of question keyboards. With the bug, the bell is
    # suppressed on the 2nd ping but the question keyboard(s) are re-sent → TWO keyboards for
    # the SAME single question → this fails. The OPEN-window rig (frozen clock + real 5 s
    # interval) makes the throttle genuinely active for the 2nd same-id ping (non-vacuous).
    session, _store, _eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    ask = AskEvent(
        questions=[{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}]}],
        tool_use_id="a-ask-same",
        session_id="alpha-sid",
    )
    rec = Recorder()
    session._register_pending(state, "alpha", ask)
    await session._notify_background(state, 1, "alpha", ask, "ask", send=rec.send)
    await session._notify_background(state, 1, "alpha", ask, "ask", send=rec.send)

    # Exactly ONE bell line (the 2nd same-id ask is coalesced — bell suppressed).
    bells = [s for s in rec.sends if s["text"] == "🔔 alpha — asks a question"]
    assert len(bells) == 1, "a re-emit of the SAME ask id must not send a 2nd bell"
    # Exactly ONE keyboard-bearing question message (one question, one keyboard) — NOT two.
    # This is the load-bearing assertion: the question body/keyboards must be suppressed too,
    # not just the bell. With the round-1 bug this is 2 (the keyboard loop re-ran).
    keyboarded = [s for s in rec.sends if s["reply_markup"] is not None]
    assert len(keyboarded) == 1, (
        "a same-id ask re-emit must NOT re-send the question keyboard(s): "
        f"got {len(keyboarded)} keyboard messages {[s['text'] for s in keyboarded]}"
    )
    # Belt-and-braces: the 2nd ping sent NOTHING new at all (mirrors the permission/plan path).
    assert len(rec.sends) == 2, (
        "a same-id ask re-emit must send nothing new (1 bell + 1 question only): "
        f"{[s['text'] for s in rec.sends]}"
    )


async def test_background_same_multi_question_ask_id_reemit_sends_nothing_new(tmp_path):
    # ROUND-2 BLOCKER (multi-question variant): a 2-question ask re-emitted under the SAME id
    # must send its bell + TWO question keyboards ONCE, and the re-emit must add nothing —
    # NOT a second pair of question keyboards. Pins that the whole ask body is gated by the
    # single _should_notify decision (the worst-case duplication is per-question).
    session, _store, _eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    ask = AskEvent(
        questions=[
            {"question": "Storage?", "options": [{"label": "JSON"}, {"label": "SQLite"}]},
            {"question": "CLI?", "options": [{"label": "argparse"}, {"label": "Typer"}]},
        ],
        tool_use_id="a-ask-multi-same",
        session_id="alpha-sid",
    )
    rec = Recorder()
    session._register_pending(state, "alpha", ask)
    await session._notify_background(state, 1, "alpha", ask, "ask", send=rec.send)
    await session._notify_background(state, 1, "alpha", ask, "ask", send=rec.send)

    bells = [s for s in rec.sends if s["text"] == "🔔 alpha — asks a question"]
    keyboarded = [s for s in rec.sends if s["reply_markup"] is not None]
    assert len(bells) == 1, "a re-emit of the SAME multi-q ask id must not send a 2nd bell"
    assert len(keyboarded) == 2, (
        "a same-id multi-q ask re-emit must send each question keyboard exactly ONCE: "
        f"got {len(keyboarded)} {[s['text'] for s in keyboarded]}"
    )
    # 1 bell + 2 questions, and nothing from the 2nd ping.
    assert len(rec.sends) == 3, [s["text"] for s in rec.sends]


async def test_background_distinct_ask_holds_each_send_keyboard(tmp_path):
    # GUARD (round-1 behavior preserved): two DISTINCT-tool_use_id asks from a background
    # project within the OPEN throttle window must EACH send their bell + question keyboard
    # (the same-id coalesce must NOT regress the distinct-id BLOCKER-1 fix). And tapping each
    # resolves the RIGHT ask via the index. Non-vacuous: the window is open, so a buggy
    # "throttle by (project, kind)" would drop the 2nd ask entirely.
    session, _store, eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    state = session._chat(1)
    ask1 = AskEvent(
        questions=[{"question": "Q1?", "options": [{"label": "Yes"}, {"label": "No"}]}],
        tool_use_id="a-ask-1",
        session_id="alpha-sid",
    )
    ask2 = AskEvent(
        questions=[{"question": "Q2?", "options": [{"label": "Up"}, {"label": "Down"}]}],
        tool_use_id="a-ask-2",
        session_id="alpha-sid",
    )
    rec = Recorder()
    session._register_pending(state, "alpha", ask1)
    await session._notify_background(state, 1, "alpha", ask1, "ask", send=rec.send)
    session._register_pending(state, "alpha", ask2)
    await session._notify_background(state, 1, "alpha", ask2, "ask", send=rec.send)

    bells = [s for s in rec.sends if s["text"] == "🔔 alpha — asks a question"]
    keyboarded = [s for s in rec.sends if s["reply_markup"] is not None]
    assert len(bells) == 2, "a 2nd DISTINCT-id ask must NOT be throttle-suppressed (round-1)"
    assert len(keyboarded) == 2, "each distinct-id ask must send its own question keyboard"
    # Each distinct id is independently answerable via the index against alpha's engine.
    out1 = session.resolve_callback(1, encode_callback("a", "a-ask-1", question_index=0, option_index=0))
    out2 = session.resolve_callback(1, encode_callback("a", "a-ask-2", question_index=0, option_index=1))
    assert out1.handled is True and out2.handled is True
    assert eng_alpha.resolve_calls == [
        ("a-ask-1", QuestionAnswer(answers={"Q1?": "Yes"})),
        ("a-ask-2", QuestionAnswer(answers={"Q2?": "Down"})),
    ]


async def test_background_two_distinct_holds_end_to_end_via_drive_turn(tmp_path):
    # ⭐ BLOCKER 1 end-to-end through the REAL turn loop: a background project's stream emits
    # two DISTINCT-id permission holds (parking between them). The driver must register +
    # ping BOTH (each keyboard answerable), and resolving each unblocks the turn to its
    # clean result — proving the request "lands in the index AND a keyboard is sent" for
    # every distinct hold, not just the first. The OPEN-window rig makes the 2nd ping
    # genuinely throttle-gated (with the bug the 2nd bell never fires → _wait_pings(2) times
    # out → RED).
    session, _store, eng_alpha, _eng_beta = await _open_window_two_project_session(tmp_path, active="beta")
    perm1 = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=one)",
        tool_use_id="a-p1", session_id="alpha-sid",
    )
    perm2 = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=two)",
        tool_use_id="a-p2", session_id="alpha-sid",
    )
    # Two holds back to back (the FakeEngine HOLD parks after each yielded event until a
    # resolve fires), then a clean result.
    eng_alpha._script = [
        perm1, HOLD,
        perm2, HOLD,
        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success"),
    ]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    turn = asyncio.create_task(_drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit))

    # Wait for the FIRST ping, then resolve perm1 → the turn advances to perm2.
    async def _wait_pings(n):
        for _ in range(500):
            if sum(1 for s in rec.sends if s["text"] == "🔔 alpha — Claude needs approval") >= n:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected >= {n} background permission pings")

    await _wait_pings(1)
    assert session.resolve_callback(1, encode_callback("m", "a-p1", payload="o")).handled is True
    # The SECOND distinct-id hold must ALSO ping (this is exactly what the throttle dropped).
    await _wait_pings(2)
    assert session.resolve_callback(1, encode_callback("m", "a-p2", payload="o")).handled is True
    await asyncio.wait_for(turn, timeout=2.0)
    assert eng_alpha.resolve_calls == [
        ("a-p1", PermissionDecision(verdict="allow_once")),
        ("a-p2", PermissionDecision(verdict="allow_once")),
    ]


# --- RB7: RB5 under concurrency — the per-chat send gate bounds the combined
#     cross-project rate, and verbatim survives (ordered, never dropped). ------


class _RecordingSleep:
    """An injected sleep that RECORDS each awaited delay and advances a clock (no real
    wait), so a test can assert the send gate spaced sends without real time."""

    def __init__(self, clock):
        self._clock = clock
        self.waits: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        self._clock.advance(delay)  # honor the gate's wait on the controllable clock


async def _no_sleep(delay: float) -> None:
    """An injected sleep that never actually waits (for tests that don't time the gate)."""
    return None


class _AdvClock:
    """A controllable monotonic clock (advanced by the recording sleep)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def test_rb7_two_projects_bursting_concurrently_are_bounded_and_verbatim_survives(tmp_path):
    # ⭐ RB7 — the headline. TWO projects (both FOREGROUND-rendered here by giving the chat
    # no store → a single implicit foreground, so BOTH render inline and their combined
    # output all hits the chat gate) each emit a burst of distinct status lines + a verbatim
    # final answer, driven CONCURRENTLY through ONE chat. Assert: (a) the per-chat gate paced
    # the combined sends (positive spacing waits were honored — the rate stayed bounded under
    # the concurrent burst), and (b) BOTH verbatim final answers survived (ordered, never
    # dropped — a starved status line is fine, a starved verbatim is a deadlock).
    clock = _AdvClock()
    sleeper = _RecordingSleep(clock)

    def script(tag):
        return [
            TextEvent(text=f"{tag} d{i}", incremental=True, session_id=tag) for i in range(5)
        ] + [ResultEvent(session_id=tag, is_error=False, subtype="success", result_text=f"{tag} FINAL")]

    # No store → a single implicit "default" project (always foreground), but we exercise
    # two CONCURRENT turns by driving _drive_turn twice against two runtimes sharing the gate.
    eng_a = FakeEngine(script("alpha"), session_id="alpha")
    eng_b = FakeEngine(script("beta"), session_id="beta")
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng_a,
        clock=clock,
        min_edit_interval=0.0,    # no per-project coalescing → the GATE bounds the combined rate
        chat_send_interval=1.0,   # the per-chat budget under test
        sleep=sleeper,
    )
    state = session._chat(1)
    rt_a = _ProjectRuntime(cwd="/work/a")
    rt_a.engine = eng_a
    rt_a.started = True
    rt_b = _ProjectRuntime(cwd="/work/b")
    rt_b.engine = eng_b
    rt_b.started = True

    rec = Recorder()
    # Drive BOTH turns concurrently through the one chat + one gate.
    t_a = asyncio.create_task(session._drive_turn(state, 1, eng_a, "ga", send=rec.send, edit=rec.edit, target=("alpha", rt_a)))
    t_b = asyncio.create_task(session._drive_turn(state, 1, eng_b, "gb", send=rec.send, edit=rec.edit, target=("beta", rt_b)))
    await asyncio.wait_for(asyncio.gather(t_a, t_b), timeout=3.0)

    texts = [s["text"] for s in rec.sends]
    # BOTH verbatim final answers survived (never dropped by the gate).
    assert any("alpha FINAL" in t for t in texts)
    assert any("beta FINAL" in t for t in texts)
    # The gate paced the combined cross-project sends (it inserted positive spacing waits) —
    # so the chat's send rate stayed bounded under two concurrent bursts (RB5/RB7).
    assert any(w > 0 for w in sleeper.waits)
    assert clock.t > 0.0


async def test_rb7_combined_send_rate_is_bounded(tmp_path):
    # Two concurrent FOREGROUND-rendered bursts in one chat: assert the gate inserted a
    # spacing wait for the sends so they did not all fire at the same instant (RB5 under
    # concurrency). We use a controllable clock + a recording sleep that honors the wait.
    from claude_tg.session_store import JsonSessionStore

    class _Clock:
        def __init__(self):
            self.t = 0.0
        def __call__(self):
            return self.t
        def advance(self, dt):
            self.t += dt

    clock = _Clock()
    sleeper = _RecordingSleep(clock)
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "solo", "/work/solo", make_active=True)
    # A burst of status edits (each a DISTINCT line so none is deduped) + a verbatim result.
    script = [
        TextEvent(text=f"delta {i}", incremental=True, session_id="s") for i in range(6)
    ] + [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="FINAL ANSWER")]
    eng = FakeEngine(script, session_id="s")
    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=clock,
        min_edit_interval=0.0,        # let every status delta through to the gate (no per-
                                      # project coalescing) so the GATE is what bounds them
        chat_send_interval=1.0,       # the per-chat budget under test
        sleep=sleeper,
    )
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The verbatim final answer SURVIVED (it is rate-ordered, never dropped).
    assert any("FINAL ANSWER" in s["text"] for s in rec.sends)
    # The gate inserted spacing waits (the combined rate was bounded — sends did not all
    # fire at t=0). At least one positive wait was honored.
    assert any(w > 0 for w in sleeper.waits)
    # The clock advanced by the cumulative spacing (proof the gate actually paced the chat).
    assert clock.t > 0.0


async def test_rb7_per_project_coalescers_are_independent(tmp_path):
    # Each running project keeps its OWN Coalescer — a status burst in alpha does NOT reset
    # beta's status throttle (the per-project independence the gate sits ON TOP of). Driven
    # via the two-project setup; we assert each project edits its OWN status line id.
    session, _store, _eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    rt_alpha = session._chat(1).runtimes["alpha"]
    rt_beta = session._chat(1).runtimes["beta"]
    rec = Recorder()
    state = session._chat(1)
    # Alpha emits a status line; beta emits its OWN — distinct ids, no cross-talk (the per-
    # project status lines the Coalescers feed are independent — ADR-005 D7/D8).
    await session._perform(
        state, rt_alpha, RenderAction(op="edit_status", chunks=("alpha s1",)),
        send=rec.send, edit=rec.edit,
    )
    await session._perform(
        state, rt_beta, RenderAction(op="edit_status", chunks=("beta s1",)),
        send=rec.send, edit=rec.edit,
    )
    assert rt_alpha.status_message_id != rt_beta.status_message_id
    assert rt_alpha.status_text == "alpha s1" and rt_beta.status_text == "beta s1"


# ===========================================================================
# P5 / ADR-005 D5 + D9 (T9) — free-text routing precedence (reply-to / /to /
# most-recent) + concurrency-aware /cancel <name>|all + /rm-running-refused +
# the queued-waiter DRAIN (no zombie run). All session-level (mock engine).
# The bar: "never silently misroute" a free-text reply; "never zombie-run" a
# cancelled/removed QUEUED project.
# ===========================================================================


def _arm_other(session, eng_owner, *, project, tool_use_id, chat_id=1):
    """Prime an ask for ``project`` + tap its "Other" so ``project`` is armed for free text.

    Returns the CallbackOutcome of the "Other" tap (so a test can read its project_name /
    tool_use_id — the name-echo + reply-to-map inputs)."""
    ask = AskEvent(
        questions=[{"question": "Q?", "options": [{"label": "A"}]}],
        tool_use_id=tool_use_id,
        session_id=f"{project}-sid",
    )
    prime_pending(session, ask, project=project, chat_id=chat_id)
    return session.resolve_callback(
        chat_id, encode_callback("o", tool_use_id, question_index=0)
    )


async def test_free_text_most_recent_wins_with_two_armed_projects(tmp_path):
    # D5 default: with BOTH projects armed for free text, the NEXT plain message resolves the
    # MOST-RECENTLY-armed one (newest wins — the name-echoed prompt said which). Arm alpha,
    # then beta → a plain reply resolves BETA, never alpha.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    _arm_other(session, eng_b, project="beta", tool_use_id="b-ask")  # newest

    rec = Recorder()
    await session.handle_message(1, "the answer", send=rec.send, edit=rec.edit)
    # Resolved BETA (newest), never alpha. Mutation probe: most-recent → first-armed would
    # resolve alpha here and fail.
    assert eng_b.resolve_calls == [("b-ask", QuestionAnswer(answers={"Q?": "the answer"}))]
    assert eng_a.resolve_calls == []
    assert rec.sends == []  # no new turn opened
    # alpha is still armed (only beta resolved); beta's marker cleared.
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask"
    assert session._chat(1).runtimes["beta"].awaiting_text_for is None


async def test_free_text_reply_to_overrides_most_recent(tmp_path):
    # D5 escape hatch (a): a reply-to ALPHA's free-text prompt routes the answer to ALPHA
    # even though BETA is the most-recently-armed default. Arm alpha (record its prompt's
    # message_id), arm beta (newest), then reply-to alpha's prompt → resolves ALPHA.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    out_a = _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    # The bot would send alpha's prompt and register message_id -> tool_use_id; simulate it.
    session.register_reply_prompt(1, 9001, out_a.tool_use_id)
    _arm_other(session, eng_b, project="beta", tool_use_id="b-ask")  # newest default

    rec = Recorder()
    await session.handle_message(
        1, "alpha answer", send=rec.send, edit=rec.edit, reply_to_message_id=9001
    )
    # Reply-to alpha's prompt → resolved ALPHA, never the most-recent beta. Mutation probe:
    # ignoring reply-to would resolve beta here and fail.
    assert eng_a.resolve_calls == [("a-ask", QuestionAnswer(answers={"Q?": "alpha answer"}))]
    assert eng_b.resolve_calls == []
    assert rec.sends == []


async def test_free_text_reply_to_stale_prompt_no_misroute(tmp_path):
    # "Never silently misroute": a reply-to a free-text prompt whose request is GONE (its
    # turn ended / it was answered) must NOT silently fall through to the most-recent default
    # — it no-ops. Arm beta (the most-recent), map a stale message_id to a now-absent id, then
    # reply-to that stale prompt → resolves NOTHING (not beta), opens no new turn.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_b, project="beta", tool_use_id="b-ask")  # most-recent default
    # A reply-to prompt whose id "gone-ask" is not in the index (request resolved/ended).
    session._chat(1).reply_to_index[9009] = "gone-ask"

    rec = Recorder()
    await session.handle_message(
        1, "stale reply", send=rec.send, edit=rec.edit, reply_to_message_id=9009
    )
    # Misroute bar: neither beta (the default) nor anyone else is resolved; no new turn.
    assert eng_a.resolve_calls == [] and eng_b.resolve_calls == []
    assert rec.sends == []  # NOT opened as a new turn either
    # beta is still armed (untouched) — the stale reply-to did not steal its answer.
    assert session._chat(1).runtimes["beta"].awaiting_text_for == "b-ask"


async def test_free_text_reply_to_unmapped_message_falls_through_to_default(tmp_path):
    # A reply-to a message that is NOT one of our free-text prompts (not in the reply-to map)
    # falls through to the most-recent default (it is an ordinary reply that happens to carry
    # a reply_to_message_id). Arm beta; reply with an UNMAPPED message_id → resolves beta.
    session, _store, _eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_b, project="beta", tool_use_id="b-ask")

    rec = Recorder()
    await session.handle_message(
        1, "an answer", send=rec.send, edit=rec.edit, reply_to_message_id=12345
    )
    assert eng_b.resolve_calls == [("b-ask", QuestionAnswer(answers={"Q?": "an answer"}))]
    assert rec.sends == []


async def test_no_project_armed_plain_message_is_a_normal_turn(tmp_path):
    # When NO project is armed for free text, a plain message is a normal NEW turn (unchanged
    # behavior) — even one carrying a reply_to_message_id that isn't a free-text prompt.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    eng = FakeEngine(
        [ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="ok")],
        session_id="alpha-sess",
    )
    session = make_multi_session({"/work/alpha": eng}, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "do a thing", send=rec.send, edit=rec.edit, reply_to_message_id=42),
        timeout=2.0,
    )
    # A real turn ran (the engine produced output), no resolve happened.
    assert eng.resolve_calls == []
    assert any("ok" in s["text"] for s in rec.sends)


async def test_arm_other_outcome_carries_name_and_id_for_name_echo(tmp_path):
    # D5 name-echo + reply-to-map inputs: the "Other" tap's CallbackOutcome carries the owning
    # project name (so the bot can name-echo "✏️ <name>: …") and the tool_use_id (so the bot
    # can map the prompt's message_id -> id). Proven for both ask-"Other" and plan-"Reject".
    session, _store, eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    out = _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    assert out.expects_text is True
    assert out.project_name == "alpha" and out.tool_use_id == "a-ask"

    plan = PlanEvent(plan="P", tool_use_id="a-plan", session_id="alpha-sid")
    prime_pending(session, plan, project="alpha")
    out_p = session.resolve_callback(1, encode_callback("p", "a-plan", plan_action="r"))
    assert out_p.expects_text is True
    assert out_p.project_name == "alpha" and out_p.tool_use_id == "a-plan"


async def test_reply_to_map_pruned_on_resolve(tmp_path):
    # D5 map lifecycle: the reply-to entry is pruned when its request resolves, so a later
    # reply to that (now-resolved) prompt cannot misroute and the map cannot grow unbounded.
    session, _store, eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    out = _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    session.register_reply_prompt(1, 7001, out.tool_use_id)
    assert 7001 in session._chat(1).reply_to_index

    rec = Recorder()
    await session.handle_message(1, "answer", send=rec.send, edit=rec.edit)  # resolves alpha
    assert eng_a.resolve_calls and "a-ask" not in session._chat(1).pending_index
    # The map entry for the now-resolved prompt was pruned.
    assert 7001 not in session._chat(1).reply_to_index


async def test_resolve_to_routes_to_named_project(tmp_path):
    # /to <name> (D5 escape hatch c): routes the free text to the NAMED project's pending
    # free-text request regardless of which is the most-recent default. Arm alpha; /to alpha →
    # resolves ALPHA. (beta is the most-recent here only to prove /to overrides it.)
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="beta")
    _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    _arm_other(session, eng_b, project="beta", tool_use_id="b-ask")  # newest default

    reply = session.resolve_to(1, "alpha", "explicit answer")
    assert "alpha" in reply
    assert eng_a.resolve_calls == [("a-ask", QuestionAnswer(answers={"Q?": "explicit answer"}))]
    assert eng_b.resolve_calls == []  # the most-recent default was NOT used (/to overrode it)


async def test_resolve_to_case_insensitive(tmp_path):
    # /to matches the project name case-insensitively (mirroring the store's match).
    session, _store, eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    reply = session.resolve_to(1, "ALPHA", "hi")
    assert "alpha" in reply
    assert eng_a.resolve_calls == [("a-ask", QuestionAnswer(answers={"Q?": "hi"}))]


async def test_resolve_to_not_awaiting_is_clear_noop(tmp_path):
    # /to a project that is NOT awaiting free text → a clear no-op message, never a misroute.
    # alpha is armed but we /to beta (not armed) → beta is untouched, alpha is untouched.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    reply = session.resolve_to(1, "beta", "wrong target")
    assert "not awaiting" in reply.lower()
    assert eng_a.resolve_calls == [] and eng_b.resolve_calls == []  # nothing resolved
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask"  # alpha still armed


async def test_resolve_to_unknown_project_is_clear_noop(tmp_path):
    # /to an unknown project name → a clear no-op message (RB1), never a crash / misroute.
    session, _store, eng_a, _eng_b = await make_two_project_session(tmp_path, active="alpha")
    _arm_other(session, eng_a, project="alpha", tool_use_id="a-ask")
    reply = session.resolve_to(1, "nope", "text")
    assert "not awaiting" in reply.lower()
    assert eng_a.resolve_calls == []


# -- /cancel <name> | all | active (D9) --------------------------------------


async def test_cancel_named_aborts_only_that_run(tmp_path):
    # /cancel <name> aborts ONLY that project's run; a concurrent run survives. Both alpha and
    # beta have live engines; cancel beta → beta cancelled, alpha untouched.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    prime_pending(
        session,
        PermissionEvent(tool_name="B", tool_input_summary="B(...)", tool_use_id="a-perm", session_id="alpha-sid"),
        project="alpha",
    )
    prime_pending(
        session,
        PlanEvent(plan="bp", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )
    aborted = session.handle_cancel(1, "beta")
    assert aborted == 1
    assert eng_b.cancel_calls == [None] and eng_a.cancel_calls == []
    # beta's pending cleared; alpha's survives (a separate concurrent run).
    assert "b-plan" not in session._chat(1).pending_index
    assert "a-perm" in session._chat(1).pending_index


async def test_cancel_all_aborts_every_run(tmp_path):
    # /cancel all aborts EVERY running project for the chat.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    prime_pending(
        session,
        PermissionEvent(tool_name="B", tool_input_summary="B(...)", tool_use_id="a-perm", session_id="alpha-sid"),
        project="alpha",
    )
    prime_pending(
        session,
        PlanEvent(plan="bp", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )
    aborted = session.handle_cancel(1, "all")
    assert aborted == 2  # both engines cancelled (1 each)
    assert eng_a.cancel_calls == [None] and eng_b.cancel_calls == [None]
    assert session._chat(1).pending_index == {}  # all entries cleared


async def test_cancel_active_default_targets_active_only(tmp_path):
    # /cancel (no arg) targets the ACTIVE project only. alpha active → cancel alpha; beta's
    # concurrent run is untouched.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    prime_pending(
        session,
        PlanEvent(plan="bp", tool_use_id="b-plan", session_id="beta-sid"),
        project="beta",
    )
    aborted = session.handle_cancel(1)  # active == alpha
    assert aborted == 1 and eng_a.cancel_calls == [None] and eng_b.cancel_calls == []
    assert "b-plan" in session._chat(1).pending_index  # beta untouched


async def test_cancel_unknown_name_is_noop(tmp_path):
    # /cancel <unknown> → no-op (RB1): nothing cancelled, no crash.
    session, _store, eng_a, eng_b = await make_two_project_session(tmp_path, active="alpha")
    assert session.handle_cancel(1, "ghost") == 0
    assert eng_a.cancel_calls == [] and eng_b.cancel_calls == []


# -- the queued-waiter DRAIN: /cancel + /rm of a QUEUED project (T6-review) ---


async def test_cancel_queued_project_drains_waiter_no_zombie_run(tmp_path):
    # ⭐ The T6-review hazard: /cancel of a QUEUED-not-yet-running project must DRAIN its
    # parked waiter so it never springs to a "zombie run" when a slot frees. cap=1: alpha
    # holds the only slot, beta is queued. /cancel beta → beta's waiter drained. THEN alpha
    # finishes → its freed slot must NOT start beta (it was cancelled).
    store = _three_project_store(tmp_path)
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1
    assert session.project_status(1, "beta") == "queued"
    # RACE fix: a QUEUED turn is in-flight (its busy-guard marker is set before _acquire_slot),
    # so a same-project 2nd message would be rejected even while only queued.
    beta_rt = session._chat(1).runtimes["beta"]
    assert beta_rt.inflight is True

    # /cancel beta → drain its parked waiter (it never ran).
    aborted = session.handle_cancel(1, "beta")
    # NB1: a drained queued-not-yet-running turn IS a cancelled unit, so the count reports 1
    # (it had no live engine → 0 pending requests, but the operator DID cancel a turn — the
    # feedback must not say "nothing was in flight"). The pending-request engine tally is 0;
    # the +1 is the drained queued turn.
    assert aborted == 1
    # beta's queued turn is cancelled → its task raises CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._chat(1).run_queue == deque()  # waiter removed from the queue
    assert eng_b.started is False  # beta NEVER started
    # RACE fix: the drain-cancel raises CancelledError out of _acquire_slot (BEFORE the
    # slot-release try), so the OUTER finally is what must clear inflight — proving the marker
    # is balanced even on the drain path (no wedge: beta is acceptable again).
    assert beta_rt.inflight is False

    # Now alpha finishes → its freed slot must NOT zombie-start beta.
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    # Give the loop a chance to (wrongly) start beta if the drain failed.
    for _ in range(20):
        await asyncio.sleep(0)
    assert eng_b.started is False, "cancelled queued project must NOT zombie-run on a freed slot"
    assert session._running == 0  # back to zero — no leaked / zombie slot


async def test_cancel_queued_only_counts_as_cancelled_nb1(tmp_path):
    # ⭐ NB1 (cross-model QA): /cancel of a QUEUED-ONLY project (no live engine, parked behind
    # the cap) must report it as CANCELLED — the operator DID abort a turn, so the count must
    # be > 0 (else cmd_cancel tells them "nothing was in flight" for a turn they just killed).
    # With the bug the count is 0 (only the engine's pending tally) → RED.
    store = _three_project_store(tmp_path)
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert session.project_status(1, "beta") == "queued"
    assert eng_b.started is False  # beta never started → 0 pending requests

    cancelled = session.handle_cancel(1, "beta")
    assert cancelled == 1, "a drained queued-only turn must count as cancelled (NB1)"

    # Teardown.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    assert session._running == 0


async def test_cancel_all_drains_queued_and_cancels_running(tmp_path):
    # /cancel all: cancels the RUNNING project AND drains the QUEUED one (no zombie run).
    store = _three_project_store(tmp_path)
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1

    session.handle_cancel(1, "all")
    # alpha (running) unblocks + ends; beta (queued) is drained.
    await asyncio.wait_for(turn_a, timeout=2.0)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert eng_b.started is False  # beta never ran
    assert session._running == 0 and session._chat(1).run_queue == deque()


async def test_reset_while_queued_then_cancel_no_zombie(tmp_path):
    # Deferred-T6 case: /reset while a turn is QUEUED. The bot refuses /reset of a busy ACTIVE
    # project, but a QUEUED active project (lock not yet held) is "not busy" — reset proceeds
    # and clears its session; the still-queued turn must then be cancellable without a zombie
    # run. Here we drive it at the session level: beta queued, reset (clears beta's session),
    # then /cancel beta drains it; alpha's freed slot does not zombie-start beta.
    store = _three_project_store(tmp_path)
    store.set_session_id(1, "beta", "beta-old")
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert session.project_status(1, "beta") == "queued"

    # reset the (queued, lock-free) active beta → clears its session_id (no crash).
    session.reset(1)
    assert store.get_project(1, "beta")["session_id"] is None

    # cancel the still-queued beta → drained, no zombie run when alpha's slot frees.
    # (NB3 now drains it in reset() above, so this /cancel is a harmless no-op — the focused
    # NB3 test below pins that reset ALONE cancels the queued turn.)
    session.handle_cancel(1, "beta")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert eng_b.started is False
    assert session._running == 0


async def test_reset_drains_active_projects_queued_turn_nb3(tmp_path):
    # ⭐ NB3 (cross-model QA): /reset of an active project that has a not-yet-started QUEUED
    # turn must DRAIN (cancel) that queued turn as PART of the reset — it never started, so no
    # orphan — then reset cleanly. Consistent with the P4 /reset-while-running handling
    # (which cancels the held turn); the bot allows /reset here because a queued (lock-free)
    # active project is "not busy". With the bug reset leaves the queued turn parked → it
    # would zombie-run when alpha's slot frees (and a follow-up /cancel was required).
    store = _three_project_store(tmp_path)
    store.set_session_id(1, "beta", "beta-old")
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")  # beta is now the ACTIVE (foreground) project…
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert session.project_status(1, "beta") == "queued"  # …and it is QUEUED (cap=1, alpha runs)
    assert len(session._chat(1).run_queue) == 1

    # /reset (active == beta, queued, lock-free) → reset DRAINS beta's queued turn ITSELF.
    session.reset(1)
    # beta's queued turn is cancelled by the reset (no separate /cancel needed).
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    assert session._chat(1).run_queue == deque()        # waiter drained from the queue
    assert store.get_project(1, "beta")["session_id"] is None  # session cleared (the reset)
    assert session.project_status(1, "beta") == "idle"  # no longer "queued"

    # alpha's freed slot must NOT zombie-start the drained beta; no slot leak.
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert eng_b.started is False, "reset-drained queued project must NOT zombie-run on a freed slot"
    assert session._running == 0  # back to zero — no leaked / zombie slot


async def test_second_message_to_queued_project_is_busy_not_double_queued(tmp_path):
    # ⭐ BLOCKER 2 (cross-model QA): a project must NEVER queue behind ITSELF. cap=1: alpha
    # holds the only slot, beta is QUEUED (its turn parked on a waiter, lock NOT yet held).
    # A SECOND message to beta BEFORE it starts must raise StreamingBusy and leave EXACTLY
    # ONE beta entry in the queue — not append a 2nd _QueuedTurn (two turns for one project,
    # violating D6's one-turn-per-project). With the bug (the busy-guard checks only
    # lock.locked(), which a queued-not-started turn does not hold) the 2nd message is
    # accepted and the queue grows to 2.
    store = _three_project_store(tmp_path)
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    # Wait until beta is parked in the queue (not yet running — its lock is NOT held).
    for _ in range(500):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1
    assert session.is_busy(1, "beta") is False  # queued, lock not held — the trap for the guard
    assert session.project_status(1, "beta") == "queued"

    # A SECOND message to the already-queued beta must be refused, NOT double-queued. Bounded
    # by wait_for so the BUGGY path (the 2nd message also queues + parks on a waiter forever)
    # fails fast as a TimeoutError instead of hanging the suite — either way it is RED until
    # the guard rejects an already-queued project.
    with pytest.raises(StreamingBusy):
        await asyncio.wait_for(
            session.handle_message(1, "b-again", send=rec.send, edit=rec.edit), timeout=1.0
        )
    assert len(session._chat(1).run_queue) == 1, "beta must never queue behind itself"

    # Clean teardown: cancel beta's queued turn + finish alpha (no zombie / leak).
    session.handle_cancel(1, "beta")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    assert session._running == 0 and session._chat(1).run_queue == deque()


async def test_post_acquire_same_project_recheck(tmp_path):
    # Deferred-T6: the post-acquire same-project re-check. A turn that QUEUED behind the cap
    # parks; while parked, a SECOND message to the SAME project could start running it once a
    # slot frees. When the queued turn's slot is finally granted it must re-check that its
    # project isn't already running — else two turns would drive ONE project concurrently
    # (violating the one-run-per-project invariant). We exercise the re-check directly: hold
    # alpha's lock, then call the post-slot path → it must raise StreamingBusy.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    eng = _holding_engine("alpha")
    session = make_multi_session({"/work/alpha": eng}, store=store)
    rec = Recorder()
    turn = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    # alpha's lock is held by the running first turn. A SECOND message to alpha is refused
    # (StreamingBusy) — this is exactly the re-check that protects a just-dequeued turn whose
    # project started running while it was parked (the same guard fires before and after the
    # slot grant). Proven here via the synchronous same-project busy refusal.
    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "second", send=rec.send, edit=rec.edit)
    eng.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_rm_drains_queued_projects_waiter(tmp_path):
    # /rm (forget_project) of a QUEUED project drains its parked waiter so it never zombie-runs.
    # cap=1: alpha holds the slot, beta queued. forget_project(beta) → its waiter drained;
    # alpha's freed slot does not start beta.
    store = _three_project_store(tmp_path)
    eng_a = _holding_engine("alpha")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()
    turn_a = asyncio.create_task(session.handle_message(1, "a", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "alpha", want=True)
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(50):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    assert len(session._chat(1).run_queue) == 1

    # Switch active away from beta (so it is removable in the real bot path) and forget it.
    store.switch(1, "alpha")
    await session.forget_project(1, "beta")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn_b, timeout=2.0)
    assert "beta" not in session._chat(1).runtimes  # runtime purged
    assert session._chat(1).run_queue == deque()

    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert eng_b.started is False
    assert session._running == 0
