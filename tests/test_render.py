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
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from claude_tg.engine.types import (
    AskEvent,
    ErrorEvent,
    PermissionEvent,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from claude_tg.render import (
    BODY_FREE_ERROR_LINE,
    CALLBACK_LIMIT,
    KIND_ATTACH,
    KIND_PERMISSION,
    KIND_SWITCH,
    THINKING_HIDDEN_LINE,
    THINKING_TAIL_MAX,
    ChatSendGate,
    Coalescer,
    RenderAction,
    answers_from_ask,
    ask_keyboard,
    ask_question_body,
    ask_question_keyboard,
    coalesce_stream,
    code_path,
    decode_callback,
    done_footer_suffix,
    encode_attach_callback,
    encode_callback,
    encode_switch_callback,
    free_text_prompt,
    notify_attention,
    notify_done,
    notify_error,
    open_project_keyboard,
    permission_keyboard,
    plan_keyboard,
    project_status_label,
    queued_suffix,
    quick_reply_dismiss,
    quick_reply_keyboard,
    render_event,
    thinking_line,
    tool_use_line,
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
    # marker and NOT the raw 600-char body. T2 now <code>-wraps + HTML-escapes the summary,
    # so the marker renders as the ESCAPED "content=&lt;600 chars&gt;" — still the count, never
    # the body (wrapping is a rendering change, not a disclosure change).
    raw_body = "S3CR3T-" + "x" * 593  # 600 chars of "content" the operator must not see
    assert len(raw_body) == 600
    summary = "Write(file_path=/tmp/secret.txt, content=<600 chars>)"
    action = render_event(
        make_permission(tool_name="Write", tool_input_summary=summary)
    )
    assert "content=&lt;600 chars&gt;" in action.text  # the safe marker (escaped) is present
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


# ---- P12 T-THINK-2: thinking renders as a capped, collapsed 🧠 status line (SB3) ----


def test_thinking_renders_as_status_line_with_brain_glyph():
    action = render_event(ThinkingEvent(text="weighing the options", incremental=True))
    assert action.op == "edit_status"  # the SAME coalesced status slot (RB5)
    assert action.verbatim is False  # NOT a permanent message
    assert action.text.startswith("🧠 ")
    assert "weighing the options" in action.text
    assert action.parse_mode is None  # plain — no path, no HTML


def test_thinking_is_capped_to_a_recent_tail_never_floods():
    # A long chain-of-thought must be CAPPED to the recent tail (SB3 — never flood Telegram).
    long = "".join(f"step{i} " for i in range(500))  # thousands of chars
    action = render_event(ThinkingEvent(text=long, incremental=True))
    # The shown text is bounded (glyph + space + ellipsis + the tail), far short of the input.
    assert len(action.text) <= len("🧠 …") + THINKING_TAIL_MAX
    assert len(action.text) < len(long)
    # It keeps the most-RECENT reasoning (the live frontier), marked clipped with a leading "…".
    assert action.text.startswith("🧠 …")
    assert action.text.endswith("step499 ".strip()) or "step499" in action.text


def test_thinking_collapses_newlines_to_single_status_line():
    action = render_event(ThinkingEvent(text="line one\n\nline two\nline three", incremental=False))
    assert "\n" not in action.text
    assert action.text == "🧠 line one line two line three"


def test_redacted_thinking_renders_fixed_opaque_line_never_raw():
    # SB3: a redacted thinking event renders ONLY the fixed opaque line — never any body, even
    # if (defensively) one were present. A redacted event ALSO renders even with empty text.
    action = render_event(
        ThinkingEvent(text="SECRET_REASONING_MUST_NOT_SHOW", incremental=True, redacted=True)
    )
    assert action.op == "edit_status"
    assert action.text == THINKING_HIDDEN_LINE
    assert "SECRET_REASONING_MUST_NOT_SHOW" not in action.text
    # Empty-text redacted still shows the hidden line (it's the whole point).
    empty_redacted = render_event(ThinkingEvent(text="", incremental=True, redacted=True))
    assert empty_redacted.op == "edit_status"
    assert empty_redacted.text == THINKING_HIDDEN_LINE


def test_empty_non_redacted_thinking_is_none():
    # Nothing readable to show (and not redacted) -> op="none" (mirrors empty incremental text).
    assert render_event(ThinkingEvent(text="", incremental=True)).op == "none"


def test_thinking_line_helper_caps_and_marks_clip():
    short = thinking_line(ThinkingEvent(text="brief", incremental=True))
    assert short == "🧠 brief"
    tail = "x" * (THINKING_TAIL_MAX + 50)
    capped = thinking_line(ThinkingEvent(text=tail, incremental=True))
    assert capped.startswith("🧠 …")
    # the body (after the glyph + space) is the last THINKING_TAIL_MAX chars + the "…".
    assert len(capped) == len("🧠 …") + THINKING_TAIL_MAX


def test_burst_of_thinking_deltas_is_bounded_not_n_edits():
    # The RB5 guarantee for thinking too: a burst of thinking_deltas folds into a BOUNDED
    # number of in-place edits via the existing Coalescer (mirrors the incremental-text test).
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    actions: list[RenderAction] = []
    for i in range(50):
        actions.extend(coalescer.offer(ThinkingEvent(text=f"reasoning step {i}", incremental=True)).actions)
    edits = [a for a in actions if a.op == "edit_status"]
    assert len(edits) == 1  # leading-edge: one edit for the whole burst, the rest buffered
    assert len(edits) < 50
    # The buffered newest is the last delta (capped/collapsed), shown on force-flush.
    flushed = coalescer.flush().actions
    assert len(flushed) == 1
    assert "reasoning step 49" in flushed[0].text


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
# code_path — path wrapped in <code> for the auto-linkify fix (R6)
# ============================================================================


def test_code_path_wraps_in_code_so_telegram_does_not_linkify():
    # R6: an ordinary path is wrapped verbatim in <code>…</code> so Telegram renders it as
    # inert monospace instead of auto-linkifying each "/segment" as a fake command-link.
    assert code_path("/tmp/p5verify/a") == "<code>/tmp/p5verify/a</code>"


def test_code_path_html_escapes_metacharacters_exactly_once():
    # A path containing HTML metacharacters (& < >) is escaped EXACTLY once so it can't
    # break the HTML message or inject a tag — and not double-escaped (no &amp;amp;).
    assert code_path("/a&b/<x>/c") == "<code>/a&amp;b/&lt;x&gt;/c</code>"
    # The "&" became "&amp;" (single escape), not "&amp;amp;".
    assert "&amp;amp;" not in code_path("/a&b")


def test_code_path_is_pure_and_accepts_non_str():
    # Pure / deterministic; tolerates a non-str (e.g. a Path) via str() so callers need not
    # pre-stringify. (The reply must still be sent with parse_mode="HTML".)
    from pathlib import Path

    assert code_path("/x") == code_path("/x")
    assert code_path(Path("/x/y")) == "<code>/x/y</code>"


# ============================================================================
# T2 — path-like values in the tool-use status line + permission-prompt body
# render as <code> (stop Telegram /segment auto-linkify); injection-proof escaping
# ============================================================================
#
# Telegram auto-linkifies each "/segment" of a bare path in a bot message as a fake
# command-link. P6/R6 fixed the command replies (code_path, bot.py); T2 fixes the two
# remaining surfaces: the "▶️ Tool(file_path=/a/b)" status line and the
# "🔐 Permission needed …" prompt body. Both now carry the SB3-safe summary inside
# <code>…</code> and are sent with parse_mode="HTML". The tool input is
# attacker-influenceable, so EVERYTHING interpolated MUST be html.escaped — a hostile
# field renders as inert text, never markup, and never breaks the HTML message.

# Telegram's allowed HTML tags (subset we ever emit here). Used to assert validity:
# after dropping these tags, NO bare "<" / ">" may remain (else Telegram rejects the
# send) — i.e. every metacharacter from the (attacker-influenceable) tool input is
# escaped and only OUR wrapper tags are live markup.
_TG_TAGS = ("<code>", "</code>", "<b>", "</b>", "<i>", "</i>", "<pre>", "</pre>")


def _strip_known_tags(s: str) -> str:
    for t in _TG_TAGS:
        s = s.replace(t, "")
    return s


def _assert_valid_telegram_html(s: str) -> None:
    """No bare angle brackets survive once our wrapper tags are removed (valid HTML)."""
    bare = _strip_known_tags(s)
    assert "<" not in bare and ">" not in bare, f"un-escaped angle bracket in: {s!r}"
    # <code> wrappers are balanced (every open has a close).
    assert s.count("<code>") == s.count("</code>")


def test_tool_use_line_wraps_path_in_code_not_bare_segments():
    # A Read/Write/Bash status line shows the path inside <code>…</code> so Telegram
    # renders it as inert monospace, NOT a row of tappable "/segment" fake commands.
    ev = ToolUseEvent(
        tool_name="Write", tool_input_summary="Write(file_path=/tmp/p5verify/a, content=<500 chars>)"
    )
    line = tool_use_line(ev)
    # The path is inside a <code> wrapper (monospace), not bare.
    assert "<code>" in line and "</code>" in line
    assert "/tmp/p5verify/a" in line
    # The path is NOT present OUTSIDE the <code> span (a bare path is what linkifies). Drop
    # the whole code span and confirm the path no longer appears.
    inner = line[line.index("<code>") + len("<code>") : line.index("</code>")]
    assert "/tmp/p5verify/a" in inner
    outside = line.replace(f"<code>{inner}</code>", "")
    assert "/tmp/p5verify/a" not in outside
    # The "<500 chars>" body marker is escaped (it would break the HTML message otherwise).
    assert "&lt;500 chars&gt;" in line
    assert "<500 chars>" not in line
    _assert_valid_telegram_html(line)


def test_tool_use_line_render_event_is_html_status():
    # render_event tags the tool-use status action parse_mode="HTML" so T7 sends the
    # <code> wrapper as real markup (without it the literal tags would show).
    ev = ToolUseEvent(tool_name="Read", tool_input_summary="Read(file_path=/a/b/c.py)")
    action = render_event(ev)
    assert action.op == "edit_status"
    assert action.verbatim is False
    assert action.parse_mode == "HTML"
    assert "<code>" in action.text


def test_tool_use_line_hostile_input_is_fully_escaped_valid_html():
    # SECURITY: a misaligned / prompt-injected Claude could put HTML metacharacters in a
    # file_path/command. They MUST render escaped (inert text), never as markup, and the
    # message must stay valid HTML (Telegram rejects invalid HTML → a dropped status).
    hostile = "Bash(command=</code><b>x</b><script>alert(1)</script>, file_path=/a&b/<x>)"
    ev = ToolUseEvent(tool_name="Bash", tool_input_summary=hostile)
    line = tool_use_line(ev)
    # No live <b>/<script> tag leaked: the only live tags are our <code> wrapper.
    assert "<b>" not in line and "<script>" not in line
    assert "&lt;b&gt;" in line and "&lt;script&gt;" in line
    assert "&lt;/code&gt;" in line  # the injected closing tag is inert
    assert "&amp;" in line  # the "&" in /a&b is escaped exactly once
    assert "&amp;amp;" not in line  # not double-escaped
    _assert_valid_telegram_html(line)


def test_status_line_has_no_paths_stays_plain_no_code():
    # The lifecycle/health status line (rate_limit / "thinking…") carries no path, so it
    # stays plain — no <code>, no parse_mode forced. (Only the tool-use line is HTML.)
    action = render_event(StatusEvent(phase="rate_limit", detail="retry in 5s"))
    assert action.op == "edit_status"
    assert "<code>" not in action.text
    assert action.parse_mode is None


def test_permission_body_wraps_summary_in_code_html():
    # The "🔐 Permission needed …" body shows the (body-free) summary inside <code> and is
    # an HTML message (parse_mode="HTML"), with the plain body as the raw fallback.
    action = render_event(
        make_permission(
            tool_name="Write",
            tool_input_summary="Write(file_path=/tmp/p5verify/a, content=<200 chars>)",
        )
    )
    assert action.op == "new"
    assert action.verbatim is True
    assert action.parse_mode == "HTML"
    assert "<code>" in action.text and "</code>" in action.text
    # The path lives inside the code span; the body marker is escaped (valid HTML).
    assert "/tmp/p5verify/a" in action.text
    assert "&lt;200 chars&gt;" in action.text
    assert "<200 chars>" not in action.text
    # The verdict prose + tool name are still present (escaped) and readable.
    assert "Permission needed" in action.text
    assert "Write" in action.text
    assert "Allow once" in action.text
    _assert_valid_telegram_html(action.text)
    # A parallel raw plain fallback is carried for the HTML-rejection path (T7 resends it).
    assert action.plain_chunks
    assert "/tmp/p5verify/a" in "".join(action.plain_chunks)
    assert "<code>" not in "".join(action.plain_chunks)  # the fallback is plain text


def test_permission_body_hostile_input_is_fully_escaped_valid_html():
    # SECURITY (this is the operator's approve/deny surface): a hostile tool_input must
    # render escaped + the message stay valid HTML so the prompt actually SENDS (an invalid
    # HTML body would be rejected by Telegram = the operator never sees the prompt = worse).
    hostile = "Write(file_path=</code><b>pwn</b>/&/etc, content=<999 chars>)"
    action = render_event(
        make_permission(tool_name="Ev<il>", tool_input_summary=hostile)
    )
    assert action.parse_mode == "HTML"
    body = action.text
    # No live injected tag; the tool NAME (also attacker-shaped) is escaped too.
    assert "<b>" not in body and "</code><b>" not in body
    assert "&lt;b&gt;pwn&lt;/b&gt;" in body
    assert "Ev&lt;il&gt;" in body  # the hostile tool name rendered inert
    assert "<il>" not in body
    assert "&amp;" in body and "&amp;amp;" not in body
    _assert_valid_telegram_html(body)


def test_permission_body_still_body_free_after_code_wrap_sb3():
    # SB3 regression: wrapping in <code> is a RENDERING change only — it must NOT expand the
    # already-collapsed body. A Write's 600-char content stays "content=<600 chars>" (escaped),
    # never the raw 600 chars.
    raw_body = "S3CR3T-" + "x" * 593
    assert len(raw_body) == 600
    summary = "Write(file_path=/tmp/secret.txt, content=<600 chars>)"
    action = render_event(make_permission(tool_name="Write", tool_input_summary=summary))
    assert "content=&lt;600 chars&gt;" in action.text
    assert raw_body not in action.text
    assert "S3CR3T" not in action.text
    # And the plain fallback is likewise body-free.
    assert raw_body not in "".join(action.plain_chunks)


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
    # The buffered newest line is the last tool_use — now <code>-wrapped HTML (T2/R6), and the
    # Coalescer carries its parse_mode forward (without that the <code> tags would render
    # literally because _emit_status rebuilds the action).
    flushed = coalescer.flush().actions[0]
    assert flushed.text == "▶️ <code>Bash(command=ls)</code>"
    assert flushed.parse_mode == "HTML"


def test_coalescer_parse_mode_tracks_newest_status_line():
    # T2: text + parse_mode are replaced together (newest-wins). An HTML tool-use line
    # buffered then REPLACED by a plain lifecycle status must flush with parse_mode=None —
    # the slot never pairs a stale HTML parse_mode with the new plain text (which would make
    # Telegram try to parse a non-existent entity / mis-render).
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=10.0)
    # Leading-edge HTML tool line fires immediately…
    first = coalescer.offer(
        ToolUseEvent(tool_name="Read", tool_input_summary="Read(file_path=/a/b)")
    ).actions
    assert first and first[0].parse_mode == "HTML" and "<code>" in first[0].text
    # …a plain status replaces it in the buffer; the flush carries plain parse_mode.
    coalescer.offer(StatusEvent(phase="rate_limit", detail="retry in 5s"))
    flushed = coalescer.flush().actions[0]
    assert flushed.parse_mode is None
    assert "<code>" not in flushed.text
    assert "rate_limit" in flushed.text


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


# ===========================================================================
# P9 / T3 — cost + usage surfacing on the done message (num_turns + cost).
#
# ResultEvent already carries total_cost_usd + num_turns; the per-turn done render
# used to drop them whenever there was result_text. done_footer_suffix builds the
# "· N turns · $X.XX" suffix (only the fields the SDK provided), and _render_result
# appends it onto the prose's last chunk (and the bare footer).
# ===========================================================================


def test_done_footer_suffix_both_present():
    res = ResultEvent(
        session_id="s", is_error=False, subtype="success", num_turns=3, total_cost_usd=0.012
    )
    assert done_footer_suffix(res) == " · 3 turns · $0.01"


def test_done_footer_suffix_only_turns():
    res = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=5)
    assert done_footer_suffix(res) == " · 5 turns"


def test_done_footer_suffix_only_cost():
    res = ResultEvent(
        session_id="s", is_error=False, subtype="success", total_cost_usd=1.5
    )
    assert done_footer_suffix(res) == " · $1.50"


def test_done_footer_suffix_absent_is_empty():
    # oneshot / a partial result may carry neither — omit gracefully (no dangling separator).
    res = ResultEvent(session_id="s", is_error=False, subtype="success")
    assert done_footer_suffix(res) == ""


def test_result_with_text_appends_turns_and_cost():
    # T3: the turns+cost are surfaced on the done message even WHEN there is result_text
    # (previously dropped). The suffix lands on the last chunk; the plain fallback gets it
    # too (positionally parallel).
    res = ResultEvent(
        session_id="s", is_error=False, subtype="success",
        num_turns=2, total_cost_usd=0.0734, result_text="All done — see **above**.",
    )
    action = render_event(res)
    assert action.text.endswith(" · 2 turns · $0.07")
    assert "above" in action.text
    assert action.plain_chunks[-1].endswith(" · 2 turns · $0.07")


def test_result_with_text_omits_suffix_when_sdk_absent():
    # No num_turns + no cost (oneshot-shaped) → the prose is sent UNCHANGED, no suffix.
    res = ResultEvent(
        session_id="s", is_error=False, subtype="success", result_text="Just the answer.",
    )
    action = render_event(res)
    assert action.text == "Just the answer."
    assert action.plain_chunks == ("Just the answer.",)


def test_done_footer_suffix_carries_no_secret():
    # SB3: the suffix is two SDK-reported numbers — never tool input/output or a path.
    res = ResultEvent(
        session_id="s", is_error=False, subtype="success", num_turns=1, total_cost_usd=0.01
    )
    suffix = done_footer_suffix(res)
    assert suffix == " · 1 turn · $0.01"  # only digits + the $ glyph (singular: "1 turn")


def test_done_footer_suffix_pluralizes_turn():
    # Cosmetic (UX): "1 turn" (singular) but "N turns" for N != 1 — never the ungrammatical
    # "1 turns". Cover the singular, the plural, and the zero-edge (also plural: "0 turns").
    one = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=1)
    assert done_footer_suffix(one) == " · 1 turn"
    many = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=2)
    assert done_footer_suffix(many) == " · 2 turns"
    zero = ResultEvent(session_id="s", is_error=False, subtype="success", num_turns=0)
    assert done_footer_suffix(zero) == " · 0 turns"


# ===========================================================================
# T6 (P9) — notification polish + smart-reply chips (render-layer PURE units).
#   * switch callback codec round-trip + byte budget + defensive decode.
#   * [Open <project>] keyboard.
#   * queued-counter suffix on the notification builders.
#   * quick-reply chips: ReplyKeyboardMarkup (one-time) + ReplyKeyboardRemove dismiss.
# ===========================================================================


def test_switch_callback_round_trips():
    # T6: encode_switch_callback -> decode_callback recovers kind="switch" + the project name.
    data = encode_switch_callback("alpha")
    assert data == f"{KIND_SWITCH}|alpha|s"
    cb = decode_callback(data)
    assert cb is not None
    assert cb.kind == "switch"
    assert cb.switch_to == "alpha"


def test_switch_callback_within_byte_budget_for_max_name():
    # T6: a max-length SB4 name (32 chars) stays well under Telegram's 64-byte callback limit.
    name = "p" * 32
    data = encode_switch_callback(name)
    assert len(data.encode("utf-8")) <= CALLBACK_LIMIT
    cb = decode_callback(data)
    assert cb is not None and cb.switch_to == name


def test_switch_callback_does_not_collide_with_hold_kinds():
    # T6: the switch kind char 'w' is distinct from ask/other/plan/permission, so a switch
    # callback never decodes to a hold (and vice versa) — collision-free.
    assert decode_callback(encode_switch_callback("alpha")).kind == "switch"
    # An ask/plan/permission callback never decodes to a switch.
    assert decode_callback(encode_callback("a", REAL_TOOL_USE_ID, question_index=0, option_index=0)).kind == "ask"
    assert decode_callback(encode_callback("p", REAL_TOOL_USE_ID, plan_action="a")).kind == "plan"
    assert decode_callback(encode_callback(KIND_PERMISSION, REAL_TOOL_USE_ID, payload="o")).kind == "permission"


def test_decode_rejects_forged_switch_name_and_payload():
    # T6 (SB1 trust boundary): a switch callback with a non-SB4 name (illegal chars / too
    # long) or a wrong payload char is forged/stale -> decode returns None (resolves nothing).
    assert decode_callback("w|bad name|s") is None       # space is not in the SB4 charset
    assert decode_callback("w|" + "p" * 33 + "|s") is None  # over 32 chars
    assert decode_callback("w|alpha|x") is None           # wrong payload char
    assert decode_callback("w||s") is None                # empty name


def test_encode_switch_callback_rejects_pipe_in_name():
    # Defensive: a '|' in the name would break the 3-field scheme -> ValueError at build.
    with pytest.raises(ValueError):
        encode_switch_callback("a|b")
    with pytest.raises(ValueError):
        encode_switch_callback("")


def test_open_project_keyboard_carries_switch_callback():
    # T6: the [Open <name>] button is a single inline button whose callback_data is the
    # compact switch encoding (decodes back to the project name).
    kb = open_project_keyboard("beta")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 1
    assert buttons[0].text == "📂 Open beta"
    assert decode_callback(buttons[0].callback_data).switch_to == "beta"


# ---- P11 T2: the attach callback codec + the /sessions attach keyboard -----


def test_attach_callback_round_trips():
    # P11 T2: encode_attach_callback -> decode_callback recovers kind="attach" + the session id.
    sid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"  # a real UUID-shaped id
    data = encode_attach_callback(sid)
    assert data == f"{KIND_ATTACH}|{sid}|x"
    cb = decode_callback(data)
    assert cb is not None
    assert cb.kind == "attach"
    assert cb.attach_session_id == sid


def test_attach_callback_within_byte_budget_for_uuid():
    # A 36-char UUID id stays well under Telegram's 64-byte callback limit.
    sid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    data = encode_attach_callback(sid)
    assert len(data.encode("utf-8")) <= CALLBACK_LIMIT
    assert decode_callback(data).attach_session_id == sid


def test_attach_callback_does_not_collide_with_other_kinds():
    # P11 T2: the attach kind char 't' is distinct from ask/other/plan/permission/switch, so
    # an attach callback never decodes to any of them (and vice versa) — collision-free.
    assert decode_callback(encode_attach_callback("sess-1")).kind == "attach"
    assert decode_callback(encode_switch_callback("alpha")).kind == "switch"
    assert decode_callback(encode_callback("a", REAL_TOOL_USE_ID, question_index=0, option_index=0)).kind == "ask"
    assert decode_callback(encode_callback("p", REAL_TOOL_USE_ID, plan_action="a")).kind == "plan"
    assert decode_callback(encode_callback(KIND_PERMISSION, REAL_TOOL_USE_ID, payload="o")).kind == "permission"


def test_decode_rejects_forged_attach_id_and_payload():
    # SB1 trust boundary: an attach callback with a non-session-shaped id (illegal chars / too
    # long) or a wrong payload char is forged/stale -> decode returns None (adopts nothing).
    assert decode_callback("t|bad id|x") is None        # space is not in the id charset
    assert decode_callback("t|" + "a" * 49 + "|x") is None  # over the 48-char bound
    assert decode_callback("t|sess-1|s") is None         # wrong payload char ('s' is switch)
    assert decode_callback("t||x") is None               # empty id


def test_encode_attach_callback_rejects_pipe_in_id():
    # Defensive: a '|' in the id would break the 3-field scheme -> ValueError at build.
    with pytest.raises(ValueError):
        encode_attach_callback("a|b")
    with pytest.raises(ValueError):
        encode_attach_callback("")


def test_sessions_keyboard_one_attach_button_per_session():
    from claude_tg.render import sessions_keyboard

    class _S:
        def __init__(self, sid):
            self.session_id = sid

    kb = sessions_keyboard([_S("aaaa1111-2222-3333-4444-555566667777"), _S("bbbb")])
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 2
    # Each button text shows the SHORT id; the callback_data carries the FULL id.
    assert buttons[0].text.startswith("📎 Attach aaaa1111")
    assert decode_callback(buttons[0].callback_data).attach_session_id == "aaaa1111-2222-3333-4444-555566667777"
    assert decode_callback(buttons[1].callback_data).attach_session_id == "bbbb"


def test_sessions_keyboard_empty_is_none_and_caps():
    from claude_tg.render import sessions_keyboard

    class _S:
        def __init__(self, sid):
            self.session_id = sid

    assert sessions_keyboard([]) is None  # no sessions → no keyboard (listing sent alone)
    # The button count is capped; an id-less session is skipped (no button, no crash).
    many = [_S(f"sess-{i}") for i in range(20)] + [_S(None)]
    kb = sessions_keyboard(many, cap=5)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert len(buttons) == 5  # honored the cap


def test_queued_suffix():
    # T6: " (N more waiting)" only when N>=1; 0/negative -> "".
    assert queued_suffix(0) == ""
    assert queued_suffix(-3) == ""
    assert queued_suffix(1) == " (1 more waiting)"
    assert queued_suffix(4) == " (4 more waiting)"


def test_notify_builders_append_queued_counter():
    # T6: the queued counter rides the attention/done/error pings when >0; omitted at 0.
    assert notify_attention("alpha", "permission") == "🔔 alpha — Claude needs approval"
    assert notify_attention("alpha", "permission", queued_waiting=2) == (
        "🔔 alpha — Claude needs approval (2 more waiting)"
    )
    assert notify_done("alpha") == "✅ alpha — done"
    assert notify_done("alpha", queued_waiting=1) == "✅ alpha — done (1 more waiting)"
    assert notify_error("alpha", "turn_error") == "⚠️ alpha — turn_error"
    assert notify_error("alpha", "turn_error", queued_waiting=3) == (
        "⚠️ alpha — turn_error (3 more waiting)"
    )


def test_notify_builders_body_free_with_queued_counter():
    # SB3: even with the queued counter, the ping carries ONLY the name + a fixed phrase + a
    # number — never any event body. A name with no secret + a count is all that appears.
    msg = notify_attention("proj_42", "plan", queued_waiting=5)
    assert msg == "🔔 proj_42 — proposes a plan (5 more waiting)"
    # No tool input / question text / plan body could be here (the builder takes none).


def test_quick_reply_keyboard_is_one_time_and_has_common_chips():
    # T6: the chips are a ReplyKeyboardMarkup, one_time_keyboard=True, with the common answers.
    kb = quick_reply_keyboard()
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.one_time_keyboard is True
    chips = [b.text for row in kb.keyboard for b in row]
    for expected in ("proceed", "keep it minimal", "explain first", "use TypeScript"):
        assert expected in chips, f"missing quick-reply chip {expected!r}"


def test_quick_reply_dismiss_is_a_keyboard_remove():
    # T6: the dismiss object is a ReplyKeyboardRemove (clears the one-time chips after capture).
    assert isinstance(quick_reply_dismiss(), ReplyKeyboardRemove)


# ---------------------------------------------------------------------------
# /sessions listing (P11 T1) — discovered sessions merged with bot projects (PURE)
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402
from typing import Optional  # noqa: E402

from claude_tg.render import (  # noqa: E402
    SESSIONS_EMPTY_NOTICE,
    ProjectMark,
    relative_age,
    sessions_listing,
    short_session_id,
)


@dataclass
class _Sess:
    """A minimal stand-in for sessions_discovery.DiscoveredSession (attribute access only)."""

    session_id: str
    cwd: Optional[str] = "/work/a"
    title: Optional[str] = "do a thing"
    last_active: Optional[int] = 1000
    running: bool = False


def test_sessions_listing_empty_returns_notice():
    assert sessions_listing([], {}, now=0.0) == SESSIONS_EMPTY_NOTICE


def test_sessions_listing_shows_short_id_running_glyph_and_code_cwd():
    sessions = [
        _Sess(session_id="abcdef0123456789", cwd="/work/proj", running=True, last_active=0),
        _Sess(session_id="0011223344556677", cwd="/work/other", running=False, last_active=0),
    ]
    out = sessions_listing(sessions, {}, now=10.0)
    # Short id (8 chars) only — the full id never appears.
    assert "abcdef01" in out and "abcdef0123456789" not in out
    # Running 🟢 vs idle ⚪.
    assert "🟢" in out and "⚪" in out
    # cwd wrapped in <code> (R6 — inert monospace, not tappable /segments), HTML parse mode.
    assert "<code>/work/proj</code>" in out
    # No bare cwd outside <code> (a bare copy would auto-linkify).
    assert ">/work/proj<" not in out.replace("<code>/work/proj</code>", "")


def test_sessions_listing_merges_and_marks_bot_known_and_active():
    sessions = [
        _Sess(session_id="sess-active", cwd="/w/a"),
        _Sess(session_id="sess-known", cwd="/w/b"),
        _Sess(session_id="sess-unknown", cwd="/w/c"),
    ]
    marks = {
        "sess-active": ProjectMark(name="alpha", active=True),
        "sess-known": ProjectMark(name="beta", active=False),
    }
    out = sessions_listing(sessions, marks, now=2000.0)
    lines = out.splitlines()
    active_line = next(line for line in lines if "alpha" in line)
    known_line = next(line for line in lines if "beta" in line)
    unknown_line = next(line for line in lines if "sess-unk" in line)
    # The active bot project is marked → and ✓ <name>; the known one ✓ but ·; the unknown
    # neither.
    assert "→" in active_line and "✓ <b>alpha</b>" in active_line
    assert "·" in known_line and "✓ <b>beta</b>" in known_line
    assert "✓" not in unknown_line and "→" not in unknown_line
    # Dedup-by-id: each discovered session appears exactly ONCE (it is not duplicated by being
    # both discovered AND a bot project).
    assert sum(1 for line in lines if "sess-act" in line) == 1


def test_sessions_listing_truncates_and_escapes_title_body_free():
    # SB3: a long, HTML-bearing first-prompt is clipped AND escaped (no raw markup, no flood).
    nasty = "<script>alert(1)</script> " + "x" * 200
    sessions = [_Sess(session_id="s1", title=nasty, cwd="/w")]
    out = sessions_listing(sessions, {}, now=0.0)
    assert "<script>" not in out  # escaped
    assert "&lt;script&gt;" in out
    assert "…" in out  # truncated
    # The clipped+escaped title row is far shorter than the raw 200-char prompt.
    assert len(out) < 400


def test_sessions_listing_title_collapses_newlines():
    sessions = [_Sess(session_id="s1", title="line one\nline two\nline three", cwd="/w")]
    out = sessions_listing(sessions, {}, now=0.0)
    assert "line one line two line three" in out


def test_sessions_listing_handles_missing_cwd_and_title():
    sessions = [_Sess(session_id="s1", cwd=None, title=None, last_active=None)]
    out = sessions_listing(sessions, {}, now=0.0)
    assert "(no path)" in out and "(untitled)" in out and "unknown" in out


def test_short_session_id_escapes_and_clips():
    assert short_session_id("abcdef0123456789") == "abcdef01"
    assert short_session_id(None) == ""
    # An odd id with HTML metacharacters is clipped to 8 chars FIRST, then escaped (defensive
    # — a session id is UUID-shaped, but a hand-crafted/forged value can never inject markup).
    out = short_session_id("<b>xxxxx-rest")
    assert "&lt;b&gt;" in out and "<b>" not in out


def test_relative_age_units_and_rb1():
    assert relative_age(1000, now=1000) == "just now"
    assert relative_age(1000, now=1000 + 30) == "just now"
    assert relative_age(1000, now=1000 + 120) == "2m ago"
    assert relative_age(1000, now=1000 + 3 * 3600) == "3h ago"
    assert relative_age(1000, now=1000 + 2 * 86400) == "2d ago"
    # RB1: a non-numeric value → "unknown"; a future timestamp (skew) → "just now", not negative.
    assert relative_age(None, now=1000) == "unknown"
    assert relative_age("nope", now=1000) == "unknown"
    assert relative_age(2000, now=1000) == "just now"


# ---------------------------------------------------------------------------
# /sessions sort + cap + truncation footer (P11 T1 live-fix — the >4096 crash)
# ---------------------------------------------------------------------------

from claude_tg.render import (  # noqa: E402
    cap_sessions,
    prioritize_sessions,
)
from claude_tg.util import TELEGRAM_MAX, _utf16_len  # noqa: E402


def test_prioritize_sessions_active_first_then_known_then_recent():
    # bucket order: active → bot-known → others-by-recency-desc.
    s_active = _Sess(session_id="active", last_active=1)        # oldest, but active
    s_known = _Sess(session_id="known", last_active=2)          # old, but bot-known
    s_new = _Sess(session_id="plain-new", last_active=1000)     # newest plain
    s_old = _Sess(session_id="plain-old", last_active=500)      # older plain
    marks = {"active": ProjectMark(name="A", active=True), "known": ProjectMark(name="K")}
    ordered = prioritize_sessions([s_old, s_new, s_known, s_active], marks)
    ids = [s.session_id for s in ordered]
    # active first (despite being oldest), then bot-known, then plain by recency DESC.
    assert ids == ["active", "known", "plain-new", "plain-old"]


def test_prioritize_sessions_missing_recency_sorts_last_in_bucket():
    a = _Sess(session_id="has-recency", last_active=100)
    b = _Sess(session_id="no-recency", last_active=None)
    ordered = prioritize_sessions([b, a], {})
    assert [s.session_id for s in ordered] == ["has-recency", "no-recency"]


def test_cap_sessions_keeps_all_known_plus_top_recent():
    # 3 bot-known (incl. active) + 10 plain; limit 5 → all 3 known + top 2 recent = 5.
    known = [_Sess(session_id=f"k{i}", last_active=i) for i in range(3)]  # old
    plain = [_Sess(session_id=f"p{i}", last_active=1000 + i) for i in range(10)]
    marks = {"k0": ProjectMark(name="a", active=True), "k1": ProjectMark(name="b"),
             "k2": ProjectMark(name="c")}
    ordered = prioritize_sessions(known + plain, marks)
    capped = cap_sessions(ordered, marks, limit=5)
    ids = {s.session_id for s in capped}
    assert {"k0", "k1", "k2"} <= ids  # ALL bot-known kept even though old
    assert len(capped) == 5  # exactly the limit
    # The 2 plain kept are the most-recent (p9, p8), not arbitrary.
    plain_kept = [s.session_id for s in capped if s.session_id.startswith("p")]
    assert set(plain_kept) == {"p9", "p8"}


def test_cap_sessions_never_hides_known_even_if_known_exceed_limit():
    # 8 bot-known but limit 3 → all 8 kept (operator's own are never hidden).
    known = [_Sess(session_id=f"k{i}", last_active=i) for i in range(8)]
    marks = {f"k{i}": ProjectMark(name=f"n{i}", active=(i == 0)) for i in range(8)}
    capped = cap_sessions(prioritize_sessions(known, marks), marks, limit=3)
    assert {s.session_id for s in capped} == {f"k{i}" for i in range(8)}


def test_sessions_listing_caps_rows_and_shows_honest_footer():
    sessions = [_Sess(session_id=f"s{i:04d}aa", cwd=f"/w/{i}", last_active=1000 - i)
                for i in range(100)]
    out = sessions_listing(sessions, {}, now=2000.0, limit=15)
    # Header reports the TRUE total; only `limit` rows are rendered.
    assert "(100 found)" in out
    row_lines = [line for line in out.splitlines() if line.startswith(("→", "·"))]
    assert len(row_lines) == 15
    # Honest footer names shown-of-total and points at /attach (escaped angle brackets).
    assert "Showing 15 of 100 sessions" in out
    assert "<code>/attach &lt;id&gt;</code>" in out
    # The most-recent 15 are shown (s0000..s0014 by last_active desc), not an arbitrary slice.
    assert "s0000aa" in out and "s0014aa" in out and "s0015aa" not in out


def test_sessions_listing_no_footer_when_all_shown():
    sessions = [_Sess(session_id=f"s{i}", cwd="/w", last_active=i) for i in range(5)]
    out = sessions_listing(sessions, {}, now=100.0, limit=15)
    assert "Showing" not in out  # total (5) ≤ shown → no truncation footer


def test_sessions_listing_capped_stays_well_under_4096():
    # Even with long cwds + titles, the CAPPED listing is comfortably under Telegram's limit.
    sessions = [
        _Sess(session_id=f"sess{i:04d}", cwd="/Users/ray/dev/" + "deep/" * 30 + f"proj{i}",
              title="x" * 500, last_active=2000 - i)
        for i in range(300)
    ]
    out = sessions_listing(sessions, {}, now=3000.0)  # default limit
    assert _utf16_len(out) <= TELEGRAM_MAX, f"capped listing is {_utf16_len(out)} > 4096"


def test_sessions_listing_UNCAPPED_would_overflow_4096_mutation_probe():
    # ⭐ Mutation-probe for the cap: with the cap effectively REMOVED (a huge limit) the SAME
    # 300-session input renders a single message FAR over Telegram's 4096 limit — the exact
    # live crash. This pins that the cap (not luck) is what keeps the message legal: if a
    # future change rendered everything, this length assertion would fire.
    sessions = [
        _Sess(session_id=f"sess{i:04d}", cwd="/Users/ray/dev/" + "deep/" * 30 + f"proj{i}",
              title="x" * 500, last_active=2000 - i)
        for i in range(300)
    ]
    uncapped = sessions_listing(sessions, {}, now=3000.0, limit=10_000)
    assert _utf16_len(uncapped) > TELEGRAM_MAX  # proves an uncapped render WOULD overflow


def test_sessions_keyboard_buttons_are_most_relevant_active_first():
    # The attach buttons follow the SAME relevance order as the listing (active → known →
    # recent), not SDK order — so the capped buttons cover the operator's own + freshest.
    from claude_tg.render import sessions_keyboard

    s_active = _Sess(session_id="active-1", last_active=1)   # oldest but active
    s_known = _Sess(session_id="known-1", last_active=2)     # old but known
    plain = [_Sess(session_id=f"p{i}", last_active=1000 + i) for i in range(10)]  # newest
    marks = {"active-1": ProjectMark(name="A", active=True), "known-1": ProjectMark(name="K")}
    kb = sessions_keyboard(plain + [s_known, s_active], marks=marks, cap=3)
    buttons = [b for row in kb.inline_keyboard for b in row]
    ids = [decode_callback(b.callback_data).attach_session_id for b in buttons]
    # active + known lead the (capped) buttons despite being the oldest sessions.
    assert ids[0] == "active-1" and ids[1] == "known-1"
    assert len(buttons) == 3
