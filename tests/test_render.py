"""Unit tests for the render layer (T6) — PURE logic, NO Telegram network.

Covers, per the T6 acceptance criteria:

* Event -> RenderAction: ask/plan/error/result render **verbatim** (full text,
  chunked via ``split_message`` when long); tool_use/status/incremental-text
  coalesce to the edit-in-place **status line**.
* Inline keyboards: ask = one button per option + "Other"; plan = Approve + Reject;
  ``callback_data`` round-trips (encode->decode) AND is **<=64 bytes** with a
  real-length UUID ``tool_use_id`` and max options; ``decode_callback`` rejects
  malformed/foreign data (returns ``None``) — feeds SB1 at T7.
* Coalesce / throttle (RB5): a burst of N incremental text deltas within one interval
  produces a BOUNDED number of edit actions (not N); verbatim events flush
  immediately; driven by an **injected clock** (no real sleep).
* Long plan/text chunked to <=4096 UTF-16 via ``split_message`` (lossless).

These tests construct :mod:`claude_tg.engine.types` events directly and never open a
session, import the SDK, or touch Telegram/network. They build
``InlineKeyboardMarkup`` objects in-process (python-telegram-bot is a pure dependency;
no bot, no token, no I/O).
"""

from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup

from claude_tg.engine.types import (
    AskEvent,
    ErrorEvent,
    PermissionEvent,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.render import (
    CALLBACK_LIMIT,
    KIND_PERMISSION,
    Coalescer,
    RenderAction,
    answers_from_ask,
    ask_keyboard,
    coalesce_stream,
    decode_callback,
    encode_callback,
    permission_keyboard,
    plan_keyboard,
    render_event,
    yolo_banner,
    yolo_indicator,
)

# A realistic SDK tool_use_id: the ``toolu_`` prefix + a UUID-shaped body (worst case
# for the callback byte budget).
REAL_TOOL_USE_ID = "toolu_01a2b3c4d5e6f7a8b9c0d1e2f3a4"  # ~36 chars
UUID_TOOL_USE_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"  # canonical 36-char UUID


# --- a deterministic, injectable clock --------------------------------------


class FakeClock:
    """A controllable monotonic clock for the Coalescer (no real time)."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


# --- builders ---------------------------------------------------------------


def make_ask(n_options: int = 2, *, tool_use_id: str = REAL_TOOL_USE_ID, n_questions: int = 1) -> AskEvent:
    questions = []
    for q in range(n_questions):
        questions.append(
            {
                "question": f"Question {q}?",
                "header": f"H{q}",
                "options": [
                    {"label": f"Option {q}-{i}", "description": f"desc {i}"}
                    for i in range(n_options)
                ],
                "multiSelect": False,
            }
        )
    return AskEvent(questions=questions, tool_use_id=tool_use_id, session_id="s1")


def make_permission(
    *,
    tool_name: str = "Bash",
    tool_input_summary: str = "Bash(command=pytest -q)",
    tool_use_id: str = REAL_TOOL_USE_ID,
) -> PermissionEvent:
    return PermissionEvent(
        tool_name=tool_name,
        tool_input_summary=tool_input_summary,
        tool_use_id=tool_use_id,
        session_id="s1",
    )


# ============================================================================
# Event -> RenderAction mapping
# ============================================================================


def test_ask_renders_verbatim_new_message_with_keyboard():
    action = render_event(make_ask(n_options=3))
    assert action.op == "new"
    assert action.verbatim is True
    assert isinstance(action.reply_markup, InlineKeyboardMarkup)
    # Question text is shown verbatim in the body.
    assert "Question 0?" in action.text


def test_plan_renders_verbatim_new_message_with_approve_reject():
    plan = PlanEvent(plan="Step 1\nStep 2", tool_use_id=REAL_TOOL_USE_ID)
    action = render_event(plan)
    assert action.op == "new"
    assert action.verbatim is True
    assert "Step 1\nStep 2" in action.text
    assert isinstance(action.reply_markup, InlineKeyboardMarkup)


def test_permission_renders_verbatim_new_message_with_three_buttons():
    action = render_event(make_permission(tool_name="Bash"))
    assert action.op == "new"
    assert action.verbatim is True
    assert isinstance(action.reply_markup, InlineKeyboardMarkup)
    # The tool name + the (body-free) summary are shown verbatim in the prompt body.
    assert "Bash" in action.text
    assert "Bash(command=pytest -q)" in action.text
    # Three verdict buttons.
    buttons = _all_buttons(action.reply_markup)
    assert len(buttons) == 3
    labels = [b.text for b in buttons]
    assert any("Allow once" in label for label in labels)
    assert any("session" in label for label in labels)
    assert any("Deny" in label for label in labels)


def test_permission_buttons_round_trip_to_once_session_deny():
    action = render_event(make_permission())
    assert action.reply_markup is not None
    decoded = [decode_callback(b.callback_data) for b in _all_buttons(action.reply_markup)]
    assert all(cb is not None and cb.kind == "permission" for cb in decoded)
    assert all(cb.tool_use_id == REAL_TOOL_USE_ID for cb in decoded if cb)
    actions = {cb.permission_action for cb in decoded if cb}
    assert actions == {"once", "session", "deny"}


def test_permission_render_is_body_free_sb3():
    # SB3: the summary is already lengths-not-bodies; render.py must NOT expand it.
    # A Write whose 600-char content collapsed to "content=<600 chars>" must show that
    # marker and NOT the raw 600-char body.
    raw_body = "S3CR3T-" + "x" * 593  # 600 chars of "content" the operator must not see
    assert len(raw_body) == 600
    summary = "Write(file_path=/tmp/secret.txt, content=<600 chars>)"
    action = render_event(
        make_permission(tool_name="Write", tool_input_summary=summary)
    )
    assert "content=<600 chars>" in action.text  # the safe marker is present
    assert raw_body not in action.text  # the raw body is absent (SB3)
    assert "S3CR3T" not in action.text


def test_permission_keyboard_requires_tool_use_id():
    # PermissionEvent.tool_use_id is non-optional in the type, but the keyboard guards
    # an empty id (nothing to route a verdict to) the same way ask/plan do.
    ev = PermissionEvent(
        tool_name="Bash", tool_input_summary="Bash(command=ls)", tool_use_id=""
    )
    with pytest.raises(ValueError):
        permission_keyboard(ev)


def test_error_renders_verbatim_no_keyboard():
    err = ErrorEvent(kind_of_error="tool_error", message="boom")
    action = render_event(err)
    assert action.op == "new"
    assert action.verbatim is True
    assert "boom" in action.text
    assert action.reply_markup is None


def test_result_with_text_renders_verbatim_final_answer():
    res = ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="The answer is 42")
    action = render_event(res)
    assert action.op == "new"
    assert action.verbatim is True
    assert "The answer is 42" in action.text


def test_result_without_text_renders_compact_footer():
    res = ResultEvent(
        session_id="s1", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.0123
    )
    action = render_event(res)
    assert action.op == "new"
    assert "done" in action.text and "3 turns" in action.text


def test_assembled_text_is_new_message_verbatim():
    action = render_event(TextEvent(text="Here is my full reply.", incremental=False))
    assert action.op == "new"
    assert action.verbatim is True
    assert action.text == "Here is my full reply."


def test_incremental_text_coalesces_to_status_line():
    action = render_event(TextEvent(text="tok", incremental=True))
    assert action.op == "edit_status"
    assert action.verbatim is False
    assert action.text == "tok"


def test_empty_incremental_text_is_none():
    assert render_event(TextEvent(text="", incremental=True)).op == "none"


def test_empty_assembled_text_is_none():
    assert render_event(TextEvent(text="", incremental=False)).op == "none"


def test_tool_use_is_one_liner_status_using_safe_summary():
    # SB3: the one-liner uses the event's tool_input_summary (lengths-not-bodies),
    # NEVER raw input — render.py must not re-derive a summary.
    ev = ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=pytest -q)")
    action = render_event(ev)
    assert action.op == "edit_status"
    assert action.verbatim is False
    assert "Bash(command=pytest -q)" in action.text


def test_status_is_one_liner_status():
    action = render_event(StatusEvent(phase="rate_limit", detail="retry in 5s"))
    assert action.op == "edit_status"
    assert "rate_limit" in action.text and "retry in 5s" in action.text


def test_renderaction_text_is_lossless_join():
    action = RenderAction(op="new", chunks=("ab", "cd", "ef"))
    assert action.text == "abcdef"


def test_unknown_event_renders_none():
    assert render_event(object()).op == "none"  # type: ignore[arg-type]


# ============================================================================
# Inline keyboards
# ============================================================================


def _all_buttons(markup: InlineKeyboardMarkup):
    return [b for row in markup.inline_keyboard for b in row]


def test_ask_keyboard_one_button_per_option_plus_other():
    ask = make_ask(n_options=3)
    markup = ask_keyboard(ask)
    buttons = _all_buttons(markup)
    # 3 options + 1 "Other".
    assert len(buttons) == 4
    labels = [b.text for b in buttons]
    assert labels[:3] == ["Option 0-0", "Option 0-1", "Option 0-2"]
    assert "Other" in labels[3]


def test_ask_keyboard_multi_question_has_options_and_other_per_question():
    ask = make_ask(n_options=2, n_questions=2)
    markup = ask_keyboard(ask)
    buttons = _all_buttons(markup)
    # (2 options + 1 Other) * 2 questions = 6.
    assert len(buttons) == 6
    others = [b for b in buttons if "Other" in b.text]
    assert len(others) == 2


def test_ask_keyboard_callback_data_round_trips_to_indices():
    ask = make_ask(n_options=2)
    markup = ask_keyboard(ask)
    buttons = _all_buttons(markup)
    # First option button.
    cb = decode_callback(buttons[0].callback_data)
    assert cb is not None
    assert cb.kind == "ask"
    assert cb.tool_use_id == REAL_TOOL_USE_ID
    assert cb.question_index == 0
    assert cb.option_index == 0
    # The "Other" button.
    other = [b for b in buttons if "Other" in b.text][0]
    ocb = decode_callback(other.callback_data)
    assert ocb is not None and ocb.kind == "other" and ocb.question_index == 0


def test_ask_keyboard_requires_tool_use_id():
    ask = AskEvent(questions=[{"question": "q", "options": []}], tool_use_id=None)
    with pytest.raises(ValueError):
        ask_keyboard(ask)


def test_plan_keyboard_has_approve_and_reject():
    plan = PlanEvent(plan="do it", tool_use_id=REAL_TOOL_USE_ID)
    markup = plan_keyboard(plan)
    buttons = _all_buttons(markup)
    assert len(buttons) == 2
    approve, reject = buttons
    assert "Approve" in approve.text and "Reject" in reject.text
    acb = decode_callback(approve.callback_data)
    rcb = decode_callback(reject.callback_data)
    assert acb is not None and acb.kind == "plan" and acb.plan_action == "approve"
    assert rcb is not None and rcb.kind == "plan" and rcb.plan_action == "reject"


def test_plan_keyboard_requires_tool_use_id():
    with pytest.raises(ValueError):
        plan_keyboard(PlanEvent(plan="x", tool_use_id=None))


# --- callback codec: round-trip + <=64-byte proof + rejection ---------------


@pytest.mark.parametrize("tid", [REAL_TOOL_USE_ID, UUID_TOOL_USE_ID])
def test_callback_round_trip_ask(tid):
    data = encode_callback("a", tid, question_index=3, option_index=9)
    cb = decode_callback(data)
    assert cb is not None
    assert (cb.kind, cb.tool_use_id, cb.question_index, cb.option_index) == (
        "ask",
        tid,
        3,
        9,
    )


@pytest.mark.parametrize("tid", [REAL_TOOL_USE_ID, UUID_TOOL_USE_ID])
def test_callback_round_trip_plan(tid):
    for action_char, expected in (("a", "approve"), ("r", "reject")):
        data = encode_callback("p", tid, plan_action=action_char)
        cb = decode_callback(data)
        assert cb is not None and cb.kind == "plan" and cb.plan_action == expected


@pytest.mark.parametrize("tid", [REAL_TOOL_USE_ID, UUID_TOOL_USE_ID])
def test_callback_round_trip_permission(tid):
    for action_char, expected in (("o", "once"), ("s", "session"), ("d", "deny")):
        data = encode_callback(KIND_PERMISSION, tid, payload=action_char)
        cb = decode_callback(data)
        assert cb is not None
        assert cb.kind == "permission"
        assert cb.tool_use_id == tid
        assert cb.permission_action == expected


def test_permission_callback_data_within_64_bytes_with_realistic_id():
    # The byte-budget proof for the new kind: even a worst-case ~49-char toolu_ id
    # ("m|<id>|<1-char action>") must fit Telegram's 64-byte callback_data limit. A
    # literal "permission|<id>|session" would be ~68 B and blow it — hence 1-char
    # kind + 1-char action code.
    long_id = "toolu_" + "a" * 43  # 49 chars, the worst case the docstring cites
    assert len(long_id) == 49
    for action_char in ("o", "s", "d"):
        data = encode_callback(KIND_PERMISSION, long_id, payload=action_char)
        assert len(data.encode("utf-8")) <= CALLBACK_LIMIT
    # The realistic SDK ids the keyboard actually builds with are well under budget too.
    for tid in (REAL_TOOL_USE_ID, UUID_TOOL_USE_ID):
        for action_char in ("o", "s", "d"):
            data = encode_callback(KIND_PERMISSION, tid, payload=action_char)
            assert len(data.encode("utf-8")) <= CALLBACK_LIMIT


def test_encode_rejects_bad_permission_action():
    # An unknown / multi-char / empty action char fails LOUDLY at build time.
    for bad in ("x", "once", "", "O"):
        with pytest.raises(ValueError):
            encode_callback(KIND_PERMISSION, REAL_TOOL_USE_ID, payload=bad)
    with pytest.raises(ValueError):
        encode_callback(KIND_PERMISSION, REAL_TOOL_USE_ID)  # missing payload


@pytest.mark.parametrize(
    "bad",
    [
        "m|tid|x",  # unknown action char -> None (NOT silently allowed)
        "m|tid|O",  # case-sensitive: uppercase is not a valid action
        "m|tid|once",  # multi-char payload (wrong arity for permission)
        "m|tid|",  # empty payload
        "m||o",  # empty id
        "m|tid|o.o",  # extra index part the permission kind never uses
        "m|only_two",  # wrong field count
        "m|tid|o|extra",  # too many fields
    ],
)
def test_decode_rejects_malformed_permission_data(bad):
    # The trust boundary that feeds SB1/RB1 at T5: a tampered/stale permission tap with
    # an unknown action char (or wrong shape) decodes to None, never a spurious verdict.
    assert decode_callback(bad) is None


def test_callback_data_is_within_64_bytes_with_real_uuid_and_max_indices():
    # The byte-budget proof: a real-length id + the largest indices a (1..4 questions,
    # many options) ask would realistically produce must still fit Telegram's 64-byte
    # callback_data limit.
    for tid in (REAL_TOOL_USE_ID, UUID_TOOL_USE_ID):
        data = encode_callback("a", tid, question_index=99, option_index=99)
        assert len(data.encode("utf-8")) <= CALLBACK_LIMIT
    # And every button a max-option ask builds is within budget.
    ask = make_ask(n_options=20, n_questions=4)
    for button in _all_buttons(ask_keyboard(ask)):
        assert len(button.callback_data.encode("utf-8")) <= CALLBACK_LIMIT


def test_permission_keyboard_buttons_within_64_bytes():
    # Every button the permission keyboard builds is within Telegram's budget, even
    # with a real-length SDK tool_use_id.
    for tid in (REAL_TOOL_USE_ID, UUID_TOOL_USE_ID):
        markup = permission_keyboard(make_permission(tool_use_id=tid))
        for button in _all_buttons(markup):
            assert len(button.callback_data.encode("utf-8")) <= CALLBACK_LIMIT


def test_encode_rejects_overlong_tool_use_id():
    # A pathologically long id that would blow the 64-byte budget fails LOUDLY at build
    # time (so Telegram never rejects it at send).
    too_long = "x" * 80
    with pytest.raises(ValueError):
        encode_callback("a", too_long, question_index=0, option_index=0)


def test_encode_rejects_separator_in_id():
    with pytest.raises(ValueError):
        encode_callback("a", "has|pipe", question_index=0, option_index=0)


def test_encode_rejects_bad_kind_and_missing_payload():
    with pytest.raises(ValueError):
        encode_callback("z", REAL_TOOL_USE_ID, question_index=0, option_index=0)
    with pytest.raises(ValueError):
        encode_callback("a", REAL_TOOL_USE_ID)  # missing indices
    with pytest.raises(ValueError):
        encode_callback("p", REAL_TOOL_USE_ID, plan_action="maybe")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        123,
        b"a|x|0.0",  # bytes, not str
        "",  # empty
        "x" * 65,  # over the byte limit
        "a|only_two",  # wrong field count
        "a||0.0",  # empty id
        "a|tid|",  # empty payload
        "z|tid|0.0",  # unknown kind
        "a|tid|0",  # ask without option index
        "a|tid|x.y",  # non-numeric indices
        "a|tid|0.0.0",  # too many index parts
        "o|tid|abc",  # other with non-numeric question index
        "p|tid|maybe",  # plan with bad action
        "totally foreign string",
    ],
)
def test_decode_rejects_malformed_or_foreign_data(bad):
    # The trust boundary that feeds SB1 at T7: anything that is not our exact scheme
    # decodes to None (ignorable) rather than raising or mis-parsing.
    assert decode_callback(bad) is None


# --- answers-map reconstruction (how T7 rebuilds the QuestionAnswer) ---------


def test_answers_from_ask_rebuilds_question_text_to_label():
    ask = make_ask(n_options=3)
    # A tap that decoded to (question 0, option 2).
    answers = answers_from_ask(ask, 0, 2)
    assert answers == {"Question 0?": "Option 0-2"}


def test_answers_from_ask_multi_question_targets_the_right_question():
    ask = make_ask(n_options=2, n_questions=3)
    assert answers_from_ask(ask, 2, 1) == {"Question 2?": "Option 2-1"}


def test_answers_from_ask_out_of_range_raises_for_t7_to_ignore():
    ask = make_ask(n_options=2)
    with pytest.raises(IndexError):
        answers_from_ask(ask, 0, 99)


def test_end_to_end_button_to_answers_map():
    # Simulate the full T7 path with NO network: build keyboard -> read a button's
    # callback_data -> decode -> reconstruct the answers map the engine consumes.
    ask = make_ask(n_options=3)
    markup = ask_keyboard(ask)
    chosen = _all_buttons(markup)[2]  # operator taps option index 2
    cb = decode_callback(chosen.callback_data)
    assert cb is not None and cb.kind == "ask"
    answers = answers_from_ask(ask, cb.question_index, cb.option_index)
    assert answers == {"Question 0?": "Option 0-2"}


# ============================================================================
# /yolo loud indicator (D6)
# ============================================================================


def test_yolo_banner_is_loud_nonempty_warning():
    banner = yolo_banner()
    assert banner  # non-empty
    assert "⚠️" in banner  # loud, unambiguous (D6 — never silently on)
    # It spells out that the bypass is active so it can't be missed.
    assert "/unyolo" in banner


def test_yolo_indicator_is_loud_nonempty_marker():
    indicator = yolo_indicator()
    assert indicator  # non-empty
    assert "⚠️" in indicator  # loud prefix for each auto-allowed action (D6)


# ============================================================================
# Coalesce / throttle (RB5)
# ============================================================================


def test_burst_of_incremental_text_is_bounded_not_n_edits():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    actions: list[RenderAction] = []
    # 50 deltas all within the SAME interval (no clock advance between them).
    for i in range(50):
        actions.extend(coalescer.offer(TextEvent(text=f"d{i}", incremental=True)).actions)
    edits = [a for a in actions if a.op == "edit_status"]
    # Leading-edge: exactly ONE edit emitted for the whole burst (the rest buffered).
    assert len(edits) == 1
    assert len(edits) < 50  # the RB5 guarantee: bounded, never N
    # The buffered (newest) status is the last delta; force-flush shows it.
    flushed = coalescer.flush().actions
    assert len(flushed) == 1
    assert flushed[0].text == "d49"


def test_throttle_releases_one_edit_per_interval():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    emitted: list[str] = []

    def feed(text: str) -> None:
        for a in coalescer.offer(TextEvent(text=text, incremental=True)).actions:
            emitted.append(a.text)

    feed("a")  # t=1000: leading-edge flush -> "a"
    feed("b")  # buffered (newest)
    feed("c")  # buffered (newest -> "c")
    clock.advance(2.0)  # interval elapsed
    feed("d")  # due -> flush newest "d"
    clock.advance(0.5)
    feed("e")  # buffered, not due
    clock.advance(2.0)
    # No new event; T7's timer calls flush_due to release the trailing edge.
    for a in coalescer.flush_due().actions:
        emitted.append(a.text)
    assert emitted == ["a", "d", "e"]


def test_flush_due_is_noop_before_interval_and_when_clean():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    # Nothing buffered.
    assert coalescer.flush_due().actions == ()
    coalescer.offer(TextEvent(text="x", incremental=True))  # leading-edge flush
    coalescer.offer(TextEvent(text="y", incremental=True))  # buffered
    # Interval not elapsed -> no release yet.
    assert coalescer.flush_due().actions == ()
    assert coalescer.flush_due().next_due_at == 1000.0 + 2.0


def test_verbatim_event_flushes_immediately_and_after_status():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=100.0)  # huge interval
    coalescer.offer(TextEvent(text="status1", incremental=True))  # leading flush
    coalescer.offer(TextEvent(text="status2", incremental=True))  # buffered (newest)
    # A verbatim error arrives — it must flush the pending status FIRST (ordering),
    # then itself, immediately, regardless of the throttle interval.
    result = coalescer.offer(ErrorEvent(kind_of_error="turn_error", message="bad"))
    ops = [(a.op, a.text) for a in result.actions]
    assert ops[0][0] == "edit_status" and ops[0][1] == "status2"  # pending flushed
    assert ops[1][0] == "new" and "bad" in ops[1][1]  # verbatim, immediate


def test_verbatim_ask_and_plan_flush_immediately():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=100.0)
    for ev in (make_ask(), PlanEvent(plan="p", tool_use_id=REAL_TOOL_USE_ID)):
        result = coalescer.offer(ev)
        assert len(result.actions) == 1
        assert result.actions[0].op == "new"
        assert result.actions[0].verbatim is True


def test_verbatim_permission_flushes_pending_status_then_itself():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=100.0)  # huge interval
    coalescer.offer(TextEvent(text="status1", incremental=True))  # leading flush
    coalescer.offer(TextEvent(text="status2", incremental=True))  # buffered (newest)
    # A permission prompt is verbatim: it must flush pending status FIRST (ordering),
    # then itself immediately, regardless of the throttle interval.
    result = coalescer.offer(make_permission())
    ops = [(a.op, a.text) for a in result.actions]
    assert ops[0][0] == "edit_status" and ops[0][1] == "status2"  # pending flushed
    assert ops[1][0] == "new"  # the permission prompt, verbatim + immediate
    assert result.actions[1].verbatim is True
    assert result.actions[1].reply_markup is not None


def test_none_events_buffer_nothing():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    result = coalescer.offer(TextEvent(text="", incremental=True))  # -> none
    assert result.actions == ()
    assert result.next_due_at is None


def test_tool_use_and_status_coalesce_into_one_status_line():
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=10.0)
    actions: list[RenderAction] = []
    actions.extend(coalescer.offer(ToolUseEvent(tool_name="Read", tool_input_summary="Read(file_path=a.py)")).actions)
    actions.extend(coalescer.offer(StatusEvent(phase="connected")).actions)
    actions.extend(coalescer.offer(ToolUseEvent(tool_name="Bash", tool_input_summary="Bash(command=ls)")).actions)
    edits = [a for a in actions if a.op == "edit_status"]
    # Leading-edge only -> one edit; the rest fold into the buffered line.
    assert len(edits) == 1
    # The buffered newest line is the last tool_use.
    assert coalescer.flush().actions[0].text == "▶️ Bash(command=ls)"


def test_coalesce_stream_helper_flushes_trailing_status():
    clock = FakeClock()
    events = [
        TextEvent(text="d0", incremental=True),
        TextEvent(text="d1", incremental=True),
        TextEvent(text="d2", incremental=True),
    ]
    actions = coalesce_stream(events, now=clock, min_interval=2.0)
    edits = [a for a in actions if a.op == "edit_status"]
    # Leading edge ("d0") + trailing flush ("d2"); never 3.
    assert [a.text for a in edits] == ["d0", "d2"]


def test_coalescer_rejects_negative_interval():
    with pytest.raises(ValueError):
        Coalescer(now=FakeClock(), min_interval=-1.0)


# ============================================================================
# Chunking (long plan / text -> <=4096 UTF-16, lossless via split_message)
# ============================================================================


def test_long_plan_is_chunked_under_4096_utf16_lossless():
    long_plan = "step\n" * 2000  # ~10000 chars, > 4096
    plan = PlanEvent(plan=long_plan, tool_use_id=REAL_TOOL_USE_ID)
    action = render_event(plan)
    assert len(action.chunks) > 1
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
    # Lossless: the header is prepended once, then the full plan is preserved.
    assert action.text.endswith(long_plan)
    assert long_plan in action.text


def test_long_assembled_text_chunked_lossless():
    text = "word " * 2000
    action = render_event(TextEvent(text=text, incremental=False))
    assert len(action.chunks) > 1
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
    assert action.text == text  # lossless


def test_long_error_chunked():
    action = render_event(ErrorEvent(kind_of_error="driver_error", message="x" * 9000))
    assert len(action.chunks) > 1
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096


def test_emoji_heavy_text_chunked_under_utf16_limit():
    # Astral emoji are 2 UTF-16 units each — the chunker must count them correctly.
    text = "😀" * 3000  # 6000 UTF-16 units
    action = render_event(TextEvent(text=text, incremental=False))
    assert len(action.chunks) >= 2
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
    assert action.text == text
