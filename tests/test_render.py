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
    BODY_FREE_ERROR_LINE,
    CALLBACK_LIMIT,
    KIND_PERMISSION,
    ChatSendGate,
    Coalescer,
    RenderAction,
    answers_from_ask,
    ask_keyboard,
    ask_question_body,
    ask_question_keyboard,
    coalesce_stream,
    decode_callback,
    encode_callback,
    free_text_prompt,
    notify_attention,
    notify_done,
    notify_error,
    permission_keyboard,
    plan_keyboard,
    project_status_label,
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
    # P6/R3 (SB3/H1): a tool_error wraps RAW tool output, so it now renders BODY-FREE — the
    # raw "boom" is NOT in the chat text; a safe summary (kind + fixed line) is. (The raw
    # body still reaches the local debug log via the driver; see test_stream_session.)
    err = ErrorEvent(kind_of_error="tool_error", message="boom")
    action = render_event(err)
    assert action.op == "new"
    assert action.verbatim is True
    assert "boom" not in action.text  # body-free: the raw body is gone
    assert "tool_error" in action.text  # but the error KIND is still shown
    assert BODY_FREE_ERROR_LINE in action.text
    assert action.reply_markup is None


def test_driver_error_stays_readable():
    # P6/R3 classification: a driver_error is BOT-AUTHORED (a timeout / transport label),
    # not raw external output — so it stays readable (good UX, no secret risk).
    err = ErrorEvent(kind_of_error="driver_error", message="send timed out after 120s")
    action = render_event(err)
    assert "send timed out after 120s" in action.text  # bot-authored detail kept
    assert BODY_FREE_ERROR_LINE not in action.text


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


def test_status_activity_phase_is_friendly_and_stable():
    """The noisy activity phases render as a calm, STABLE 'thinking…' line — no raw phase
    name, no per-event detail (whose variation would defeat the identical-line dedupe that
    stops status-message spam)."""
    text = render_event(StatusEvent(phase="connected", detail="thinking_tokens", model="x")).text
    assert text == "💭 Claude is thinking…"
    assert "connected" not in text and "thinking_tokens" not in text and "x" not in text
    # init folds to the same calm family; identical text across a burst → dedupe to 1 line.
    assert render_event(StatusEvent(phase="connected")).text == text


def test_ask_question_keyboard_is_single_question_slice():
    """A multi-question ask renders one keyboard PER question — each carries only that
    question's options (+ an Other), so the buttons sit under their own question rather
    than in one giant stacked wall."""
    ask = AskEvent(
        questions=[
            {"question": "Storage?", "options": [{"label": "JSON"}, {"label": "SQLite"}]},
            {"question": "CLI?", "options": [{"label": "argparse"}, {"label": "Typer"}, {"label": "Click"}]},
        ],
        tool_use_id="tid",
    )
    kb0 = ask_question_keyboard(ask, 0)
    kb1 = ask_question_keyboard(ask, 1)
    # Q0: 2 options + Other = 3 rows; Q1: 3 options + Other = 4 rows.
    assert len(kb0.inline_keyboard) == 3
    assert len(kb1.inline_keyboard) == 4
    # Every option button on kb0 decodes to question_index 0; on kb1, question_index 1.
    for row in kb0.inline_keyboard[:-1]:  # last row is Other
        cb = decode_callback(row[0].callback_data)
        assert cb.kind == "ask" and cb.question_index == 0
    for row in kb1.inline_keyboard[:-1]:
        cb = decode_callback(row[0].callback_data)
        assert cb.kind == "ask" and cb.question_index == 1
    # The trailing row is the per-question Other.
    assert decode_callback(kb1.inline_keyboard[-1][0].callback_data).kind == "other"


def test_ask_question_body_numbers_multi_questions():
    ask = AskEvent(
        questions=[
            {"question": "Q one", "header": "Storage"},
            {"question": "Q two", "header": "CLI"},
            {"question": "Q three", "header": "Commands"},
        ],
        tool_use_id="tid",
    )
    assert ask_question_body(ask, 1) == "❓ (2/3) CLI: Q two"
    # A single-question ask has no (k/N) counter.
    single = AskEvent(questions=[{"question": "Just one"}], tool_use_id="tid")
    assert ask_question_body(single, 0) == "❓ Just one"


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
# Proactive background-project notifications (P5 / ADR-005 D4) — pure strings
# ============================================================================


def test_notify_attention_permission_is_name_prefixed_bell():
    msg = notify_attention("work", "permission")
    assert msg == "🔔 work — Claude needs approval"
    assert msg.startswith("🔔 ")  # bell glyph (D4)
    assert "work" in msg  # name-prefixed so the operator knows WHICH project


def test_notify_attention_ask_and_plan_have_their_own_phrases():
    assert notify_attention("bot", "ask") == "🔔 bot — asks a question"
    assert notify_attention("bot", "plan") == "🔔 bot — proposes a plan"


def test_notify_attention_unknown_kind_falls_back_safely():
    # RB1: a pending kind the relay did not expect degrades to a generic, still
    # body-free "needs attention" ping rather than raising / leaking the raw kind.
    msg = notify_attention("work", "totally-unknown-kind")
    assert msg == "🔔 work — needs attention"
    assert "totally-unknown-kind" not in msg  # the stray value is never echoed


def test_notify_done_is_name_prefixed_check():
    msg = notify_done("work")
    assert msg == "✅ work — done"
    assert msg.startswith("✅ ")  # done glyph (D4)


def test_notify_error_is_name_prefixed_warning_with_short_label():
    msg = notify_error("work", "tool_error")
    assert msg == "⚠️ work — tool_error"
    assert msg.startswith("⚠️ ")  # warning glyph (D4)


def test_notify_error_blank_label_falls_back():
    # RB1: a blank/whitespace short_error never leaves an empty tail.
    assert notify_error("work", "") == "⚠️ work — error"
    assert notify_error("work", "   ") == "⚠️ work — error"


def test_notify_attention_is_body_free_sb3():
    # SB3 mutation-probe: a held PermissionEvent whose summary carried a secret-bearing
    # tool_input must NEVER surface in the ping — the attention phrase is FIXED, so even
    # if a caller had the event in hand, the body cannot leak through this builder.
    secret = "S3CR3T-" + "x" * 200
    leaky_summary = f"Write(file_path=/tmp/x, content={secret})"
    ev = make_permission(tool_name="Write", tool_input_summary=leaky_summary)
    # The builder takes only (name, kind) — it cannot even SEE the event's body.
    msg = notify_attention("work", ev.kind)  # PendingKind == event.kind == "permission"
    assert msg == "🔔 work — Claude needs approval"
    assert secret not in msg
    assert "S3CR3T" not in msg
    assert leaky_summary not in msg


def test_notify_error_is_body_free_sb3():
    # SB3: only the SHORT, body-free label the relay supplies appears — never a raw body.
    # A caller that wrongly handed raw content would still only get its (stripped) text,
    # but the relay supplies the body-free ErrorKind; we assert a secret-bearing body
    # passed as the label is not silently expanded into anything else and a real raw
    # tool body never reaches this builder (it takes a short label, not an event/input).
    raw_body = "S3CR3T-" + "y" * 300
    # The relay passes the body-free kind, NOT the raw body:
    msg = notify_error("work", "tool_error")
    assert raw_body not in msg
    assert msg == "⚠️ work — tool_error"


def test_notification_builders_are_pure_no_io():
    # Purity / determinism: same inputs -> identical output, no side effects, no I/O.
    assert notify_attention("p", "ask") == notify_attention("p", "ask")
    assert notify_done("p") == notify_done("p")
    assert notify_error("p", "boom") == notify_error("p", "boom")


# ============================================================================
# Name-echoed free-text prompt (P5 / ADR-005 D5) — pure string
# ============================================================================


def test_free_text_prompt_is_name_echoed():
    # D5 name-echo: the prompt carries the project name so the operator knows WHICH project
    # the next plain message (or a reply to this prompt) resolves when several are awaiting.
    msg = free_text_prompt("work")
    assert msg == "✏️ work: reply with your answer…"
    assert "work" in msg and msg.startswith("✏️")


def test_free_text_prompt_is_pure_and_body_free():
    # Pure / deterministic, and carries ONLY the (SB4-validated) name + a fixed phrase — no
    # event body (SB3): there is nothing here from which a question/plan/tool body could leak.
    assert free_text_prompt("bot") == free_text_prompt("bot")
    assert free_text_prompt("a-b_C9") == "✏️ a-b_C9: reply with your answer…"


# ============================================================================
# Per-project status labels for /projects (P5 / ADR-005 D7) — pure label map
# ============================================================================


def test_project_status_label_covers_every_status_value():
    # Every enum value design D7 / ADR-005 fixes (the set T4/T7 will set) maps to its
    # human /projects label. If T4 adds/renames a value, this is where it surfaces.
    expected = {
        "idle": "idle",
        "running": "running",
        "awaiting_approval": "awaiting approval",
        "awaiting_answer": "awaiting answer",
        "awaiting_plan": "awaiting plan",
        "queued": "queued",
    }
    for value, label in expected.items():
        assert project_status_label(value) == label


def test_project_status_label_unknown_value_falls_back_to_idle():
    # RB1: an unexpected enum / a stray string / None (a project with no runtime) reads
    # as "idle" rather than crashing the /projects render (D7: no runtime -> idle).
    assert project_status_label("nonsense") == "idle"
    assert project_status_label("") == "idle"
    assert project_status_label(None) == "idle"
    assert project_status_label(123) == "idle"  # type: ignore[arg-type]


def test_awaiting_labels_are_human_readable_spaced():
    # The awaiting_* enum keys are snake_case; the labels are spelled out for the column.
    for value in ("awaiting_approval", "awaiting_answer", "awaiting_plan"):
        label = project_status_label(value)
        assert "_" not in label  # rendered, not the raw enum key
        assert label.startswith("awaiting ")


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
    # P6/R3: a turn_error renders body-free now (raw "bad" is gone) — the ordering/flush
    # behavior under test is unchanged; assert the error block by its KIND, not the body.
    assert ops[1][0] == "new" and "turn_error" in ops[1][1]  # verbatim, immediate
    assert "bad" not in ops[1][1]


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


# ============================================================================
# CommonMark -> Telegram HTML wiring (the prose render paths) + raw fallback.
# ============================================================================


def test_assembled_text_renders_as_html_with_raw_fallback():
    # An assembled TextEvent with markdown -> parse_mode=="HTML", **x** became <b>x</b>,
    # and the ORIGINAL raw markdown is preserved in plain_chunks for the send fallback.
    action = render_event(
        TextEvent(text="A **bold** word and `code`.", incremental=False)
    )
    assert action.parse_mode == "HTML"
    assert "<b>bold</b>" in action.text
    assert "<code>code</code>" in action.text
    # Raw fallback parallels chunks and is the un-converted markdown.
    assert len(action.plain_chunks) == len(action.chunks)
    assert "".join(action.plain_chunks) == "A **bold** word and `code`."
    assert "**bold**" in action.plain_chunks[0]  # raw, NOT the HTML


def test_result_text_renders_as_html_with_raw_fallback():
    res = ResultEvent(
        session_id="s1",
        is_error=False,
        subtype="success",
        result_text="Done. See `file.py` and **note** this.",
    )
    action = render_event(res)
    assert action.parse_mode == "HTML"
    assert "<code>file.py</code>" in action.text
    assert "<b>note</b>" in action.text
    assert len(action.plain_chunks) == len(action.chunks)
    assert "".join(action.plain_chunks) == "Done. See `file.py` and **note** this."


def test_plan_body_renders_as_html_with_header_and_keyboard():
    plan = PlanEvent(plan="Step **one**\nStep two", tool_use_id=REAL_TOOL_USE_ID)
    action = render_event(plan)
    assert action.parse_mode == "HTML"
    # The bot-scaffolding header is preserved; the Claude plan body is HTML-converted.
    assert "Proposed plan" in action.text
    assert "<b>one</b>" in action.text
    assert isinstance(action.reply_markup, InlineKeyboardMarkup)
    # Raw fallback carries the original markdown (header + un-converted plan).
    assert len(action.plain_chunks) == len(action.chunks)
    assert "Step **one**" in "".join(action.plain_chunks)


def test_prose_html_escapes_stray_angle_brackets_from_claude():
    # A stray < / & from Claude must be escaped in the HTML chunk (so it can't break the
    # message) while the raw fallback keeps the literal characters.
    action = render_event(TextEvent(text="compare a < b && c", incremental=False))
    assert "&lt;" in action.text and "&amp;&amp;" in action.text
    assert "a < b && c" in "".join(action.plain_chunks)  # raw preserved verbatim


def test_done_footer_stays_plain_text_no_html():
    # The bot-generated done-footer (no result_text) is NOT prose -> plain, no parse_mode.
    res = ResultEvent(
        session_id="s1", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.0123
    )
    action = render_event(res)
    assert action.parse_mode is None
    assert action.plain_chunks == ()
    assert "done" in action.text and "3 turns" in action.text


def test_error_block_stays_plain_text():
    # Error blocks are bot scaffolding, shown exactly -> plain text (no HTML conversion).
    # Use a driver_error (bot-authored, rendered readably) so a literal "<x>" is present to
    # prove no HTML escaping. A tool_error would render body-free (covered above) — the
    # plain-text/no-escape property under test is the same for both.
    action = render_event(ErrorEvent(kind_of_error="driver_error", message="boom <x>"))
    assert action.parse_mode is None
    assert action.plain_chunks == ()
    assert "boom <x>" in action.text  # verbatim, not escaped


def test_long_prose_html_chunks_each_under_4096_and_raw_parallel():
    # A long markdown reply: chunk RAW first (sub-limit) then convert each -> every HTML
    # chunk stays under Telegram's 4096 UTF-16 limit, and plain_chunks parallels it.
    text = ("This is **paragraph** number with `code`.\n" * 400)
    action = render_event(TextEvent(text=text, incremental=False))
    assert len(action.chunks) > 1
    assert len(action.plain_chunks) == len(action.chunks)
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
        assert "<b>paragraph</b>" in chunk  # each chunk is independently valid HTML
    # The raw fallback rejoins to the original markdown (lossless).
    assert "".join(action.plain_chunks) == text


def test_oversized_fenced_block_splits_into_valid_pre_pieces():
    # A single huge ```code``` block (a big file dump) is split into multiple COMPLETE
    # fences so each emitted HTML piece is an individually valid <pre> under the limit —
    # never a mid-fence cut that would render as plain prose.
    big = "```python\n" + ("x = 1\n" * 1500) + "```"
    action = render_event(TextEvent(text=big, incremental=False))
    assert len(action.chunks) > 1
    for chunk in action.chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
        assert "<pre>" in chunk and chunk.rstrip().endswith("</pre>")
    assert len(action.plain_chunks) == len(action.chunks)


def test_ask_question_body_html_escapes_question_and_converts_markdown():
    from claude_tg.render import ask_question_body, ask_question_body_html

    ask = AskEvent(
        questions=[{"question": "Use **JSON** or <raw> ?", "header": "Storage"}],
        tool_use_id="tid",
    )
    html_body = ask_question_body_html(ask, 0)
    # The Claude question text is HTML-converted (**JSON** -> bold) and escaped (<raw>).
    assert "<b>JSON</b>" in html_body
    assert "&lt;raw&gt;" in html_body
    assert "Storage" in html_body  # the scaffolding header label survives
    # The plain variant is unchanged (the raw fallback the send path resends on rejection).
    assert ask_question_body(ask, 0) == "❓ Storage: Use **JSON** or <raw> ?"


# ============================================================================
# ChatSendGate (RB5 under concurrency, P5 / ADR-005 D8) — pure timing decisions
# over an injected clock, mirroring the Coalescer test style (no real sleeps).
# The gate decides the WAIT before a send may proceed; the session does the
# awaiting. Verbatim is PRIORITY over coalesced status churn (never starved).
# ============================================================================


def test_chat_send_gate_leading_edge_is_immediate():
    # The first send through an idle gate goes immediately (no artificial lag), whether it
    # is verbatim or a status edit — the leading edge, exactly like the Coalescer.
    clock = FakeClock()
    gate = ChatSendGate(now=clock, interval=1.0)
    assert gate.reserve(verbatim=True) == 0.0
    clock.advance(5.0)  # idle long past the interval
    assert gate.reserve(verbatim=False) == 0.0


def test_chat_send_gate_spaces_subsequent_sends_by_interval():
    # Two sends in quick succession through one gate are spaced by the interval: the first
    # goes now (wait 0), the second must wait the remaining interval (bounded rate).
    clock = FakeClock()
    gate = ChatSendGate(now=clock, interval=1.0)
    assert gate.reserve(verbatim=True) == 0.0  # send #1 now
    # No time has passed; send #2 must wait ~1 s (the per-chat budget).
    assert gate.reserve(verbatim=True) == pytest.approx(1.0)
    # After the clock advances past that reservation, the next send is immediate again.
    clock.advance(2.0)
    assert gate.reserve(verbatim=True) == 0.0


def test_chat_send_gate_n_sends_are_bounded_not_simultaneous():
    # ⭐ The RB5 property at the unit level: N back-to-back sends (the worst case — N
    # concurrent projects flushing at the same instant) do NOT all go at once; their
    # cumulative scheduled offsets grow by the interval, so the COMBINED rate is bounded.
    clock = FakeClock()  # frozen — every send arrives at the same instant
    gate = ChatSendGate(now=clock, interval=1.0)
    waits = [gate.reserve(verbatim=True) for _ in range(5)]
    # 0, 1, 2, 3, 4 — strictly increasing by the interval (≤ one send per interval).
    assert waits == [pytest.approx(i * 1.0) for i in range(5)]


def test_chat_send_gate_verbatim_jumps_ahead_of_future_status_depth_invariant():
    # ⭐ The load-bearing D8 priority rule, in its COLLISION-FREE form (round-3 BLOCKER 3). A
    # verbatim arriving amid LIVE status churn jumps ahead of all status reserved AFTER it,
    # independent of how much status FOLLOWS — the deadlock-prevention case (a prompt must
    # reach the operator; subsequent status churn must not bury it). And no two sends collide.
    #
    # This is the realistic flush pattern: status is reserved one-at-a-time as each project's
    # Coalescer flushes, NOT all pre-committed in a single instant. A verbatim interleaved into
    # that stream is spaced ~1 interval off the last ACTUAL send and the following status falls
    # in BEHIND it. TEETH: the priority-breaking mutation (verbatim base=_last_actual ->
    # base=_tail) makes the verbatim wait scale with the FOLLOWING churn → the depth-invariance
    # below FAILS. (Note: a verbatim cannot leapfrog status whose fire-time was ALREADY handed
    # to a waiting caller — un-scheduling a committed send is impossible — so the separate
    # ``...behind_committed_backlog...`` test pins that collision-free boundary.)
    def verbatim_wait_then_status_follows(following: int) -> float:
        clock = FakeClock()
        gate = ChatSendGate(now=clock, interval=1.0)
        gate.reserve(verbatim=False)  # leading-edge status fires now (the last ACTUAL send)
        w = gate.reserve(verbatim=True)  # verbatim jumps to ~1 interval off that actual send
        for _ in range(following):  # FUTURE status — must all fall BEHIND the verbatim
            assert gate.reserve(verbatim=False) >= w + 1.0 - 1e-9
        return w

    shallow = verbatim_wait_then_status_follows(5)
    deep = verbatim_wait_then_status_follows(50)
    assert shallow == pytest.approx(1.0), "verbatim is ~1 interval off the last actual send"
    assert deep == pytest.approx(shallow), "depth-invariant in the FOLLOWING churn: NOT 5 vs 50"


def test_chat_send_gate_verbatim_behind_committed_backlog_is_collision_free():
    # The collision-free boundary (round-3 BLOCKER 3). When a DEEP status backlog was ALREADY
    # reserved (every slot 0..K-1 handed to a waiting caller) BEFORE the verbatim exists, the
    # verbatim CANNOT land on an occupied slot — un-scheduling a committed send is impossible —
    # so it takes the next FREE slot (K), collision-free, rather than firing on top of a
    # reserved status (the pre-fix bug: it shared a slot, double-spending the per-chat budget).
    # This relaxes the old (buggy) "depth-invariant even behind a fully-committed backlog"
    # claim — impossible without a collision — in favour of the HARD combined-budget guarantee.
    # In production the backlog is at most ~MAX_CONCURRENT_RUNS deep (status is throttled
    # per-project by the Coalescer), so this bound is small; the deadlock-prevention property
    # that actually matters (ahead of FUTURE status) is pinned by the test above.
    clock = FakeClock()
    gate = ChatSendGate(now=clock, interval=1.0)
    k = 30
    status_waits = [gate.reserve(verbatim=False) for _ in range(k)]
    assert status_waits == [pytest.approx(i * 1.0) for i in range(k)]  # 0..29, each committed
    verbatim_wait = gate.reserve(verbatim=True)
    # Collision-free: the verbatim does NOT share slot 1..29 with a committed status; it lands
    # at the next free slot (30). The pre-fix gate returned ~1.0 here (colliding with the status
    # already reserved at +1) — exactly the combined-budget violation BLOCKER 3 fixes.
    assert verbatim_wait == pytest.approx(float(k))
    # And every reserved fire-time (the K status + the verbatim) is distinct / ≥interval apart.
    all_times = sorted(status_waits + [verbatim_wait])  # frozen clock → waits ARE fire-times
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(all_times, all_times[1:]))


def test_chat_send_gate_no_two_sends_share_an_interval_verbatim_amid_status(tmp_path=None):
    # ⭐ Round-3 cross-model-QA BLOCKER 3. The per-chat budget is a COMBINED rate: NO two
    # sends (verbatim OR status) may fire within one interval of each other. The pre-fix gate
    # spaced a verbatim off ``_last_actual`` while a status was ALREADY reserved at that same
    # future slot → the verbatim and that status both landed at the SAME timestamp, firing two
    # sends in one interval (over-budget under status churn). This pins the combined budget by
    # recording EVERY reserved fire-time (status + verbatim, interleaved) and asserting they
    # are all ≥ interval apart.
    clock = FakeClock()
    interval = 1.0
    gate = ChatSendGate(now=clock, interval=interval)
    # Realistic interleave: a couple of status edits reserve future slots, THEN a verbatim
    # arrives amid them (the exact churn the priority rule must handle), then more status.
    fire_times: list[float] = []

    def reserve(verbatim: bool) -> None:
        fire_times.append(clock() + gate.reserve(verbatim=verbatim))

    reserve(verbatim=False)  # status #1 — leading edge (fires now)
    reserve(verbatim=False)  # status #2 — reserved 1 interval out
    reserve(verbatim=True)   # a VERBATIM arrives amid the status churn (the bug trigger)
    reserve(verbatim=False)  # status #3 — must fall behind the verbatim
    reserve(verbatim=True)   # a second verbatim

    ordered = sorted(fire_times)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    # THE invariant: every consecutive pair of reserved fire-times is ≥ one interval apart —
    # no two sends share a slot, so the COMBINED per-chat rate is bounded regardless of how
    # verbatim and status interleave. (Pre-fix this FAILS: the verbatim collides with status#2
    # at the same timestamp → a 0.0 gap.)
    assert all(g >= interval - 1e-9 for g in gaps), (
        f"two sends within one interval (combined budget violated): {ordered}"
    )
    # All distinct (a sanity restatement of the above for the exact-collision case).
    assert len(set(round(t, 9) for t in fire_times)) == len(fire_times), (
        f"two sends reserved the SAME timestamp: {fire_times}"
    )


def test_chat_send_gate_verbatim_jumps_ahead_of_future_status_collision_free(tmp_path=None):
    # The preserved D8 priority (collision-free form): a verbatim arriving BEFORE a status
    # backlog builds jumps ahead of all FUTURE status — the deadlock-prevention case (a prompt
    # must reach the operator; subsequent status churn must not bury it). And no two collide.
    clock = FakeClock()
    gate = ChatSendGate(now=clock, interval=1.0)
    w_status1 = gate.reserve(verbatim=False)   # leading edge, t=0
    w_verbatim = gate.reserve(verbatim=True)    # jumps to t=1 (only status#1 precedes it)
    w_status2 = gate.reserve(verbatim=False)    # FUTURE status — must fall BEHIND the verbatim
    assert w_status1 == pytest.approx(0.0)
    assert w_verbatim == pytest.approx(1.0), "verbatim is ~1 interval off the last actual send"
    # The future status yields to the verbatim (lands at/after t=2, never sharing t=1).
    assert w_status2 >= w_verbatim + 1.0 - 1e-9, "future status must not collide with / precede the verbatim"


def test_chat_send_gate_zero_interval_never_waits():
    # interval=0 disables spacing (a valid low-traffic choice) — every send is immediate.
    clock = FakeClock()
    gate = ChatSendGate(now=clock, interval=0.0)
    assert [gate.reserve(verbatim=v) for v in (True, False, True, False)] == [0.0, 0.0, 0.0, 0.0]


def test_chat_send_gate_rejects_negative_interval():
    with pytest.raises(ValueError):
        ChatSendGate(now=FakeClock(), interval=-1.0)
