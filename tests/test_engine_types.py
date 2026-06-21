"""Unit tests for the decision -> substrate mapping (the load-bearing [FLAG] logic).

These assert the empirical contract from `normalized_interface.md` §2 / ADR-001:
the native ``answers`` map keyed by question text; plan reject rides the deny
message (no native feedback field); permission allow carries ``updated_input`` and
deny carries ``message``; and every allow returns a dict (the B ``updatedInput``
record gotcha). No SDK / network — pure dataclass logic.
"""

import pytest

from claude_tg.engine.types import (
    Cancel,
    FreeTextReply,
    PermissionVerdict,
    PlanVerdict,
    QuestionAnswer,
    SubstrateDecision,
    decision_to_substrate,
)

# --- question answer ⭐ : native answers-map keyed by question text -----------


def test_question_answer_is_allow_with_native_answers_map():
    # [FLAG] C3: answer rides the ALLOW channel as updated_input["answers"], keyed
    # by VERBATIM question text -> selected label, merged onto the original input.
    tool_input = {"questions": [{"question": "Pick one?"}]}
    d = decision_to_substrate(
        QuestionAnswer({"Pick one?": "Bravo"}), tool_input=tool_input
    )
    assert d.allow is True
    assert d.message is None
    assert d.updated_input == {
        "questions": [{"question": "Pick one?"}],
        "answers": {"Pick one?": "Bravo"},
    }


def test_question_answer_multiselect_comma_separated_in_one_string():
    # Multi-select = comma-separated labels in the one answer string for that question.
    d = decision_to_substrate(
        QuestionAnswer({"Pick some?": "Alpha, Bravo"}), tool_input={}
    )
    assert d.allow is True
    assert d.updated_input == {"answers": {"Pick some?": "Alpha, Bravo"}}


def test_question_answer_does_not_mutate_caller_input():
    tool_input = {"questions": [1]}
    decision_to_substrate(QuestionAnswer({"Q?": "A"}), tool_input=tool_input)
    assert tool_input == {"questions": [1]}  # base copied, not mutated


# --- plan verdict ⭐ : approve=allow / reject rides deny message --------------


def test_plan_approve_is_bare_allow_carrying_record():
    # Approve -> allow; echoes original input so the updatedInput record gotcha holds.
    d = decision_to_substrate(PlanVerdict(approve=True), tool_input={"plan": "do X"})
    assert d.allow is True
    assert d.message is None
    assert d.updated_input == {"plan": "do X"}


def test_plan_reject_rides_deny_message():
    # [FLAG] C4: NO native plan-feedback field — reject feedback rides the DENY message.
    d = decision_to_substrate(PlanVerdict(approve=False, feedback="add a logging step"))
    assert d.allow is False
    assert d.message == "add a logging step"
    assert d.updated_input is None


def test_plan_reject_without_feedback_still_denies():
    d = decision_to_substrate(PlanVerdict(approve=False))
    assert d.allow is False
    assert d.message == ""  # empty, never None on deny


# --- permission verdict : allow carries updated_input / deny carries message --


def test_permission_allow_carries_updated_input():
    d = decision_to_substrate(
        PermissionVerdict("allow"), tool_input={"file_path": "/a", "content": "x"}
    )
    assert d.allow is True
    # No explicit updated_input on the verdict -> echo the original (modified-input
    # channel left unchanged) so the allow always carries a record.
    assert d.updated_input == {"file_path": "/a", "content": "x"}


def test_permission_allow_with_modified_input_overrides_original():
    d = decision_to_substrate(
        PermissionVerdict("allow", updated_input={"file_path": "/safe"}),
        tool_input={"file_path": "/risky"},
    )
    assert d.allow is True
    assert d.updated_input == {"file_path": "/safe"}  # rewrite wins


def test_permission_deny_carries_message():
    d = decision_to_substrate(PermissionVerdict("deny", message="not allowed"))
    assert d.allow is False
    assert d.message == "not allowed"
    assert d.updated_input is None


def test_permission_allow_always_returns_a_dict_record_b_gotcha():
    # [FLAG] B: an allow MUST carry updatedInput as a record (else ZodError). Even
    # with no tool_input and no updated_input, allow yields {} (a dict), never None.
    d = decision_to_substrate(PermissionVerdict("allow"))
    assert d.allow is True
    assert d.updated_input == {}
    assert isinstance(d.updated_input, dict)


# --- cancel ------------------------------------------------------------------


def test_cancel_maps_to_deny():
    d = decision_to_substrate(Cancel())
    assert d.allow is False
    assert d.message == "cancelled"


# --- free-text reply is NOT a verdict ---------------------------------------


def test_free_text_reply_is_not_a_permission_verdict():
    # Free-text is a new turn (send), not a verdict — mapping it is a programming error.
    with pytest.raises(TypeError):
        decision_to_substrate(FreeTextReply("hello"))


def test_unknown_decision_type_raises():
    with pytest.raises(TypeError):
        decision_to_substrate(object())  # type: ignore[arg-type]


# --- SubstrateDecision constructors -----------------------------------------


def test_make_allow_guarantees_dict_and_copies():
    src = {"a": 1}
    d = SubstrateDecision.make_allow(src)
    assert d.allow and d.updated_input == {"a": 1}
    d.updated_input["b"] = 2  # type: ignore[index]
    assert src == {"a": 1}  # input was copied, not aliased


def test_make_allow_none_yields_empty_dict():
    d = SubstrateDecision.make_allow(None)
    assert d.allow and d.updated_input == {}


def test_make_deny_defaults_to_empty_message():
    d = SubstrateDecision.make_deny()
    assert not d.allow and d.message == ""
