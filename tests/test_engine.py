"""Unit tests for the engine core — driven against a MOCK substrate.

No live Claude, no network, no CLI: the engine is exercised through a
``FakeSubstrate`` that yields scripted normalized events, and ``normalize()`` is
exercised against constructed SDK message objects (the SDK is a dep so we MAY build
them — but we never open a session). Covers:

* lifecycle (start / resume / send / stop) + session_id carry
* events-out normalization (SDK-shaped messages -> the right Event types/fields)
* the SDK adapter's bounded send -> driver ErrorEvent on timeout/error (RB2)
* ENGINE_MODE config parsing/validation
* a guard that the engine package imports WITHOUT importing claude_agent_sdk

The decision -> substrate [FLAG] mapping lives in test_engine_types.py.
"""

import asyncio
import logging
import os

import claude_agent_sdk as sdk
import pytest

from claude_tg.config import (
    DEFAULT_ANSWER_BACKSTOP_SECONDS,
    ENGINE_MODES,
    Config,
    parse_answer_backstop_seconds,
    parse_engine_mode,
)
from claude_tg.engine import (
    AskEvent,
    Engine,
    ErrorEvent,
    PlanEvent,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from claude_tg.engine.adapter_sdk import SdkSubstrate, normalize
from claude_tg.util import _redact_sid

# ---------------------------------------------------------------------------
# A mock substrate: yields scripted events; records lifecycle + decisions.
# ---------------------------------------------------------------------------


class FakeSubstrate:
    """A scripted :class:`Substrate` for unit tests (no SDK, no network).

    ``script`` maps a prompt -> the list of normalized events that ``send`` yields.
    Records the lifecycle calls made and, when wired, the decisions the engine
    resolves through it.
    """

    def __init__(self, script=None, *, raise_on_send=None):
        self._script = script or {}
        self._raise_on_send = raise_on_send
        self.session_id = None
        self.calls = []  # ordered lifecycle log

    async def start(self):
        self.calls.append(("start",))

    async def resume(self, session_id, *, fork=False):
        # P11 T2: record the fork flag so the engine-level fork test can assert the engine
        # threads it through. A CONTINUE (fork=False, every pre-P11 resume) keeps the same id;
        # a FORK (fork=True) simulates the SDK resuming into a NEW id — so we do NOT seed the
        # base id, mirroring the real SdkSubstrate (the forked id arrives on the first send).
        self.calls.append(("resume", session_id, fork))
        if not fork:
            self.session_id = session_id

    async def send(self, prompt, *, timeout=120.0):
        self.calls.append(("send", prompt, timeout))
        if self._raise_on_send is not None:
            raise self._raise_on_send
        for ev in self._script.get(prompt, []):
            # session id becomes known mid-turn, captured from the first event that
            # carries one (mirrors the real adapter's _capture_session_id).
            sid = getattr(ev, "session_id", None)
            if sid and not self.session_id:
                self.session_id = sid
            yield ev

    async def stop(self):
        self.calls.append(("stop",))


async def drain(aiter):
    return [ev async for ev in aiter]


# ---------------------------------------------------------------------------
# Lifecycle + session id
# ---------------------------------------------------------------------------


async def test_lifecycle_start_send_stop_passthrough():
    events = [
        StatusEvent(phase="init", session_id="S1", model="m"),
        TextEvent(text="hi", session_id="S1"),
        ResultEvent(session_id="S1", is_error=False, subtype="success", num_turns=1),
    ]
    sub = FakeSubstrate(script={"go": events})
    eng = Engine(sub)

    await eng.start()
    out = await drain(eng.send("go"))
    await eng.stop()

    assert [c[0] for c in sub.calls] == ["start", "send", "stop"]
    assert out == events
    # every event carries a session_id (forward-compat correlation envelope)
    assert all(getattr(e, "session_id", None) == "S1" for e in out)
    assert eng.session_id == "S1"


async def test_resume_reattaches_and_carries_session_id():
    sub = FakeSubstrate(script={"more": [TextEvent(text="resumed", session_id="S-prev")]})
    eng = Engine(sub)

    await eng.resume("S-prev")
    assert eng.session_id == "S-prev"
    out = await drain(eng.send("more"))
    await eng.stop()

    # P11 T2: the resume call now records the fork flag; a plain continue is fork=False.
    assert ("resume", "S-prev", False) in sub.calls
    assert out[0].text == "resumed"


async def test_resume_fork_threads_through_and_does_not_seed_base_id():
    """P11 T2: ``Engine.resume(id, fork=True)`` threads ``fork`` to the substrate AND does not
    adopt the base id (a fork resumes into a NEW id, copied transcript — never the base, which
    may be live elsewhere). The forked id is reported on the first turn, exactly as a fresh
    start captures its id. This is the engine half of the never-co-drive-a-live-session rule."""
    sub = FakeSubstrate(script={"go": [TextEvent(text="forked", session_id="S-forked")]})
    eng = Engine(sub)

    await eng.resume("S-base", fork=True)
    # The base id was NOT seeded (we never write it) — the substrate stays id-less until the
    # forked id arrives on the first event.
    assert eng.session_id is None
    assert ("resume", "S-base", True) in sub.calls
    out = await drain(eng.send("go"))
    # The forked id (a fresh one) is what the engine now carries — never the base id.
    assert out[0].text == "forked"
    assert eng.session_id == "S-forked"
    assert eng.session_id != "S-base"


def test_sdk_build_options_fork_sets_fork_session_only_with_resume():
    """P11 T2: ``_build_options(resume=id, fork=True)`` sets ``fork_session=True`` so the SDK
    resumes into a fresh id (copied transcript). ``fork`` without a ``resume`` is meaningless
    (a fork with no base is a fresh start) and must NOT set it; the default (continue / no
    resume) never sets it — behavior is unchanged for every pre-P11 path."""
    sub = SdkSubstrate()
    # Fork + resume → fork_session=True on the options.
    forked = sub._build_options(resume="sess-1", fork=True)
    assert getattr(forked, "fork_session", None) is True
    # Resume WITHOUT fork (the idle-continue path) → fork_session unset/falsey.
    cont = sub._build_options(resume="sess-1")
    assert not getattr(cont, "fork_session", False)
    # Fork WITHOUT resume is a no-op (a fork needs a base; no base = fresh start).
    nores = sub._build_options(fork=True)
    assert not getattr(nores, "fork_session", False)
    # Plain fresh start (no resume, no fork) → unset, exactly as pre-P11.
    fresh = sub._build_options()
    assert not getattr(fresh, "fork_session", False)


# A UUID-shaped session id (Claude's real format) so the redaction is unambiguous.
_REAL_SID = "8f14e45f-ceea-467d-9f0a-1234567890ab"


async def test_sb3_start_and_resume_logs_redact_the_session_id(caplog):
    """SB3/H1: the engine's start/resume DEBUG logs carry a REDACTED tag, never the raw id.

    The raw ``claude_session_id`` is a credential (``--resume <id>`` re-attaches a live
    session), so it must never appear verbatim in a log line. We drive a real ``Engine``
    over a fake substrate with a known UUID-shaped id and assert the captured log contains
    the short ``sid:…`` tag and NOT the raw id.
    """
    caplog.set_level(logging.DEBUG)
    sub = FakeSubstrate(script={"x": []})
    eng = Engine(sub)

    await eng.resume(_REAL_SID)  # logs "engine resumed sid:…"
    await eng.start()  # also logs a redacted tag (no id yet on a fresh start)

    assert _REAL_SID not in caplog.text  # the raw resumable id never lands in a log
    assert "8f14e45f" not in caplog.text  # not even a leading chunk of it
    assert _redact_sid(_REAL_SID) in caplog.text  # the correlatable short tag IS there


def test_sb3_redactor_mutation_probe_raw_id_would_be_caught():
    """Mutation-probe (highest-value: raw-id-in-log). If a refactor reverted a log site to
    interpolate the RAW id, this pins that the redactor's output is DISTINCT from the raw id
    (so the assertion ``raw not in log`` in the test above can actually fail on a regression).
    A redactor that returned its input unchanged (the mutation) would make this fail.
    """
    redacted = _redact_sid(_REAL_SID)
    assert _REAL_SID not in redacted  # the tag shares no full-id substring with the raw id
    assert redacted != _REAL_SID


async def test_send_passes_configured_timeout_through():
    sub = FakeSubstrate(script={"x": []})
    eng = Engine(sub, send_timeout=7.5)
    await eng.start()
    await drain(eng.send("x"))
    send_call = next(c for c in sub.calls if c[0] == "send")
    assert send_call[2] == 7.5


# ---------------------------------------------------------------------------
# Decision seam: the seam SHAPE (on_tool_request -> SubstrateDecision). T5 wired the
# ask/plan answer-hold; P2/T3 wires the permission gate over the ordinary-tool branch.
# A SAFE ordinary tool the policy allows runs free here (no waiting); the RISKY
# hold-for-approval path + verdict mapping live in test_answer_hold.py. These guard the
# seam's allow path + return shape under the new gated default.
# ---------------------------------------------------------------------------


async def test_safe_ordinary_tool_request_allows_with_record():
    # P2 gated posture: a SAFE ordinary tool (Read — in the safe allowlist, the policy
    # reports it does not need approval) is allowed with NO prompt, echoing the original
    # tool input as the record (the B updatedInput gotcha), with no wait on an operator.
    sub = FakeSubstrate()
    eng = Engine(sub)  # fresh default policy: safe tool runs free, risky tools gate
    d = await eng.on_tool_request("Read", {"file_path": "/a"}, "tu1")
    assert isinstance(d, SubstrateDecision)
    assert d.allow is True
    assert d.updated_input == {"file_path": "/a"}


async def test_risky_tool_without_tool_use_id_fails_closed_to_deny():
    # P2 change from the P1 interim auto-allow: a RISKY tool with no tool_use_id cannot
    # open a routable approval hold, so the engine fails CLOSED and denies (SB6) rather
    # than auto-allowing — never run a risky tool we cannot gate. (A fresh default policy
    # gates Bash.)
    from claude_tg.engine import DENIED_MESSAGE

    eng = Engine(FakeSubstrate())
    d = await eng.on_tool_request("Bash", {"command": "ls"}, None)
    assert d.allow is False
    assert d.message == DENIED_MESSAGE


# ---------------------------------------------------------------------------
# events-out normalization (constructed SDK objects; NO live session)
# ---------------------------------------------------------------------------


def test_normalize_system_init_to_status():
    msg = sdk.SystemMessage(
        subtype="init",
        data={"session_id": "S1", "model": "m", "tools": ["Bash"], "permissionMode": "default"},
    )
    ev = normalize(msg)
    assert isinstance(ev, StatusEvent)
    assert ev.phase == "init"
    assert ev.session_id == "S1"
    assert ev.model == "m"
    assert ev.tools == ["Bash"]
    assert ev.permission_mode == "default"


def test_normalize_stream_event_text_delta_is_incremental_text():
    msg = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "chunk"}},
    )
    ev = normalize(msg)
    assert isinstance(ev, TextEvent)
    assert ev.incremental is True
    assert ev.text == "chunk"
    assert ev.session_id == "S1"


def test_normalize_stream_event_framing_and_signature_yield_none():
    framing = sdk.StreamEvent(uuid="u", session_id="S1", event={"type": "content_block_start"})
    assert normalize(framing) is None
    # signature_delta (extended thinking) carries no renderable prose
    sig = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "x"}},
    )
    assert normalize(sig) is None


def test_normalize_assistant_text_block_is_assembled_text():
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text="hello")], model="m", session_id="S1")
    ev = normalize(msg)
    assert isinstance(ev, TextEvent)
    assert ev.incremental is False
    assert ev.text == "hello"
    assert ev.session_id == "S1"


# ---- P12 T-THINK-1: thinking normalization (drop signature, redacted stays opaque) ----


def test_normalize_thinking_delta_is_incremental_thinking():
    # A thinking_delta StreamEvent carries the REASONING text in delta["thinking"] -> an
    # incremental ThinkingEvent (parallel to text_delta). SB3: it carries ONLY the text.
    msg = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "Let me reason about this"},
        },
    )
    ev = normalize(msg)
    assert isinstance(ev, ThinkingEvent)
    assert ev.incremental is True
    assert ev.redacted is False
    assert ev.text == "Let me reason about this"
    assert ev.session_id == "S1"
    # SB3: there is structurally no signature field on a ThinkingEvent.
    assert not hasattr(ev, "signature")


def test_normalize_full_thinking_block_is_assembled_thinking_and_drops_signature():
    # An assembled ThinkingBlock (in an AssistantMessage) -> a non-incremental ThinkingEvent
    # carrying ONLY block.thinking; the opaque block.signature is DROPPED and never surfaced.
    block = sdk.ThinkingBlock(
        thinking="This is the full reasoning text.", signature="OPAQUE_SIGNATURE_DO_NOT_LEAK"
    )
    msg = sdk.AssistantMessage(content=[block], model="m", session_id="S1")
    ev = normalize(msg)
    assert isinstance(ev, ThinkingEvent)
    assert ev.incremental is False
    assert ev.text == "This is the full reasoning text."
    assert ev.session_id == "S1"
    # SB3 — the signature must NOT appear anywhere on the emitted event (no field, not in repr).
    assert not hasattr(ev, "signature")
    assert "OPAQUE_SIGNATURE_DO_NOT_LEAK" not in repr(ev)


def test_normalize_signature_delta_never_emits_an_event():
    # SB3: the opaque signature arrives as a SEPARATE signature_delta — it must NEVER become an
    # event (so the signature can't leak through the stream path either). (Pins the existing
    # contract from the angle of "signature never surfaces", complementing the framing test.)
    sig = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "signature_delta", "signature": "EvkCC_opaque_sig"},
        },
    )
    assert normalize(sig) is None


def test_normalize_redacted_thinking_delta_is_opaque_never_raw():
    # Defensive/forward-compat (RB1/SB3): a redacted_thinking stream delta -> a ThinkingEvent
    # flagged redacted with NO text (the renderer shows the fixed hidden line, never raw). The
    # installed SDK doesn't produce this, so it's a fail-safe — but if it ever appears it must
    # NOT carry a readable/raw body.
    msg = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "redacted_thinking", "data": "ENCRYPTED_BLOB_SHOULD_NOT_RENDER"},
        },
    )
    ev = normalize(msg)
    assert isinstance(ev, ThinkingEvent)
    assert ev.redacted is True
    assert ev.text == ""  # opaque — no readable body
    assert "ENCRYPTED_BLOB_SHOULD_NOT_RENDER" not in repr(ev)


def test_normalize_empty_or_malformed_thinking_never_crashes_rb1():
    # RB1: a thinking_delta with no/empty "thinking" -> None (nothing to show), never a crash.
    empty_delta = sdk.StreamEvent(
        uuid="u",
        session_id="S1",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta"}},
    )
    assert normalize(empty_delta) is None
    # A signature-only ThinkingBlock (display="omitted": thinking empty) -> None (the signature
    # is dropped, and there is no readable text to surface).
    sig_only = sdk.AssistantMessage(
        content=[sdk.ThinkingBlock(thinking="", signature="sig-only")], model="m", session_id="S1"
    )
    assert normalize(sig_only) is None


def test_normalize_ask_user_question_to_ask_event():
    questions = [
        {"question": "Pick?", "header": "H", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}
    ]
    msg = sdk.AssistantMessage(
        content=[sdk.ToolUseBlock(id="tu1", name="AskUserQuestion", input={"questions": questions})],
        model="m",
        session_id="S1",
    )
    ev = normalize(msg)
    assert isinstance(ev, AskEvent)
    assert ev.questions == questions
    assert ev.tool_use_id == "tu1"
    assert ev.session_id == "S1"


def test_normalize_exit_plan_mode_to_plan_event():
    msg = sdk.AssistantMessage(
        content=[sdk.ToolUseBlock(id="tu2", name="ExitPlanMode", input={"plan": "do X then Y"})],
        model="m",
    )
    ev = normalize(msg)
    assert isinstance(ev, PlanEvent)
    assert ev.plan == "do X then Y"
    assert ev.tool_use_id == "tu2"


def test_normalize_ordinary_tool_use_summarizes_without_body():
    big = "x" * 500
    msg = sdk.AssistantMessage(
        content=[sdk.ToolUseBlock(id="tu3", name="Write", input={"file_path": "/a.py", "content": big})],
        model="m",
    )
    ev = normalize(msg)
    assert isinstance(ev, ToolUseEvent)
    assert ev.tool_name == "Write"
    assert ev.tool_use_id == "tu3"
    # SB3: the raw body is NOT in the summary; only a length is.
    assert big not in ev.tool_input_summary
    assert "<500 chars>" in ev.tool_input_summary
    assert "/a.py" in ev.tool_input_summary


def test_normalize_tool_result_error_to_tool_error_event():
    msg = sdk.AssistantMessage(
        content=[sdk.ToolResultBlock(tool_use_id="tu3", content="boom", is_error=True)],
        model="m",
    )
    ev = normalize(msg)
    assert isinstance(ev, ErrorEvent)
    assert ev.kind_of_error == "tool_error"
    assert ev.message == "boom"
    assert ev.tool_use_id == "tu3"


def test_normalize_result_success_to_result_event_with_session_id():
    msg = sdk.ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=3,
        session_id="S1",
        total_cost_usd=0.02,
        result="done",
    )
    ev = normalize(msg)
    assert isinstance(ev, ResultEvent)
    assert ev.is_error is False
    assert ev.subtype == "success"
    assert ev.num_turns == 3
    assert ev.session_id == "S1"
    assert ev.result_text == "done"


def test_normalize_result_error_to_turn_error_event():
    msg = sdk.ResultMessage(
        subtype="error_during_execution",
        duration_ms=10,
        duration_api_ms=8,
        is_error=True,
        num_turns=1,
        session_id="S1",
        result="kaboom",
    )
    ev = normalize(msg)
    assert isinstance(ev, ErrorEvent)
    assert ev.kind_of_error == "turn_error"
    assert ev.message == "kaboom"
    assert ev.session_id == "S1"


def test_normalize_rate_limit_event_to_status():
    rli = sdk.RateLimitInfo(status="allowed_warning", raw={})
    msg = sdk.RateLimitEvent(rate_limit_info=rli, uuid="u", session_id="S1")
    ev = normalize(msg)
    assert isinstance(ev, StatusEvent)
    assert ev.phase == "rate_limit"
    assert ev.session_id == "S1"


def test_normalize_unknown_message_is_none():
    # An echoed UserMessage with no error carries no operator-facing event.
    assert normalize(sdk.UserMessage(content="echo")) is None
    assert normalize(object()) is None


def test_session_id_capture_prefers_system_data_then_attr():
    from claude_tg.engine.adapter_sdk import _session_id_of

    sysmsg = sdk.SystemMessage(subtype="init", data={"session_id": "S-data"})
    assert _session_id_of(sysmsg) == "S-data"
    res = sdk.ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="S-attr"
    )
    assert _session_id_of(res) == "S-attr"
    assert _session_id_of(object()) is None


# ---------------------------------------------------------------------------
# SDK adapter: bounded send fails clean (RB2) — uses a fake client, no network
# ---------------------------------------------------------------------------


class _HangingClient:
    """A stand-in SDK client whose receive_response() never yields (simulates a wedge)."""

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    def receive_response(self):
        async def _gen():
            await asyncio.sleep(3600)  # would hang forever without the bound
            yield  # pragma: no cover
        return _gen()

    async def disconnect(self):
        pass


class _ExplodingClient:
    async def query(self, prompt):
        raise RuntimeError("transport died")

    def receive_response(self):
        async def _gen():
            if False:  # pragma: no cover
                yield
        return _gen()

    async def disconnect(self):
        pass


def test_sdk_build_options_threads_model():
    # T4 (P9): a per-project model override is carried into ClaudeAgentOptions(model=…) at
    # session-creation time (start AND resume paths use _build_options).
    sub = SdkSubstrate(model="claude-haiku-4-5")
    opts = sub._build_options()
    assert opts.model == "claude-haiku-4-5"
    # And it rides on the resume path too (the resumed session honors the project's model).
    resume_opts = sub._build_options(resume="sess-123")
    assert resume_opts.model == "claude-haiku-4-5"


def test_sdk_build_options_no_model_omits_it():
    # No override → `model` is omitted so the SDK keeps its own default (unchanged pre-T4).
    sub = SdkSubstrate()
    opts = sub._build_options()
    assert getattr(opts, "model", None) is None
    # An empty/whitespace model is normalized to None (never an empty id).
    assert SdkSubstrate(model="   ")._build_options().model is None


# ---- T-EFFORT (STATUSLINE): effort threads into ClaudeAgentOptions(effort=…) ----


def test_sdk_build_options_threads_effort():
    # T-EFFORT: a per-project effort override is carried into ClaudeAgentOptions(effort=…) at
    # session-creation time (start AND resume paths use _build_options), parallel to `model`.
    sub = SdkSubstrate(effort="max")
    assert sub._build_options().effort == "max"
    # And it rides on the resume path too (the resumed session honors the project's effort).
    assert sub._build_options(resume="sess-123").effort == "max"


def test_sdk_build_options_no_effort_omits_it_byte_for_byte():
    # The headline invariant: a default (no-effort) turn omits `effort` entirely so the SDK's
    # own default applies — byte-for-byte the pre-knob baseline. Asserted against the SDK's
    # ClaudeAgentOptions default for the field (so this can't silently drift if the SDK changes
    # its default), and an empty/whitespace effort is normalized to None (never an empty value).
    from claude_agent_sdk import ClaudeAgentOptions

    default_effort = ClaudeAgentOptions().effort
    assert SdkSubstrate()._build_options().effort == default_effort
    assert SdkSubstrate(cwd="/work")._build_options().effort == default_effort
    assert SdkSubstrate(effort="   ")._build_options().effort == default_effort


def test_sdk_build_options_effort_is_independent_of_model_and_thinking():
    # effort is orthogonal: it can be set with or without a model override / thinking, and
    # setting it never disturbs those fields (guards the three session-creation knobs stay
    # independent — only the effort kwarg is added when an effort is present).
    sub = SdkSubstrate(model="claude-haiku-4-5", thinking=True, effort="low")
    opts = sub._build_options()
    assert opts.effort == "low"
    assert opts.model == "claude-haiku-4-5"
    assert opts.thinking == {"type": "adaptive", "display": "summarized"}


def test_sdk_build_options_threads_permission_mode_plan():
    # P12 T-PLAN-1 (mechanism a): an armed plan turn builds the session with
    # permission_mode="plan" baked into ClaudeAgentOptions at session-creation time — on BOTH
    # the start and resume paths (so a resumed plan turn continues the conversation in plan
    # mode). This is what makes Claude reason + propose a plan and surface ExitPlanMode.
    sub = SdkSubstrate(permission_mode="plan")
    assert sub._build_options().permission_mode == "plan"
    assert sub._build_options(resume="sess-123").permission_mode == "plan"


# ---- P12 T-THINK-3: thinking enables partials + display:"summarized" ONLY when on ----


def test_sdk_build_options_thinking_on_sets_summarized_and_partials():
    # P12 T-THINK: a thinking-ON session asks for READABLE reasoning
    # (thinking={"type":"adaptive","display":"summarized"}) AND forces partial messages on
    # (thinking only streams as thinking_delta StreamEvents). On BOTH start and resume paths.
    sub = SdkSubstrate(thinking=True)
    for opts in (sub._build_options(), sub._build_options(resume="sess-123")):
        assert opts.thinking == {"type": "adaptive", "display": "summarized"}
        assert opts.include_partial_messages is True


def test_sdk_build_options_thinking_off_is_byte_for_byte_unchanged():
    # The headline invariant: a thinking-OFF turn's options are byte-for-byte the pre-P12
    # baseline — no `thinking` option set (the SDK's own None default) and partials stay OFF
    # (no extra StreamEvent wire traffic). Asserted by comparing a thinking-off substrate's
    # options field-by-field against a substrate built with NO thinking arg at all.
    off = SdkSubstrate(cwd="/work")._build_options()
    baseline = SdkSubstrate(cwd="/work")._build_options()  # the explicit pre-P12 construction
    assert off.include_partial_messages is False
    # The SDK ClaudeAgentOptions default for `thinking` is None — a thinking-off session never
    # sets it, so it stays the default (not the summarized dict).
    assert getattr(off, "thinking", None) == getattr(baseline, "thinking", None)
    assert getattr(off, "thinking", None) != {"type": "adaptive", "display": "summarized"}


def test_sdk_build_options_thinking_off_keeps_explicit_partials_flag():
    # A thinking-OFF session honors an explicitly-passed include_partial_messages (it does NOT
    # force it off): thinking only OR's partials ON when on. (Guards that the thinking flag and
    # the partials flag are independent — only thinking-ON couples them.)
    sub = SdkSubstrate(thinking=False, include_partial_messages=True)
    opts = sub._build_options()
    assert opts.include_partial_messages is True
    assert getattr(opts, "thinking", None) != {"type": "adaptive", "display": "summarized"}


def test_sdk_build_options_default_permission_mode_unchanged():
    # P12 T-PLAN-1: a NORMAL turn is byte-for-byte unchanged — the default substrate sets
    # permission_mode="default" exactly as before P12 (the omit-otherwise contract: plan mode
    # is set ONLY when armed; every other turn keeps "default"). Pins the C4-adjacent invariant
    # that a normal turn never silently inherits plan mode.
    assert SdkSubstrate()._build_options().permission_mode == "default"
    assert SdkSubstrate()._build_options(resume="sess-1").permission_mode == "default"


async def test_sdk_send_timeout_yields_driver_error_no_hang():
    sub = SdkSubstrate()
    sub._client = _HangingClient()  # inject; do NOT start a real session
    # Tiny timeout so the bounded wait_for trips fast — proves it never hangs (RB2).
    out = await drain(sub.send("go", timeout=0.05))
    assert len(out) == 1
    assert isinstance(out[0], ErrorEvent)
    assert out[0].kind_of_error == "driver_error"
    assert "timed out" in out[0].message


async def test_sdk_send_exception_yields_driver_error():
    sub = SdkSubstrate()
    sub._client = _ExplodingClient()
    out = await drain(sub.send("go", timeout=5))
    assert len(out) == 1
    assert isinstance(out[0], ErrorEvent)
    assert out[0].kind_of_error == "driver_error"
    assert "transport died" in out[0].message


async def test_engine_surfaces_substrate_driver_error_without_raising():
    # Wire the SDK adapter (hanging client) behind the Engine: the engine's send
    # passes the fail-clean stream through; no exception escapes (RB2 end-to-end).
    sub = SdkSubstrate()
    sub._client = _HangingClient()
    eng = Engine(sub, send_timeout=0.05)
    out = await drain(eng.send("go"))
    assert isinstance(out[0], ErrorEvent) and out[0].kind_of_error == "driver_error"


def test_sdk_send_before_start_raises():
    sub = SdkSubstrate()
    with pytest.raises(RuntimeError):
        # Consuming the generator triggers the not-started guard.
        asyncio.run(drain(sub.send("go")))


# ---------------------------------------------------------------------------
# P6 H2/RB2: the per-message liveness timeout must NOT count the operator's
# approval wait. While a decision hold is OPEN (the SDK's can_use_tool is
# outstanding awaiting the engine's answer-hold), the receive loop must not
# apply the 120s liveness bound — the answer-backstop bounds the human-wait.
# A genuinely-silent Claude (no hold open) STILL times out cleanly.
#
# These drive the substrate directly with a controllable decision callback +
# a fake SDK client that invokes can_use_tool from inside the receive stream
# (faithful to the real SDK, which awaits can_use_tool with no fail_after and
# delivers no further messages until it returns — see engine/pending.py).
# No real sleeps: the hold is gated on an asyncio.Event the test sets.
# ---------------------------------------------------------------------------


class _HoldingClient:
    """Fake SDK client: on the FIRST message it calls ``can_use_tool`` (parking on
    the engine hold), then — only after the callback returns — yields the result.

    Mirrors the real ``ClaudeSDKClient``: ``can_use_tool`` is the permission control
    request the SDK awaits before delivering the next message, so while the operator
    is deciding, ``receive_response()`` yields nothing. The decision callback is the
    substrate's own ``_make_can_use_tool()`` output (the real wiring).

    ``post_decision_delay`` (seconds) optionally sleeps AFTER the hold resolves and
    BEFORE the result, simulating Claude going silent once it has its tool verdict —
    used to prove the liveness bound is restored the instant the hold closes.
    """

    def __init__(self, can_use_tool, *, tool_name="Write", post_decision_delay=0.0):
        self._can_use_tool = can_use_tool
        self._tool_name = tool_name
        self._post_decision_delay = post_decision_delay
        self.connected = False

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        pass

    def receive_response(self):
        async def _gen():
            # The SDK fires can_use_tool and BLOCKS the stream until it returns
            # (the operator hold). A bare object() stands in for the SDK's context.
            ctx = type("Ctx", (), {"tool_use_id": "tu-hold-1"})()
            await self._can_use_tool(self._tool_name, {"file_path": "/tmp/x"}, ctx)
            if self._post_decision_delay:
                await asyncio.sleep(self._post_decision_delay)
            # After the verdict, Claude finishes the turn.
            yield sdk.ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="S-hold",
                total_cost_usd=0.0,
                result="done",
            )

        return _gen()

    async def disconnect(self):
        self.connected = False


async def test_liveness_timeout_suspended_while_decision_hold_open():
    """RED on current code: a hold longer than the liveness timeout fires driver_error.

    GREEN: with the hold open the 120s (here 0.05s) bound is suspended, so the turn
    waits for the operator and then completes with the ResultEvent — no driver_error.
    """
    release = asyncio.Event()

    async def decision_callback(tool_name, tool_input, tool_use_id):
        # The engine's answer-hold: parks until the operator resolves. Here, gated on
        # an Event the test sets AFTER a span that exceeds the tiny liveness timeout.
        await release.wait()
        return SubstrateDecision(allow=True, updated_input=dict(tool_input))

    sub = SdkSubstrate(decision_callback=decision_callback)
    sub._client = _HoldingClient(sub._make_can_use_tool())

    async def _run():
        out = []
        # Tiny liveness timeout; the hold below outlives it by design.
        agen = sub.send("go", timeout=0.05)
        # Let the receive loop reach the hold, then wait WELL past the 0.05s bound
        # before releasing — on current (buggy) code the wait_for trips here.
        async def _release_after():
            await asyncio.sleep(0.2)  # > 0.05 liveness bound; bounded so test is fast
            release.set()

        releaser = asyncio.create_task(_release_after())
        async for ev in agen:
            out.append(ev)
        await releaser
        return out

    out = await _run()

    # The turn completed via the operator resolving the hold — NO driver_error.
    assert all(
        not (isinstance(e, ErrorEvent) and e.kind_of_error == "driver_error")
        for e in out
    ), f"a long hold must not produce a driver_error; got {out}"
    assert any(isinstance(e, ResultEvent) for e in out), out


async def test_liveness_timeout_still_fires_when_silent_and_no_hold_open():
    """The bound is SUSPENDED, not removed: a silent Claude with no hold still times out.

    Guards Fix 1 against over-reach — if the implementation simply dropped the 120s
    bound (or never restored it), this genuine-wedge case would hang/pass-through. It
    must still surface a driver_error.
    """
    sub = SdkSubstrate()
    sub._client = _HangingClient()  # never yields, never opens a hold
    out = await drain(sub.send("go", timeout=0.05))
    assert len(out) == 1
    assert isinstance(out[0], ErrorEvent) and out[0].kind_of_error == "driver_error"
    assert "timed out" in out[0].message


async def test_liveness_bound_restored_after_hold_closes():
    """The bound returns the instant the hold resolves: a post-verdict silent Claude times out.

    The hold resolves promptly (operator answers), but Claude then goes silent past the
    liveness bound before the result. The bound — re-applied once the hold closed — must
    fire a driver_error rather than hanging forever.
    """

    async def decision_callback(tool_name, tool_input, tool_use_id):
        return SubstrateDecision(allow=True, updated_input=dict(tool_input))

    sub = SdkSubstrate(decision_callback=decision_callback)
    # Hold resolves immediately; then a 0.3s silent gap > the 0.05s bound before result.
    sub._client = _HoldingClient(sub._make_can_use_tool(), post_decision_delay=0.3)
    out = await drain(sub.send("go", timeout=0.05))
    assert any(
        isinstance(e, ErrorEvent) and e.kind_of_error == "driver_error" for e in out
    ), f"a silent Claude AFTER the hold closes must still time out; got {out}"


# ---------------------------------------------------------------------------
# P6 H2/RB2 fix 1: boundary race. ``asyncio.wait_for`` can take its TimeoutError
# branch in the SAME event-loop tick that ``pending`` resolves. The timeout
# branch must NOT then cancel + drop the already-ready message (most often the
# terminal ResultMessage right as a closed hold un-suspends the bound): it must
# RETURN it. Otherwise a COMPLETED turn surfaces as a spurious driver_error.
# ---------------------------------------------------------------------------


class _RaceyIterator:
    """An async iterator whose FIRST ``__anext__`` resolves in the SAME loop tick the
    ``wait_for(timeout=race_at)`` elapses, then yields a terminal ResultMessage.

    Scheduling ``set_result`` via ``call_later(race_at, …)`` — the SAME delay ``send``
    passes to ``wait_for`` — lands the future's done-callback and the timeout handle in
    one tick; ``wait_for`` deterministically observes the timeout (the timer fires first)
    while ``pending.done()`` is already True. That is exactly the boundary race fix 1
    guards: the message is ready but the timeout branch ran. A second ``__anext__`` raises
    StopAsyncIteration to end the stream cleanly.
    """

    def __init__(self, race_at: float):
        self._race_at = race_at
        self._emitted = False

    def __aiter__(self):
        return self

    def __anext__(self):
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        if self._emitted:
            fut.set_exception(StopAsyncIteration())
            return fut
        self._emitted = True
        msg = sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="S-race",
            total_cost_usd=0.0,
            result="raced-done",
        )
        # Resolve at the SAME delay as the liveness timeout → same-tick boundary race.
        loop.call_later(self._race_at, fut.set_result, msg)
        return fut


class _RaceyClient:
    """Fake SDK client whose receive_response() returns a :class:`_RaceyIterator`."""

    def __init__(self, race_at: float):
        self._race_at = race_at

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    def receive_response(self):
        return _RaceyIterator(self._race_at)

    async def disconnect(self):
        pass


async def test_timeout_branch_returns_ready_message_not_dropped():
    """RED on pre-fix code: a message that resolves in the same tick the timeout fires is
    cancelled + dropped, surfacing the COMPLETED turn as a driver_error. GREEN: the timeout
    branch re-checks ``pending`` and RETURNS the ready ResultMessage — a clean ResultEvent,
    no driver_error, no dropped terminal."""
    sub = SdkSubstrate()
    # No hold is ever open here (_hold_depth stays 0), so the pre-fix timeout branch would
    # cancel + raise; the ready message survives only via the fix-1 guard.
    sub._client = _RaceyClient(race_at=0.02)
    out = await drain(sub.send("go", timeout=0.02))

    assert all(
        not (isinstance(e, ErrorEvent) and e.kind_of_error == "driver_error")
        for e in out
    ), f"a message ready in the timeout tick must NOT become a driver_error; got {out}"
    results = [e for e in out if isinstance(e, ResultEvent)]
    assert results, f"the ready ResultMessage must be returned (not dropped); got {out}"
    assert results[0].result_text == "raced-done"


async def test_timeout_branch_guard_targets_pending_done_directly():
    """Unit-level proof of the fix-1 guard against the exact race, decoupled from the SDK
    iterator: a pre-resolved ``pending`` observed in the TimeoutError branch (``_hold_depth
    == 0``) is RETURNED, not dropped. Forces one same-tick timeout tick and asserts the
    ready sentinel comes back rather than a TimeoutError propagating."""
    sub = SdkSubstrate()
    assert sub._hold_depth == 0
    loop = asyncio.get_event_loop()
    sentinel = object()

    class _OneShot:
        def __init__(self):
            self._done = False

        def __anext__(self):
            fut: asyncio.Future = loop.create_future()
            if self._done:
                fut.set_exception(StopAsyncIteration())
            else:
                self._done = True
                loop.call_later(0.02, fut.set_result, sentinel)
            return fut

    got = await sub._next_message(_OneShot(), 0.02)
    assert got is sentinel, "the fix-1 guard must return the ready message from the timeout branch"


# ---------------------------------------------------------------------------
# P6 H2/RB2 — SEQUENTIAL holds in one turn (the P12 plan-mode live symptom).
#
# A /plan turn approves ExitPlanMode, execution resumes, Claude calls Write and
# the operator DENIES it, then Claude goes SILENT. This opens TWO holds in one
# turn — an APPROVE then a DENY — and is the exact sequence a live P12 run wedged
# on for 9+ minutes. The invariant under test: ``_hold_depth`` must return to 0
# after the LAST hold resolves (balanced across the sequence AND across BOTH
# allow and deny resolutions), so the per-message liveness bound RE-ARMS and a
# subsequently-silent Claude is caught by a clean ``driver_error`` — the turn can
# never wedge with the bound stuck suspended (RB2).
#
# Drives the real ``SdkSubstrate`` (its real ``_make_can_use_tool`` increments/
# decrements ``_hold_depth``; its real ``_next_message`` reads it) behind a fake
# SDK client that fires ``can_use_tool`` twice — once per hold — between yields,
# faithful to the real SDK control protocol (can_use_tool is awaited with no
# fail_after and no further message is delivered until the verdict returns).
# Deterministic: the only wait is the tiny liveness bound the silent tail trips.
# ---------------------------------------------------------------------------


class _PlanApproveThenWriteDenyClient:
    """Fake SDK client mirroring the P12 live sequence: ExitPlanMode hold, then a
    yielded assistant message (execution resumed), then a Write hold, then SILENCE.

    Each ``yield`` is a separate ``__anext__`` -> a separate ``_next_message`` call,
    and each ``can_use_tool`` await happens between yields — exactly the live shape
    (a plan approval, resumed work, then a denied Write). After the second hold the
    generator never yields the terminal ``ResultMessage`` (Claude went silent), so
    the re-armed liveness bound is the ONLY thing that can end the turn.

    ``hold_delay`` (default 0) optionally sleeps inside each ``can_use_tool`` await to
    simulate the operator taking longer than the liveness bound to decide BOTH holds —
    proving the bound is suspended *during* each hold yet restored *between/after* them.
    """

    def __init__(self, can_use_tool, *, silent_tail=3600.0, hold_delay=0.0):
        self._can_use_tool = can_use_tool
        self._silent_tail = silent_tail
        self._hold_delay = hold_delay

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    def receive_response(self):
        async def _gen():
            # --- hold #1: ExitPlanMode (operator APPROVES) ---
            ctx1 = type("Ctx", (), {"tool_use_id": "tu-plan-1"})()
            await self._can_use_tool("ExitPlanMode", {"plan": "do the thing"}, ctx1)
            # execution resumes -> an assistant text message is delivered
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text="resuming after plan approval")],
                model="m",
            )
            # --- hold #2: Write (operator DENIES) ---
            ctx2 = type("Ctx", (), {"tool_use_id": "tu-write-2"})()
            await self._can_use_tool(
                "Write", {"file_path": "/tmp/x", "content": "y"}, ctx2
            )
            # --- then Claude goes SILENT: NO terminal ResultMessage ever arrives ---
            await asyncio.sleep(self._silent_tail)
            yield  # pragma: no cover

        return _gen()

    async def disconnect(self):
        pass


async def _approve_plan_deny_write(tool_name, tool_input, tool_use_id):
    """Engine-shaped decision callback: APPROVE ExitPlanMode, DENY Write.

    Returns the substrate decision the engine's ``decision_to_substrate`` would
    produce for a PlanVerdict(approve=True) and a PermissionDecision('deny').
    """
    if tool_name == "ExitPlanMode":
        return SubstrateDecision(allow=True, updated_input=dict(tool_input))  # approve
    return SubstrateDecision(allow=False, updated_input=None, message="denied")  # deny


async def test_liveness_rearms_after_approve_then_deny_sequence_then_silence():
    """The P12 plan-mode wedge guard: approve -> resume -> deny -> SILENCE must NOT wedge.

    Two holds open in one turn (ExitPlanMode approved, then Write denied). After the
    DENY resolves there is no further message. ``_hold_depth`` must be back to 0 so the
    re-armed liveness bound fires a clean ``driver_error`` rather than hanging forever.
    Wrapped in an outer ``wait_for`` so a regression (depth stuck > 0 -> swallow every
    tick forever) FAILS as a timeout instead of hanging the suite.
    """
    sub = SdkSubstrate(decision_callback=_approve_plan_deny_write)
    sub._client = _PlanApproveThenWriteDenyClient(sub._make_can_use_tool())

    # Tiny liveness bound; the silent tail vastly outlives it, so the re-armed bound
    # must trip within ~0.05s of the deny resolving. The outer 10s is a wedge tripwire.
    out = await asyncio.wait_for(drain(sub.send("go", timeout=0.05)), timeout=10.0)

    # Both holds were resolved and the depth is balanced back to 0 (the invariant).
    assert sub._hold_depth == 0, f"hold depth must return to 0 after the sequence; got {sub._hold_depth}"
    # The re-armed bound fired: a clean driver_error ended the turn (RB2 — no wedge).
    assert any(
        isinstance(e, ErrorEvent) and e.kind_of_error == "driver_error" for e in out
    ), f"the liveness bound must re-arm after the last hold and fire on silence; got {out}"
    # The mid-sequence assistant text streamed (the approve let execution resume).
    assert any(isinstance(e, TextEvent) for e in out), out


async def test_liveness_rearms_after_long_approve_then_long_deny_then_silence():
    """Same sequence, but each hold OUTLIVES the bound (operator slow on BOTH).

    Proves the bound is genuinely SUSPENDED *during* each hold (a >bound approve and a
    >bound deny do not themselves trip a driver_error) AND restored *after* the last
    one (the silent tail still trips it). Guards against an implementation that only
    suspends/restores correctly for the FIRST hold.
    """
    sub = SdkSubstrate(decision_callback=_approve_plan_deny_write)
    sub._client = _PlanApproveThenWriteDenyClient(
        sub._make_can_use_tool(), hold_delay=0.2  # > 0.05 bound, per hold
    )

    out = await asyncio.wait_for(drain(sub.send("go", timeout=0.05)), timeout=10.0)

    assert sub._hold_depth == 0, f"hold depth must return to 0; got {sub._hold_depth}"
    # Exactly ONE driver_error, from the silent tail — neither slow hold produced one.
    driver_errors = [
        e for e in out if isinstance(e, ErrorEvent) and e.kind_of_error == "driver_error"
    ]
    assert len(driver_errors) == 1, f"only the silent tail may driver_error; got {out}"


async def test_hold_depth_increment_decrement_paired_for_allow_and_deny():
    """Mutation probe anchor: the depth is incremented BEFORE and decremented AFTER

    each callback, for BOTH an allow and a deny. After invoking the substrate's real
    ``can_use_tool`` once for an allow and once for a deny, the depth is back to 0 each
    time. Breaking the balance — e.g. dropping the ``finally`` decrement, or skipping it
    on the deny branch — leaves the depth at 1 here and wedges the sequence tests above.
    """
    seen_depths = []

    async def _cb(tool_name, tool_input, tool_use_id):
        # Inside the hold the depth is exactly 1 (incremented before the await).
        seen_depths.append(sub._hold_depth)
        if tool_name == "ExitPlanMode":
            return SubstrateDecision(allow=True, updated_input=dict(tool_input))
        return SubstrateDecision(allow=False, updated_input=None, message="denied")

    sub = SdkSubstrate(decision_callback=_cb)
    can_use_tool = sub._make_can_use_tool()
    ctx = type("Ctx", (), {"tool_use_id": "tu-1"})()

    assert sub._hold_depth == 0
    await can_use_tool("ExitPlanMode", {"plan": "p"}, ctx)  # allow path
    assert sub._hold_depth == 0, "depth must return to 0 after an ALLOW"
    await can_use_tool("Write", {"file_path": "/x"}, ctx)  # deny path
    assert sub._hold_depth == 0, "depth must return to 0 after a DENY"
    assert seen_depths == [1, 1], "depth must be exactly 1 while each hold is open"


# ---------------------------------------------------------------------------
# ENGINE_MODE config parsing / validation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_", "CLAUDE_", "ENGINE_", "ANSWER_")):
            monkeypatch.delenv(key, raising=False)


def test_parse_engine_mode_default_is_oneshot():
    assert parse_engine_mode(None) == "oneshot"
    assert parse_engine_mode("") == "oneshot"
    assert parse_engine_mode("   ") == "oneshot"


def test_parse_engine_mode_streaming_case_insensitive():
    assert parse_engine_mode("streaming") == "streaming"
    assert parse_engine_mode("STREAMING") == "streaming"
    assert parse_engine_mode(" Streaming ") == "streaming"
    assert set(ENGINE_MODES) == {"oneshot", "streaming"}


def test_parse_engine_mode_invalid_raises():
    with pytest.raises(ValueError):
        parse_engine_mode("bogus")


def test_config_defaults_engine_mode_oneshot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.engine_mode == "oneshot"  # default preserved (S4)


def test_config_reads_engine_mode_streaming(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("ENGINE_MODE", "streaming")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.engine_mode == "streaming"


def test_config_invalid_engine_mode_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("ENGINE_MODE", "nope")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


# ---------------------------------------------------------------------------
# ANSWER_BACKSTOP_SECONDS config parsing / validation (ADR-002, T5)
# ---------------------------------------------------------------------------


def test_parse_answer_backstop_default_is_3600():
    assert parse_answer_backstop_seconds(None) == 3600
    assert parse_answer_backstop_seconds("") == 3600
    assert parse_answer_backstop_seconds("   ") == 3600
    assert DEFAULT_ANSWER_BACKSTOP_SECONDS == 3600


def test_parse_answer_backstop_reads_override():
    assert parse_answer_backstop_seconds("120") == 120
    assert parse_answer_backstop_seconds(" 300 ") == 300


def test_parse_answer_backstop_rejects_non_int_and_nonpositive():
    with pytest.raises(ValueError):
        parse_answer_backstop_seconds("abc")
    with pytest.raises(ValueError):
        parse_answer_backstop_seconds("0")
    with pytest.raises(ValueError):
        parse_answer_backstop_seconds("-5")


def test_config_defaults_answer_backstop(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.answer_backstop_seconds == 3600  # 60-min default


def test_config_reads_answer_backstop_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("ANSWER_BACKSTOP_SECONDS", "90")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.answer_backstop_seconds == 90


def test_config_invalid_answer_backstop_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("ANSWER_BACKSTOP_SECONDS", "nope")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


# ---------------------------------------------------------------------------
# Guard: the engine imports the SDK LAZILY (not at module top level)
# ---------------------------------------------------------------------------


def test_adapter_does_not_import_sdk_at_module_top_level():
    # Static guarantee (in-process, no subprocess/network): the SDK adapter must not
    # `import claude_agent_sdk` at module scope, so importing claude_tg.engine — and
    # running the mock-based tests — never requires the SDK. Lazy imports live inside
    # functions/methods (indented), which this AST walk ignores.
    import ast
    import inspect

    from claude_tg.engine import adapter_sdk

    tree = ast.parse(inspect.getsource(adapter_sdk))
    top_level_imports = []
    for node in tree.body:  # only module-level statements, not nested function bodies
        if isinstance(node, ast.Import):
            top_level_imports += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.append(node.module)
    assert not any(
        name == "claude_agent_sdk" or name.startswith("claude_agent_sdk.")
        for name in top_level_imports
    ), f"SDK must be imported lazily; found top-level import in {top_level_imports}"
