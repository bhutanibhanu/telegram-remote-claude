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

    async def resume(self, session_id):
        self.calls.append(("resume", session_id))
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

    assert ("resume", "S-prev") in sub.calls
    assert out[0].text == "resumed"


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
