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
import logging
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
    ThinkingEvent,
    ToolUseEvent,
)
from claude_tg.permissions import PermissionPolicy
from claude_tg.render import RenderAction, encode_callback, encode_switch_callback
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
    stream_message_timeout_seconds=300.0,
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
        stream_message_timeout_seconds=stream_message_timeout_seconds,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
    )


# ---------------------------------------------------------------------------
# A scripted fake Engine. resolve()/cancel() record calls; send() yields the
# scripted events and (optionally) PARKS on a "hold" sentinel until resolve fires.
# ---------------------------------------------------------------------------

HOLD = object()  # sentinel in a script: park send() here until a resolve/cancel arrives


class FakeEngine:
    def __init__(self, script: list, *, session_id="sess-1", resolve_result=True, ctx_pct=None, last_model=None, limit_status=None, last_activity=None):
        self._script = script
        self.session_id = session_id
        # STATUSLINE T-SL-CORE: the ctx % the statusline reads via engine.context_percentage().
        # Default None (→ "ctx —"); a test sets it to assert the figure flows into the line.
        self._ctx_pct = ctx_pct
        # STATUSLINE: the actual model id the SDK reported (engine.last_model()). Default None
        # (→ the statusline falls through to "default" when no model is configured); a test sets
        # it to assert the LIVE model flows into the bar instead of the literal word "default".
        self._last_model = last_model
        # observability T3: the rolling-limit signal the statusline reads via engine.limit_status()
        # — (status, pct_or_None) or None. Default None (→ the 🪙 field is OMITTED); a test sets a
        # value (a tuple, or a callable to simulate a raising read for the RB1 probe).
        self._limit_status = limit_status
        # observability T5: the activity snapshot the activity line reads via engine.last_activity()
        # — an ActivitySnapshot or None. Default None (→ the activity line shows nothing); a test
        # sets a value (or a callable, e.g. a lambda over a mutable box to make the snapshot CHANGE
        # across events, or a raising lambda to exercise the activity line's best-effort RB1 guard).
        self._last_activity = last_activity
        self.resolve_calls: list[tuple[str, object]] = []
        self.cancel_calls: list = []
        # P14 T-FIRE: records the ``proactive`` flag passed to each send() (the force-gate
        # signal threaded by _drive_turn) so a fire test can assert it was set.
        self.proactive_calls: list[bool] = []
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

    async def send(self, prompt: str, *, timeout=None, proactive=False, **_kwargs):
        # P14 T-FIRE: ``proactive`` is recorded so a fire test can assert the force-gate flag
        # was threaded into engine.send; ignored otherwise (the FakeEngine doesn't gate). The
        # ``**_kwargs`` absorbs ``images`` (P10) so the fake stays signature-compatible.
        self.proactive_calls.append(proactive)
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

    async def context_percentage(self):
        # STATUSLINE T-SL-CORE / T-SL-WIRE (B1): the best-effort ctx % the statusline reads
        # (None → "ctx —"). ASYNC to mirror the real Engine.context_percentage(), which awaits
        # the SDK's coroutine get_context_usage() — so the live awaited path is exercised (a
        # non-awaited regression would fail: awaiting a sync int raises).
        return self._ctx_pct

    def last_model(self):
        # STATUSLINE: the actual model id the SDK reported (sync, like the real Engine).
        return self._last_model

    def limit_status(self):
        # observability T3: the rolling-limit signal the statusline reads (sync, like the real
        # Engine.limit_status()). When the configured value is callable it is CALLED — a test can
        # pass a lambda that raises to exercise the statusline's best-effort RB1 guard.
        if callable(self._limit_status):
            return self._limit_status()
        return self._limit_status

    def last_activity(self):
        # observability T5: the activity snapshot the activity line reads (sync, like the real
        # Engine.last_activity()). A callable is CALLED — a test can pass a lambda over a mutable
        # box so the snapshot CHANGES across events, or a lambda that raises for the RB1 probe.
        if callable(self._last_activity):
            return self._last_activity()
        return self._last_activity


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

    async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
        # T6/P9: notification sends pass link_preview_options=LinkPreviewOptions(is_disabled=
        # True) to suppress link previews; capture it (via **kwargs) so the no-preview tests
        # can assert it, while ordinary sends (no such kwarg) still record None.
        self.sends.append({
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
            "link_preview_options": kwargs.get("link_preview_options"),
        })
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


async def test_error_event_renders_clean_message(caplog):
    # P6/R3 (SB3/H1): a tool_error wraps RAW tool output → it renders BODY-FREE to the chat
    # (a clean ⚠️ summary, the raw "it broke" absent), while the raw detail still reaches the
    # LOCAL debug log so the operator can debug. This replaces the old assertion that the raw
    # body appeared in the chat — the new body-free behavior is the SB3 fix.
    caplog.set_level(logging.DEBUG)
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
    # A clean ⚠️ error block was sent, but it is BODY-FREE: the raw tool body never rides it.
    err_send = next(s for s in rec.sends if s["text"].startswith("⚠️") and "tool_error" in s["text"])
    assert "it broke" not in err_send["text"]
    assert all("it broke" not in s["text"] for s in rec.sends)  # nowhere in the chat
    # The raw detail DID reach the local debug log (so it is recoverable for debugging).
    assert "it broke" in caplog.text


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


# ---------------------------------------------------------------------------
# P6/R5: duplicate-render dedup. Production builds the substrate with
# include_partial_messages=False, so a normal answer turn yields the final prose
# TWICE — once as an assembled TextEvent (op="new"), and again as the terminal
# ResultEvent.result_text (op="new"). Nothing deduped result_text against the
# assembled text, so the answer was sent twice (#1). Sibling cases: a transient
# status-edit FAILURE left the old status line orphaned (#2); a failing tool's
# tool_error ErrorEvent + the terminal turn_error ErrorEvent rendered the SAME
# error twice (#3). These tests assert each duplicate is now sent exactly once.
# ---------------------------------------------------------------------------


def _send_texts(rec) -> list[str]:
    """The plain text of every NEW message the driver sent (status edits excluded)."""
    return [s["text"] for s in rec.sends]


async def test_result_text_duplicating_assistant_prose_is_sent_once():
    # #1 (PRIMARY): the assembled answer arrives as a TextEvent(incremental=False) AND the
    # terminal ResultEvent.result_text carries the SAME string. The prose body must be sent
    # exactly ONCE (the assistant TextEvent), and the terminal frame collapses to the compact
    # ✅ done footer — never a verbatim re-send of the identical prose.
    answer = "Here is the **final** answer with detail."
    engine = FakeEngine(
        [
            TextEvent(text=answer, incremental=False),
            ResultEvent(
                session_id="s", is_error=False, subtype="success", result_text=answer
            ),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The distinctive prose substring appears in exactly ONE sent message (the assistant
    # TextEvent render); the ResultEvent did NOT re-send it.
    bodies_with_answer = [t for t in _send_texts(rec) if "final" in t and "detail" in t]
    assert len(bodies_with_answer) == 1, (
        f"the answer prose must be sent exactly once, got {len(bodies_with_answer)}: "
        f"{bodies_with_answer!r}"
    )
    # The "done" indicator still appears (the terminal frame rendered the compact footer).
    assert any(t.startswith("✅ done") for t in _send_texts(rec)), (
        "the compact done footer must still appear after the deduped result"
    )


async def test_distinct_result_text_still_renders_both_messages():
    # #1 guard (no over-suppression): when the terminal ResultEvent.result_text DIFFERS from
    # the assistant prose, BOTH bodies must still render — the dedup is exact-match only.
    engine = FakeEngine(
        [
            TextEvent(text="Intermediate progress note.", incremental=False),
            ResultEvent(
                session_id="s",
                is_error=False,
                subtype="success",
                result_text="The genuinely different final summary.",
            ),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    texts = _send_texts(rec)
    assert any("Intermediate progress note." in t for t in texts), "assistant prose dropped"
    assert any("genuinely different final summary" in t for t in texts), (
        "a distinct result_text must NOT be suppressed"
    )


async def test_multi_message_turn_with_distinct_prose_all_render():
    # #1 guard: two DISTINCT assistant messages mid-turn, then a result whose text equals the
    # SECOND. The first message and the second message both show (distinct), and the result
    # does not duplicate the second — exactly one copy of each distinct body.
    first = "First step done."
    second = "Second step done — this is the final answer."
    engine = FakeEngine(
        [
            TextEvent(text=first, incremental=False),
            TextEvent(text=second, incremental=False),
            ResultEvent(
                session_id="s", is_error=False, subtype="success", result_text=second
            ),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    texts = _send_texts(rec)
    assert sum(1 for t in texts if "First step done." in t) == 1
    assert sum(1 for t in texts if "Second step done" in t) == 1, (
        "the second prose must appear once — not duplicated by the identical result_text"
    )


# ---- P12 T-THINK-4: dedup HOLDS with partial messages on (final answer renders ONCE) ----


async def test_thinking_on_final_answer_renders_exactly_once_with_partials():
    # P12 T-THINK-4 (the headline + mutation-probe): with include_partial_messages ON, a turn
    # now interleaves thinking_delta + incremental text_delta (TextEvent incremental=True) WITH
    # the assembled answer (TextEvent incremental=False) and the terminal ResultEvent whose
    # result_text repeats it. The dedup must STILL render the final answer EXACTLY ONCE:
    #   * thinking + incremental text are status-line-only (op="edit_status"; transient,
    #     cleared at turn end) — they never become a permanent message and never feed the dedup;
    #   * the assembled TextEvent is the ONE verbatim answer; the terminal ResultEvent collapses
    #     to the ✅ done footer (the _TurnDedup #1 path), not a second copy.
    # MUTATION-PROBE: if the dedup were broken/removed, the answer prose would appear TWICE
    # (assembled TextEvent + ResultEvent.result_text) and the first assertion would fail.
    answer = "The **final** answer with distinctive detail."
    engine = FakeEngine(
        [
            ThinkingEvent(text="First I should consider the constraints", incremental=True),
            ThinkingEvent(text="…then weigh the trade-offs carefully", incremental=True),
            TextEvent(text="The ", incremental=True),  # streamed answer fragment (partials ON)
            TextEvent(text="final answer", incremental=True),  # another fragment
            TextEvent(text=answer, incremental=False),  # the assembled, verbatim answer
            ResultEvent(
                session_id="s", is_error=False, subtype="success", result_text=answer
            ),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
        timeout=2.0,
    )
    # The distinctive answer prose is sent EXACTLY ONCE as a verbatim message — NOT duplicated
    # by the identical result_text (the dedup holds with partials on).
    bodies = [t for t in _send_texts(rec) if "final" in t and "distinctive detail" in t]
    assert len(bodies) == 1, (
        f"the final answer must render exactly once with partials on, got {len(bodies)}: {bodies!r}"
    )
    # The compact done footer still appears (the terminal frame collapsed to it).
    assert any(t.startswith("✅ done") for t in _send_texts(rec)), (
        "the done footer must still appear after the deduped result"
    )


async def test_thinking_stays_in_transient_status_line_never_a_permanent_message():
    # T-THINK-4: thinking (and the incremental text) must NOT bleed into a permanent message —
    # the reasoning rides the status line (op="edit_status") and the status message is DELETED
    # at turn end, so no 🧠 reasoning is left behind as a verbatim send. The final answer (an
    # assembled TextEvent) is the only verbatim prose message.
    engine = FakeEngine(
        [
            ThinkingEvent(text="REASONING_TOKEN_should_be_transient", incremental=True),
            TextEvent(text="ANSWER_TOKEN final answer.", incremental=False),
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_session(engine)  # frozen clock => every status update is "due"
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
        timeout=2.0,
    )
    # The reasoning appears only on the STATUS line (a send that creates it and/or an edit),
    # and that status message is DELETED at turn end — so it never persists as the answer.
    assert rec.deletes, "the transient status line (carrying the 🧠 reasoning) must be deleted at turn end"
    # The final answer IS a permanent verbatim message; the reasoning token is NOT mixed into it.
    answer_msgs = [t for t in _send_texts(rec) if "ANSWER_TOKEN" in t]
    assert len(answer_msgs) == 1
    assert "REASONING_TOKEN_should_be_transient" not in answer_msgs[0], (
        "thinking must not bleed into the final answer message"
    )


async def test_transient_status_edit_failure_does_not_orphan_old_status_line():
    # #2: a status line is created, then a status EDIT fails (message gone / too old). The
    # edit-failure fallback sends a BRAND-NEW status message — but turn-end cleanup deletes
    # only the LATEST status_message_id, so before the fix the FIRST status line was orphaned
    # and left visible. After the fix the edit-failure path best-effort DELETEs the old
    # status id before sending the replacement, so the orphan id is cleaned up.
    engine = FakeEngine(
        [
            ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)"),  # creates status #1
            TextEvent(text="now editing", incremental=True),  # EDIT of #1 -> made to fail
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok"),
        ]
    )
    session = make_session(engine)  # frozen clock => every status update is "due"
    rec = Recorder()

    # The FIRST status edit fails (forces the fresh-status-message fallback).
    fail_state = {"first": True}

    async def flaky_edit(*, message_id, text, parse_mode=None):
        if fail_state["first"]:
            fail_state["first"] = False
            raise RuntimeError("Telegram BadRequest: message to edit not found")
        rec.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})

    # The Recorder hands out ids 101, 102, … in send order. The first status line is the
    # first send -> id 101; the edit-failure fallback then sends a replacement -> id 102.
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=flaky_edit, delete=rec.delete),
        timeout=2.0,
    )
    deleted_ids = {d["message_id"] for d in rec.deletes}
    # The ORPHANED first status line (id 101) must have been deleted — it was abandoned when
    # the edit failed and a replacement was sent. Before the fix only the final id is deleted.
    assert 101 in deleted_ids, (
        f"the orphaned pre-failure status line (id 101) must be deleted, deletes={rec.deletes!r}"
    )
    # And the turn still ends cleanly with no lingering status id on the runtime.
    assert active_rt(session).status_message_id is None


async def test_tool_error_then_terminal_turn_error_renders_error_once():
    # #3 (R5 dedup, preserved under R3 body-free): a failing tool renders a tool_error
    # ErrorEvent, and the terminal ResultMessage(is_error) surfaces a near-identical
    # turn_error ErrorEvent carrying the SAME raw message. R5's _TurnDedup compares the RAW
    # .message (kept intact for exactly this reason) and suppresses the duplicate terminal
    # turn_error → exactly ONE error block renders. Both kinds now render BODY-FREE, so we
    # count the ⚠️ error blocks (the raw msg is absent from the chat — SB3).
    msg = "Command failed: exit code 2"
    engine = FakeEngine(
        [
            ErrorEvent(kind_of_error="tool_error", message=msg, is_error=True),
            ErrorEvent(kind_of_error="turn_error", message=msg, is_error=True),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert all(msg not in t for t in _send_texts(rec))  # body-free: raw body never sent
    error_blocks = [t for t in _send_texts(rec) if t.startswith("⚠️")]
    assert len(error_blocks) == 1, (
        f"the error must render exactly once (R5 dedup), got {len(error_blocks)}: {error_blocks!r}"
    )


async def test_distinct_terminal_error_still_renders():
    # #3 guard (no over-suppression), preserved under R3 body-free: a tool_error then a
    # terminal turn_error with a DIFFERENT raw message → R5 does NOT suppress (the raw
    # .messages differ), so BOTH error blocks render. Both render body-free now, so we
    # distinguish them by KIND (the raw bodies are absent from the chat — SB3).
    engine = FakeEngine(
        [
            ErrorEvent(kind_of_error="tool_error", message="tool blew up", is_error=True),
            ErrorEvent(kind_of_error="turn_error", message="turn aborted for another reason", is_error=True),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    texts = _send_texts(rec)
    # Raw bodies never reach the chat (body-free); the two DISTINCT errors both still render,
    # told apart by kind — proving the dedup did not over-suppress the distinct terminal one.
    assert all("tool blew up" not in t and "turn aborted" not in t for t in texts)
    assert any(t.startswith("⚠️") and "tool_error" in t for t in texts)
    assert any(t.startswith("⚠️") and "turn_error" in t for t in texts), (
        "a distinct terminal error must NOT be suppressed"
    )


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
# P6 H2/RB2: a driver_error on an ALREADY-VERIFIED session must tear down +
# rebuild that project's engine, so the NEXT turn starts a fresh client (no
# wedge-until-restart). The P5/QF3 recovery only covered the resume-failure
# case (first turn of a freshly-resumed session); a verified session that
# later driver_errors (the long-approval-timeout finding, or any transport
# failure) used to leave the engine in place → every later turn re-times-out.
#
# Mock-only: a factory that hands out a SEQUENCE of engines for one project so
# the test can assert the 2nd turn built a FRESH engine and the 1st was stopped.
# No real sleeps; the driver_error is a scripted event.
# ===========================================================================


def make_sequence_session(engines: list, *, store=None, config=None) -> StreamingSession:
    """A session whose factory pops the NEXT engine from ``engines`` on each build.

    Models per-project rebuild: a fresh ``_ensure_engine`` for the same project gets a
    new engine instance, so a test can prove a torn-down engine was replaced rather than
    reused. (``make_multi_session`` reuses one engine per cwd — the opposite contract.)
    """
    seq = list(engines)

    def factory(*, cwd, backstop_seconds, permission_policy):
        assert seq, "factory asked to build more engines than the test scripted"
        return seq.pop(0)

    return StreamingSession(
        config or make_config(),
        session_store=store,
        engine_factory=factory,
        clock=lambda: 0.0,
    )


async def test_verified_session_driver_error_rebuilds_engine_next_turn_succeeds():
    """RED on current code: a verified-session driver_error leaves the engine in place,
    so the next turn reuses the SAME (dead) engine. GREEN: the engine is torn down +
    rebuilt, so turn 2 runs on a FRESH engine and succeeds — no wedge."""
    # Turn 1: a fresh-started session that emits a driver_error mid-turn (transport/
    # liveness failure on an already-verified session — NOT a resume failure).
    eng1 = FakeEngine(
        [ErrorEvent(kind_of_error="driver_error", message="send timed out after 120s", is_error=True)]
    )
    # Turn 2: a DISTINCT engine that completes cleanly — proves the rebuild happened.
    eng2 = FakeEngine(
        [ResultEvent(session_id="s2", is_error=False, subtype="success", result_text="recovered")]
    )
    session = make_sequence_session([eng1, eng2])
    rec = Recorder()

    # Turn 1: surfaces the driver_error (rendered), then the engine must be torn down.
    await asyncio.wait_for(
        session.handle_message(1, "first", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng1.started is True
    assert eng1.stopped is True, "the dead verified-session engine must be stop()ed"
    # The runtime's engine reference was dropped so the next turn rebuilds fresh.
    rt = active_rt(session, 1)
    assert rt.engine is not eng1, "the dead engine must not be reused on the next turn"

    # No slot/lock leak after the failed turn (the chat must be usable).
    assert session.is_busy(1) is False
    assert session._running == 0

    # Turn 2: a fresh engine is built + started and the turn completes — NOT wedged.
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng2.started is True, "the next turn must run on a freshly-built engine"
    assert any("recovered" in s["text"] for s in rec.sends)
    assert session.is_busy(1) is False
    assert session._running == 0


async def test_clean_turn_does_not_rebuild_engine():
    """Over-reach guard: a turn that completes cleanly (no driver_error) must REUSE its
    engine on the next turn — the rebuild path fires ONLY on a driver_error, never on a
    healthy turn (else every turn would pay a fresh start)."""
    eng1 = FakeEngine(
        [ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="ok")]
    )
    # If the impl wrongly rebuilds after a clean turn, the factory hands out eng2 and the
    # reuse assertion below fails (eng2 started / eng1 stopped).
    eng2 = FakeEngine(
        [ResultEvent(session_id="s2", is_error=False, subtype="success", result_text="second")]
    )
    session = make_sequence_session([eng1, eng2])
    rec = Recorder()

    await asyncio.wait_for(
        session.handle_message(1, "first", send=rec.send, edit=rec.edit), timeout=2.0
    )
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The SAME engine ran both turns (idempotent _ensure_engine); never torn down/rebuilt.
    assert eng1.stopped is False
    assert eng2.started is False, "a clean turn must not trigger a rebuild"
    assert active_rt(session, 1).engine is eng1


async def test_tool_error_does_not_rebuild_engine():
    """Over-reach guard: an ordinary tool_error / turn_error (Claude reporting a failed
    tool) is NOT a driver_error and must NOT tear down the engine — only a transport/
    liveness driver_error wedges a session, so only it triggers the rebuild."""
    eng1 = FakeEngine(
        [
            ErrorEvent(kind_of_error="tool_error", message="Bash: command not found", is_error=True),
            ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="ok"),
        ]
    )
    eng2 = FakeEngine(
        [ResultEvent(session_id="s2", is_error=False, subtype="success", result_text="second")]
    )
    session = make_sequence_session([eng1, eng2])
    rec = Recorder()

    await asyncio.wait_for(
        session.handle_message(1, "first", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng1.stopped is False, "a tool_error must not tear down the engine"
    assert active_rt(session, 1).engine is eng1
    # The next turn still reuses eng1 (no rebuild).
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert eng2.started is False


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
    # R6 (auto-linkify): the cwd is wrapped in <code>…</code> and the refusal is sent with
    # parse_mode="HTML" so Telegram renders the path as inert monospace, not a row of
    # tappable fake "/segment" command-links. The path appears ONLY inside the wrapper.
    assert f"<code>{outside}</code>" in refusal
    assert rec.sends[0]["parse_mode"] == "HTML"
    assert str(outside) not in refusal.replace(f"<code>{outside}</code>", "")
    # R6 (HTML validity): the "<name> <path>" placeholders MUST be escaped — this is an HTML
    # message, so a bare "<name>" would be parsed as a broken tag and Telegram would reject
    # the whole send. Assert they are written as &lt;…&gt; (and no bare "<name>" leaks).
    assert "&lt;name&gt;" in refusal and "&lt;path&gt;" in refusal
    assert "<name>" not in refusal and "<path>" not in refusal
    # Belt-and-suspenders: stripping the only real tags (<code>…</code>) must leave NO stray
    # "<"/">" — proof the message carries no other unescaped angle bracket Telegram'd reject.
    bare = refusal.replace(f"<code>{outside}</code>", "")
    assert "<" not in bare and ">" not in bare
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


def make_sequential_session(
    engines_by_cwd: dict, *, store, config=None, discover=None, probe_one=None
) -> StreamingSession:
    """A session whose factory hands out the NEXT engine for a cwd on each BUILD.

    ``engines_by_cwd`` maps a cwd → a LIST of engines; successive builds for that cwd
    pop the next one. Used by the QF3 recovery tests where the first engine resumes
    (and fails) and the engine is then DROPPED, so the next turn must BUILD a SECOND,
    fresh engine — letting us assert the dead id is never re-resumed.

    ``discover`` / ``probe_one`` are optional injected P11-attach seams (the discovery
    lookup + the first-write liveness re-probe) so the adopt-recovery tests below can drive a
    deterministic verdict; omitted → the StreamingSession defaults (the real ones).
    """
    queues = {cwd: list(engines) for cwd, engines in engines_by_cwd.items()}

    def factory(*, cwd, backstop_seconds, permission_policy):
        return queues[cwd].pop(0)

    kwargs = {}
    if discover is not None:
        kwargs["discover"] = discover
    if probe_one is not None:
        kwargs["probe_one"] = probe_one
    return StreamingSession(
        config or make_config(),
        session_store=store,
        engine_factory=factory,
        clock=lambda: 0.0,
        **kwargs,
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


# ===========================================================================
# P11 T2 hardening (post-SHIP) — (1) recovery clears the persisted fork_pending; (2) the
# fork_pending clear-at-CLEAN-TURN boundary (NOT at resume-connect) is data-corruption-critical
# and durably pinned. Both use a REAL JsonSessionStore (so fork_pending is observable on disk).
# ===========================================================================


class ForkResumeOkButFirstTurnFailsEngine:
    """A fork-aware engine: resume(fork=…) CONNECTS, the first send yields a resume-failure
    event → _recover_failed_resume fires. Records (id, fork) per resume."""

    def __init__(self, script, *, session_id):
        self._script = script
        self.session_id = session_id
        self.started = False
        self.stopped = False
        self.resume_calls: list[tuple[str, bool]] = []

    async def start(self):
        self.started = True

    async def resume(self, session_id, *, fork=False):
        self.resume_calls.append((session_id, fork))
        self.started = True
        self.session_id = session_id

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        for ev in self._script:
            yield ev

    def resolve(self, tool_use_id, decision):
        return True

    def cancel(self, tool_use_id=None):
        return 1


class ForkResumeThenRaiseEngine:
    """A fork-aware engine: resume(fork=…) CONNECTS, then the first send RAISES mid-stream so
    the turn never reaches the clean-turn finalize (fork_pending stays set). Records resumes."""

    def __init__(self, *, session_id=None):
        self.session_id = session_id
        self.started = False
        self.stopped = False
        self.resume_calls: list[tuple[str, bool]] = []

    async def start(self):
        self.started = True

    async def resume(self, session_id, *, fork=False):
        self.resume_calls.append((session_id, fork))
        self.started = True
        # A fork lands on a fresh id, but no CLEAN turn persists it (send raises below), so the
        # store keeps the BASE id — the exact "connected but no clean turn" state under test.
        self.session_id = "forked-but-uncommitted" if fork else session_id

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        raise RuntimeError("transport blip mid-turn — no clean turn completes")
        yield  # unreachable; makes this an async generator

    def resolve(self, tool_use_id, decision):
        return True

    def cancel(self, tool_use_id=None):
        return 1


async def test_recovery_clears_persisted_fork_pending(tmp_path):
    """⭐ Hardening item 1: when a resumed turn fails and _recover_failed_resume fires, it
    clears NOT ONLY the dead session_id but ALSO the persisted ``fork_pending`` marker. A
    leftover marker would, after a restart, needlessly re-probe/fork the bot's OWN fresh
    session (self-healing, not a co-drive — but untidy). We adopt a session (fork_pending=True),
    re-probe IDLE (so it continues), the resume connects but the first turn fails → recovery
    clears both."""
    from claude_tg.session_store import JsonSessionStore
    from claude_tg.sessions_discovery import DiscoveredSession as _D

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "api"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")

    # Engine 1: resume connects (continue — re-probe is idle), first turn yields a resume
    # failure. Engine 2: the fresh engine the recovered next turn builds.
    eng1 = ForkResumeOkButFirstTurnFailsEngine(
        [
            ErrorEvent(kind_of_error="turn_error", message="No conversation found with session id base-1"),
            ResultEvent(session_id="base-1", is_error=True, subtype="error_during_execution"),
        ],
        session_id="base-1",
    )
    eng2 = FakeEngine(
        [ResultEvent(session_id="fresh-1", is_error=False, subtype="success", result_text="ok")],
        session_id="fresh-1",
    )
    session = make_sequential_session(
        {str(proj): [eng1, eng2]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
        discover=lambda: [_D(session_id="base-1", cwd=str(proj), title="t", last_active=0, running=False)],
        probe_one=_probe_returning(False, False),  # re-probe IDLE → continue the base id
    )
    # Adopt: pins base-1 + fork_pending=True.
    name = session.attach_session(1, "base-1").project_name
    assert store.get_fork_pending(1, name) is True

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    # The re-probe said idle → the resume CONTINUED the base id (no fork).
    assert eng1.resume_calls == [("base-1", False)]
    # Recovery fired: the dead id is cleared AND — the item-1 fix — fork_pending is cleared too.
    assert store.get_project(1, name)["session_id"] is None
    assert store.get_fork_pending(1, name) is False, \
        "recovery must clear the persisted fork_pending (item 1)"
    assert any("Couldn't resume" in s["text"] for s in rec.sends)


async def test_fork_pending_cleared_only_after_clean_turn_not_at_connect(tmp_path):
    """⭐ Hardening item 2 — the DATA-CORRUPTION-CRITICAL timing boundary. ``fork_pending`` is
    cleared ONLY after a CLEAN turn, NOT at resume-connect. We attach a LIVE-elsewhere session
    → the first resume re-probes → FORKS and CONNECTS, but the turn RAISES before a clean turn
    completes (a restart-equivalent gap). Because no clean turn ran:
      * ``fork_pending`` is STILL True (the binding decision is not yet committed), and
      * the store STILL holds the BASE id (no forked id was persisted).
    So a RESTART here (a fresh StreamingSession over the same store) must RE-PROBE and FORK
    AGAIN — never ``resume(fork=False)`` on the still-live base id (which would co-drive).

    This pins the boundary against a "simplify: clear fork_pending at connect" regression —
    which broke ZERO existing tests yet is a real co-drive bug. The mutation-probe below moves
    the clear to connect and asserts THIS test then fails."""
    from claude_tg.session_store import JsonSessionStore
    from claude_tg.sessions_discovery import DiscoveredSession as _D

    root = tmp_path / "root"
    root.mkdir()
    proj = root / "live"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")

    disc = [_D(session_id="live-base", cwd=str(proj), title="t", last_active=0, running=True)]

    # --- Process A: attach + a first turn that forks+connects but RAISES before a clean turn.
    eng_a = ForkResumeThenRaiseEngine(session_id=None)
    session_a = make_sequential_session(
        {str(proj): [eng_a]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
        discover=lambda: list(disc),
        probe_one=_probe_returning(True, False),  # base id is LIVE → fork at first write
    )
    name = session_a.attach_session(1, "live-base").project_name
    assert store.get_fork_pending(1, name) is True

    # The first turn: resume FORKS + connects, then send() raises → the clean-turn finalize is
    # NEVER reached, so fork_pending is NOT cleared. The raise propagates out of handle_message.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session_a.handle_message(1, "go", send=Recorder().send, edit=Recorder().edit), timeout=2.0)
    # The resume FORKED off the live base id (never co-drove it).
    assert eng_a.resume_calls == [("live-base", True)]
    # ⭐ The boundary: fork_pending is STILL True (connected, but no clean turn committed it),
    # and the persisted id is STILL the base id (no forked id landed — no clean result).
    assert store.get_fork_pending(1, name) is True, \
        "fork_pending must persist until a CLEAN turn completes (NOT at connect)"
    assert store.get_project(1, name)["session_id"] == "live-base"

    # --- Process B (RESTART): a fresh StreamingSession over the SAME store. The base id is
    # still live; the persisted fork_pending re-triggers a fresh re-probe → it must FORK AGAIN.
    eng_b = ForkResumeThenRaiseEngine(session_id=None)
    session_b = make_sequential_session(
        {str(proj): [eng_b]},
        store=store,
        config=make_roots_config(tmp_path, root=root),
        discover=lambda: list(disc),
        probe_one=_probe_returning(True, False),  # still LIVE at the restart re-probe
    )
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session_b.handle_message(1, "go2", send=Recorder().send, edit=Recorder().edit), timeout=2.0)
    # ⭐ The post-restart resume RE-PROBED and FORKED — never resume(fork=False) on the live base.
    assert eng_b.resume_calls == [("live-base", True)], \
        "after a restart-before-clean-turn, the re-probe must FORK again (never co-drive)"


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


# ---------------------------------------------------------------------------
# T2 (P8) — path-like values in the tool-status line + permission body render as
# <code> on the LIVE send path (parse_mode="HTML"); hostile input never breaks the
# send. The pure-render proofs live in test_render.py; these assert the transport.
# ---------------------------------------------------------------------------


async def test_tool_use_status_line_is_sent_html_with_code_path(tmp_path):
    # T2/R6: the "▶️ Tool(file_path=…)" status line is sent with parse_mode="HTML" and the
    # path inside <code>…</code> so Telegram renders it as inert monospace, NOT tappable
    # "/segment" fake-links. (Frozen clock → the leading-edge status fires immediately.)
    engine = FakeEngine(
        [
            ToolUseEvent(
                tool_name="Write",
                tool_input_summary="Write(file_path=/tmp/p5verify/a, content=<7 chars>)",
            ),
            ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok"),
        ]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    status = next(s for s in rec.sends if "▶️" in s["text"])
    assert status["parse_mode"] == "HTML"  # sent as HTML (or the <code> tags would show)
    assert "<code>" in status["text"] and "</code>" in status["text"]
    assert "/tmp/p5verify/a" in status["text"]
    # The path appears ONLY inside the code span (the bare form is what linkifies).
    inner = status["text"][
        status["text"].index("<code>") + len("<code>") : status["text"].index("</code>")
    ]
    outside = status["text"].replace(f"<code>{inner}</code>", "")
    assert "/tmp/p5verify/a" not in outside
    # The "<7 chars>" body marker is escaped → the HTML message is valid (it would otherwise
    # be a broken tag and Telegram would reject the whole status send).
    assert "&lt;7 chars&gt;" in status["text"]


async def test_foreground_permission_body_is_sent_html_code_path_buttons_intact(tmp_path):
    # T2/R6: the "🔐 Permission needed …" prompt (the operator's approve/deny surface) is
    # sent with parse_mode="HTML", its summary inside <code> (monospace path), and the three
    # verdict buttons still attached and routable.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    perm = PermissionEvent(
        tool_name="Bash",
        tool_input_summary="Bash(command=ls /tmp/p5verify/a)",
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
        raise AssertionError("permission prompt was never rendered inline")
    prompt = next(s for s in rec.sends if "🔐 Permission needed" in s["text"])
    assert prompt["parse_mode"] == "HTML"
    assert "<code>" in prompt["text"] and "</code>" in prompt["text"]
    assert "/tmp/p5verify/a" in prompt["text"]
    # The keyboard rode the prompt and the tap still routes to alpha's held request.
    assert prompt["reply_markup"] is not None
    session.resolve_callback(1, encode_callback("m", "a-perm", payload="o"))
    await turn
    assert eng_alpha.resolve_calls == [("a-perm", PermissionDecision(verdict="allow_once"))]


async def test_permission_body_hostile_input_sends_and_falls_back_plain(tmp_path):
    # SECURITY (load-bearing): a misaligned/prompt-injected Claude could put HTML
    # metacharacters in the tool input. The prompt MUST still reach the operator. Two
    # guarantees on the live send path:
    #  1. the HTML body is VALID (escaped) so a normal Recorder sends it fine;
    #  2. EVEN IF Telegram rejected the HTML (fail_html Recorder), the parallel PLAIN body is
    #     resent (parse_mode=None) — the prompt is never dropped (a dropped approve/deny
    #     prompt is a worse bug than plain text).
    hostile = "Bash(command=</code><b>pwn</b> && rm -rf /tmp/p5verify, file_path=/a&b)"
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    perm = PermissionEvent(
        tool_name="Bash", tool_input_summary=hostile, tool_use_id="a-perm", session_id="alpha-sid",
    )
    eng_alpha._script = [perm, HOLD, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success")]
    rt_alpha = session._chat(1).runtimes["alpha"]
    # fail_html → the HTML send raises (as Telegram would on a bad entity), exercising the fallback.
    rec = Recorder(fail_html=True)
    turn = asyncio.create_task(_drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit))
    for _ in range(500):
        if any("🔐 Permission needed" in s["text"] and s["parse_mode"] is None for s in rec.sends):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("permission prompt (plain fallback) never sent")
    # The HTML attempt was made first (escaped — no live injected tag), then a PLAIN resend.
    html_try = next(s for s in rec.sends if "🔐 Permission needed" in s["text"] and s["parse_mode"] == "HTML")
    assert "<b>pwn</b>" not in html_try["text"]  # injected tag is inert (escaped)
    assert "&lt;b&gt;pwn&lt;/b&gt;" in html_try["text"]
    plain = next(s for s in rec.sends if "🔐 Permission needed" in s["text"] and s["parse_mode"] is None)
    # The plain fallback is the RAW summary (no <code> wrapper) — what the bot showed pre-R6.
    assert "<code>" not in plain["text"]
    assert hostile in plain["text"]
    assert plain["reply_markup"] is not None  # the keyboard still rides the fallback
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
    # not just the bell. With the round-1 bug this is 2 (the keyboard loop re-ran). T6/P9: the
    # bell line itself now carries an [Open <name>] switch button, so count only the QUESTION
    # keyboards (exclude the bell) — the original intent.
    keyboarded = [
        s for s in rec.sends
        if s["reply_markup"] is not None and s["text"] != "🔔 alpha — asks a question"
    ]
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
    # T6/P9: the bell carries an [Open <name>] switch button now, so count only QUESTION
    # keyboards (exclude the bell line).
    keyboarded = [
        s for s in rec.sends
        if s["reply_markup"] is not None and s["text"] != "🔔 alpha — asks a question"
    ]
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
    # T6/P9: the bell carries an [Open <name>] switch button now, so count only QUESTION
    # keyboards (exclude the bell line).
    keyboarded = [
        s for s in rec.sends
        if s["reply_markup"] is not None and s["text"] != "🔔 alpha — asks a question"
    ]
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


async def test_cancel_idle_runtime_no_engine_counts_zero():
    # NB (round-3): the window-abort count must NOT fire for an IDLE runtime. A project with a
    # runtime but no in-flight turn (engine never started → engine is None, nothing drained) is
    # genuinely idle: /cancel must return 0 so cmd_cancel says "nothing in flight" (truthfully).
    # This pins the ``rt.inflight`` term of the window-abort predicate — without it, an idle
    # /cancel would wrongly report a cancelled turn (drained==0 and engine is None both hold).
    engine = FakeEngine([])
    session = make_session(engine)
    name, rt = session._active_runtime(1, create_default=True)  # idle runtime: no turn, no engine
    assert name is not None and rt.engine is None and rt.inflight is False
    assert session.handle_cancel(1, name) == 0  # idle → not a window-abort → 0
    assert engine.cancel_calls == []


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


# ===========================================================================
# P5 (T6/T7-review): the OUTER end-of-turn ``inflight=False`` finally must clear the
# in-flight marker on the ERROR / REFUSAL exit paths too — not just the happy /
# drain-cancel paths (which already have committed regressions above:
# ``test_inflight_marker_cleared_after_turn_so_next_message_accepted``, the distinct-
# project guard, and ``test_cancel_queued_project_drains_waiter_no_zombie_run``).
#
# ``inflight`` is the busy-guard's SINGLE source of "a turn is in flight" — set the
# instant a turn is accepted (right after the busy-guard passes, BEFORE ``_acquire_slot``)
# and cleared ONLY in the outer finally in ``handle_message``. If ANY exit path fails to
# clear it, that project is WEDGED forever (every future message → StreamingBusy with no
# turn actually running). Each test below drives one error/refusal exit, then asserts BOTH
# ``rt.inflight is False`` AND that a SUBSEQUENT same-project message is ACCEPTED (the real
# proof of no-wedge — a leaked marker would re-raise StreamingBusy on that second message).
# All three paths set ``inflight=True`` BEFORE the failure (the mark is at the top of the
# try; the failures happen inside ``_acquire_slot``'s try / inside the lock), so the full
# no-wedge invariant is asserted for each. Deterministic — no real sleeps; every wait is a
# bounded loop or ``asyncio.wait_for``. (Previously these paths were verified only by a
# throwaway probe; this commits them.)
# ===========================================================================


async def test_mid_stream_engine_raise_clears_inflight_project_usable_again(tmp_path):
    # PATH 1 — mid-stream engine raise (RB2). A turn whose engine raises PARTWAY through
    # ``send()`` (after ``inflight=True``, inside ``_drive_turn``) must clear ``inflight``
    # in the outer finally and leave the project USABLE — not wedged busy forever. The
    # ``test_slot_leak_safety_*`` test above pins the SLOT side of this raise; this pins
    # the INFLIGHT side + the no-wedge (a second message runs to completion).
    store = _three_project_store(tmp_path)

    class BoomMidStreamEngine(FakeEngine):
        # Raises mid-stream on the FIRST turn only; subsequent turns fall back to the
        # scripted path (set on ``_script``) so the no-wedge recovery turn can complete.
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.boom = True

        async def send(self, prompt, *, timeout=None):
            if self.boom:
                self.boom = False
                yield ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)")
                raise RuntimeError("engine exploded mid-stream")
            async for event in super().send(prompt, timeout=timeout):
                yield event

    eng_a = BoomMidStreamEngine([], session_id="alpha-sess")
    session = make_multi_session({"/work/alpha": eng_a}, store=store, config=make_config())
    rec = Recorder()

    # Turn 1 raises mid-stream; the RuntimeError must surface (clean-fail, RB2).
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            session.handle_message(1, "boom", send=rec.send, edit=rec.edit, delete=rec.delete),
            timeout=2.0,
        )
    rt = session._chat(1).runtimes["alpha"]
    assert rt.inflight is False  # the outer finally cleared it despite the raise
    assert session._running == 0  # slot freed too (no leak)
    assert session.is_busy(1, "alpha") is False  # lock released

    # NO-WEDGE PROOF: a SECOND message to the same project is ACCEPTED and runs to
    # completion (a leaked marker would re-raise StreamingBusy here instead).
    eng_a._script = [
        ResultEvent(session_id="alpha-sess", is_error=False, subtype="success", result_text="recovered")
    ]
    await asyncio.wait_for(
        session.handle_message(1, "again", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("recovered" in s["text"] for s in rec.sends)
    assert rt.inflight is False


async def test_post_wait_lock_recheck_streaming_busy_clears_inflight_on_dequeue(tmp_path):
    # PATH 2 — the post-wait ``if target_rt.lock.locked(): raise StreamingBusy`` re-check
    # AFTER a queued wait / slot grant. That raise happens INSIDE the outer try (after
    # ``inflight=True`` and after ``_acquire_slot`` returned), so its OWN raise must not
    # leave the marker set. ``test_post_acquire_same_project_recheck`` exercises the same
    # synchronous re-check but does NOT assert the inflight invariant — this drives the REAL
    # post-wait re-raise on a DEQUEUED turn and pins that its marker is cleared. cap=1.
    #
    # Sequence that forces the inner re-raise on a dequeued turn:
    #   * gamma takes the only slot and holds it (parked at HOLD).
    #   * beta message #1 QUEUES behind the cap (parks on a waiter; inflight=True; lock NOT held).
    #   * we manually ACQUIRE beta's lock (simulating "a 2nd beta message started running it
    #     while #1 was parked") — now beta.lock.locked() is True.
    #   * gamma finishes → its freed slot is TRANSFERRED to beta #1, which wakes, returns from
    #     ``_acquire_slot``, hits the post-wait ``lock.locked()`` re-check → raises StreamingBusy.
    #   * that StreamingBusy propagates out of beta #1's task; the OUTER finally must clear
    #     beta.inflight (the slot it momentarily held is released by the inner finally first).
    store = _three_project_store(tmp_path)
    eng_g = _holding_engine("gamma")
    eng_b = _holding_engine("beta")
    session = make_multi_session(
        {"/work/gamma": eng_g, "/work/beta": eng_b},
        store=store,
        config=make_config(max_concurrent_runs=1),
    )
    rec = Recorder()

    # gamma takes the only slot.
    store.switch(1, "gamma")
    turn_g = asyncio.create_task(session.handle_message(1, "g", send=rec.send, edit=rec.edit))
    await _wait_busy(session, 1, "gamma", want=True)

    # beta #1 queues behind the cap (parks on a waiter; not yet running).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "b", send=rec.send, edit=rec.edit))
    for _ in range(500):
        if session._chat(1).run_queue:
            break
        await asyncio.sleep(0)
    beta_rt = session._chat(1).runtimes["beta"]
    assert len(session._chat(1).run_queue) == 1
    assert beta_rt.inflight is True  # queued turn is in-flight
    assert beta_rt.lock.locked() is False  # but not yet holding its lock

    # Simulate "a concurrent beta turn started running while #1 was parked": grab beta's lock
    # so the dequeued #1 must hit the post-wait ``lock.locked()`` re-check and re-raise.
    await beta_rt.lock.acquire()
    try:
        # gamma finishes → transfers its freed slot to the parked beta #1, which wakes,
        # finds its lock held, and raises StreamingBusy out of the post-wait re-check.
        eng_g.cancel()
        await asyncio.wait_for(turn_g, timeout=2.0)
        with pytest.raises(StreamingBusy):
            await asyncio.wait_for(turn_b, timeout=2.0)
        # ⭐ THE INVARIANT: beta #1's OWN post-wait StreamingBusy raise cleared its marker via
        # the outer finally — the project is NOT wedged in-flight forever.
        assert beta_rt.inflight is False
    finally:
        beta_rt.lock.release()
    # The slot beta #1 momentarily held was released by the inner finally (no leak).
    await _wait_running(session, 0)

    # NO-WEDGE PROOF: a fresh beta message is ACCEPTED and runs to completion.
    eng_b._script = [
        ResultEvent(session_id="beta-sess", is_error=False, subtype="success", result_text="beta-recovered")
    ]
    eng_b._gate = asyncio.Event()  # fresh gate (the prior cancel had set it)
    await asyncio.wait_for(
        session.handle_message(1, "b-again", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("beta-recovered" in s["text"] for s in rec.sends)
    assert beta_rt.inflight is False


async def test_sb2_path_not_allowed_refusal_clears_inflight_project_usable_again(tmp_path):
    # PATH 3 — SB2 cwd-refusal. ``_ensure_engine`` raises ``PathNotAllowed`` (the project's
    # stored cwd drifted OUT of the permitted roots) INSIDE the lock, AFTER ``inflight=True``.
    # The turn is refused via send + returns; the outer finally must still clear ``inflight``
    # so the project is not wedged busy. ``test_turn_refused_when_cwd_no_longer_within_roots``
    # asserts the refusal text + ``is_busy`` (lock) side; this pins the INFLIGHT side + the
    # no-wedge (the project still accepts messages — it re-refuses rather than wedging).
    from claude_tg.session_store import JsonSessionStore

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"  # a real dir, but OUTSIDE the permitted root
    outside.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "drifted", str(outside), make_active=True)

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
    # The SB2 refusal fired (engine never started) ...
    assert engine.started is False
    assert any("permitted roots" in s["text"] for s in rec.sends)
    # ... and the in-flight marker for the refused project was cleared by the outer finally.
    rt = session._chat(1).runtimes["drifted"]
    assert rt.inflight is False
    assert session.is_busy(1, "drifted") is False  # lock released too (not wedged)

    # NO-WEDGE PROOF: a SECOND message to the SAME (still-drifted) project is ACCEPTED by the
    # busy-guard — it reaches the SB2 refusal AGAIN rather than being rejected as busy (a
    # leaked ``inflight`` would raise StreamingBusy here instead of re-refusing). Two refusals,
    # not a wedge.
    before = len([s for s in rec.sends if "permitted roots" in s["text"]])
    await asyncio.wait_for(
        session.handle_message(1, "go-again", send=rec.send, edit=rec.edit), timeout=2.0
    )
    after = len([s for s in rec.sends if "permitted roots" in s["text"]])
    assert after == before + 1, "the 2nd message must re-refuse (reach SB2), not be rejected as busy"
    assert rt.inflight is False


# ===========================================================================
# P6/C2 (SB2): the LIVE factory-wiring guard — StreamingSession.__init__ binds the
# config's path-confinement context (allowed_roots / allow_any_path / cwd) into the
# DEFAULT engine factory so the REAL bot's engines confine the SDK's file/search tools.
#
# Every OTHER test builds Engine(...) with explicit path kwargs or injects a fake factory,
# so none of them exercises the production binding: an __init__ refactor could drop the
# binding (revert to ``engine_factory or _default_engine_factory``) and silently turn C2
# OFF for the live bot with all other tests still green. This test obtains an engine via
# the session's OWN bound factory the same way ``_ensure_engine`` does and asserts it
# confines. TEETH (verified in a throwaway): with the binding dropped, the default factory
# is called with allowed_roots=() — under empty roots EVERY path is out-of-root, so the
# in-root auto-allow assertion below goes RED (an in-root Read would HOLD), and the direct
# ``_allowed_roots == config.allowed_roots`` assertion goes RED (() != the narrow root).
# ===========================================================================


async def _resolve_when_pending(eng, tool_use_id, decision):
    """Resolve a held request as soon as it registers on the engine's pending registry.

    Mirrors the helper in test_tool_path_confinement — no real wait; spins the loop until
    the request is pending, then resolves it so the awaiting ``on_tool_request`` unblocks.
    """
    for _ in range(1000):
        if eng._pending.has_pending(tool_use_id):
            return eng.resolve(tool_use_id, decision)
        await asyncio.sleep(0)
    raise AssertionError(f"request {tool_use_id} never became pending")


async def test_default_factory_binds_config_path_confinement_into_live_engine(tmp_path):
    # A real StreamingSession with NARROW allowed_roots and NO injected engine_factory: the
    # engine it builds (via its OWN bound default factory) must enforce the C2 path layer.
    root = tmp_path / "root"
    root.mkdir()
    config = make_roots_config(tmp_path, root=root)  # allowed_roots=(root,), allow_any_path=False
    session = StreamingSession(config, session_store=None, clock=lambda: 0.0)

    # Obtain an engine EXACTLY as _ensure_engine does (cwd + backstop + a fresh policy) —
    # through the session's bound default factory, NOT an injected one.
    engine = session._engine_factory(
        cwd=str(root),
        backstop_seconds=float(config.answer_backstop_seconds),
        permission_policy=PermissionPolicy(),
    )

    # (1) Direct teeth: the live config's path context reached the engine. Dropping the
    # __init__ binding makes these () / (defaults), so the narrow-root assertion goes RED.
    assert engine._allowed_roots == config.allowed_roots
    assert engine._cwd == str(root)
    assert engine._allow_any_path is config.allow_any_path

    # (2) End-to-end confinement through the production binding: an OUT-of-root tool request
    # HOLDS for approval (a PermissionEvent is injected + the request becomes pending and
    # resolves to the operator's verdict), even an otherwise-auto SAFE Read.
    out_id = "tu-out"
    op = asyncio.create_task(
        _resolve_when_pending(engine, out_id, PermissionDecision("allow_once"))
    )
    decision = await asyncio.wait_for(
        engine.on_tool_request("Read", {"file_path": "/etc/shadow"}, out_id), timeout=5
    )
    assert await op is True  # it was HELD → the operator resolved a real pending request
    assert decision.allow is True  # operator allowed it once (the hold was honored)

    # (3) Teeth + P2 regression: an IN-root Read AUTO-ALLOWS with no hold. Under the dropped
    # binding (empty roots) this would HOLD instead (never resolved → would time out), so
    # this both proves in-root is unchanged AND is RED-on-unbind. The request must NOT be
    # pending at any point, so we assert it returns promptly without a resolver.
    in_decision = await asyncio.wait_for(
        engine.on_tool_request(
            "Read", {"file_path": str(root / "ok.py")}, "tu-in"
        ),
        timeout=5,
    )
    assert in_decision.allow is True  # auto-allowed (in-root, no prompt)
    assert not engine._pending.has_pending("tu-in")  # never held


# ===========================================================================
# P6/H2/RB2: the LIVE factory-wiring guard for the per-message liveness bound —
# StreamingSession.__init__ binds config.stream_message_timeout_seconds into the
# DEFAULT engine factory as Engine.send_timeout (the per-message asyncio.wait_for
# the substrate applies). The bound is config-driven, GENEROUS by default (so an
# approved long-running tool that emits no intermediate message does not trip a
# spurious driver_error), and still bounds a genuinely-silent Claude.
#
# Like the C2 binding test above, EVERY other test injects a fake factory or builds
# Engine(...) directly, so none exercises the production send_timeout binding: an
# __init__ refactor could drop it and silently revert the live bot to the hardcoded
# 120 s with all other tests green. This obtains an engine via the session's OWN
# bound factory (as _ensure_engine does) and asserts the configured bound reached it.
# TEETH: with the binding dropped (or reverted to the 120 s default), the override
# assertion (== 45.0) goes RED.
# ===========================================================================


async def test_default_factory_binds_stream_message_timeout_into_live_engine(tmp_path):
    # A real StreamingSession with a small configured liveness bound and NO injected
    # engine_factory: the engine it builds (via its OWN bound default factory) must carry
    # that bound as send_timeout (NOT the hardcoded 120 s).
    config = make_config(workdir=str(tmp_path), stream_message_timeout_seconds=45.0)
    session = StreamingSession(config, session_store=None, clock=lambda: 0.0)

    # Obtain an engine EXACTLY as _ensure_engine does — through the session's bound default
    # factory, NOT an injected one.
    engine = session._engine_factory(
        cwd=str(tmp_path),
        backstop_seconds=float(config.answer_backstop_seconds),
        permission_policy=PermissionPolicy(),
    )

    # Teeth: the configured bound reached the engine. Dropping the __init__ binding leaves
    # the 120 s default, so this == 45.0 assertion goes RED.
    assert engine._send_timeout == 45.0
    assert engine._send_timeout == config.stream_message_timeout_seconds


async def test_default_factory_uses_generous_default_stream_message_timeout(tmp_path):
    # Over-reach / regression guard: with NO STREAM_MESSAGE_TIMEOUT_SECONDS override the live
    # engine gets the GENEROUS 300 s default — NOT the old hardcoded 120 s — so a normal
    # multi-minute approved tool (build/test/install) completes without a spurious
    # driver_error. (RED if anyone re-pins the live bound back to 120 s.)
    from claude_tg.config import DEFAULT_STREAM_MESSAGE_TIMEOUT_SECONDS

    config = make_config(workdir=str(tmp_path))  # default stream_message_timeout_seconds
    assert config.stream_message_timeout_seconds == DEFAULT_STREAM_MESSAGE_TIMEOUT_SECONDS == 300.0
    session = StreamingSession(config, session_store=None, clock=lambda: 0.0)

    engine = session._engine_factory(
        cwd=str(tmp_path),
        backstop_seconds=float(config.answer_backstop_seconds),
        permission_policy=PermissionPolicy(),
    )
    assert engine._send_timeout == 300.0, "the live bound must default to the generous 300 s, not 120 s"


# ===========================================================================
# P9 / T3 — the driver accumulates each turn's SDK-reported cost into the
# project's durable cumulative total (shown by /status). P9 / T2 — get_yolo /
# active_run_count read-only accessors for the /status health view.
# ===========================================================================


async def test_turn_accumulates_project_cost(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    engine = FakeEngine(
        [ResultEvent(
            session_id="s1", is_error=False, subtype="success",
            num_turns=2, total_cost_usd=0.03, result_text="done",
        )]
    )
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    # The turn's cost landed on the project it ran on (alpha).
    assert store.get_cost(1, "alpha") == pytest.approx(0.03)


async def test_two_turns_accumulate_cumulative_cost(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    for cost in (0.01, 0.02):
        engine = FakeEngine(
            [ResultEvent(
                session_id="s1", is_error=False, subtype="success",
                num_turns=1, total_cost_usd=cost, result_text="ok",
            )]
        )
        session = make_session(engine, store=store)
        rec = Recorder()
        await asyncio.wait_for(
            session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
        )
    # Cumulative across both turns, persisted in the store.
    assert store.get_cost(1, "alpha") == pytest.approx(0.03)


async def test_turn_without_cost_does_not_charge(tmp_path):
    # A ResultEvent with no total_cost_usd (oneshot-shaped) leaves the cumulative untouched.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    engine = FakeEngine(
        [ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert store.get_cost(1, "alpha") == 0.0


async def test_get_yolo_reflects_active_policy(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session = make_session(FakeEngine([]), store=store)
    assert session.get_yolo(1) is False  # fail-closed default
    session.set_yolo(1, True)
    assert session.get_yolo(1) is True
    session.set_yolo(1, False)
    assert session.get_yolo(1) is False


def test_get_yolo_no_runtime_is_false(tmp_path):
    # Read-only: a chat with no active project / runtime reports False, creates nothing.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    session = make_session(FakeEngine([]), store=store)
    assert session.get_yolo(1) is False
    assert store.get_active(1) is None  # not created by the read-only query


def test_active_run_count_starts_zero():
    session = make_session(FakeEngine([]))
    assert session.active_run_count() == 0


async def test_background_turn_cost_lands_on_captured_project_not_active(tmp_path):
    # ⭐ The teeth for "cost is accumulated onto the CAPTURED project (turn_name), not the
    # active one". A BACKGROUND turn runs on alpha while BETA is the active/foreground
    # project; its ResultEvent carries total_cost_usd → the cost must land on ALPHA (the
    # project the turn ran ON), and beta (active) must stay at $0.00.
    #
    # MUTATION PROBE: if _drive_turn's add_cost were called with the ACTIVE project instead
    # of turn_name, the cost would land on beta and this test FAILS on BOTH asserts (alpha
    # would be 0.0, beta would be 0.07). It is the only cost test that distinguishes the
    # captured-vs-active project — the existing cost tests use a single project that is also
    # active, so they cannot catch this misrouting.
    session, store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    eng_alpha._script = [
        ResultEvent(
            session_id="alpha-sid", is_error=False, subtype="success",
            num_turns=2, total_cost_usd=0.07, result_text="bg answer",
        )
    ]
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    await _drive_project(session, 1, "alpha", rt_alpha, send=rec.send, edit=rec.edit)
    # Cost landed on the CAPTURED project (alpha, the one that ran) …
    assert store.get_cost(1, "alpha") == pytest.approx(0.07)
    # … and NOT on the active/foreground project (beta) — the mutation probe.
    assert store.get_cost(1, "beta") == 0.0


# ===========================================================================
# T4 (P9): per-project model routing threaded into the engine the session builds.
# The default factory bakes the per-project model (override → CLAUDE_MODEL → SDK
# default) into the substrate's ClaudeAgentOptions(model=…). These build the engine
# exactly as _ensure_engine does (through the session's OWN bound default factory).
# ===========================================================================


def _make_config_with_model(tmp_path, *, model=None):
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset({1}),
        workdir=tmp_path,
        claude_bin="claude",
        model=model,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
        engine_mode="streaming",
        answer_backstop_seconds=3600,
        max_concurrent_runs=3,
        render_chat_send_interval_seconds=0.0,
        allowed_roots=(),
        allow_any_path=True,
    )


def test_default_factory_threads_per_project_model_into_substrate(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    store.set_model(1, "alpha", "claude-haiku-4-5")  # a /fast override on alpha

    session = StreamingSession(
        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
    )
    # The session resolves alpha's override...
    assert session._resolve_project_model(1, "alpha") == "claude-haiku-4-5"
    # ...and the engine it builds carries it into the substrate's ClaudeAgentOptions.
    engine = session._build_engine(1, str(tmp_path), PermissionPolicy(), "claude-haiku-4-5")
    assert engine._substrate._model == "claude-haiku-4-5"


def test_resolve_project_model_falls_back_to_config_then_none(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)

    # No override + a configured CLAUDE_MODEL → the configured default wins.
    session = StreamingSession(
        _make_config_with_model(tmp_path, model="claude-opus-4-8"),
        session_store=store,
        clock=lambda: 0.0,
    )
    assert session._resolve_project_model(1, "alpha") == "claude-opus-4-8"

    # No override + no configured model → None (the SDK default; `model` omitted).
    session2 = StreamingSession(
        _make_config_with_model(tmp_path, model=None), session_store=store, clock=lambda: 0.0
    )
    assert session2._resolve_project_model(1, "alpha") is None
    assert session2._build_engine(1, str(tmp_path), PermissionPolicy(), None)._substrate._model is None


def test_set_model_persists_on_active_project_and_get_model_reads_it(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    session = StreamingSession(
        _make_config_with_model(tmp_path, model="claude-opus-4-8"),
        session_store=store,
        clock=lambda: 0.0,
    )
    # /deep sets the override on the active project + persists.
    assert session.set_model(1, "claude-opus-4-8") == "claude-opus-4-8"
    assert store.get_model(1, "alpha") == "claude-opus-4-8"
    assert session.get_model(1) == "claude-opus-4-8"
    # /auto clears it → get_model falls back to the configured default.
    assert session.set_model(1, None) is None
    assert store.get_model(1, "alpha") is None
    assert session.get_model(1) == "claude-opus-4-8"  # configured CLAUDE_MODEL


def test_set_model_bad_value_is_safe_falls_back(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    session = StreamingSession(
        _make_config_with_model(tmp_path, model=None), session_store=store, clock=lambda: 0.0
    )
    # An empty/whitespace "model" normalizes to a clear (no override) — never an empty id.
    assert session.set_model(1, "   ") is None
    assert store.get_model(1, "alpha") is None
    # The engine then builds with model=None (SDK default), never a broken empty id.
    assert session._resolve_project_model(1, "alpha") is None


def test_injected_factory_never_receives_model_kwarg(tmp_path):
    # An injected (test) factory keeps the 3-kwarg contract; _build_engine must NOT pass
    # `model` to it (it would TypeError). The fixed-3-kwarg lambda below would raise on an
    # unexpected `model=` — its clean return proves the gate (_factory_accepts_model) works.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    store.set_model(1, "alpha", "claude-haiku-4-5")  # override present, but must not be passed
    sentinel = object()
    session = StreamingSession(
        _make_config_with_model(tmp_path),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: sentinel,
        clock=lambda: 0.0,
    )
    assert session._factory_accepts_model is False
    # Resolves the override, but builds via the 3-kwarg injected factory without it.
    model = session._resolve_project_model(1, "alpha")
    assert model == "claude-haiku-4-5"
    assert session._build_engine(1, str(tmp_path), PermissionPolicy(), model) is sentinel


# ===========================================================================
# T-EFFORT (STATUSLINE) — the per-project reasoning-EFFORT knob: the default
# factory bakes the resolved override into ClaudeAgentOptions(effort=…), the
# session resolves/persists it, and an /effort change rebuilds the warm engine
# on the NEXT turn (effort is a session-creation knob, mirroring model/thinking).
# ===========================================================================


def test_default_factory_threads_per_project_effort_into_substrate(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    store.set_effort(1, "alpha", "max")  # an /effort max override on alpha

    session = StreamingSession(
        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
    )
    # The session resolves alpha's override...
    assert session._resolve_project_effort(1, "alpha") == "max"
    # ...and the engine it builds carries it into the substrate's ClaudeAgentOptions.
    engine = session._build_engine(
        1, str(tmp_path), PermissionPolicy(), None, effort="max"
    )
    assert engine._substrate._effort == "max"


def test_resolve_project_effort_no_override_is_none_no_config_default(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    # No override → None (there is NO CLAUDE_* global default for effort — the SDK default
    # applies, so the kwarg is omitted). Even with a configured model (which DOES default),
    # effort stays None.
    session = StreamingSession(
        _make_config_with_model(tmp_path, model="claude-opus-4-8"),
        session_store=store,
        clock=lambda: 0.0,
    )
    assert session._resolve_project_effort(1, "alpha") is None
    # And the engine then builds with effort=None (SDK default), never an empty value.
    assert (
        session._build_engine(1, str(tmp_path), PermissionPolicy(), None, effort=None)
        ._substrate._effort
        is None
    )


def test_set_effort_persists_on_active_project_and_resolves_it(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    session = StreamingSession(
        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
    )
    # /effort max sets the override on the active project + persists.
    assert session.set_effort(1, "max") == "max"
    assert store.get_effort(1, "alpha") == "max"
    assert session.get_effort(1) == "max"
    assert session._resolve_project_effort(1, "alpha") == "max"
    # /effort default (None) clears it → resolve falls back to None (SDK default).
    assert session.set_effort(1, None) is None
    assert store.get_effort(1, "alpha") is None
    assert session.get_effort(1) is None
    assert session._resolve_project_effort(1, "alpha") is None


def test_set_effort_bad_value_is_safe_clears(tmp_path):
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    session = StreamingSession(
        _make_config_with_model(tmp_path), session_store=store, clock=lambda: 0.0
    )
    # An unrecognized/garbage level normalizes to a clear (no override) — never a bad id.
    assert session.set_effort(1, "turbo") is None
    assert store.get_effort(1, "alpha") is None
    assert session._resolve_project_effort(1, "alpha") is None


def test_injected_factory_never_receives_effort_kwarg(tmp_path):
    # An injected (test) factory keeps the 3-kwarg contract; _build_engine must NOT pass
    # `effort` to it (it would TypeError). The fixed-3-kwarg lambda below would raise on an
    # unexpected `effort=` — its clean return proves the gate (_factory_accepts_model) works.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    store.set_effort(1, "alpha", "max")  # override present, but must not be passed
    sentinel = object()
    session = StreamingSession(
        _make_config_with_model(tmp_path),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: sentinel,
        clock=lambda: 0.0,
    )
    assert session._factory_accepts_model is False
    effort = session._resolve_project_effort(1, "alpha")
    assert effort == "max"
    # Builds via the 3-kwarg injected factory WITHOUT effort (the gate strips it).
    assert (
        session._build_engine(1, str(tmp_path), PermissionPolicy(), None, effort=effort)
        is sentinel
    )


async def test_ensure_engine_rebuilds_on_effort_change_next_turn(tmp_path):
    # T-EFFORT: changing /effort must rebuild the session on the NEXT turn (effort is a
    # session-creation knob baked into ClaudeAgentOptions — not hot-switchable). A back-to-back
    # SAME-effort turn reuses the warm engine (the match-key includes engine_effort); an effort
    # change drops the warm engine and builds a fresh one. Mirrors the warm-reuse regression
    # test but proves the INVERSE (a change forces the rebuild).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(tmp_path), make_active=True)

    eng1 = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
        session_id="s",
    )
    eng2 = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
        session_id="s",
    )
    session = make_sequential_session({str(tmp_path): [eng1, eng2]}, store=store)

    # Turn 1: builds eng1 with the current effort (None — no override yet).
    e1, _ = await session._ensure_engine(1)
    assert e1 is eng1 and eng1.started is True
    rt = session._chat(1).runtimes["api"]
    assert rt.engine_effort is None

    # A same-effort second call reuses the warm engine (no rebuild, no second pop).
    e1b, _ = await session._ensure_engine(1)
    assert e1b is eng1, "a warm engine at the same effort must be reused"
    assert eng1.stopped is False

    # Now change /effort → the persisted override no longer matches engine_effort.
    assert session.set_effort(1, "max") == "max"
    # Next turn rebuilds: the warm eng1 is discarded (best-effort stop) and eng2 is built fresh
    # with the new effort baked in.
    e2, _ = await session._ensure_engine(1)
    assert e2 is eng2, "an effort change must rebuild the session on the next turn"
    assert eng1.stopped is True, "the stale-effort engine is torn down before the rebuild"
    assert rt.engine_effort == "max"


# ===========================================================================
# T6 (P9) — notification polish + chips (SESSION level, mock-only).
#   1. no link previews on background pings
#   2. [Open <project>] switch button on attention + done pings; SB1-gated switch routing
#   3. queued counter "(N more waiting)" when projects are parked behind the cap
#   4. free-text chip dismissal (handle_message returns True for a free-text capture)
# ===========================================================================
from telegram import LinkPreviewOptions  # noqa: E402


async def test_background_pings_disable_link_preview(tmp_path):
    # T6.1: a background hold/terminal ping passes link_preview_options(is_disabled=True) so
    # a path/URL in the (body-free) ping never balloons into a Telegram preview card.
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,
        chat_send_interval=0.0,
        sleep=_no_sleep,
    )
    state = session._chat(1)
    rec = Recorder()
    perm = PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="p1")
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    done = ResultEvent(session_id="s", is_error=False, subtype="success")
    await session._notify_terminal(state, "alpha", done, send=rec.send)
    err = ErrorEvent(kind_of_error="turn_error", message="x")
    await session._notify_terminal(state, "alpha", err, send=rec.send)
    # EVERY notification send disabled the link preview.
    assert rec.sends, "expected notification sends"
    for s in rec.sends:
        lpo = s["link_preview_options"]
        assert isinstance(lpo, LinkPreviewOptions) and lpo.is_disabled is True, s["text"]


async def test_attention_ping_carries_open_project_button(tmp_path):
    # T6.2: a background "needs attention" ping (permission) carries BOTH the verdict keyboard
    # AND an [Open <name>] switch button row.
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,
        chat_send_interval=0.0,
        sleep=_no_sleep,
    )
    state = session._chat(1)
    rec = Recorder()
    perm = PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="p1")
    await session._notify_background(state, 1, "alpha", perm, "permission", send=rec.send)
    ping = next(s for s in rec.sends if s["text"].startswith("🔔 alpha"))
    buttons = [b for row in ping["reply_markup"].inline_keyboard for b in row]
    texts = [b.text for b in buttons]
    # The three verdict buttons PLUS the [Open alpha] switch row.
    assert "📂 Open alpha" in texts
    assert any(t == "✅ Allow once" for t in texts)
    # The switch button's callback decodes to a switch for alpha.
    open_btn = next(b for b in buttons if b.text == "📂 Open alpha")
    assert open_btn.callback_data == encode_switch_callback("alpha")


async def test_done_ping_carries_open_project_button(tmp_path):
    # T6.2: a background "done" ping carries the [Open <name>] switch button (jump to project).
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,
        chat_send_interval=0.0,
        sleep=_no_sleep,
    )
    state = session._chat(1)
    rec = Recorder()
    done = ResultEvent(session_id="s", is_error=False, subtype="success")
    await session._notify_terminal(state, "alpha", done, send=rec.send)
    ping = next(s for s in rec.sends if s["text"].startswith("✅ alpha"))
    buttons = [b for row in ping["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["📂 Open alpha"]
    assert buttons[0].callback_data == encode_switch_callback("alpha")


async def test_error_ping_has_no_open_button_per_t6_scope(tmp_path):
    # T6.2 scope: the switch button is on attention + done; an ERROR ping carries none.
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,
        chat_send_interval=0.0,
        sleep=_no_sleep,
    )
    state = session._chat(1)
    rec = Recorder()
    err = ErrorEvent(kind_of_error="tool_error", message="x")
    await session._notify_terminal(state, "alpha", err, send=rec.send)
    ping = next(s for s in rec.sends if s["text"].startswith("⚠️ alpha"))
    assert ping["reply_markup"] is None


async def test_switch_button_tap_switches_active_project(tmp_path):
    # T6.2: tapping [Open <name>] routes a switch outcome carrying the target name. The session
    # does NOT mutate the store (the bot's /switch helper does the SB2 path revalidation +
    # write) — it decodes + returns the name.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
    )
    out = session.resolve_callback(1, encode_switch_callback("beta"))
    assert out.handled is True
    assert out.switch_to == "beta"
    # The session itself does not switch (no SB2 path check available here) — that is the bot.
    assert store.get_active(1) == "alpha"


async def test_switch_button_tap_no_store_is_benign_noop(tmp_path):
    # RB1: a switch tap with no registry is a benign no-op (nothing to switch within).
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
    )
    out = session.resolve_callback(1, encode_switch_callback("beta"))
    assert out.handled is False
    assert out.switch_to is None


async def test_switch_button_forged_callback_resolves_nothing(tmp_path):
    # MUTATION PROBE / SB1 defense-in-depth: a FORGED switch callback (non-SB4 name) decodes
    # to None → resolve_callback handles nothing and switches nothing.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
    )
    out = session.resolve_callback(1, "w|bad name|s")  # space → decode None
    assert out.handled is False
    assert out.switch_to is None
    assert store.get_active(1) == "alpha"


async def test_queued_counter_in_pings_when_projects_queued(tmp_path):
    # T6.3: with N projects parked behind the cap, the ping shows "(N more waiting)". Build the
    # queue state directly (a real waiter future) and assert the counter rides the ping.
    session = StreamingSession(
        make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0,
        chat_send_interval=0.0,
        sleep=_no_sleep,
    )
    state = session._chat(1)
    # Two parked (not-done) waiters → "(2 more waiting)".
    from claude_tg.stream_session import _QueuedTurn

    loop = asyncio.get_running_loop()
    for _ in range(2):
        rt = _ProjectRuntime(cwd="/x")
        state.run_queue.append(_QueuedTurn(runtime=rt, future=loop.create_future()))
    assert session.queued_waiting(1) == 2
    rec = Recorder()
    done = ResultEvent(session_id="s", is_error=False, subtype="success")
    await session._notify_terminal(state, "alpha", done, send=rec.send)
    ping = next(s for s in rec.sends if s["text"].startswith("✅ alpha"))
    assert ping["text"] == "✅ alpha — done (2 more waiting)"
    # Clean up the futures so the loop has no pending tasks at teardown.
    for q in state.run_queue:
        q.future.cancel()


async def test_queued_counter_excludes_done_waiters(tmp_path):
    # T6.3: a transferred/drained waiter (done future) is NOT counted — only live parked turns.
    session = StreamingSession(
        make_config(), session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: FakeEngine([]),
        clock=lambda: 100.0, chat_send_interval=0.0, sleep=_no_sleep,
    )
    state = session._chat(1)
    from claude_tg.stream_session import _QueuedTurn

    loop = asyncio.get_running_loop()
    live = loop.create_future()
    done_fut = loop.create_future()
    done_fut.set_result(None)  # already transferred → excluded
    state.run_queue.append(_QueuedTurn(runtime=_ProjectRuntime(cwd="/x"), future=live))
    state.run_queue.append(_QueuedTurn(runtime=_ProjectRuntime(cwd="/y"), future=done_fut))
    assert session.queued_waiting(1) == 1
    live.cancel()


async def test_free_text_capture_returns_true_for_chip_dismissal(tmp_path):
    # T6.4: handle_message returns True when the message is a free-text capture (so the bot
    # dismisses the one-time chips), and False for a normal new turn.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    eng = FakeEngine([], session_id="alpha-sid")
    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,
    )
    # Arm free-text capture on alpha (as an "Other"/reject tap would).
    state = session._chat(1)
    rt = session._runtime(1, "alpha", "/work/alpha")
    rt.engine = eng
    rt.started = True
    ask = AskEvent(
        questions=[{"question": "Q?", "options": [{"label": "A"}]}],
        tool_use_id="a-other", session_id="alpha-sid",
    )
    session._register_pending(state, "alpha", ask)
    rt.awaiting_text_for = "a-other"
    rt.awaiting_text_mode = "ask_other"
    rt.awaiting_text_question_index = 0
    rt.awaiting_text_armed_at = session._next_armed_seq(state)
    rec = Recorder()
    captured = await session.handle_message(
        1, "my free text answer", send=rec.send, edit=rec.edit, delete=rec.delete
    )
    assert captured is True, "a free-text capture must return True so the bot dismisses chips"
    # The free text was routed to the engine as the answer (not a new turn).
    assert eng.resolve_calls and eng.resolve_calls[-1][0] == "a-other"


# ===========================================================================
# P11 / T2 — attach: adopt ANY discovered session as a controllable project.
#
# ⭐ The BINDING fork-vs-continue decision is made at the FIRST WRITE (in _ensure_engine),
# re-derived from a FRESH single-session liveness re-probe — NOT frozen at attach time.
# Persisted via ``fork_pending`` so it survives a restart. Discovery (attach lookup) AND the
# re-probe are both INJECTED (no real SDK / ps / ~/.claude); the engine captures each resume's
# (id, fork) so a test proves what actually happened at the write. The HARD safety rule under
# test: a base id that is LIVE or UNCERTAIN at first write is FORKED (never co-driven); a
# confidently-idle one continues; an out-of-ALLOWED_ROOTS cwd is refused.
# ===========================================================================

from claude_tg.session_store import JsonSessionStore as _AttachStore  # noqa: E402
from claude_tg.sessions_discovery import DiscoveredSession as _Disc  # noqa: E402
from claude_tg.stream_session import AttachOutcome as _AttachOutcome  # noqa: E402


class ForkCapturingEngine:
    """A fake Engine that records every resume's (id, fork) so a test can prove the decision.

    Mirrors FakeEngine but its ``resume`` accepts the P11 ``fork`` kwarg (the fork path calls
    ``engine.resume(id, fork=True)``). On a FORK it adopts a fresh ``forked_session_id``
    (simulating the SDK resuming into a NEW id, copied transcript) so the persisted result id is
    the FORK's, never the live base id; on a CONTINUE it keeps the resumed id.
    """

    def __init__(self, *, forked_session_id="forked-new-id"):
        self.session_id = None
        self.started = False
        self.stopped = False
        self.resume_calls: list[tuple[str, bool]] = []
        self._forked_session_id = forked_session_id
        self._gate = asyncio.Event()

    async def start(self):
        self.started = True

    async def resume(self, session_id, *, fork=False):
        self.resume_calls.append((session_id, fork))
        self.started = True
        # A FORK lands on a brand-new id (never the live base); a CONTINUE keeps the base.
        self.session_id = self._forked_session_id if fork else session_id

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        # One clean turn whose result carries whatever id the engine currently holds (the
        # forked id after a fork, the continued id otherwise) — so _drive_turn persists THAT.
        yield ResultEvent(
            session_id=self.session_id, is_error=False, subtype="success", result_text="ok"
        )

    def resolve(self, tool_use_id, decision):
        self._gate.set()
        return True

    def cancel(self, tool_use_id=None):
        self._gate.set()
        return 1


def _probe_returning(running, degraded=False):
    """A probe_one seam returning a FIXED ``(running, degraded)`` verdict (B2+B3 re-probe)."""
    def _probe(session_id, cwd):
        return (running, degraded)
    return _probe


def make_attach_session(
    store, discovered, *, engine=None, workdir="/work",
    allowed_roots=(), allow_any_path=True, probe_one=None,
):
    """A real StreamingSession over ``store`` with INJECTED discovery + re-probe + a fork engine.

    ``discovered`` is the list ``self._discover`` returns (the attach lookup). ``probe_one`` is
    the FIRST-WRITE single-session re-probe seam → ``(running, degraded)``; it defaults to a
    CONFIDENTLY-IDLE verdict ``(False, False)`` so a plain idle attach continues unless a test
    scripts otherwise. The engine factory returns ONE ``ForkCapturingEngine`` so a test reads
    its ``resume_calls``. Defaults to ``allow_any_path=True`` (SB2 no-ops for the in-root tests).
    """
    eng = engine if engine is not None else ForkCapturingEngine()
    session = StreamingSession(
        make_config(
            engine_mode="streaming", workdir=workdir,
            allowed_roots=allowed_roots, allow_any_path=allow_any_path,
        ),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,
        discover=lambda: list(discovered),
        probe_one=probe_one if probe_one is not None else _probe_returning(False, False),
    )
    return session, eng


# ---- attach mechanics (lookup / SB2 / naming / idempotency) ---------------


def test_attach_adopts_persists_base_id_and_fork_pending(tmp_path):
    """Attach adopts the session as an active project pinned to the base id, persists the
    PERSISTED ``fork_pending`` marker (the binding decision is deferred to first write), and
    the attach-time message does NOT over-promise an outcome."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="sess-1", cwd=str(tmp_path), title="My Task", last_active=0, running=False)]
    session, _eng = make_attach_session(store, disc, workdir=str(tmp_path))

    outcome = session.attach_session(1, "sess-1")
    assert isinstance(outcome, _AttachOutcome) and outcome.ok
    name = outcome.project_name
    assert name is not None
    rec = store.get_project(1, name)
    assert rec["session_id"] == "sess-1"          # pinned to the base id
    assert store.get_active(1) == name            # made active
    assert store.get_fork_pending(1, name) is True  # adopted-not-yet-resumed (B2+B3)
    # The message does NOT promise forked/continue (decided at first write); it explains the
    # auto-fork-if-live guarantee honestly.
    assert "forking automatically if it's active elsewhere" in outcome.message
    assert outcome.forked is False  # not yet known at attach time


def test_attach_out_of_root_cwd_is_refused_not_adopted(tmp_path):
    """⭐ SB2: a discovered session whose cwd is OUTSIDE ALLOWED_ROOTS is REFUSED with a clear
    message — never silently adopted + driven in an arbitrary dir. No project is created."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="ext-sess", cwd=str(outside), title="t", last_active=0, running=False)]
    session, _eng = make_attach_session(
        store, disc, workdir=str(root),
        allowed_roots=(str(root),), allow_any_path=False,  # confinement ON
    )

    outcome = session.attach_session(1, "ext-sess")
    assert outcome.ok is False
    assert "permitted roots" in outcome.message
    assert store.list_projects(1) == {}  # NOTHING adopted


def test_attach_unknown_session_id_clean_error_no_crash(tmp_path):
    """RB2: an id absent from discovery → a clean refusal, no project, no crash."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="known-1", cwd=str(tmp_path), title="t", last_active=0, running=False)]
    session, _eng = make_attach_session(store, disc, workdir=str(tmp_path))

    outcome = session.attach_session(1, "does-not-exist")
    assert outcome.ok is False
    assert "No Claude session" in outcome.message
    assert store.list_projects(1) == {}


def test_attach_no_store_replies_needs_persistence():
    """RB1: with no store there is no registry to attach into — a clean notice, no deref."""
    session = StreamingSession(
        make_config(engine_mode="streaming"),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: ForkCapturingEngine(),
        clock=lambda: 0.0,
        discover=lambda: [_Disc(session_id="x", cwd="/w", title="t", last_active=0)],
    )
    outcome = session.attach_session(1, "x")
    assert outcome.ok is False and "persistence" in outcome.message.lower()


def test_attach_appears_in_projects_listing(tmp_path):
    """The adopted session is a first-class project: it shows up in list_projects + get_active
    so /projects + /switch work on it (SB4-valid name)."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="sess-q", cwd=str(tmp_path), title="Fix the bug", last_active=0, running=False)]
    session, _eng = make_attach_session(store, disc, workdir=str(tmp_path))

    outcome = session.attach_session(1, "sess-q")
    name = outcome.project_name
    import re as _re
    assert _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name), f"name {name!r} must be SB4-valid"
    assert name in store.list_projects(1)
    assert store.get_active(1) == name


def test_attach_derives_sb4_name_from_messy_title(tmp_path):
    """Naming: a messy title (spaces/slashes/unicode) is sanitized to the SB4 charset; an empty
    one falls back to attached-<shortid>. Two attaches of similar names DEDUPE (no collision)."""
    store = _AttachStore(tmp_path / "s.json")
    d1 = _Disc(session_id="s1", cwd=str(tmp_path), title="Fix: the /login bug!! 🎉", last_active=0)
    d2 = _Disc(session_id="s2", cwd=str(tmp_path), title="Fix: the /login bug!! 🎉", last_active=0)
    d3 = _Disc(session_id="s3deadbeef", cwd="/", title="   ", last_active=0)  # empty → fallback
    session, _eng = make_attach_session(store, [d1, d2, d3], workdir=str(tmp_path), allow_any_path=True)

    n1 = session.attach_session(1, "s1").project_name
    n2 = session.attach_session(1, "s2").project_name
    n3 = session.attach_session(1, "s3deadbeef").project_name
    import re as _re
    for n in (n1, n2, n3):
        assert _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", n)
    assert n1 != n2  # deduped — two similar titles don't collide
    assert n3.startswith("attached-")  # empty title + no basename → the shortid fallback
    assert len({n1, n2, n3}) == 3
    assert set(store.list_projects(1)) == {n1, n2, n3}


def test_attach_same_id_twice_is_idempotent_switch_no_duplicate(tmp_path):
    """Re-attaching an id the chat already adopted just SWITCHES to it — no duplicate project."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="dup-sess", cwd=str(tmp_path), title="t", last_active=0, running=True)]
    session, _eng = make_attach_session(store, disc, workdir=str(tmp_path))

    first = session.attach_session(1, "dup-sess")
    assert first.ok
    n_before = set(store.list_projects(1))
    second = session.attach_session(1, "dup-sess")
    assert second.ok
    assert second.project_name == first.project_name  # same project
    assert set(store.list_projects(1)) == n_before    # NO new project


# ---- the BINDING decision at FIRST WRITE (re-probe), incl. B2 restart + B3 race ----------


async def test_attach_idle_first_write_reprobe_idle_continues(tmp_path):
    """A confidently-idle base id (idle at attach AND idle at the first-write re-probe) drives a
    NORMAL turn that CONTINUES the same id (fork=False) and persists it; fork_pending CLEARED."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="idle-1", cwd=str(tmp_path), title="t", last_active=0, running=False)]
    session, eng = make_attach_session(
        store, disc, workdir=str(tmp_path), probe_one=_probe_returning(False, False),
    )
    name = session.attach_session(1, "idle-1").project_name

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng.resume_calls == [("idle-1", False)]  # CONTINUED the same id (re-probe idle)
    assert store.get_project(1, name)["session_id"] == "idle-1"
    # First successful turn → fork_pending cleared (subsequent resumes are ordinary continues).
    assert store.get_fork_pending(1, name) is False


async def test_attach_live_first_write_forks_and_persists_forked_id(tmp_path):
    """⭐ THE HARD RULE end-to-end: a base id LIVE at the first-write re-probe is FORKED
    (resume fork=True) and the FORKED id is persisted — the live base id is never co-driven."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="live-9", cwd=str(tmp_path), title="t", last_active=0, running=True)]
    eng = ForkCapturingEngine(forked_session_id="fork-abc")
    session, _ = make_attach_session(
        store, disc, engine=eng, workdir=str(tmp_path), probe_one=_probe_returning(True, False),
    )
    name = session.attach_session(1, "live-9").project_name

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng.resume_calls == [("live-9", True)]   # FORKED off the live base id
    assert store.get_project(1, name)["session_id"] == "fork-abc"  # forked id persisted
    assert store.get_fork_pending(1, name) is False  # cleared after the first clean turn


async def test_attach_restart_before_first_turn_still_forks(tmp_path):
    """⭐ B2 — restart before the first turn. Attach a session, then RECREATE StreamingSession
    from the SAME store (the in-memory attach_fork is gone) and send a message. The PERSISTED
    fork_pending triggers a fresh re-probe; with the base id now live the resume FORKS — never
    a co-driving resume(fork=False) on the persisted base id."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="live-r", cwd=str(tmp_path), title="t", last_active=0, running=True)]
    # 1) Attach in session A.
    session_a, _ = make_attach_session(store, disc, workdir=str(tmp_path), probe_one=_probe_returning(True, False))
    name = session_a.attach_session(1, "live-r").project_name
    assert store.get_fork_pending(1, name) is True  # the durable marker survives the restart

    # 2) Simulate a RESTART: a brand-new StreamingSession over the SAME store (no in-memory
    #    runtime / attach_fork). The first-write re-probe reports the base id is LIVE.
    eng_b = ForkCapturingEngine(forked_session_id="fork-after-restart")
    session_b = StreamingSession(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng_b,
        clock=lambda: 0.0,
        discover=lambda: list(disc),
        probe_one=_probe_returning(True, False),  # base id is live NOW
    )
    rec = Recorder()
    await asyncio.wait_for(session_b.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    # The restarted process FORKED (never co-drove the persisted base id).
    assert eng_b.resume_calls == [("live-r", True)]
    assert store.get_project(1, name)["session_id"] == "fork-after-restart"


async def test_attach_idle_then_goes_live_before_first_write_forks(tmp_path):
    """⭐ B3 — the attach→first-write race. Attach while IDLE (idle at attach), but the
    first-write re-probe reports the base id has since gone LIVE → the resume FORKS (caught at
    the write), never co-driving. The decision is the FRESH probe, not the stale attach-time
    liveness."""
    store = _AttachStore(tmp_path / "s.json")
    # IDLE at attach time…
    disc = [_Disc(session_id="race-1", cwd=str(tmp_path), title="t", last_active=0, running=False)]
    eng = ForkCapturingEngine(forked_session_id="fork-race")
    # …but the first-write re-probe says LIVE.
    session, _ = make_attach_session(
        store, disc, engine=eng, workdir=str(tmp_path), probe_one=_probe_returning(True, False),
    )
    name = session.attach_session(1, "race-1").project_name

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng.resume_calls == [("race-1", True)]  # FORKED — the race was caught at the write
    assert store.get_project(1, name)["session_id"] == "fork-race"


async def test_attach_first_write_reprobe_uncertain_forks(tmp_path):
    """Safe-default-on-doubt at the FIRST WRITE: if the re-probe can't confirm idle
    (degraded=True), the resume FORKS rather than risk co-driving a possibly-live session."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="unc-1", cwd=str(tmp_path), title="t", last_active=0, running=False)]
    eng = ForkCapturingEngine(forked_session_id="fork-unc")
    session, _ = make_attach_session(
        store, disc, engine=eng, workdir=str(tmp_path),
        probe_one=_probe_returning(False, True),  # could NOT tell → uncertain
    )
    name = session.attach_session(1, "unc-1").project_name

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng.resume_calls == [("unc-1", True)]  # forked on doubt
    assert store.get_project(1, name)["session_id"] == "fork-unc"


async def test_attach_idle_second_turn_is_ordinary_continue_no_refork(tmp_path):
    """After the first successful turn clears fork_pending, a SECOND turn is an ordinary
    continue — NO second re-probe, NO re-fork (the forked/continued id is ours alone now)."""
    store = _AttachStore(tmp_path / "s.json")
    disc = [_Disc(session_id="idle-2", cwd=str(tmp_path), title="t", last_active=0, running=False)]
    probe_calls = []

    def probe(session_id, cwd):
        probe_calls.append(session_id)
        return (False, False)  # confidently idle

    eng = ForkCapturingEngine()
    session, _ = make_attach_session(store, disc, engine=eng, workdir=str(tmp_path), probe_one=probe)
    name = session.attach_session(1, "idle-2").project_name

    rec = Recorder()
    await asyncio.wait_for(session.handle_message(1, "t1", send=rec.send, edit=rec.edit), timeout=2.0)
    assert store.get_fork_pending(1, name) is False  # cleared after the first clean turn
    assert probe_calls == ["idle-2"]  # re-probed exactly once (the first write)

    # A SECOND turn: the engine is warm-started, so no resume at all; even on a fresh engine it
    # would be a plain continue. Crucially fork_pending is cleared so NO further re-probe fires.
    await asyncio.wait_for(session.handle_message(1, "t2", send=rec.send, edit=rec.edit), timeout=2.0)
    assert probe_calls == ["idle-2"]  # STILL only the one first-write probe (no re-probe)


# ---- mutation probe 1: the fork-if-live rule is at first write (re-probe) -----------------

async def test_mutation_probe_fork_if_live_at_first_write(tmp_path):
    """MUTATION PROBE — the fork-if-live decision is genuinely gated on the FIRST-WRITE
    re-probe. INTACT: a base id the re-probe reports LIVE forks; one it reports IDLE continues.
    If someone froze the decision at attach (skipped the re-probe and continued the persisted
    base id), the LIVE assertion would FAIL — catching the co-drive-on-restart/race regression.
    The IDLE assertion proves it's not just always-forking."""
    # LIVE at re-probe → fork.
    store_l = _AttachStore(tmp_path / "l.json")
    eng_l = ForkCapturingEngine(forked_session_id="fk")
    s_l, _ = make_attach_session(
        store_l, [_Disc(session_id="L", cwd=str(tmp_path), title="t", last_active=0, running=False)],
        engine=eng_l, workdir=str(tmp_path), probe_one=_probe_returning(True, False),
    )
    s_l.attach_session(1, "L")
    rec = Recorder()
    await asyncio.wait_for(s_l.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng_l.resume_calls == [("L", True)], "a LIVE-at-write base id MUST fork (never co-drive)"

    # IDLE at re-probe → continue.
    store_i = _AttachStore(tmp_path / "i.json")
    eng_i = ForkCapturingEngine()
    s_i, _ = make_attach_session(
        store_i, [_Disc(session_id="I", cwd=str(tmp_path), title="t", last_active=0, running=False)],
        engine=eng_i, workdir=str(tmp_path), probe_one=_probe_returning(False, False),
    )
    s_i.attach_session(1, "I")
    rec2 = Recorder()
    await asyncio.wait_for(s_i.handle_message(1, "go", send=rec2.send, edit=rec2.edit), timeout=2.0)
    assert eng_i.resume_calls == [("I", False)], "a confidently-idle base id continues the same id"


# ---- mutation probe 2: the SB2 out-of-root refusal ------------------------

def test_mutation_probe_sb2_out_of_root_refusal(tmp_path):
    """MUTATION PROBE — SB2 genuinely gates the attach on the discovered cwd. In-root → adopted;
    the SAME id moved OUT-of-root → refused. Dropping resolve_within_roots would FAIL the
    out-of-root assertion. The in-root case proves it's not just always-refusing."""
    root = tmp_path / "ok"
    root.mkdir()
    outside = tmp_path / "no"
    outside.mkdir()

    store_ok = _AttachStore(tmp_path / "ok.json")
    s_ok, _ = make_attach_session(
        store_ok, [_Disc(session_id="X", cwd=str(root), title="t", last_active=0)],
        workdir=str(root), allowed_roots=(str(root),), allow_any_path=False,
    )
    assert s_ok.attach_session(1, "X").ok is True
    assert store_ok.list_projects(1) != {}

    store_no = _AttachStore(tmp_path / "no.json")
    s_no, _ = make_attach_session(
        store_no, [_Disc(session_id="X", cwd=str(outside), title="t", last_active=0)],
        workdir=str(root), allowed_roots=(str(root),), allow_any_path=False,
    )
    refused = s_no.attach_session(1, "X")
    assert refused.ok is False, "an out-of-ALLOWED_ROOTS cwd MUST be refused (SB2)"
    assert store_no.list_projects(1) == {}


# ===========================================================================
# P14 T-FIRE ⭐ — fire_schedule: the proactive turn entry point (force-gate +
# audit + overlap policy). Mock engine; a live AuditLog so the records assert.
# ===========================================================================


def _make_fire_session(engine, tmp_path, *, store=None):
    """A StreamingSession with a live, body-free AuditLog wired (for proactive_fire/skip)."""
    from claude_tg.audit import AuditLog

    session = make_session(engine, store=store)
    session.audit_log = AuditLog(tmp_path / "audit.jsonl")
    return session


def _schedule(name="ci", *, chat_id=1, project=None, prompt="run the tests"):
    from claude_tg.scheduler import Schedule

    return Schedule(
        name=name, interval_seconds=60, prompt=prompt, chat_id=chat_id,
        next_run=0.0, project=project,
    )


async def test_fire_schedule_drives_a_proactive_turn_and_audits_fire(tmp_path):
    """fire_schedule sends the body-free ⏰ header, audits a ``proactive_fire`` session_event,
    and drives the turn with ``proactive=True`` (the force-gate signal reaches engine.send)."""
    engine = FakeEngine([
        TextEvent(text="all green", session_id="s"),
        ResultEvent(session_id="s", is_error=False, subtype="success"),
    ])
    session = _make_fire_session(engine, tmp_path)
    rec = Recorder()

    ok = await session.fire_schedule(_schedule("ci"), send=rec.send, edit=rec.edit, delete=rec.delete)

    assert ok is True
    # The force-gate flag was threaded into engine.send (a proactive turn).
    assert engine.proactive_calls == [True]
    # A body-free ⏰ <name> (scheduled) header was sent (the name only, never the prompt).
    assert any("ci" in s["text"] and "scheduled" in s["text"] for s in rec.sends)
    assert not any("run the tests" in s["text"] for s in rec.sends)  # prompt never echoed
    # The fire was audited body-free: a proactive_fire session_event with the task name.
    events = session.audit_log.tail(20)
    fires = [e for e in events if e.kind == "session_event" and (e.summary or "").startswith("proactive_fire")]
    assert len(fires) == 1
    assert "ci" in (fires[0].summary or "")
    assert "run the tests" not in (fires[0].summary or "")  # SB3: no prompt in the record


async def test_fire_schedule_into_busy_project_skips_no_overlap_and_audits_skip(tmp_path):
    """⭐ Overlap policy: a fire into a BUSY chat/project does NOT stack a second turn — it is
    a clean skip (StreamingBusy), with a body-free ⏰ skipped notice + a ``proactive_skip``
    audit. The other turn keeps running untouched (no two turns over each other)."""
    # A first turn that PARKS (holds), keeping the project busy.
    engine = FakeEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = _make_fire_session(engine, tmp_path)
    rec = Recorder()

    first = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Fire a schedule into the SAME (busy) chat — it must skip, not stack.
    fired = await session.fire_schedule(_schedule("ci"), send=rec.send, edit=rec.edit, delete=rec.delete)
    assert fired is False  # skipped (busy)
    assert any("skipped" in s["text"] and "ci" in s["text"] for s in rec.sends)
    skips = [
        e for e in session.audit_log.tail(20)
        if e.kind == "session_event" and (e.summary or "").startswith("proactive_skip")
    ]
    assert len(skips) == 1 and skips[0].decision == "busy"

    # The first turn was never disturbed — release + finish it cleanly.
    engine.cancel()
    await asyncio.wait_for(first, timeout=2.0)


async def test_fire_schedule_rb1_a_turn_error_becomes_a_skip_never_raises(tmp_path):
    """⭐ RB1-total: if the driven turn raises a NON-busy error, fire_schedule CATCHES it,
    audits a ``proactive_skip`` (decision=error), and returns False — it NEVER propagates (so a
    single bad fire can't kill the firing loop or the bot)."""
    class BoomEngine(FakeEngine):
        async def send(self, prompt, *, timeout=None, proactive=False, **_kwargs):
            self.proactive_calls.append(proactive)
            raise RuntimeError("turn boom")
            yield  # pragma: no cover - unreachable; makes this an async generator

    engine = BoomEngine([])
    session = _make_fire_session(engine, tmp_path)
    rec = Recorder()

    # Must NOT raise despite the turn blowing up.
    fired = await session.fire_schedule(_schedule("ci"), send=rec.send, edit=rec.edit, delete=rec.delete)
    assert fired is False
    skips = [
        e for e in session.audit_log.tail(20)
        if e.kind == "session_event" and (e.summary or "").startswith("proactive_skip")
    ]
    assert len(skips) == 1 and skips[0].decision == "error"


async def test_fire_schedule_pins_the_override_project(tmp_path):
    """``project_override`` (the task's pinned project) targets THAT project even when another
    is active — a scheduled task runs its OWN project, not whatever the chat switched to."""
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    store.create(1, "beta", str(tmp_path / "beta"), make_active=True)  # beta now active
    engine = FakeEngine([ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = _make_fire_session(engine, tmp_path, store=store)
    rec = Recorder()

    # Fire a schedule PINNED to alpha while beta is active.
    await session.fire_schedule(
        _schedule("t", project="alpha"), send=rec.send, edit=rec.edit, delete=rec.delete
    )
    # The turn ran against alpha's runtime (the pinned project), not the active beta.
    assert "alpha" in session._chat(1).runtimes
    # beta remains the store's active project (the fire did not switch it).
    assert store.get_active(1) == "beta"


async def test_handle_message_threads_proactive_into_engine_send():
    """handle_message(proactive=True) threads the force-gate flag into engine.send; a NORMAL
    turn passes proactive=False (default). The kwarg-threading guard (independent of
    fire_schedule), so the engine's force-gate is armed for a proactive turn."""
    engine = FakeEngine([ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine)
    rec = Recorder()

    # Normal turn → proactive=False reaches engine.send.
    await session.handle_message(1, "normal", send=rec.send, edit=rec.edit)
    assert engine.proactive_calls[-1] is False

    # Proactive turn → proactive=True reaches engine.send (the force-gate signal).
    await session.handle_message(1, "fire", send=rec.send, edit=rec.edit, proactive=True)
    assert engine.proactive_calls[-1] is True


async def test_handle_message_project_override_pins_named_project(tmp_path):
    """handle_message(project_override='alpha') runs against alpha even when beta is active —
    a proactive task targets ITS pinned project (the turn never retargets to the active one)."""
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    store.create(1, "beta", str(tmp_path / "beta"), make_active=True)  # beta active
    engine = FakeEngine([ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = make_session(engine, store=store)
    rec = Recorder()

    await session.handle_message(
        1, "go", send=rec.send, edit=rec.edit, command_initiated=True, project_override="alpha"
    )
    # The turn built alpha's runtime (the pinned project), beta stays the store's active.
    assert "alpha" in session._chat(1).runtimes
    assert store.get_active(1) == "beta"


async def test_fire_schedule_skips_deauthorized_chat_sb1_at_fire_time(tmp_path):
    """⭐ Codex-QA BLOCKER 2 — SB1 AT FIRE TIME: a schedule whose chat_id is NO LONGER in the
    allowlist must NEVER fire (a chat removed from TELEGRAM_ALLOWED_CHAT_IDS since /every). The
    fire SKIPS — no header, no turn, no proactive_fire — and audits a proactive_skip
    (decision=unauthorized). MUTATION-PROBE companion: drop the allowlist re-check in
    fire_schedule and this de-authorized chat would receive a proactive turn → the test fails."""
    # The session's config allowlists ONLY chat 1; the schedule targets chat 999 (de-authorized).
    engine = FakeEngine([ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = _make_fire_session(engine, tmp_path)  # make_config default allowed=(1,)
    rec = Recorder()

    fired = await session.fire_schedule(
        _schedule("ci", chat_id=999), send=rec.send, edit=rec.edit, delete=rec.delete
    )

    assert fired is False  # skipped — never fired
    assert engine.proactive_calls == []  # the turn NEVER ran (engine.send never called)
    assert rec.sends == []  # NO header sent to the de-authorized chat
    events = session.audit_log.tail(20)
    # Audited as unauthorized, NOT as a proactive_fire.
    skips = [e for e in events if e.kind == "session_event" and (e.summary or "").startswith("proactive_skip")]
    assert len(skips) == 1 and skips[0].decision == "unauthorized"
    assert not any((e.summary or "").startswith("proactive_fire") for e in events)


async def test_fire_schedule_allowlisted_chat_still_fires(tmp_path):
    """The companion to the SB1-at-fire test: an ALLOWLISTED chat (1) still fires normally —
    the re-check only blocks de-authorized chats, it does not break legitimate fires."""
    engine = FakeEngine([ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = _make_fire_session(engine, tmp_path)  # allowed=(1,)
    rec = Recorder()

    fired = await session.fire_schedule(
        _schedule("ci", chat_id=1), send=rec.send, edit=rec.edit, delete=rec.delete
    )
    assert fired is True
    assert engine.proactive_calls == [True]  # the gated turn ran
    fires = [
        e for e in session.audit_log.tail(20)
        if e.kind == "session_event" and (e.summary or "").startswith("proactive_fire")
    ]
    assert len(fires) == 1


async def test_fire_schedule_busy_skip_sends_only_skip_notice_not_header(tmp_path):
    """⭐ Codex-QA NON-BLOCKING (header ordering): a fire skipped because the chat is BUSY
    sends ONLY the ⏰ skipped notice — NOT a start header (the busy pre-check runs BEFORE the
    header). So a busy-skip never shows a confusing header-then-skip pair."""
    # A first turn that PARKS, keeping the chat busy.
    engine = FakeEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")])
    session = _make_fire_session(engine, tmp_path)
    rec = Recorder()

    first = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    rec.sends.clear()  # ignore anything the first turn sent; focus on the fire's sends

    fired = await session.fire_schedule(_schedule("ci"), send=rec.send, edit=rec.edit, delete=rec.delete)
    assert fired is False
    # EXACTLY ONE send from the fire: the skip notice. No "(scheduled)…" start header.
    assert len(rec.sends) == 1
    assert "skipped" in rec.sends[0]["text"] and "ci" in rec.sends[0]["text"]
    assert not any("(scheduled)" in s["text"] for s in rec.sends)
    # Audited as busy, and NO proactive_fire was recorded for the skipped fire.
    events = session.audit_log.tail(20)
    assert any(e.kind == "session_event" and (e.summary or "").startswith("proactive_skip")
               and e.decision == "busy" for e in events)
    assert not any((e.summary or "").startswith("proactive_fire") for e in events)

    engine.cancel()
    await asyncio.wait_for(first, timeout=2.0)


# ---------------------------------------------------------------------------
# STATUSLINE T-SL-CORE — the pinned statusline pin/edit lifecycle.
#
# _update_statusline builds the foreground statusline body from CURRENT state and reconciles
# it with the chat's ONE pinned line: first use SENDS + PINS (silently); a changed state EDITS
# in place; an identical state is a no-op; an edit FAILURE clears the id + re-sends + re-pins
# (orphan recovery); and a pin/edit/send raising is SWALLOWED (RB1 — never breaks a turn). All
# I/O funnels through the per-chat gate as the non-verbatim kind (RB5). Only ONE id is held.
#
# These exercise the machinery directly with fake send/edit/pin/unpin closures (T-SL-WIRE will
# call _update_statusline from the live turn path; this unit is machinery-only).
# ---------------------------------------------------------------------------


class StatuslineRecorder:
    """Captures the send/edit/pin/unpin calls _update_statusline performs (with fault injection).

    ``fail_edit`` makes the FIRST edit raise (the orphan-recovery trigger — the operator
    unpinned/deleted the line). ``fail_pin`` / ``fail_send`` make pin / send raise (the RB1
    swallow probe). ``fail_pin_times=N`` makes only the first N pins raise (then succeed — the
    pin-retry probe). Each call is recorded so the sequence + the silent-pin flag are assertable.
    """

    def __init__(self, *, fail_edit=False, fail_pin=False, fail_send=False, fail_pin_times=0):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.pins: list[dict] = []
        self.unpins: list[dict] = []
        self._next_id = 500
        self._fail_edit_first = fail_edit
        self._fail_pin = fail_pin
        self._fail_send = fail_send
        self._fail_pin_remaining = fail_pin_times

    async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
        self.sends.append({"text": text, "parse_mode": parse_mode})
        if self._fail_send:
            raise RuntimeError("Telegram error: send failed")
        self._next_id += 1
        return self._next_id

    async def edit(self, *, message_id, text, parse_mode=None) -> None:
        if self._fail_edit_first:
            self._fail_edit_first = False
            raise RuntimeError("Telegram BadRequest: message to edit not found")
        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})

    async def pin(self, *, message_id, disable_notification=None) -> None:
        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})
        if self._fail_pin:
            raise RuntimeError("Telegram error: pin failed")
        if self._fail_pin_remaining > 0:
            self._fail_pin_remaining -= 1
            raise RuntimeError("Telegram error: pin failed (transient)")

    async def unpin(self, *, message_id) -> None:
        self.unpins.append({"message_id": message_id})


def _prime_statusline_project(session, *, chat_id=1, engine=None, status="running"):
    """Resolve the chat's active project + give its runtime an engine + status (statusline read).

    _update_statusline reads the FOREGROUND project's live state. With no store this auto-creates
    the implicit ``default`` runtime; we attach a (fake) engine for the ctx-% read and set the
    status enum (working vs idle). Returns the (name, runtime).
    """
    name, rt = session._active_runtime(chat_id, create_default=True)
    if engine is not None:
        rt.engine = engine
    rt.status = status
    return name, rt


async def test_update_statusline_first_use_sends_then_pins_silently():
    # First update for a chat: SEND the body, then PIN it with the notification disabled.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    name, _rt = _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1, f"first use must SEND exactly once, sends={rec.sends!r}"
    assert len(rec.pins) == 1, f"first use must PIN exactly once, pins={rec.pins!r}"
    # The pin is SILENT (disable_notification=True) — design §3.1 (no re-ping).
    assert rec.pins[0]["disable_notification"] is True
    # The pinned id is the just-sent id (501 — the recorder hands out 501, 502, …).
    assert rec.pins[0]["message_id"] == 501
    # No edit on first use.
    assert rec.edits == []
    # The body is the locked format (working marker on, the project name, the ctx %, the gate).
    body = rec.sends[0]["text"]
    assert body.startswith("⚙️ 📁 ")
    assert "🧠 ctx 6%" in body
    assert "🔒 gate" in body
    assert rec.sends[0]["parse_mode"] == "HTML"
    # The id + text are tracked on the chat (the one-pin invariant).
    state = session._chat(1)
    assert state.statusline_message_id == 501
    assert state.statusline_text == body


# --- observability T3: the 🪙 rolling-limit field on the pinned statusline -----


async def test_statusline_renders_limit_pct_when_engine_reports_it():
    # The FOREGROUND engine's limit_status() → a precise "🪙 <pct>%" on the bar (mirrors the
    # ctx-% test). The read is best-effort + foreground-only.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6, limit_status=("approaching", 68))
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1
    body = rec.sends[0]["text"]
    assert "🪙 68%" in body


async def test_statusline_omits_limit_field_when_engine_reports_none():
    # No limit signal (limit_status() → None) → the 🪙 field is OMITTED (never fabricated).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6, limit_status=None)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    body = rec.sends[0]["text"]
    for glyph in ("🪙", "🟢", "🟡", "🔴"):
        assert glyph not in body
    # The ctx field still renders — the omission is the limit field only.
    assert "🧠 ctx 6%" in body


async def test_statusline_limit_read_raises_field_omitted_line_still_built():
    # RB1: a limit_status() that RAISES must omit the field, never break the line (best-effort,
    # off the turn's critical path) — the statusline is still sent with the ctx field intact.
    session = make_session(FakeEngine([]))

    def _boom():
        raise RuntimeError("limit read blew up")

    eng = FakeEngine([], ctx_pct=6, limit_status=_boom)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1, "the line must still be built despite the raising limit read"
    body = rec.sends[0]["text"]
    assert "🧠 ctx 6%" in body
    for glyph in ("🪙", "🟢", "🟡", "🔴"):
        assert glyph not in body


async def test_statusline_limit_field_is_foreground_only(tmp_path):
    # Foreground-only: _maybe_update_statusline SKIPS when ``for_project`` is not the chat's
    # foreground, so a BACKGROUND project's turn (even one whose engine reports a limit) never
    # rewrites the pinned line — and the FOREGROUND project DOES render the 🪙 field. A real
    # store is needed (with store=None every project is implicitly foreground).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "bg", "/work", make_active=False)
    store.create(1, "fg", "/work", make_active=True)  # fg is the active/foreground project
    session = make_session(FakeEngine([]), store=store)
    # Attach a foreground engine reporting an ``ok`` (🟢) limit to the active project's runtime.
    _name, rt = session._active_runtime(1, create_default=False)
    assert rt is not None
    rt.engine = FakeEngine([], ctx_pct=6, limit_status=("ok", None))
    rt.status = "running"
    rec = StatuslineRecorder()
    # A different (non-foreground) project name must be skipped entirely (no send/edit).
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin, for_project="bg",
    )
    assert rec.sends == [] and rec.edits == [], "a background project must not write the bar"
    # The FOREGROUND project DOES render the limit field (the 🟢 badge).
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin, for_project="fg",
    )
    assert len(rec.sends) == 1 and "🟢" in rec.sends[0]["text"]


async def test_update_statusline_second_changed_edits_in_place_no_repin():
    # A SUBSEQUENT update with changed state EDITS in place — no re-pin, no re-send.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    # First update → send + pin.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    # Change state (turn ends → idle; ctx grows to 7) and update again.
    rt = active_rt(session)
    rt.status = "idle"
    eng._ctx_pct = 7
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1, "the second update must NOT re-send (edit in place)"
    assert len(rec.pins) == 1, "the second update must NOT re-pin"
    assert len(rec.edits) == 1, f"the second update must EDIT once, edits={rec.edits!r}"
    edited = rec.edits[0]
    assert edited["message_id"] == 501  # the SAME pinned message is edited
    assert "🧠 ctx 7%" in edited["text"]
    assert not edited["text"].startswith("⚙️")  # idle → no working marker
    assert edited["parse_mode"] == "HTML"
    # The tracked text is the new body.
    assert session._chat(1).statusline_text == edited["text"]


async def test_update_statusline_identical_state_is_no_io():
    # Identical text → skip entirely (no send, no edit — a no-op edit raises "not modified" and
    # wastes a send slot).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    # Nothing changed — a second update must be a pure no-op.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1, "an identical state must not re-send"
    assert rec.edits == [], "an identical state must not edit (no-op skip)"
    assert len(rec.pins) == 1


async def test_update_statusline_edit_failure_resends_and_repins_orphan_recovery():
    # The operator unpinned/deleted the line → the in-place edit raises "message to edit not
    # found". Recovery: clear the dead id, best-effort UNPIN the stale one, re-SEND + re-PIN.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder(fail_edit=True)  # the first edit will raise
    # First update → send (id 501) + pin.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert session._chat(1).statusline_message_id == 501
    # Change state → an EDIT is attempted; it fails → recovery re-sends (id 502) + re-pins.
    active_rt(session).status = "idle"
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 2, f"recovery must re-SEND a fresh line, sends={rec.sends!r}"
    assert len(rec.pins) == 2, f"recovery must re-PIN the fresh line, pins={rec.pins!r}"
    # The stale id (501) was best-effort UNPINNED before re-pinning (one-pin invariant).
    assert {u["message_id"] for u in rec.unpins} == {501}
    # The chat now holds the NEW id (502), and only one.
    assert session._chat(1).statusline_message_id == 502
    assert rec.pins[-1]["message_id"] == 502
    assert rec.pins[-1]["disable_notification"] is True


async def test_update_statusline_pin_failure_is_swallowed_turn_unaffected():
    # ⭐ RB1: a PIN that raises must NEVER escape — _update_statusline returns normally, the
    # line is still sent + tracked (only the bar placement is lost).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder(fail_pin=True)
    # Must NOT raise.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert len(rec.sends) == 1  # the line was still sent
    assert len(rec.pins) == 1  # the pin was attempted (and raised, swallowed)
    # The id is still tracked (the send succeeded), so the next update edits in place.
    assert session._chat(1).statusline_message_id == 501


async def test_update_statusline_send_failure_is_swallowed_turn_unaffected():
    # ⭐ RB1: a SEND that raises must NEVER escape — _update_statusline returns normally and no
    # id is left half-set.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder(fail_send=True)
    # Must NOT raise.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    # The send raised before any id was returned → nothing pinned, nothing tracked.
    assert rec.pins == []
    assert session._chat(1).statusline_message_id is None
    assert session._chat(1).statusline_text is None


async def test_update_statusline_only_one_id_ever_held_across_many_updates():
    # The one-pin invariant: across many state changes, exactly one id is held and only edits
    # happen after the first pin.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=1)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    for pct in (1, 2, 3, 4, 5):
        eng._ctx_pct = pct
        await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)

    assert len(rec.sends) == 1, "only the FIRST update sends; the rest edit"
    assert len(rec.pins) == 1, "only ONE pin ever"
    assert len(rec.edits) == 4, "the 4 changed updates each edit in place"
    # All edits target the single held id.
    assert {e["message_id"] for e in rec.edits} == {501}
    assert session._chat(1).statusline_message_id == 501


async def test_update_statusline_no_foreground_project_is_noop(tmp_path):
    # With no active project (read-only resolve, create_default=False) there is nothing to
    # describe → no I/O, no created runtime.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    assert store.get_active(1) is None  # nothing active yet
    session = make_session(FakeEngine([]), store=store)
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert rec.sends == [] and rec.pins == [] and rec.edits == []
    # The read-only resolve must NOT have created a runtime.
    assert store.get_active(1) is None


async def test_update_statusline_yolo_mode_shows_in_line():
    # The mode field reflects the project's posture: a yolo (allow-all) policy → "🔒 yolo".
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _name, rt = _prime_statusline_project(session, engine=eng, status="running")
    rt.policy.yolo = True
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert "🔒 yolo" in rec.sends[0]["text"]


async def test_update_statusline_plan_armed_shows_in_line():
    # An armed /plan (plan_next) → "🔒 plan" (when not yolo).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _name, rt = _prime_statusline_project(session, engine=eng, status="idle")
    rt.plan_next = True
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert "🔒 plan" in rec.sends[0]["text"]


async def test_update_statusline_ctx_none_when_no_engine_shows_em_dash():
    # No live engine on the runtime → ctx is unknown → "🧠 ctx —" (never a fabricated 0%).
    session = make_session(FakeEngine([]))
    _prime_statusline_project(session, engine=None, status="idle")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert "🧠 ctx —" in rec.sends[0]["text"]
    assert "0%" not in rec.sends[0]["text"]


async def test_update_statusline_engine_ctx_raises_is_swallowed_shows_dash():
    # ⭐ RB1: a context_percentage() that raises is swallowed (the line still renders, ctx —).
    class _BoomCtxEngine(FakeEngine):
        async def context_percentage(self):
            raise RuntimeError("ctx boom")

    session = make_session(FakeEngine([]))
    _prime_statusline_project(session, engine=_BoomCtxEngine([]), status="running")
    rec = StatuslineRecorder()
    # Must NOT raise.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert len(rec.sends) == 1
    assert "🧠 ctx —" in rec.sends[0]["text"]


async def test_update_statusline_edit_and_resend_both_raise_still_swallowed():
    # ⭐ The make-or-break RB1 mutation probe: the in-place edit raises AND the recovery re-send
    # ALSO raises (the chat is fully wedged at the Telegram layer). _update_statusline must STILL
    # return normally — the outermost best-effort guard swallows everything; the turn is
    # unaffected. (Proves no exception can escape via the recovery path either.)
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")

    # First update with a working send/pin to establish the pinned id.
    good = StatuslineRecorder()
    await session._update_statusline(1, send=good.send, edit=good.edit, pin=good.pin, unpin=good.unpin)
    assert session._chat(1).statusline_message_id == 501

    # Now: the edit raises (orphan trigger) AND the re-send raises too.
    async def boom_edit(*, message_id, text, parse_mode=None):
        raise RuntimeError("edit not found")

    async def boom_send(*, text, reply_markup=None, parse_mode=None, **kwargs):
        raise RuntimeError("send failed too")

    async def boom_unpin(*, message_id):
        raise RuntimeError("unpin failed too")

    active_rt(session).status = "idle"  # change state → an edit is attempted
    # Must NOT raise despite every closure failing.
    await session._update_statusline(1, send=boom_send, edit=boom_edit, pin=good.pin, unpin=boom_unpin)
    # The dead id was cleared on the failed-edit path (recovery couldn't re-establish one).
    assert session._chat(1).statusline_message_id is None


# ===========================================================================
# STATUSLINE T-SL-WIRE — the statusline wired into the LIVE turn lifecycle.
#
# These drive REAL turns / commands through the session and assert the pinned line is
# updated at the right moments:
#   * turn START pins the line with the working ⚙️ marker ON; turn END edits it OFF + ctx %;
#   * /switch (the session-level _maybe_update_statusline, for_project=None) rewrites the line
#     to the now-active project; the knob refreshes (/yolo, /effort, /fast) flip the field live;
#   * ⭐ a BACKGROUND turn (a non-active project running) does NOT rewrite the foreground line
#     (the make-or-break foreground-only invariant — mutation probe).
# All triggers are best-effort (a pin/edit failure never breaks the turn).
# ---------------------------------------------------------------------------


class PinRecorder:
    """Captures pin/unpin calls (the statusline's send/edit ride the regular Recorder).

    In a real turn the SAME send/edit closures carry BOTH the turn's output AND the
    statusline, so the integration tests use the regular :class:`Recorder` for send/edit
    (statusline lines are identified by the 📁 glyph) and THIS for pin/unpin.
    """

    def __init__(self):
        self.pins: list[dict] = []
        self.unpins: list[dict] = []

    async def pin(self, *, message_id, disable_notification=None) -> None:
        self.pins.append({"message_id": message_id, "disable_notification": disable_notification})

    async def unpin(self, *, message_id) -> None:
        self.unpins.append({"message_id": message_id})


def _statusline_sends(rec: "Recorder") -> list[dict]:
    """The subset of ``rec.sends`` that are statusline lines (carry the 📁 worktree glyph)."""
    return [s for s in rec.sends if "📁" in (s.get("text") or "")]


def _statusline_edits(rec: "Recorder") -> list[dict]:
    """The subset of ``rec.edits`` that are statusline lines (carry the 📁 worktree glyph)."""
    return [e for e in rec.edits if "📁" in (e.get("text") or "")]


async def test_foreground_turn_pins_at_start_then_refreshes_at_end():
    # ⭐ A FOREGROUND turn: the line is PINNED at turn start with the working ⚙️ marker ON, then
    # EDITED in place at turn end with the marker OFF and the ctx % refreshed. This is the core
    # turn-lifecycle wiring (T7): _drive_turn calls _update_statusline at start + end.
    engine = FakeEngine(
        [
            TextEvent(text="working", incremental=False),
            ResultEvent(session_id="sess-1", is_error=False, subtype="success", result_text="done!"),
        ],
        ctx_pct=12,
    )
    session = make_session(engine)
    # Prime the active project's runtime with the SAME engine so _statusline_text reads ctx 12%.
    name, rt = session._active_runtime(1, create_default=True)
    rt.engine = engine
    rec = Recorder()
    pins = PinRecorder()
    state = session._chat(1)
    await asyncio.wait_for(
        session._drive_turn(
            state, 1, engine, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete,
            pin=pins.pin, unpin=pins.unpin, target=(name, rt),
        ),
        timeout=2.0,
    )
    sl_sends = _statusline_sends(rec)
    sl_edits = _statusline_edits(rec)
    # Turn START: exactly one statusline SEND, PINNED silently, with the working ⚙️ marker ON.
    assert len(sl_sends) == 1, f"turn start must pin the line once, statusline sends={sl_sends!r}"
    assert sl_sends[0]["text"].startswith("⚙️ 📁 "), "turn start → working ⚙️ marker ON"
    assert "🧠 ctx 12%" in sl_sends[0]["text"]
    assert len(pins.pins) == 1 and pins.pins[0]["disable_notification"] is True
    # Turn END: the line is EDITED in place (same pinned id), marker OFF (idle), ctx refreshed.
    assert sl_edits, "turn end must edit the statusline (marker off + ctx refresh)"
    end = sl_edits[-1]
    assert not end["text"].startswith("⚙️"), "turn end → working marker OFF (idle)"
    assert "🧠 ctx 12%" in end["text"]
    assert end["message_id"] == pins.pins[0]["message_id"], "the SAME pinned line is edited"
    # Only ONE pin across the whole turn (the one-pin invariant holds through the lifecycle).
    assert len(pins.pins) == 1


async def test_turn_without_pin_closures_still_runs_no_statusline():
    # Back-compat: a caller that does NOT inject pin/unpin (every pre-T-SL-WIRE path / test)
    # drives the turn normally — the statusline is simply not pinned/edited, the turn is
    # unaffected. (_maybe_update_statusline no-ops when any closure is missing.)
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
        ctx_pct=5,
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit),  # no pin/unpin
        timeout=2.0,
    )
    assert any("ok" in s["text"] for s in rec.sends)  # the turn ran + rendered its result
    assert _statusline_sends(rec) == [], "no pin closures → no statusline send"
    assert _statusline_edits(rec) == []


async def test_background_turn_does_not_rewrite_foreground_statusline(tmp_path):
    # ⭐⭐ THE MAKE-OR-BREAK WIRING INVARIANT (design §3.1): a BACKGROUND turn (alpha runs while
    # BETA is the active/foreground project) must NEVER touch the pinned statusline — the line
    # describes the FOREGROUND project only. _drive_turn gates its start/end statusline triggers
    # on _is_foreground(turn_name); a background turn skips them.
    #
    # MUTATION PROBE: if the turn-start/turn-end triggers were NOT foreground-gated (i.e. a
    # background turn rewrote the line), this test FAILS — the recorder would capture a 📁
    # statusline send/edit/pin for the backgrounded alpha turn.
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    eng_alpha._script = [
        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="bg done"),
    ]
    eng_alpha._ctx_pct = 9
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    pins = PinRecorder()
    state = session._chat(1)
    # Drive ALPHA (a BACKGROUND project — beta is active) to completion WITH pin/unpin wired.
    await asyncio.wait_for(
        session._drive_turn(
            state, 1, eng_alpha, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete,
            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
        ),
        timeout=2.0,
    )
    # The turn RAN as a BACKGROUND turn (its terminal is a "✅ alpha — done" ping, NOT inline
    # result text — the P5 background-notify path; this also confirms it took the background
    # branch, the exact scenario the foreground gate must cover) …
    assert any(s["text"].startswith("✅ alpha — done") for s in rec.sends)
    # … but the foreground statusline was NEVER written — no 📁 send/edit, no pin.
    assert _statusline_sends(rec) == [], "a BACKGROUND turn must NOT pin/send the foreground line"
    assert _statusline_edits(rec) == [], "a BACKGROUND turn must NOT edit the foreground line"
    assert pins.pins == [], "a BACKGROUND turn must NOT pin the foreground line"
    # And no statusline id was established for the chat (nothing was pinned).
    assert session._chat(1).statusline_message_id is None


async def test_foreground_turn_among_two_projects_updates_line(tmp_path):
    # The complement of the background probe: when the RUNNING project IS the foreground (alpha
    # active), its turn DOES pin/refresh the line — so the gate keys on foreground, not on
    # "two projects exist". (Together with the background test this pins the invariant exactly.)
    session, _store, eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="alpha")
    eng_alpha._script = [
        ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="fg done"),
    ]
    eng_alpha._ctx_pct = 4
    rt_alpha = session._chat(1).runtimes["alpha"]
    rec = Recorder()
    pins = PinRecorder()
    state = session._chat(1)
    await asyncio.wait_for(
        session._drive_turn(
            state, 1, eng_alpha, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete,
            pin=pins.pin, unpin=pins.unpin, target=("alpha", rt_alpha),
        ),
        timeout=2.0,
    )
    sl_sends = _statusline_sends(rec)
    assert len(sl_sends) == 1, "the FOREGROUND project's turn pins the line"
    assert "📁 alpha" in sl_sends[0]["text"], "the line names the foreground project (alpha)"
    assert len(pins.pins) == 1


async def test_switch_rewrites_statusline_to_new_project(tmp_path):
    # /switch's session-level refresh (_maybe_update_statusline with for_project=None — the
    # command path is foreground by definition) REWRITES the pinned line for the NOW-active
    # project. Establish a line on alpha, switch active to beta, refresh → the line names beta.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    eng = FakeEngine([], ctx_pct=3)
    session = make_multi_session({str(tmp_path / "a"): eng, str(tmp_path / "b"): eng}, store=store)
    rec = Recorder()
    pins = PinRecorder()
    # First refresh (alpha active) → pin a line naming alpha.
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert _statusline_sends(rec) and "📁 alpha" in _statusline_sends(rec)[0]["text"]
    # /switch → beta is now the active/foreground project; refresh rewrites the SAME line.
    store.switch(1, "beta")
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    sl_edits = _statusline_edits(rec)
    assert sl_edits, "the switch must EDIT the existing pinned line (not re-send)"
    assert "📁 beta" in sl_edits[-1]["text"], "the line now names the switched-to project (beta)"
    assert len(pins.pins) == 1, "switch edits in place — no re-pin"


async def test_yolo_change_flips_mode_on_statusline():
    # /yolo flips the mode field 🔒 gate → 🔒 yolo live (the knob refresh path: set_yolo then
    # _maybe_update_statusline for_project=None).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="idle")
    rec = Recorder()
    pins = PinRecorder()
    # Initial line → 🔒 gate (the fail-closed default).
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
    # /yolo → set_yolo(True) → refresh → 🔒 yolo.
    session.set_yolo(1, True)
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert "🔒 yolo" in _statusline_edits(rec)[-1]["text"]


async def test_effort_change_flips_model_suffix_on_statusline(tmp_path):
    # /effort max → the 🤖 model·effort suffix updates live (set_effort persists, refresh shows
    # ·max). Uses a real store so set_effort persists the override the statusline reads back.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    eng = FakeEngine([], ctx_pct=6)
    session = make_multi_session({str(tmp_path): eng}, store=store)
    rt = session._runtime(1, "alpha", str(tmp_path))
    rt.engine = eng
    rec = Recorder()
    pins = PinRecorder()
    # Initial line (no /effort override) → effort shows the SDK DEFAULT (·high), not ·max
    # (display-only default so the bar always shows the current effort; turns are unchanged).
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    initial = _statusline_sends(rec)[0]["text"]
    assert "·high" in initial and "·max" not in initial
    # /effort max → set_effort persists → refresh → the suffix shows ·max.
    session.set_effort(1, "max")
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert "·max" in _statusline_edits(rec)[-1]["text"], "the 🤖 model·effort suffix flips to ·max"


async def test_fast_model_change_flips_label_on_statusline(tmp_path):
    # /fast → the 🤖 model label flips to the fast model's family label (haiku) live.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    eng = FakeEngine([], ctx_pct=6)
    session = make_multi_session({str(tmp_path): eng}, store=store)
    rt = session._runtime(1, "alpha", str(tmp_path))
    rt.engine = eng
    rec = Recorder()
    pins = PinRecorder()
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    # /fast → set_model to a haiku id → refresh → the label reads "haiku".
    session.set_model(1, "claude-haiku-4-5")
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert "🤖 haiku" in _statusline_edits(rec)[-1]["text"], "/fast → the model label flips to haiku"


async def test_statusline_shows_live_model_not_default_when_unconfigured(tmp_path):
    # When NO per-project override and NO CLAUDE_MODEL is configured, the SDK picks its own
    # model — the bar must show the model the SDK ACTUALLY reported (engine.last_model), e.g.
    # 🤖 opus, NOT the literal word "default".
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    eng = FakeEngine([], ctx_pct=6, last_model="claude-opus-4-8")
    session = make_multi_session({str(tmp_path): eng}, store=store)
    rt = session._runtime(1, "alpha", str(tmp_path))
    rt.engine = eng
    # No model override and (in the test config) no CLAUDE_MODEL → resolver yields None.
    assert session._resolve_project_model(1, "alpha") is None
    rec = Recorder()
    pins = PinRecorder()
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    line = _statusline_sends(rec)[0]["text"]
    assert "🤖 opus" in line, "the live SDK model must be shown"
    assert "default" not in line, "the literal word 'default' must NOT appear"


async def test_statusline_falls_back_to_default_only_when_model_truly_unknown(tmp_path):
    # Belt-and-braces: no override, no CLAUDE_MODEL, AND no live model yet (engine.last_model
    # None) → the bar shows 🤖 default (the honest "we don't know yet" state), never a blank 🤖.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path), make_active=True)
    eng = FakeEngine([], ctx_pct=6, last_model=None)
    session = make_multi_session({str(tmp_path): eng}, store=store)
    rt = session._runtime(1, "alpha", str(tmp_path))
    rt.engine = eng
    rec = Recorder()
    pins = PinRecorder()
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project=None
    )
    assert "🤖 default" in _statusline_sends(rec)[0]["text"]


async def test_maybe_update_statusline_missing_closures_is_noop():
    # The closure-presence gate: if ANY of send/edit/pin/unpin is None (a caller that didn't
    # wire the statusline), _maybe_update_statusline is a pure no-op (the turn is unaffected).
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = Recorder()
    pins = PinRecorder()
    # pin=None → no-op (no send/edit/pin attempted).
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=None, unpin=pins.unpin, for_project=None
    )
    assert rec.sends == [] and rec.edits == [] and pins.pins == []


async def test_maybe_update_statusline_background_gate_is_noop(tmp_path):
    # The foreground gate at the helper level: _maybe_update_statusline with a for_project that
    # is NOT the chat's foreground is a no-op (this is what the turn-start/end triggers rely on).
    session, _store, _eng_alpha, _eng_beta = await make_two_project_session(tmp_path, active="beta")
    rec = Recorder()
    pins = PinRecorder()
    # alpha is NOT foreground (beta is active) → the helper skips.
    await session._maybe_update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin, for_project="alpha"
    )
    assert rec.sends == [] and rec.edits == [] and pins.pins == []


# ===========================================================================
# STATUSLINE T-SL-WIRE — Codex NO_SHIP follow-up fixes (B1/B2/B3 + pin-retry).
# ---------------------------------------------------------------------------


async def test_ctx_percentage_is_awaited_end_to_end_via_async_engine():
    # ⭐ B1 (make-or-break): the statusline body reads the ctx % via an AWAITED async
    # engine.context_percentage(). FakeEngine.context_percentage is now async; if the session
    # ever stopped awaiting it, the line would show "ctx —" and this FAILS. Proves the headline
    # SDK percentage actually reaches the rendered line through the awaited chain.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=37)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert "🧠 ctx 37%" in rec.sends[0]["text"], "the awaited async ctx % must reach the line (B1)"


async def test_statusline_text_is_async_and_awaits_ctx_and_returns_built_for():
    # B1 at the builder level: _statusline_text is a coroutine that awaits the async ctx source.
    # B2: it returns (text, built_for_project) — the project the body describes, for the final
    # pre-write foreground re-check.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=21)
    name, _rt = _prime_statusline_project(session, engine=eng, status="idle")
    built = await session._statusline_text(1)
    assert built is not None
    body, built_for = built
    assert "🧠 ctx 21%" in body
    assert built_for == name  # the (text, built_for) contract — B2


async def test_statusline_text_none_when_no_foreground(tmp_path):
    # No active project → None (the write helpers treat None as "nothing to write").
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    session = make_session(FakeEngine([]), store=store)
    assert await session._statusline_text(1) is None


def _two_project_statusline_session(tmp_path):
    """A real-store session with alpha (active) + beta, each engine ready for a statusline read."""
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    eng = FakeEngine([], ctx_pct=5)
    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
    session = StreamingSession(
        cfg, session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,
    )
    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
    return session, store


async def test_switch_after_snapshot_writes_current_line_not_stale(tmp_path):
    # ⭐⭐ B2 (the foreground-switch race): _update_statusline snapshots the body, THEN the gated
    # send awaits — a /switch in that window must NOT write the stale previous-project line. The
    # fix REBUILDS the body from current state right before the write. We wrap _statusline_text
    # so the SWITCH lands between the snapshot (1st call) and the rebuild (2nd call) — exactly the
    # race window — and assert the line that LANDS names the NEW project (beta), not alpha.
    #
    # MUTATION PROBE: revert the rebuild-after-wait and the SEND carries alpha (the snapshot) →
    # this FAILS (it requires beta, the post-switch foreground).
    session, store = _two_project_statusline_session(tmp_path)
    real_text = session._statusline_text
    calls = {"n": 0}

    async def racing_text(chat_id):
        calls["n"] += 1
        body = await real_text(chat_id)  # 1st call → alpha (the snapshot); 2nd → beta (rebuild)
        if calls["n"] == 1:
            # The snapshot read just returned alpha; a /switch lands BEFORE the rebuild read.
            store.switch(1, "beta")
        return body

    session._statusline_text = racing_text
    rec = Recorder()
    pins = PinRecorder()
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    assert calls["n"] >= 2, "the body must be REBUILT after the snapshot (B2)"
    sl_sends = _statusline_sends(rec)
    assert sl_sends, "the line was sent"
    assert "📁 beta" in sl_sends[0]["text"], "B2: the line names the POST-switch foreground (beta)"
    assert "📁 alpha" not in sl_sends[0]["text"], "B2: never the stale pre-switch project (alpha)"


async def test_switch_after_snapshot_on_edit_writes_current_line(tmp_path):
    # B2 on the EDIT path: an established line, then a /switch between the edit's snapshot and its
    # rebuild → the now-current project (beta) is edited in, never the stale snapshot (alpha).
    session, store = _two_project_statusline_session(tmp_path)
    # alpha starts RUNNING so its first pinned line differs from the idle line the 2nd update
    # builds → the 2nd update reaches the EDIT path (not the identical-text skip).
    session._runtime(1, "alpha", str(tmp_path / "a")).status = "running"
    rec = Recorder()
    pins = PinRecorder()
    # Establish a pinned line on alpha first (no racing wrapper yet).
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
    # Now wrap _statusline_text so a /switch lands between the edit's snapshot and its rebuild.
    real_text = session._statusline_text
    calls = {"n": 0}

    async def racing_text(chat_id):
        calls["n"] += 1
        body = await real_text(chat_id)
        if calls["n"] == 1:
            store.switch(1, "beta")  # switch AFTER the snapshot read, BEFORE the rebuild
        return body

    session._statusline_text = racing_text
    session._runtime(1, "alpha", str(tmp_path / "a")).status = "idle"  # alpha line now differs
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    sl_edits = _statusline_edits(rec)
    assert sl_edits, "an edit happened"
    assert "📁 beta" in sl_edits[-1]["text"], "B2 (edit): the now-current project is written"
    assert "📁 alpha" not in sl_edits[-1]["text"]


class _SwitchDuringCtxEngine(FakeEngine):
    """A FakeEngine whose ASYNC context_percentage() performs a /switch mid-await (the residual

    B2 window Codex flagged): _statusline_text captures the foreground project, THEN awaits
    context_percentage() — this fake switches the store's active project DURING that await, so the
    body built by THAT call is for the OLD (pre-switch) project. The final pre-write foreground
    re-check must then SKIP the stale write.

    ``switch_on_call`` selects WHICH ctx call performs the switch (1-based). _update_statusline
    calls _statusline_text twice — the top-level snapshot (call 1) and the gated-helper REBUILD
    (call 2). To exercise the residual race we switch on the REBUILD call so it captures the old
    project just before its await, then finds itself no-longer-foreground at the guard.
    """

    def __init__(self, *, store, switch_to, switch_on_call=2, **kw):
        super().__init__([], **kw)
        self._store = store
        self._switch_to = switch_to
        self._switch_on_call = switch_on_call
        self._calls = 0

    async def context_percentage(self):
        self._calls += 1
        if self._calls == self._switch_on_call:
            self._store.switch(1, self._switch_to)  # ⭐ /switch lands DURING this ctx await
        return self._ctx_pct


async def test_switch_during_ctx_await_skips_stale_write_send(tmp_path):
    # ⭐⭐ B2 RESIDUAL (Codex's re-opened probe): the B1 ctx-await is itself a /switch window.
    # _statusline_text captures alpha, then awaits context_percentage() — which switches active to
    # beta mid-await — so the rebuilt body is alpha's (built_for="alpha"). The FINAL sync
    # foreground re-check (built_for still foreground?) is now beta → SKIP the stale alpha write.
    #
    # MUTATION PROBE: remove the post-await `_is_foreground(built_for)` guard in
    # _statusline_send_and_pin and this FAILS — the stale alpha line is sent (exactly Codex's bug).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    eng = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
    session = StreamingSession(
        cfg, session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,
    )
    session._runtime(1, "alpha", str(tmp_path / "a")).engine = eng
    session._runtime(1, "beta", str(tmp_path / "b")).engine = eng
    rec = Recorder()
    pins = PinRecorder()
    # First update (no line yet → the send path). alpha is foreground at the snapshot; the ctx
    # await switches active→beta; the rebuilt body is alpha's but built_for="alpha" is no longer
    # foreground → the stale send is SKIPPED.
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    # NO stale alpha line was sent (the residual race is closed).
    assert all("📁 alpha" not in (s.get("text") or "") for s in rec.sends), \
        "B2 residual: a stale alpha line must NOT be sent when /switch lands during the ctx await"
    assert pins.pins == [], "nothing pinned (the stale send was skipped)"
    assert session._chat(1).statusline_message_id is None, "no half-set state from a skipped send"
    # The foreground is now beta (the switch took effect); a SUBSEQUENT update writes beta.
    assert store.get_active(1) == "beta"


async def test_switch_during_ctx_await_skips_stale_write_edit(tmp_path):
    # B2 RESIDUAL on the EDIT path: an established (pinned) line, then a later update whose ctx
    # await switches active→beta → the rebuilt body is alpha's (built_for="alpha", no longer
    # foreground) → the stale EDIT is SKIPPED (the pinned line keeps its last good text).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "a"), make_active=True)
    store.create(1, "beta", str(tmp_path / "b"), make_active=False)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # A plain engine for the FIRST (line-establishing) update; swap in the switching engine after.
    plain = FakeEngine([], ctx_pct=5)
    cfg = make_config(allowed_roots=(str(tmp_path),), allow_any_path=False)
    session = StreamingSession(
        cfg, session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: plain,
        clock=lambda: 0.0,
    )
    rt_alpha = session._runtime(1, "alpha", str(tmp_path / "a"))
    rt_alpha.engine = plain
    rt_alpha.status = "running"  # the first line is the running line
    session._runtime(1, "beta", str(tmp_path / "b")).engine = plain
    rec = Recorder()
    pins = PinRecorder()
    # 1) Establish + pin alpha's line.
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    assert "📁 alpha" in _statusline_sends(rec)[0]["text"]
    pinned_text_before = session._chat(1).statusline_text
    # 2) Now alpha's engine switches active→beta DURING the next update's ctx await; alpha's status
    #    changes so a (stale) edit WOULD be attempted — but the final foreground guard skips it.
    rt_alpha.engine = _SwitchDuringCtxEngine(store=store, switch_to="beta", ctx_pct=5)
    rt_alpha.status = "idle"
    await session._update_statusline(
        1, send=rec.send, edit=rec.edit, pin=pins.pin, unpin=pins.unpin
    )
    # NO stale alpha edit landed (the residual race is closed); the tracked text is unchanged.
    assert all("📁 alpha" not in (e.get("text") or "") for e in _statusline_edits(rec)), \
        "B2 residual (edit): a stale alpha edit must NOT land when /switch hits during ctx await"
    assert session._chat(1).statusline_text == pinned_text_before, "the pinned text is left intact"
    assert store.get_active(1) == "beta"


async def test_plan_turn_shows_plan_mode_while_running_then_gate(tmp_path):
    # ⭐ B3: during an ACTUAL plan-mode turn the line shows 🔒 plan (not 🔒 gate). plan_next is
    # consumed by handle_message BEFORE _drive_turn, so the live flag is in_plan_turn (set at
    # turn start from the consumed plan_turn, cleared at turn end). Turn start → plan; end → gate.
    #
    # MUTATION PROBE: if _statusline_text still read only plan_next (consumed → False), the turn
    # would show 🔒 gate and this FAILS.
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="planned")],
        ctx_pct=8,
    )
    session = make_session(engine)
    name, rt = session._active_runtime(1, create_default=True)
    rt.engine = engine
    rec = Recorder()
    pins = PinRecorder()
    state = session._chat(1)
    # Drive a PLAN turn (plan_turn=True — the value handle_message would pass after consuming
    # the one-shot plan_next).
    await asyncio.wait_for(
        session._drive_turn(
            state, 1, engine, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete,
            pin=pins.pin, unpin=pins.unpin, target=(name, rt), plan_turn=True,
        ),
        timeout=2.0,
    )
    sl_sends = _statusline_sends(rec)
    sl_edits = _statusline_edits(rec)
    # Turn START line → 🔒 plan (the live plan turn).
    assert sl_sends and "🔒 plan" in sl_sends[0]["text"], "B3: a running plan turn shows 🔒 plan"
    # Turn END line → back to 🔒 gate (in_plan_turn cleared; plan_next was already consumed).
    assert sl_edits and "🔒 gate" in sl_edits[-1]["text"], "B3: after the plan turn → 🔒 gate"
    # The live flag is cleared after the turn (no lingering plan mode).
    assert rt.in_plan_turn is False


async def test_non_plan_turn_does_not_show_plan_mode():
    # B3 complement: a NORMAL turn (plan_turn=False) never shows 🔒 plan — it shows 🔒 gate.
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")],
        ctx_pct=8,
    )
    session = make_session(engine)
    name, rt = session._active_runtime(1, create_default=True)
    rt.engine = engine
    rec = Recorder()
    pins = PinRecorder()
    state = session._chat(1)
    await asyncio.wait_for(
        session._drive_turn(
            state, 1, engine, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete,
            pin=pins.pin, unpin=pins.unpin, target=(name, rt),  # plan_turn defaults False
        ),
        timeout=2.0,
    )
    for s in _statusline_sends(rec):
        assert "🔒 plan" not in s["text"]
    assert "🔒 gate" in _statusline_sends(rec)[0]["text"]
    assert rt.in_plan_turn is False


async def test_pin_fails_then_retried_on_next_update():
    # ⭐ Pin-retry (non-blocking): the SEND succeeds but the first PIN raises → the line is sent
    # + tracked but UNPINNED (statusline_pinned False). A later update RETRIES the pin even when
    # the text is unchanged — so a transient pin failure self-heals instead of sticking unpinned.
    #
    # MUTATION PROBE: without the retry, the identical-text skip would short-circuit and the
    # second update would NOT pin (pins stays length 1) → this FAILS.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder(fail_pin_times=1)  # the FIRST pin raises, later pins succeed
    # First update: send ok, pin raises → tracked but not pinned.
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert len(rec.sends) == 1
    assert len(rec.pins) == 1, "the first pin was attempted (and raised)"
    state = session._chat(1)
    assert state.statusline_message_id is not None
    assert state.statusline_pinned is False, "a failed pin leaves the line UNPINNED"
    # Second update with IDENTICAL state: must RETRY the pin (not skip past the unpinned state).
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert len(rec.sends) == 1, "no re-send (the line is already sent)"
    assert len(rec.pins) == 2, "the pin was RETRIED on the next update (pin-retry fix)"
    assert rec.pins[-1]["message_id"] == state.statusline_message_id
    assert state.statusline_pinned is True, "the retry succeeded → now pinned"
    # A THIRD identical update is now a true no-op (pinned + identical → skip).
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert len(rec.pins) == 2, "once pinned, an identical update skips (no needless re-pin)"


async def test_successful_pin_sets_pinned_flag():
    # The happy path of the pin-retry bookkeeping: a successful first pin sets statusline_pinned
    # True so subsequent identical updates correctly skip.
    session = make_session(FakeEngine([]))
    eng = FakeEngine([], ctx_pct=6)
    _prime_statusline_project(session, engine=eng, status="running")
    rec = StatuslineRecorder()
    await session._update_statusline(1, send=rec.send, edit=rec.edit, pin=rec.pin, unpin=rec.unpin)
    assert session._chat(1).statusline_pinned is True
    assert len(rec.pins) == 1


# --- observability T4: the proactive one-time limit warning --------------------
#
# The warning fires at TURN END (in _drive_turn's finally, after the statusline refresh) when the
# foreground engine's limit_status() first crosses into "approaching"/"limited", de-duped per
# limit-window on _ChatState.limit_warned (re-armed when the status returns to "ok"). It is
# foreground/authorized-only (SB1), body-free (SB3), and best-effort (RB1 — never breaks a turn).

#: A fragment unique to the T4 warning line, used to count warnings among the turn's sends.
_WARN_MARK = "Approaching your Claude session limit"


def _warnings(rec) -> list[str]:
    """The warning messages among a Recorder's sends (T4 — identified by the fixed phrase)."""
    return [s["text"] for s in rec.sends if _WARN_MARK in s["text"]]


def _ok_result():
    """A fresh clean ResultEvent script item (one per turn so a re-driven engine has events)."""
    return ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")


async def test_limit_warning_fires_once_on_first_crossing_approaching():
    # WHEN a turn ends with the foreground limit signal in "approaching" and not yet warned →
    # EXACTLY ONE warning is posted, body-free (no request content; the only number is the pct).
    engine = FakeEngine([_ok_result()], limit_status=("approaching", 88))
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    warns = _warnings(rec)
    assert len(warns) == 1, f"exactly one warning expected, got {warns!r}"
    # SB3 body-free: no request content; the prompt "go" must not appear; 🟡 wording + the pct.
    assert "🟡" in warns[0]
    assert "🪙 88%" in warns[0]
    assert "go" not in warns[0]
    # The de-dup flag is armed (this chat won't warn again until the status returns to ok).
    assert session._chat(1).limit_warned is True


async def test_limit_warning_deduped_while_still_approaching():
    # A SECOND turn while STILL "approaching" posts NO new warning (de-dup per limit-window).
    status = ("approaching", 90)
    engine = FakeEngine([_ok_result(), _ok_result()], limit_status=lambda: status)
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "first", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "the first crossing warns once"
    # Second turn, still approaching → no new warning.
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "still approaching → de-duped, no second warning"


async def test_limit_warning_rearms_after_ok_then_warns_again():
    # The re-arm regression: approaching → warn; ok → re-arm (no message); approaching → warn AGAIN.
    box = {"v": ("approaching", 70)}
    engine = FakeEngine(
        [_ok_result(), _ok_result(), _ok_result()], limit_status=lambda: box["v"]
    )
    session = make_session(engine)
    rec = Recorder()
    # Turn 1: approaching → one warning.
    await asyncio.wait_for(
        session.handle_message(1, "t1", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1
    assert session._chat(1).limit_warned is True
    # Turn 2: recovered to ok → the flag re-arms, NO new message.
    box["v"] = ("ok", 10)
    await asyncio.wait_for(
        session.handle_message(1, "t2", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "ok must not warn"
    assert session._chat(1).limit_warned is False, "ok re-arms the de-dup flag"
    # Turn 3: approaching AGAIN → warns again (the re-arm worked).
    box["v"] = ("approaching", 72)
    await asyncio.wait_for(
        session.handle_message(1, "t3", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 2, "a fresh crossing after ok warns again"


async def test_limit_warning_never_when_ok_throughout():
    # status "ok" for the whole turn → NEVER warns (no spurious heads-up).
    engine = FakeEngine([_ok_result()], limit_status=("ok", 20))
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert _warnings(rec) == [], "an ok turn must never warn"
    assert session._chat(1).limit_warned is False


async def test_limit_warning_fires_for_limited_status():
    # status "limited" (🔴) warns once — the harder end of the threshold also triggers the heads-up.
    engine = FakeEngine([_ok_result()], limit_status=("limited", 100))
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    warns = _warnings(rec)
    assert len(warns) == 1
    assert "🔴" in warns[0], "limited uses the 🔴 wording"


async def test_limit_warning_no_signal_no_warning():
    # No limit signal (limit_status() → None) → no warning, flag stays re-armed, turn completes.
    engine = FakeEngine([_ok_result()], limit_status=None)
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert _warnings(rec) == []
    assert session._chat(1).limit_warned is False
    # The turn still completed (the clean result rendered).
    assert any("ok" in s["text"] for s in rec.sends)


async def test_limit_warning_raising_read_swallowed_turn_completes():
    # RB1: a limit_status() that RAISES posts no warning and NEVER breaks the turn (the result
    # still renders); the de-dup flag is untouched by the failed read.
    def _boom():
        raise RuntimeError("limit read blew up")

    engine = FakeEngine([_ok_result()], limit_status=_boom)
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert _warnings(rec) == [], "a raising read posts no warning"
    assert any("ok" in s["text"] for s in rec.sends), "the turn still completed (RB1)"


async def test_limit_warning_background_turn_does_not_warn_foreground(tmp_path):
    # SB1 + foreground-only: a BACKGROUND project's turn (even one whose engine reports
    # "approaching") must NOT warn the foreground chat, and must not arm the foreground's flag.
    # A real store is needed — with store=None every project is implicitly foreground.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "fg", "/work", make_active=True)  # fg is the active/foreground project
    store.create(1, "bg", "/work", make_active=False)
    # Drive the BACKGROUND project ("bg") directly via _drive_turn (handle_message would pin the
    # active project; we want the background turn's exact end-of-turn warning path).
    engine = FakeEngine([_ok_result()], limit_status=("approaching", 95))
    session = make_session(engine, store=store)
    _bg_name, bg_rt = session._override_runtime(1, "bg")
    bg_rt.engine = engine
    rec = Recorder()
    await asyncio.wait_for(
        session._drive_turn(
            session._chat(1), 1, engine, "go",
            send=rec.send, edit=rec.edit, target=("bg", bg_rt),
        ),
        timeout=2.0,
    )
    assert _warnings(rec) == [], "a background turn must not warn the foreground"
    assert session._chat(1).limit_warned is False, "the foreground's de-dup flag is untouched"


async def test_limit_warning_foreground_turn_warns_with_store(tmp_path):
    # The companion to the background test: the FOREGROUND turn DOES warn (so the background
    # skip above is genuinely the foreground gate, not a store/wiring artifact).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "fg", "/work", make_active=True)
    engine = FakeEngine([_ok_result()], limit_status=("approaching", 80))
    session = make_session(engine, store=store)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "the foreground turn warns once"
    assert session._chat(1).limit_warned is True


async def test_limit_warning_unknown_status_is_non_event_when_armed():
    # The most important gap: an UNRECOGNIZED status ("throttled") at turn end is a true
    # non-event — it must NOT wrongly RE-ARM. Starting warned=True → stays True (and no message).
    # A mutation that fell through to clearing the flag on unknown status fails this.
    engine = FakeEngine([_ok_result()], limit_status=("throttled", None))
    session = make_session(engine)
    session._chat(1).limit_warned = True  # already warned this window
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert _warnings(rec) == [], "an unknown status must not warn"
    assert session._chat(1).limit_warned is True, "an unknown status must not re-arm"


async def test_limit_warning_unknown_status_is_non_event_when_unarmed():
    # The other half: an UNRECOGNIZED status starting warned=False → stays False (and no message).
    # A mutation that fell through to SETTING the flag (or warning) on unknown status fails this.
    engine = FakeEngine([_ok_result()], limit_status=("throttled", None))
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert _warnings(rec) == [], "an unknown status must not warn"
    assert session._chat(1).limit_warned is False, "an unknown status must not set the flag"


async def test_limit_warning_send_failure_swallowed_and_rewarns():
    # RB1 + re-warn: the warning's send RAISES on an "approaching" turn → swallowed (turn
    # completes, no crash) AND limit_warned stays False, so the NEXT approaching turn warns again.
    class _WarnFailRecorder(Recorder):
        """A Recorder that raises on the T4 warning send (only), the first time it's attempted."""

        def __init__(self):
            super().__init__()
            self._fail_warn_once = True

        async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs):
            if self._fail_warn_once and _WARN_MARK in text:
                self._fail_warn_once = False
                raise RuntimeError("Telegram error: warning send failed")
            return await super().send(text=text, reply_markup=reply_markup,
                                      parse_mode=parse_mode, **kwargs)

    engine = FakeEngine([_ok_result(), _ok_result()], limit_status=("approaching", 85))
    session = make_session(engine)
    rec = _WarnFailRecorder()
    # Turn 1: the warning send RAISES — swallowed (RB1); the turn still completes.
    await asyncio.wait_for(
        session.handle_message(1, "first", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert any("ok" in s["text"] for s in rec.sends), "the turn completed despite the failed warn"
    assert session._chat(1).limit_warned is False, (
        "a FAILED warning must leave the flag re-armed (never swallow the only heads-up)"
    )
    # Turn 2: still approaching, and now the send succeeds → it warns AGAIN.
    await asyncio.wait_for(
        session.handle_message(1, "second", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "the re-armed warning fires on the next approaching turn"
    assert session._chat(1).limit_warned is True


async def test_limit_warning_escalation_approaching_to_limited_stays_silent():
    # Escalation stays silent: approaching (warns, sets flag) → next turn "limited" while already
    # warned → NO second warning (one heads-up per non-ok window, intended — no per-status re-warn).
    box = {"v": ("approaching", 78)}
    engine = FakeEngine([_ok_result(), _ok_result()], limit_status=lambda: box["v"])
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "t1", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "approaching warns once"
    # Escalate to limited while still in the same non-ok window (already warned) → silent.
    box["v"] = ("limited", 100)
    await asyncio.wait_for(
        session.handle_message(1, "t2", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "escalation approaching→limited must not re-warn"


async def test_limit_warning_rearms_after_limited_then_ok_then_approaching():
    # Re-arm after LIMITED (not just after approaching): limited (warns) → ok (re-arm) →
    # approaching → warns again.
    box = {"v": ("limited", 100)}
    engine = FakeEngine(
        [_ok_result(), _ok_result(), _ok_result()], limit_status=lambda: box["v"]
    )
    session = make_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "t1", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1 and "🔴" in _warnings(rec)[0], "limited warns once (🔴)"
    assert session._chat(1).limit_warned is True
    # Recover to ok → re-arm, no message.
    box["v"] = ("ok", 5)
    await asyncio.wait_for(
        session.handle_message(1, "t2", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 1, "ok must not warn"
    assert session._chat(1).limit_warned is False, "ok after limited re-arms the flag"
    # Approaching again → warns again (the re-arm after limited worked).
    box["v"] = ("approaching", 81)
    await asyncio.wait_for(
        session.handle_message(1, "t3", send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert len(_warnings(rec)) == 2, "a fresh crossing after limited→ok warns again"


async def test_limit_warning_no_engine_turn_rearms():
    # The documented worst-case "extra heads-up" path: a turn whose runtime has NO engine at end
    # → no warning AND the flag is cleared (re-armed). Drive _drive_turn directly with a runtime
    # whose engine is None (handle_message would attach one); the engine arg only feeds the stream.
    engine = FakeEngine([_ok_result()])
    session = make_session(engine)
    name, rt = session._active_runtime(1, create_default=True)
    rt.engine = None  # the runtime carries NO engine for the warning's foreground read
    session._chat(1).limit_warned = True  # was warned in a prior window
    rec = Recorder()
    await asyncio.wait_for(
        session._drive_turn(
            session._chat(1), 1, engine, "go",
            send=rec.send, edit=rec.edit, target=(name, rt),
        ),
        timeout=2.0,
    )
    assert _warnings(rec) == [], "no engine → no warning"
    assert session._chat(1).limit_warned is False, "a no-engine turn clears (re-arms) the flag"


# ---------------------------------------------------------------------------
# observability T5 — the live activity line (ActivityMixin)
#
# A TRANSIENT message showing "what's running right now" (the current tool + active-subagent
# type-names, ⚙️), POSTED on first foreground activity, EDITED in place as activity changes
# (throttled — skip-identical + ≲1 edit/sec, never a new message per change), and REMOVED at turn
# end (no lingering ⚙️; NOT a per-turn "done" footer). Foreground-only (SB1), body-free (SB3 —
# names only), best-effort (RB1 — never breaks a turn). Mock-only, like the rest of this file.
# ---------------------------------------------------------------------------


def _snap(tool=None, subagents=()):
    """An ActivitySnapshot (the engine.last_activity() shape — names only, SB3-clean)."""
    from claude_tg.engine.adapter_sdk import ActivitySnapshot

    return ActivitySnapshot(current_tool=tool, subagents=tuple(subagents))


def _advancing_clock(step=10.0):
    """A monotonic clock that ADVANCES ``step`` seconds on each call (past the throttle interval).

    Used so a deterministic time-throttle test can let successive edits THROUGH (step ≫ 1 s) — and
    its companion ``_frozen_clock`` (0.0) coalesces them. No real time is consumed."""
    box = {"t": 0.0}

    def now():
        box["t"] += step
        return box["t"]

    return now


# --- _render_activity (pure) -------------------------------------------------


def test_render_activity_none_snapshot_is_none():
    # No activity → nothing to show (the caller removes/skips).
    assert StreamingSession._render_activity(None) is None


def test_render_activity_tool_only():
    assert StreamingSession._render_activity(_snap(tool="Bash")) == "⚙️ Bash"


def test_render_activity_tool_and_subagents():
    line = StreamingSession._render_activity(
        _snap(tool="Bash", subagents=("Explore", "general-purpose"))
    )
    assert line == "⚙️ Explore, general-purpose · Bash"


def test_render_activity_subagents_only_no_tool():
    assert StreamingSession._render_activity(_snap(subagents=("Explore",))) == "⚙️ Explore"


def test_render_activity_many_subagents_collapse_to_count():
    # > 3 subagents → a COUNT instead of a wall of names (still names-free of args either way).
    line = StreamingSession._render_activity(
        _snap(tool="Bash", subagents=("a", "b", "c", "d", "e"))
    )
    assert line == "⚙️ 5 agents · Bash"


def test_render_activity_empty_snapshot_is_none():
    # A defensively-empty snapshot (no tool, no subagents) renders nothing.
    assert StreamingSession._render_activity(_snap()) is None


def test_render_activity_is_names_only_sb3():
    # SB3: even if a tool/subagent name arrived from a secret-laden upstream input, the snapshot is
    # names-only by construction and the render carries ONLY those names — HTML-escaped once, no
    # args/paths/prompt. We drive a "name" that LOOKS like it could carry junk and assert the line
    # contains the (escaped) name and nothing resembling a body/arg.
    line = StreamingSession._render_activity(
        _snap(tool="Bash", subagents=("general-purpose",))
    )
    assert line == "⚙️ general-purpose · Bash"
    # No raw '<'/'>' (HTML-escaped) and none of the body-shaped tokens an arg would carry.
    for forbidden in ("<", ">", "command=", "/Users/", "prompt", "secret", "--"):
        assert forbidden not in line


def test_render_activity_html_escapes_names_once():
    # A name containing HTML-significant chars is escaped exactly once (parse_mode="HTML" safety).
    line = StreamingSession._render_activity(_snap(tool="a<b>&c"))
    assert line == "⚙️ a&lt;b&gt;&amp;c"


# --- _maybe_update_activity: post once, edit in place, throttle, RB1 ---------


async def test_activity_posts_one_message_on_first_activity():
    # First foreground activity → EXACTLY ONE send (the line is posted), no edit.
    box = {"v": _snap(tool="Bash")}
    eng = FakeEngine([], last_activity=lambda: box["v"])
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1, f"first activity must POST exactly once, sends={rec.sends!r}"
    assert rec.edits == [], "no edit on the first post"
    assert rec.sends[0]["text"] == "⚙️ Bash"
    assert rec.sends[0]["parse_mode"] == "HTML"
    assert session._chat(1).activity_message_id == 101
    assert session._chat(1).activity_text == "⚙️ Bash"


async def test_activity_change_edits_same_message_not_a_new_send():
    # A subsequent CHANGE EDITS the same message (an edit op), NOT a second send. The clock must
    # advance past the throttle so the change is allowed through (not coalesced).
    box = {"v": _snap(tool="Bash")}
    eng = FakeEngine([], last_activity=lambda: box["v"])
    session = make_session(eng, clock=_advancing_clock())
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1 and rec.edits == []
    # Activity changes (a new tool) → EDIT in place, NOT a new send.
    box["v"] = _snap(tool="Grep")
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1, "a change must EDIT, never a second send"
    assert len(rec.edits) == 1, "the change is an edit op"
    assert rec.edits[0]["message_id"] == 101, "the SAME message is edited in place"
    assert rec.edits[0]["text"] == "⚙️ Grep"
    assert session._chat(1).activity_text == "⚙️ Grep"


async def test_activity_skip_identical_no_edit():
    # An UNCHANGED snapshot → no edit (skip-identical: a no-op Telegram edit raises + wastes a slot).
    box = {"v": _snap(tool="Bash")}
    eng = FakeEngine([], last_activity=lambda: box["v"])
    session = make_session(eng, clock=_advancing_clock())
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1
    # Same snapshot again → no edit (and no new send).
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1 and rec.edits == [], "an unchanged snapshot triggers no edit"


async def test_activity_time_throttle_coalesces_rapid_changes():
    # Rapid successive CHANGES within the throttle interval are COALESCED — the edit count is
    # BOUNDED, not one-per-change. With a FROZEN clock (0.0) every edit lands inside the 1 s window
    # after the first post, so all post-first changes are skipped (the strongest coalescing).
    box = {"v": _snap(tool="Bash")}
    eng = FakeEngine([], last_activity=lambda: box["v"])
    session = make_session(eng, clock=lambda: 0.0)  # frozen → every change inside the interval
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1
    # Five rapid distinct changes, all within the throttle window → coalesced to ZERO edits.
    for tool in ("Grep", "Read", "Edit", "Write", "Glob"):
        box["v"] = _snap(tool=tool)
        await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert len(rec.sends) == 1, "no extra sends from the burst"
    assert len(rec.edits) <= 1, f"rapid changes must coalesce (bounded edits), got {rec.edits!r}"
    # The in-memory text was NOT advanced by a throttled skip (it still reflects the FIRST post,
    # "⚙️ Bash"), so the next change PAST the interval still shows the latest state. Advance the
    # clock and change to a DIFFERENT tool than the first post.
    assert session._chat(1).activity_text == "⚙️ Bash", "a throttled skip never advanced the stored text"
    session._clock = _advancing_clock()
    box["v"] = _snap(tool="Glob")  # the latest state, distinct from the first post
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert any(e["text"] == "⚙️ Glob" for e in rec.edits), "the next change past the interval shows the latest state"


async def test_activity_idle_render_skips_no_write():
    # last_activity() → None (idle) → nothing posted/edited (removal is the finalize's job).
    eng = FakeEngine([], last_activity=None)
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert rec.sends == [] and rec.edits == [], "idle activity writes nothing"
    assert session._chat(1).activity_message_id is None


async def test_activity_raising_last_activity_swallowed():
    # RB1: a last_activity() that RAISES posts nothing and never breaks (the caller swallows).
    def _boom():
        raise RuntimeError("activity read blew up")

    eng = FakeEngine([], last_activity=_boom)
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    # Must not raise.
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert rec.sends == [] and rec.edits == []


async def test_activity_raising_send_swallowed():
    # RB1: a raising SEND is swallowed (the whole update is best-effort) — no exception escapes.
    class BoomSend(Recorder):
        async def send(self, *, text, reply_markup=None, parse_mode=None, **kwargs) -> int:
            raise RuntimeError("Telegram send failed")

    eng = FakeEngine([], last_activity=lambda: _snap(tool="Bash"))
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = BoomSend()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    # No id was stored (the send raised before returning one).
    assert session._chat(1).activity_message_id is None


async def test_activity_missing_closures_is_noop():
    # No send/edit closures injected (a caller/test that didn't wire them) → no-op, no raise.
    eng = FakeEngine([], last_activity=lambda: _snap(tool="Bash"))
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    await session._maybe_update_activity(1, send=None, edit=None, for_project=None)
    assert session._chat(1).activity_message_id is None


# --- _finalize_activity: remove at turn end ----------------------------------


async def test_finalize_activity_deletes_and_clears():
    # At turn end the transient line is DELETED and its id cleared.
    eng = FakeEngine([], last_activity=lambda: _snap(tool="Bash"))
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    assert session._chat(1).activity_message_id == 101
    await session._finalize_activity(1, delete=rec.delete)
    assert len(rec.deletes) == 1 and rec.deletes[0]["message_id"] == 101
    assert session._chat(1).activity_message_id is None
    assert session._chat(1).activity_text is None


async def test_finalize_activity_raising_delete_swallowed_state_cleared():
    # RB1: a raising delete is swallowed AND the state is cleared regardless (no stale id leaks).
    class BoomDelete(Recorder):
        async def delete(self, *, message_id) -> None:
            raise RuntimeError("Telegram delete failed")

    eng = FakeEngine([], last_activity=lambda: _snap(tool="Bash"))
    session = make_session(eng)
    _name, rt = session._active_runtime(1, create_default=True)
    rt.engine = eng
    rec = BoomDelete()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project=None)
    await session._finalize_activity(1, delete=rec.delete)  # must not raise
    assert session._chat(1).activity_message_id is None, "a failed delete still clears the id"


# --- end-to-end through a turn: posts, then collapses/removes at turn end -----


async def test_activity_line_posted_during_turn_and_removed_at_end():
    # A FOREGROUND turn that emits a tool_use posts the activity line during the turn (its engine's
    # last_activity() reports the tool), then REMOVES it at turn end (delete + id cleared). No
    # lingering ⚙️, no per-turn "done" footer.
    eng = FakeEngine(
        [
            ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)"),
            ResultEvent(session_id="sess-1", is_error=False, subtype="success", result_text="done!"),
        ],
        last_activity=lambda: _snap(tool="Bash"),
    )
    session = make_session(eng)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete),
        timeout=2.0,
    )
    # The activity line was posted (⚙️ Bash among the sends).
    assert any(s["text"] == "⚙️ Bash" for s in rec.sends), "the activity line was posted during the turn"
    # And removed at turn end: its id is cleared, and a delete was issued for it.
    assert session._chat(1).activity_message_id is None, "the activity line id is cleared at turn end"
    # No lingering ⚙️ activity message text persists as the final state (the id is gone).
    # (The statusline ⚙️ working-marker is a SEPARATE pinned line; the transient activity line is
    # identified by its body "⚙️ <tool>" with no statusline fields like 🧠/🔒.)
    assert session._chat(1).activity_text is None


async def test_activity_line_foreground_only_background_turn_does_not_post(tmp_path):
    # Foreground-only: a BACKGROUND project's turn must NOT post/edit the FOREGROUND activity line.
    # A real store is needed (with store=None every project is implicitly foreground).
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "fg", "/work", make_active=True)  # fg is foreground
    store.create(1, "bg", "/work", make_active=False)
    eng = FakeEngine(
        [
            ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)"),
            ResultEvent(session_id="sess-1", is_error=False, subtype="success", result_text="done!"),
        ],
        last_activity=lambda: _snap(tool="Bash"),
    )
    session = make_session(eng, store=store)
    _bg_name, bg_rt = session._override_runtime(1, "bg")
    bg_rt.engine = eng
    rec = Recorder()
    await asyncio.wait_for(
        session._drive_turn(
            session._chat(1), 1, eng, "go",
            send=rec.send, edit=rec.edit, delete=rec.delete, target=("bg", bg_rt),
        ),
        timeout=2.0,
    )
    # No activity line was posted for the foreground chat (the background turn is silent).
    assert not any(s["text"] == "⚙️ Bash" for s in rec.sends), "a background turn must not post the activity line"
    assert session._chat(1).activity_message_id is None


# --- B2 regression lock: the SYNC foreground re-check before the raw send/edit -----
#
# These two tests PIN the make-or-break B2 guard in _activity_send (activity.py:_activity_send)
# and _activity_edit: the gate wait (awaited via the injected _sleep) is a /switch window, so
# immediately before the raw send/edit there is a SYNCHRONOUS _is_foreground re-check with NO
# await between it and the write. If a /switch happened during the wait, the stale write is
# DROPPED. The earlier foreground-only tests don't catch a DELETION of this sync re-check (they
# use for_project=None, or wait==0 so _sleep never runs). Here the injected _sleep FLIPS the
# chat's foreground away mid-wait, so removing the re-check at _activity_send/_activity_edit
# would let the stale write through and FAIL these assertions.


def _b2_session(store, *, on_sleep):
    """A StreamingSession with a real interval + frozen clock + an injected sleep hook (B2 lock).

    ``on_sleep(delay)`` is invoked from inside the awaited gate wait (the /switch window) so a
    test can flip the chat's foreground away DURING the wait — exercising the sync re-check that
    runs AFTER the sleep, immediately before the raw send/edit (no await between)."""
    eng = FakeEngine([], last_activity=lambda: _snap(tool="Bash"))

    async def _sleep(delay: float) -> None:
        on_sleep(delay)

    return StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,            # frozen → the gate's wait is deterministic
        chat_send_interval=5.0,       # a real interval so a pre-primed gate returns wait > 0
        sleep=_sleep,                 # the injected awaited wait = the /switch window
    ), eng


async def test_activity_send_b2_switch_during_gate_wait_drops_stale_post(tmp_path):
    # B2 (POST path): a /switch DURING the gate wait → the stale post is DROPPED by the sync
    # foreground re-check. Deleting that re-check would send the line to the now-stale foreground.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "fg", "/work", make_active=True)   # "fg" is foreground at call time
    store.create(1, "other", "/work", make_active=False)

    flips: list[float] = []

    def _flip_foreground_away(delay: float) -> None:
        # During the awaited gate wait, /switch away from "fg" so the sync re-check (after the
        # sleep, before the raw send) sees "fg" is no longer foreground.
        flips.append(delay)
        store.switch(1, "other")

    session, eng = _b2_session(store, on_sleep=_flip_foreground_away)
    _name, rt = session._override_runtime(1, "fg")
    rt.engine = eng
    rec = Recorder()
    # Pre-prime the gate so the activity write's reserve(verbatim=False) returns wait > 0 (so
    # _sleep — and thus the mid-wait /switch — actually runs). On a frozen clock a fresh gate's
    # first reserve is 0 (leading edge); one prior reservation pushes the tail to +interval.
    session._gate(session._chat(1)).reserve(verbatim=False)

    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project="fg")

    assert flips, "the gate wait (the /switch window) must have been awaited (wait > 0)"
    assert rec.sends == [], "a /switch during the gate wait must DROP the stale post (B2)"
    assert session._chat(1).activity_message_id is None, "no id stored for a dropped post"


async def test_activity_edit_b2_switch_during_gate_wait_drops_stale_edit(tmp_path):
    # B2 (EDIT path): with the line already posted, a /switch DURING the gate wait of a later
    # CHANGE → the stale edit is DROPPED by the sync re-check. Deleting that re-check would edit
    # the line for the now-stale foreground.
    from claude_tg.session_store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "fg", "/work", make_active=True)
    store.create(1, "other", "/work", make_active=False)

    # First, post the activity line cleanly while "fg" stays foreground (a no-op sleep). Use a
    # mutable box so the snapshot can change for the second call (a genuine CHANGE → edit path).
    box = {"v": _snap(tool="Bash")}
    eng = FakeEngine([], last_activity=lambda: box["v"])

    async def _noop_sleep(delay: float) -> None:
        return None

    session = StreamingSession(
        make_config(),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: eng,
        clock=lambda: 0.0,            # frozen → the gate's wait is deterministic
        chat_send_interval=5.0,       # a real interval so the change's reserve returns wait > 0
        sleep=_noop_sleep,
    )
    _name, rt = session._override_runtime(1, "fg")
    rt.engine = eng
    rec = Recorder()
    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project="fg")
    assert len(rec.sends) == 1 and session._chat(1).activity_message_id is not None, "line posted"

    # Now arm the mid-wait /switch and drive a CHANGE that must take the edit path. On the frozen
    # clock the post advanced the gate tail to slot 0, so the change's reserve(verbatim=False)
    # lands at +interval → wait > 0 → the injected _sleep (the /switch window) runs. Reset the
    # throttle ts so the change isn't coalesced by the time-throttle (frozen clock → now==last).
    flips: list[float] = []

    async def _flip_sleep(delay: float) -> None:
        flips.append(delay)
        store.switch(1, "other")  # /switch away from "fg" during the gate wait

    session._sleep = _flip_sleep
    session._chat(1).activity_last_edit_ts = -100.0  # past the throttle → the change reaches the gate
    box["v"] = _snap(tool="Grep")  # a genuine change → the edit path

    await session._maybe_update_activity(1, send=rec.send, edit=rec.edit, for_project="fg")

    assert flips, "the gate wait (the /switch window) must have been awaited (wait > 0)"
    assert rec.edits == [], "a /switch during the gate wait must DROP the stale edit (B2)"
    # The post-edit state was NOT advanced (the dropped edit never recorded the new body).
    assert session._chat(1).activity_text == "⚙️ Bash", "a dropped edit leaves the shown text unchanged"
