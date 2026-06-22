"""T7 bot-level streaming + SB1 callback-handler tests (mock Telegram + engine).

These cover the bot.py wiring: the ENGINE_MODE switch keeps one-shot the default; the
streaming path delegates to a StreamingSession; and — the security-critical part — the
``on_callback`` handler enforces SB1 (an explicit allowlist recheck inside the handler)
so a NON-allowlisted callback never routes a decision. No live Telegram / Claude / net.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeResult, ClaudeRunner
from claude_tg.config import Config
from claude_tg.engine.types import ResultEvent
from claude_tg.session_store import JsonSessionStore
from claude_tg.stream_session import CallbackOutcome, StreamingBusy, StreamingSession


def make_config(
    allowed=(1,),
    engine_mode="oneshot",
    workdir="/work",
    *,
    allowed_roots=(),
    allow_any_path=False,
):
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset(allowed),
        workdir=Path(workdir),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
        engine_mode=engine_mode,
        answer_backstop_seconds=3600,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
    )


class FakeRunner:
    def __init__(self, result=None):
        self._result = result if result is not None else ClaudeResult(ok=True, text="ok")
        self.run_calls = []

    async def run(self, chat_id, text):
        self.run_calls.append((chat_id, text))
        return self._result

    def reset(self, chat_id):
        pass

    def get_cwd(self, chat_id):
        return "/work"

    def set_cwd(self, chat_id, path):
        return path


class FakeStreaming:
    """Stands in for StreamingSession at the bot boundary."""

    def __init__(self, outcome=None, busy=False):
        self.handle_message_calls = []
        self.resolve_calls = []
        self.cancel_calls = []
        self.reset_calls = []
        self.yolo_calls = []
        self._outcome = outcome or CallbackOutcome(handled=True, note="ok")
        self._busy = busy

    async def handle_message(self, chat_id, text, *, send, edit, delete=None):
        self.handle_message_calls.append((chat_id, text))
        if self._busy:
            raise StreamingBusy()

    def resolve_callback(self, chat_id, data):
        self.resolve_calls.append((chat_id, data))
        return self._outcome

    def handle_cancel(self, chat_id):
        self.cancel_calls.append(chat_id)
        return 1

    def reset(self, chat_id):
        self.reset_calls.append(chat_id)

    def set_yolo(self, chat_id, on):
        self.yolo_calls.append((chat_id, on))


def make_update(chat_id=1, text="hello"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.effective_message = upd.message
    return upd


def make_callback_update(chat_id=1, data="a|tid|0.0"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    upd.callback_query.message.reply_text = AsyncMock()
    upd.effective_message = upd.callback_query.message
    return upd


def make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.args = []
    return ctx


# ---------------------------------------------------------------------------
# ENGINE_MODE switch: oneshot is the default and unchanged.
# ---------------------------------------------------------------------------


def test_build_application_enables_concurrent_updates():
    """The answer-hold parks a turn handler INSIDE engine.send awaiting the operator's tap,
    and that tap arrives as a SEPARATE update. Without concurrent update processing, PTB
    would queue the tap behind the parked turn handler — a deadlock (the turn waits for the
    tap; the tap waits for the turn to return). Guard that build_application enables it."""
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = bot.build_application()
    assert app.concurrent_updates  # a positive max, not 0/disabled


async def test_oneshot_is_default_and_uses_runner():
    runner = FakeRunner(ClaudeResult(ok=True, text="the answer"))
    # No streaming passed AND default config => oneshot.
    bot = TelegramClaudeBot(make_config(), runner)
    assert bot.streaming is None
    upd = make_update(1, "do it")
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == [(1, "do it")]
    upd.message.reply_text.assert_awaited_once_with("the answer")


async def test_streaming_disabled_when_mode_oneshot_even_if_passed():
    # Defense: even if a StreamingSession is passed, oneshot config keeps it off.
    runner = FakeRunner()
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner, streaming=streaming)
    assert bot.streaming is None
    await bot.on_message(make_update(1, "hi"), make_ctx())
    assert runner.run_calls == [(1, "hi")]
    assert streaming.handle_message_calls == []


async def test_streaming_mode_delegates_to_driver():
    runner = FakeRunner()
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), runner, streaming=streaming)
    assert bot.streaming is streaming
    await bot.on_message(make_update(1, "build it"), make_ctx())
    assert streaming.handle_message_calls == [(1, "build it")]
    assert runner.run_calls == []  # one-shot runner NOT used in streaming mode


async def test_streaming_busy_replies_still_working():
    streaming = FakeStreaming(busy=True)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "again")
    await bot.on_message(upd, make_ctx())
    assert "still working" in upd.message.reply_text.await_args.args[0].lower()


async def test_streaming_passes_working_delete_closure():
    # The bot binds a `delete` closure (Task 2) and hands it to handle_message; invoking
    # it deletes the message via ctx.bot.delete_message(chat_id, message_id).
    captured: dict = {}

    class CapturingStreaming(FakeStreaming):
        async def handle_message(self, chat_id, text, *, send, edit, delete=None):
            self.handle_message_calls.append((chat_id, text))
            captured["delete"] = delete

    streaming = CapturingStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    ctx = make_ctx()
    ctx.bot.delete_message = AsyncMock()
    await bot.on_message(make_update(1, "go"), ctx)
    assert callable(captured["delete"]), "bot must pass a delete closure to handle_message"
    # Invoking the closure deletes the message via the bot API for this chat.
    await captured["delete"](message_id=42)
    ctx.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=42)


# ---------------------------------------------------------------------------
# SB1: the callback handler's allowlist recheck (defense in depth).
# ---------------------------------------------------------------------------


async def test_callback_from_unauthorized_chat_never_resolves():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=999, data="a|tid|0.0")  # NOT allowlisted
    await bot.on_callback(upd, make_ctx())
    # The query is answered (spinner stops) but the engine is NEVER touched.
    upd.callback_query.answer.assert_awaited()
    assert streaming.resolve_calls == []


async def test_permission_callback_from_unauthorized_chat_never_resolves():
    # SB1 for a PERMISSION tap: a forged "m|tid|s" (allow-for-session) from a chat that
    # is NOT allowlisted must be answered + dropped — resolve_callback never reached, so
    # an attacker cannot approve a risky tool. False-pass guard: if on_callback skipped
    # the _authorized recheck for permission taps this would record a resolve call.
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=999, data="m|tid|s")  # NOT allowlisted
    await bot.on_callback(upd, make_ctx())
    upd.callback_query.answer.assert_awaited()
    assert streaming.resolve_calls == []


async def test_permission_callback_from_authorized_chat_routes_to_resolve():
    streaming = FakeStreaming(outcome=CallbackOutcome(handled=True, note="Allowed once"))
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=1, data="m|tid|o")
    await bot.on_callback(upd, make_ctx())
    assert streaming.resolve_calls == [(1, "m|tid|o")]
    upd.callback_query.answer.assert_awaited()


async def test_callback_from_authorized_chat_routes_to_resolve():
    streaming = FakeStreaming(outcome=CallbackOutcome(handled=True, note="Answered: Red"))
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=1, data="a|tid|0.0")
    await bot.on_callback(upd, make_ctx())
    assert streaming.resolve_calls == [(1, "a|tid|0.0")]
    upd.callback_query.answer.assert_awaited()


async def test_callback_other_prompts_for_free_text():
    streaming = FakeStreaming(
        outcome=CallbackOutcome(handled=True, note="Type your answer", expects_text=True)
    )
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=1, data="o|tid|0")
    await bot.on_callback(upd, make_ctx())
    assert streaming.resolve_calls == [(1, "o|tid|0")]
    # The operator is prompted to type the free-text answer.
    upd.callback_query.message.reply_text.assert_awaited()


async def test_callback_in_oneshot_mode_is_ignored():
    # No streaming driver => a callback is answered and dropped (never crashes).
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="oneshot"), FakeRunner())
    upd = make_callback_update(chat_id=1, data="a|tid|0.0")
    await bot.on_callback(upd, make_ctx())
    upd.callback_query.answer.assert_awaited()


async def test_callback_handler_survives_driver_exception():
    # RB1: even if resolve_callback raises, the handler answers and does not crash.
    streaming = FakeStreaming()
    streaming.resolve_callback = MagicMock(side_effect=RuntimeError("boom"))
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=1, data="a|tid|0.0")
    await bot.on_callback(upd, make_ctx())  # must not raise
    upd.callback_query.answer.assert_awaited()


# ---------------------------------------------------------------------------
# /cancel + /reset wiring.
# ---------------------------------------------------------------------------


async def test_cmd_cancel_streaming_calls_handle_cancel():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/cancel")
    await bot.cmd_cancel(upd, make_ctx())
    assert streaming.cancel_calls == [1]
    assert "cancelled" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_cancel_oneshot_is_noop_message():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/cancel")
    await bot.cmd_cancel(upd, make_ctx())
    assert "one-shot" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_cancel_unauthorized_ignored():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(999, "/cancel")
    await bot.cmd_cancel(upd, make_ctx())
    assert streaming.cancel_calls == []
    upd.message.reply_text.assert_not_awaited()


async def test_cmd_reset_also_resets_streaming():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/reset")
    await bot.cmd_reset(upd, make_ctx())
    assert streaming.reset_calls == [1]


async def test_cmd_reset_streaming_preserves_active_cwd_after_switch(tmp_path):
    """QF1 / B1 (D4): in streaming mode /reset must NOT corrupt the active project's cwd.

    Reproduces the real bug with a REAL store + REAL ClaudeRunner sharing it. The runner
    seeds its ``_cwds`` from the flat view (the ACTIVE project's cwd) at construction and
    never tracks /switch — so after restart→/switch→/reset, calling ``runner.reset`` would
    ``store.update(chat, None, <stale cwd>)`` and clobber the now-active project's cwd.

    Setup mirrors that sequence: ``alpha`` (cwd ``/work/alpha``) is active when the runner
    is built (so ``runner._cwds[chat] == "/work/alpha"`` — the stale value), then we switch
    to ``beta`` (cwd inside roots). After ``/reset`` in streaming mode:
      * beta's cwd is UNCHANGED (not clobbered with alpha's stale ``/work/alpha``),
      * beta's session_id is cleared (fresh conversation),
      * alpha is untouched.

    Mutation check: if ``cmd_reset`` called ``runner.reset`` in streaming mode, beta's cwd
    would become ``/work/alpha`` and this test would fail.
    """
    beta_cwd = str(tmp_path / "beta")
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", beta_cwd, make_active=False)
    # Give beta a session_id so we can assert /reset clears it.
    store.update(1, session_id="beta-session", cwd=None)  # writes the ACTIVE project (alpha)…
    store.switch(1, "beta")
    store.update(1, session_id="beta-session", cwd=None)  # …now beta is active → set beta's id
    store.switch(1, "alpha")  # back to alpha so the runner seeds its stale cwd from alpha

    # Build the runner WHILE alpha is active → runner._cwds[1] == "/work/alpha" (the stale
    # value that the corruption would write onto whatever project is active at /reset time).
    runner = ClaudeRunner(make_config(engine_mode="streaming"), session_store=store)
    assert runner._cwds.get(1) == "/work/alpha"

    # Simulate the operator's /switch to beta (the runner does NOT track this).
    store.switch(1, "beta")

    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), runner, streaming=session)
    upd = make_update(1, "/reset")
    await bot.cmd_reset(upd, make_ctx())

    # beta (the active project) keeps its cwd; its session is cleared; alpha is untouched.
    assert store.get_project(1, "beta")["cwd"] == beta_cwd  # NOT clobbered with /work/alpha
    assert store.get_project(1, "beta")["session_id"] is None  # fresh conversation
    assert store.get_project(1, "alpha")["cwd"] == "/work/alpha"  # untouched
    assert store.get_active(1) == "beta"  # /reset keeps the active project
    assert "fresh" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_reset_oneshot_calls_runner_reset(tmp_path):
    """QF1: one-shot /reset is UNCHANGED — it clears the runner's session via runner.reset.

    A real store + runner (no streaming). The runner's flat-view session is cleared and the
    active project's cwd is preserved (one-shot writes its own cwd, which is correct here).
    """
    store = JsonSessionStore(tmp_path / "state.json")
    store.update(1, session_id="one-shot-session", cwd="/work/solo")
    runner = ClaudeRunner(make_config(engine_mode="oneshot"), session_store=store)
    assert runner._sessions.get(1) == "one-shot-session"

    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    assert bot.streaming is None
    upd = make_update(1, "/reset")
    await bot.cmd_reset(upd, make_ctx())

    assert runner._sessions.get(1) is None  # session cleared
    assert store.load().get("1", {}).get("session_id") is None  # persisted clear
    assert store.get_project(1, "default")["cwd"] == "/work/solo"  # cwd preserved
    assert "fresh" in upd.message.reply_text.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# /yolo + /unyolo wiring (P2, D6).
# ---------------------------------------------------------------------------


async def test_cmd_yolo_streaming_sets_yolo_and_replies_loud_banner():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/yolo")
    await bot.cmd_yolo(upd, make_ctx())
    assert streaming.yolo_calls == [(1, True)]
    # The reply is the LOUD banner — carries the ⚠️ glyph (allow-all never silent, D6).
    reply = upd.message.reply_text.await_args.args[0]
    assert "⚠️" in reply


async def test_cmd_unyolo_streaming_clears_yolo():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/unyolo")
    await bot.cmd_unyolo(upd, make_ctx())
    assert streaming.yolo_calls == [(1, False)]
    assert "restored" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_yolo_oneshot_is_explained_not_applied():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/yolo")
    await bot.cmd_yolo(upd, make_ctx())
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_yolo_unauthorized_ignored():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(999, "/yolo")
    await bot.cmd_yolo(upd, make_ctx())
    assert streaming.yolo_calls == []
    upd.message.reply_text.assert_not_awaited()


async def test_cmd_unyolo_unauthorized_ignored():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(999, "/unyolo")
    await bot.cmd_unyolo(upd, make_ctx())
    assert streaming.yolo_calls == []
    upd.message.reply_text.assert_not_awaited()


def test_build_application_registers_callback_handler():
    # The CallbackQueryHandler is wired (SB1 surface exists) without starting polling.
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = bot.build_application()
    from telegram.ext import CallbackQueryHandler

    handlers = [h for group in app.handlers.values() for h in group]
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)


def test_build_application_registers_multi_project_handlers_before_skill_passthrough():
    # P4/T5: /projects, /switch, /rm are specific CommandHandlers wired BEFORE the
    # on_skill_command COMMAND passthrough — first-match-wins keeps them from being
    # forwarded as skills. Assert each is a registered command and precedes the
    # catch-all COMMAND MessageHandler in handler order.
    from telegram.ext import CommandHandler, MessageHandler

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = bot.build_application()
    ordered = [h for group in app.handlers.values() for h in group]
    cmd_names: set[str] = set()
    skill_passthrough_idx = None
    for i, h in enumerate(ordered):
        if isinstance(h, CommandHandler):
            cmd_names |= {c.lstrip("/").lower() for c in h.commands}
        # The skill passthrough is the COMMAND MessageHandler bound to on_skill_command.
        if isinstance(h, MessageHandler) and getattr(h.callback, "__name__", "") == "on_skill_command":
            skill_passthrough_idx = i
    assert {"projects", "switch", "rm"} <= cmd_names
    # Every multi-project CommandHandler comes before the skill passthrough.
    assert skill_passthrough_idx is not None
    for i, h in enumerate(ordered):
        if isinstance(h, CommandHandler) and (
            {"projects", "switch", "rm"} & {c.lstrip("/").lower() for c in h.commands}
        ):
            assert i < skill_passthrough_idx


# ===========================================================================
# P4 / T5 — multi-project navigation commands (/projects · /switch · /rm · /pwd · /cd).
#
# These wire a REAL StreamingSession over a REAL JsonSessionStore (the registry CRUD
# under test) + a scripted FakeEngine factory (no SDK / no network), so the bot's
# store/is_busy/get_cwd facades are exercised for real. The HOLD-parked engine lets a
# turn hold the lock so the load-bearing /switch busy-guard can be asserted.
# ===========================================================================

HOLD = object()  # sentinel: park engine.send() here until resolve()/cancel() fires


class HoldEngine:
    """Minimal scripted engine: yields its script; a HOLD parks send() until released."""

    def __init__(self, script):
        self._script = script
        self.session_id = "sess-mp"
        self.started = False
        self.resumed = None
        self.stopped = False
        self._gate = asyncio.Event()

    async def start(self):
        self.started = True

    async def resume(self, session_id):
        self.resumed = session_id
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        for item in self._script:
            if item is HOLD:
                await self._gate.wait()
                self._gate.clear()
                continue
            yield item

    def resolve(self, tool_use_id, decision):
        self._gate.set()
        return True

    def cancel(self, tool_use_id=None):
        self._gate.set()
        return 1


def make_streaming(store, *, script=None, workdir="/work"):
    """A real StreamingSession over ``store`` whose factory returns a HoldEngine.

    NOTE (T7): the session's Config is built with ``allow_any_path=True`` so that the
    driver's SB2 cwd re-validation (added in T7) NO-OPS for these bot-command tests —
    they exercise navigation/busy-guard behavior, not turn-path confinement, and their
    project cwds (``/work/alpha`` etc.) are not real dirs. This is independent of the
    *bot's* Config (the T6 ``/new`` tests construct their own ``make_config`` with real
    ``allowed_roots`` to exercise SB2 on the path-input command); the driver's turn path
    reads THIS session config, so a parked real turn is not blocked by SB2 here.
    """
    engine = HoldEngine(script if script is not None else [])
    session = StreamingSession(
        make_config(engine_mode="streaming", workdir=workdir, allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=lambda: 0.0,
    )
    return session, engine


def make_cmd_ctx(args=None):
    ctx = make_ctx()
    ctx.args = list(args or [])
    return ctx


# ---- /projects ------------------------------------------------------------


async def test_cmd_projects_lists_with_active_marker(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "alpha" in reply and "beta" in reply
    assert "/work/alpha" in reply and "/work/beta" in reply
    # The active project (alpha) carries the marker; beta does not.
    alpha_line = next(line for line in reply.splitlines() if "alpha" in line)
    beta_line = next(line for line in reply.splitlines() if "beta" in line)
    assert "→" in alpha_line and "→" not in beta_line


async def test_cmd_projects_empty_hints_new(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "/new" in reply and "no project" in reply.lower()


async def test_cmd_projects_oneshot_streaming_only_notice():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_projects_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(999, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    upd.message.reply_text.assert_not_awaited()


# ---- /switch --------------------------------------------------------------


async def test_cmd_switch_happy_sets_active(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    # allow_any_path=True so the QF2 SB2 cwd re-validation no-ops for these fake /work/*
    # cwds (this test exercises plain switch behavior; the in-roots/out-of-root SB2 paths
    # have their own dedicated tests above).
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"
    reply = upd.message.reply_text.await_args.args[0]
    assert "beta" in reply and "resume" in reply.lower()


async def test_cmd_switch_in_roots_cwd_switches(tmp_path):
    """QF2 / B2 (false-pass guard): /switch to a project whose stored cwd IS inside the
    permitted roots still switches normally (the re-validation must not block valid cwds).

    The bot's Config carries real ``allowed_roots`` + ``allow_any_path=False`` so the SB2
    re-validation actually runs (the session's own config is independent, per make_streaming).
    """
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "beta"
    inside.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(root / "alpha"), make_active=True)
    store.create(1, "beta", str(inside), make_active=False)
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"  # in-roots cwd → switched
    assert "beta" in upd.message.reply_text.await_args.args[0]


async def test_cmd_switch_out_of_root_cwd_refused_active_unchanged(tmp_path):
    """QF2 / B2 (SB2 conformance): /switch to a project whose stored cwd is OUTSIDE the
    permitted roots is refused; store.switch is NOT called and the active project is
    unchanged. Mutation check: drop the re-validation and the active project would flip.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"  # a real dir, OUTSIDE the permitted root
    outside.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(root / "alpha"), make_active=True)
    store.create(1, "evil", str(outside), make_active=False)  # cwd escapes the root
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )

    # Spy on store.switch to prove the refusal leaves the store untouched.
    switch_calls = []
    orig_switch = store.switch
    store.switch = lambda *a, **k: switch_calls.append((a, k))  # type: ignore[assignment]
    upd = make_update(1, "/switch evil")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["evil"]))
    store.switch = orig_switch  # type: ignore[assignment]

    reply = upd.message.reply_text.await_args.args[0]
    assert "permitted roots" in reply.lower() and "evil" in reply
    assert switch_calls == [], "store.switch must NOT be called for an out-of-root target"
    assert store.get_active(1) == "alpha"  # active project UNCHANGED


async def test_cmd_switch_missing_cwd_refused_fail_closed(tmp_path):
    """QF2 (fail-closed judgement call): a target project with a missing/empty stored cwd
    is refused rather than crashing or switching — defensive against a hand-edited/sparse
    record. The active project is left unchanged.
    """
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "chats": {
                    "1": {
                        "active": "alpha",
                        "projects": {
                            "alpha": {"cwd": str(tmp_path / "alpha")},
                            "nocwd": {},  # sparse record: no cwd key at all
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonSessionStore(path)
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/switch nocwd")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["nocwd"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "no recorded directory" in reply.lower()
    assert store.get_active(1) == "alpha"  # fail-closed: active unchanged


async def test_cmd_switch_busy_guard_precedes_revalidation(tmp_path):
    """QF2 ordering: the busy-guard still fires FIRST — a /switch (even to an out-of-root
    target) while a turn is in flight is refused with the busy message, BEFORE the cwd
    re-validation, and the store is never touched (busy-guard is load-bearing for relay
    correctness, D2). Pins the QF2 restructure didn't reorder the guards.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(root / "alpha"), make_active=True)
    store.create(1, "evil", str(outside), make_active=False)
    session, engine = make_streaming(store, script=[HOLD], workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )

    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    for _ in range(200):
        if session.is_busy(1):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1), "the held turn should hold the lock"

    upd = make_update(1, "/switch evil")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["evil"]))
    reply = upd.message.reply_text.await_args.args[0]
    # The BUSY message (not the out-of-root message) — the guard fired first.
    assert "/cancel" in reply and ("flight" in reply.lower() or "finish" in reply.lower())
    assert store.get_active(1) == "alpha"

    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_cmd_switch_while_busy_refused_store_untouched(tmp_path):
    # LOAD-BEARING busy-guard (D2): while a turn holds the lock, /switch must refuse and
    # NOT call store.switch — a mid-hold active-project change deadlocks the parked turn.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, engine = make_streaming(store, script=[HOLD])  # the turn parks holding the lock
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)

    # Drive a turn that parks on HOLD (acquires + holds the per-chat turn lock).
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    # Wait until the turn is actually in flight (lock held).
    for _ in range(200):
        if session.is_busy(1):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1), "the held turn should hold the lock"

    # Spy on store.switch to prove it is NOT called while busy.
    switch_calls = []
    orig_switch = store.switch
    store.switch = lambda *a, **k: switch_calls.append((a, k))  # type: ignore[assignment]
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    store.switch = orig_switch  # type: ignore[assignment]

    reply = upd.message.reply_text.await_args.args[0]
    assert "/cancel" in reply and ("flight" in reply.lower() or "finish" in reply.lower())
    assert switch_calls == [], "store.switch must NOT be called while a turn is in flight"
    assert store.get_active(1) == "alpha"  # active unchanged

    # Release the held turn so the task completes cleanly (no leaked task).
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_cmd_switch_unknown_name_lists_available(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/switch nope")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["nope"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "nope" in reply
    # The error lists the available names so the operator can pick a real one.
    assert "alpha" in reply and "beta" in reply
    assert store.get_active(1) == "alpha"  # unchanged


async def test_cmd_switch_no_arg_usage(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/switch")
    await bot.cmd_switch(upd, make_cmd_ctx(args=[]))
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_switch_oneshot_streaming_only_notice():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_switch_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(999, "/switch alpha")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["alpha"]))
    upd.message.reply_text.assert_not_awaited()


# ---- /rm ------------------------------------------------------------------


async def test_cmd_rm_happy_non_active(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm beta")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["beta"]))
    assert "beta" not in store.list_projects(1)
    assert "alpha" in store.list_projects(1)  # active project survives
    assert "removed" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_rm_active_refused(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm alpha")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["alpha"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "active" in reply.lower() and "/switch" in reply
    assert "alpha" in store.list_projects(1)  # NOT removed


async def test_cmd_rm_active_refused_case_insensitive(tmp_path):
    # The store matches names case-insensitively, so the active-guard must too: /rm ALPHA
    # when the active project is "alpha" must be refused (else a casing trick would let the
    # store remove the active project via its case-insensitive resolve).
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm ALPHA")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["ALPHA"]))
    assert "active" in upd.message.reply_text.await_args.args[0].lower()
    assert "alpha" in store.list_projects(1)  # NOT removed


async def test_cmd_rm_unknown_name_errors(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm ghost")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["ghost"]))
    assert "ghost" in upd.message.reply_text.await_args.args[0]
    assert "alpha" in store.list_projects(1)


async def test_cmd_rm_no_arg_usage(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm")
    await bot.cmd_rm(upd, make_cmd_ctx(args=[]))
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_rm_oneshot_streaming_only_notice():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/rm beta")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["beta"]))
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_rm_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(999, "/rm beta")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["beta"]))
    upd.message.reply_text.assert_not_awaited()
    assert "beta" in store.list_projects(1)  # untouched


# ---- /pwd (streaming shows the active project; one-shot unchanged) ---------


async def test_cmd_pwd_streaming_shows_active_project(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/pwd")
    await bot.cmd_pwd(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "api" in reply and "/work/api" in reply


async def test_cmd_pwd_streaming_no_active_project_hint(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/pwd")
    await bot.cmd_pwd(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "no active project" in reply.lower()
    # Read-only: /pwd must NOT auto-create a project.
    assert store.get_active(1) is None


async def test_cmd_pwd_oneshot_unchanged():
    # One-shot mode keeps the runner.get_cwd behavior EXACTLY (no project surface).
    runner = FakeRunner()
    runner.get_cwd = lambda chat_id: "/some/dir"
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    upd = make_update(1, "/pwd")
    await bot.cmd_pwd(upd, make_cmd_ctx())
    assert "/some/dir" in upd.message.reply_text.await_args.args[0]


# ---- /cd (streaming: fixed-per-project; one-shot unchanged) ---------------


async def test_cmd_cd_streaming_says_fixed_per_project(tmp_path):
    # D4: /cd in streaming mode replies that cwd is fixed per project and mutates NOTHING.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", "/work/api", make_active=True)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/cd /somewhere/else")
    await bot.cmd_cd(upd, make_cmd_ctx(args=["/somewhere/else"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "fixed per project" in reply.lower() and "/new" in reply
    # The active project's cwd is untouched (no store mutation).
    assert store.get_project(1, "api")["cwd"] == "/work/api"
    assert store.get_active(1) == "api"


async def test_cmd_cd_oneshot_still_confines_and_sets(tmp_path):
    # SB2 regression (mirrors test_bot.test_cmd_cd_happy): one-shot /cd still resolves
    # within roots + calls set_cwd unchanged. Reuse the one-shot FakeRunner from
    # test_bot.py (it implements set_cwd); allow_any_path keeps SB2 from short-circuiting
    # so the happy set_cwd path runs (tmp_path is outside the /work workdir).
    from tests.test_bot import FakeRunner as OneshotRunner
    from tests.test_bot import make_config as oneshot_config
    from tests.test_bot import make_update as oneshot_update

    runner = OneshotRunner()
    bot = TelegramClaudeBot(oneshot_config(allow_any_path=True), runner)
    upd = oneshot_update(1, "")
    await bot.cmd_cd(upd, make_cmd_ctx(args=[str(tmp_path)]))
    assert runner.cwd == str(tmp_path.resolve())  # set_cwd ran with the canonical path
    assert str(tmp_path.resolve()) in upd.message.reply_text.await_args.args[0]


# ---- RB1: streaming + no STATE_FILE (store is None) must not crash ---------


async def test_cmd_switch_no_store_is_graceful_not_crash():
    """RB1 (T5 review): ENGINE_MODE=streaming with STATE_FILE unset → store is None.
    /switch must reply gracefully, never AttributeError on a None store."""
    session, _ = make_streaming(None)  # no persistence
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/switch alpha")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["alpha"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "/new" in reply  # graceful no-projects notice (no exception raised)


async def test_cmd_rm_no_store_is_graceful_not_crash():
    """RB1 (T5 review): /rm with a None store replies gracefully, never crashes."""
    session, _ = make_streaming(None)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm alpha")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["alpha"]))
    assert "remove" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_rm_non_active_case_insensitive(tmp_path):
    """/rm of a NON-active project resolves case-insensitively (store._resolve_name) and
    deletes it, leaving the active project untouched."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/rm BETA")  # different case than stored "beta"
    await bot.cmd_rm(upd, make_cmd_ctx(args=["BETA"]))
    assert "beta" not in store.list_projects(1)  # removed via case-insensitive resolve
    assert "alpha" in store.list_projects(1)  # active untouched
    assert "removed" in upd.message.reply_text.await_args.args[0].lower()


# ---- /rm purges the in-memory runtime (QF5 / B4) --------------------------


class _RecordingHoldEngine(HoldEngine):
    """A HoldEngine that records the cwd + policy it was BUILT with (for B4 assertions)."""

    def __init__(self, script, *, cwd, policy):
        super().__init__(script)
        self.built_cwd = cwd
        self.built_policy = policy


def _cwd_routing_session(store, *, root, scripts_by_cwd):
    """A real StreamingSession whose factory builds a distinct _RecordingHoldEngine per cwd.

    Each build records ``(cwd, policy)`` so a test can prove the recreated project ran in
    the NEW cwd with a FRESH (fail-closed) policy. The bot + session share REAL
    ``allowed_roots=(root,)`` (allow_any_path=False) so /new's SB2 confinement and the
    turn-path cwd re-validation both have teeth on the real dirs. ``scripts_by_cwd`` maps a
    cwd → the event script that cwd's engine yields (each build of a cwd reuses its script).
    """
    built: list[_RecordingHoldEngine] = []

    def factory(*, cwd, backstop_seconds, permission_policy):
        eng = _RecordingHoldEngine(
            list(scripts_by_cwd.get(cwd, [])), cwd=cwd, policy=permission_policy
        )
        built.append(eng)
        return eng

    session = StreamingSession(
        make_config(
            engine_mode="streaming", workdir=str(root), allowed_roots=(root,)
        ),
        session_store=store,
        engine_factory=factory,
        clock=lambda: 0.0,
    )
    return session, built


async def test_cmd_rm_purges_runtime_so_recreate_does_not_leak_cwd_or_yolo(tmp_path):
    # B4 (QF5): /rm must purge the project's in-memory runtime. Otherwise re-creating the
    # SAME name via /new reuses the stale runtime — running the recreated project in the OLD
    # cwd and inheriting the OLD /yolo + allow-session grants (D4 cwd leak / SB5 bypass leak),
    # because _runtime caches by name and ignores the new cwd on a hit.
    #
    # Mutation probe: if cmd_rm does NOT call forget_project, the recreated `work` reuses the
    # old runtime → the final turn's engine is built with the OLD cwd / a dirty policy →
    # the cwd + fail-closed assertions below fail.
    root = tmp_path / "root"
    root.mkdir()
    work_old = root / "work_old"
    work_old.mkdir()
    work_new = root / "work_new"
    work_new.mkdir()
    other_dir = root / "other"
    other_dir.mkdir()

    ok = ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "work", str(work_old), make_active=True)
    store.create(1, "other", str(other_dir), make_active=False)

    session, built = _cwd_routing_session(
        store,
        root=root,
        scripts_by_cwd={str(work_old): [ok], str(work_new): [ok], str(other_dir): [ok]},
    )
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    rec_ctx = make_ctx()

    # Turn 1 on `work` (active) → builds + starts work's engine in work_old.
    await asyncio.wait_for(
        session.handle_message(
            1, "hi work", send=rec_ctx.bot.send_message, edit=rec_ctx.bot.edit_message_text
        ),
        timeout=2.0,
    )
    work_rt = session._chat(1).runtimes["work"]
    assert work_rt.cwd == str(work_old) and work_rt.started is True
    work_engine = work_rt.engine
    assert work_engine is not None and work_engine.built_cwd == str(work_old)

    # Dirty work's policy: /yolo ON + an allow-session grant (the bypass posture that must
    # NOT survive a /rm + /new of the same name).
    work_rt.policy.set_yolo(True)
    work_rt.policy.grant_session("Bash")
    assert work_rt.policy.yolo is True

    # Switch active away to `other` so `work` is non-active (and therefore removable).
    upd_sw = make_update(1, "/switch other")
    await bot.cmd_switch(upd_sw, make_cmd_ctx(args=["other"]))
    assert store.get_active(1) == "other"
    # work's runtime is still cached (its engine still started — D2 stop happens on the next
    # turn, not on the bot-level switch), so /rm has a real runtime to purge.
    assert "work" in session._chat(1).runtimes

    # /rm work → store-remove + forget_project: the runtime is dropped and its engine stopped.
    upd_rm = make_update(1, "/rm work")
    await bot.cmd_rm(upd_rm, make_cmd_ctx(args=["work"]))
    assert "work" not in store.list_projects(1)  # gone from the registry
    assert "work" not in session._chat(1).runtimes  # B4: in-memory runtime PURGED
    assert work_engine.stopped is True  # its engine was best-effort stopped on purge
    assert "removed" in upd_rm.message.reply_text.await_args.args[0].lower()

    # Re-create `work` at a DIFFERENT (in-roots) cwd and switch to it.
    upd_new = make_update(1, "/new work " + str(work_new))
    await bot.cmd_new(upd_new, make_cmd_ctx(args=["work", str(work_new)]))
    assert store.get_active(1) == "work"  # /new auto-switches
    assert store.get_project(1, "work")["cwd"] == str(work_new)

    # Run a turn on the recreated `work` → a FRESH runtime is built from the store record.
    await asyncio.wait_for(
        session.handle_message(
            1, "hi new work", send=rec_ctx.bot.send_message, edit=rec_ctx.bot.edit_message_text
        ),
        timeout=2.0,
    )
    new_rt = session._chat(1).runtimes["work"]
    # No cwd leak: the recreated project runs in the NEW cwd, not the old one.
    assert new_rt.cwd == str(work_new)
    assert new_rt.engine is not None and new_rt.engine.built_cwd == str(work_new)
    assert new_rt.engine is not work_engine  # a brand-new engine, not the stale one
    # No yolo / grant leak: the fresh runtime's policy is fail-closed (the engine was built
    # with this same fresh policy object — SB5).
    assert new_rt.policy.yolo is False
    assert new_rt.policy.granted_tools() == frozenset()
    assert new_rt.engine.built_policy.yolo is False
    assert new_rt.engine.built_policy.granted_tools() == frozenset()


async def test_cmd_rm_with_no_in_memory_runtime_is_clean_noop(tmp_path):
    # Regression: /rm of a project that has NO in-memory runtime (never run this process) is
    # a clean no-op for forget_project — it still removes the store record and replies, never
    # crashing on the absent runtime.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)  # never used → no runtime
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    # Sanity: beta has no in-memory runtime.
    assert "beta" not in session._chat(1).runtimes
    upd = make_update(1, "/rm beta")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["beta"]))
    assert "beta" not in store.list_projects(1)
    assert "removed" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_rm_purges_runtime_case_insensitively(tmp_path):
    # forget_project resolves the runtime key case-insensitively (mirroring the store match):
    # /rm WORK purges the runtime stored under "work". Without the case-insensitive match the
    # stale runtime would survive and leak on a later /new.
    root = tmp_path / "root"
    root.mkdir()
    work_dir = root / "work"
    work_dir.mkdir()
    other_dir = root / "other"
    other_dir.mkdir()

    ok = ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "work", str(work_dir), make_active=True)
    store.create(1, "other", str(other_dir), make_active=False)
    session, _ = _cwd_routing_session(
        store, root=root, scripts_by_cwd={str(work_dir): [ok], str(other_dir): [ok]}
    )
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    rec_ctx = make_ctx()
    # Build work's runtime (turn while active).
    await asyncio.wait_for(
        session.handle_message(
            1, "hi", send=rec_ctx.bot.send_message, edit=rec_ctx.bot.edit_message_text
        ),
        timeout=2.0,
    )
    assert "work" in session._chat(1).runtimes
    work_engine = session._chat(1).runtimes["work"].engine
    # Switch away, then /rm with DIFFERENT casing than the stored "work".
    await bot.cmd_switch(make_update(1, "/switch other"), make_cmd_ctx(args=["other"]))
    await bot.cmd_rm(make_update(1, "/rm WORK"), make_cmd_ctx(args=["WORK"]))
    assert "work" not in session._chat(1).runtimes  # purged despite the case mismatch
    assert work_engine.stopped is True


async def test_cmd_projects_survives_sparse_and_dangling_active(tmp_path):
    """RB1: a hand-edited/sparse on-disk doc (record missing cwd; active pointing at a
    missing project) must not crash /projects — fall back to (no path), no marker."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"version": 2, "chats": {"1": {"active": "ghost", "projects": {"alpha": {}}}}}
        ),
        encoding="utf-8",
    )
    store = JsonSessionStore(path)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "alpha" in reply and "(no path)" in reply  # sparse record rendered, no crash


# ===========================================================================
# P4 / T6 — /new <name> <path> (the SB2 path-input command).
#
# Wires a REAL StreamingSession over a REAL JsonSessionStore (so store.create is the
# CRUD under test) + the bot's Config carrying allowed_roots / allow_any_path (the SB2
# policy). The bot's resolve_within_roots reads the bot's config; in-roots existing
# tmp dirs exercise the happy path, out-of-root / traversal / symlink the SB2 refusals.
# ===========================================================================


async def test_cmd_new_happy_creates_resolved_cwd_and_switches(tmp_path):
    # In-roots existing dir → create with the RESOLVED cwd, make active, confirm.
    proj = tmp_path / "work"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new work " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", str(proj)]))
    assert store.get_active(1) == "work"
    # The stored cwd is the RESOLVED (canonical) path, not the raw arg.
    assert store.get_project(1, "work")["cwd"] == str(proj.resolve())
    reply = upd.message.reply_text.await_args.args[0]
    assert "work" in reply and str(proj.resolve()) in reply


async def test_cmd_new_out_of_root_refused_not_created(tmp_path):
    # SB2: a path OUTSIDE allowed_roots (and allow_any_path=False) is refused; the
    # project is NOT created. allowed_roots is a sibling subdir, the target is elsewhere.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new bad " + str(outside))
    await bot.cmd_new(upd, make_cmd_ctx(args=["bad", str(outside)]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "not allowed" in reply.lower()
    assert store.list_projects(1) == {}  # NOT created


async def test_cmd_new_symlink_escape_refused(tmp_path):
    # SB2: a symlink that points OUTSIDE the roots is followed by resolve() and refused
    # (one traversal/symlink case is enough — the resolver canonicalizes both). The link
    # itself sits inside the root; its target escapes.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new sneaky " + str(link))
    await bot.cmd_new(upd, make_cmd_ctx(args=["sneaky", str(link)]))
    assert "not allowed" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}  # NOT created


async def test_cmd_new_allow_any_path_accepts_out_of_root(tmp_path):
    # ALLOW_ANY_PATH opt-out: with allow_any_path=True an out-of-root existing dir is
    # accepted (the explicit escape hatch — SB2 confinement disabled).
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(
            engine_mode="streaming",
            workdir=str(root),
            allowed_roots=(root,),
            allow_any_path=True,
        ),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new anywhere " + str(outside))
    await bot.cmd_new(upd, make_cmd_ctx(args=["anywhere", str(outside)]))
    assert store.get_active(1) == "anywhere"
    assert store.get_project(1, "anywhere")["cwd"] == str(outside.resolve())


async def test_cmd_new_not_a_directory_refused(tmp_path):
    # An in-roots path that does not exist (or is a file) → "Not a directory", not created.
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    missing = tmp_path / "nope"  # in-roots but does not exist
    upd = make_update(1, "/new ghost " + str(missing))
    await bot.cmd_new(upd, make_cmd_ctx(args=["ghost", str(missing)]))
    assert "not a directory" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}  # NOT created


async def test_cmd_new_file_target_refused(tmp_path):
    # An in-roots path that IS a file (not a dir) → "Not a directory", not created.
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new f " + str(f))
    await bot.cmd_new(upd, make_cmd_ctx(args=["f", str(f)]))
    assert "not a directory" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}


async def test_cmd_new_invalid_name_refused_no_create(tmp_path):
    # SB4: a bad name (slash) is refused BEFORE the filesystem is touched; not created.
    proj = tmp_path / "work"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new bad/name " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["bad/name", str(proj)]))
    assert "invalid project name" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}  # NOT created


async def test_cmd_new_duplicate_refused(tmp_path):
    # Creating the same name twice → the second is refused (DuplicateProject).
    proj = tmp_path / "work"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    await bot.cmd_new(make_update(1, "/new dup " + str(proj)), make_cmd_ctx(args=["dup", str(proj)]))
    assert store.get_active(1) == "dup"
    upd = make_update(1, "/new dup " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["dup", str(proj)]))
    assert "already exists" in upd.message.reply_text.await_args.args[0].lower()
    assert list(store.list_projects(1)) == ["dup"]  # still exactly one


async def test_cmd_new_while_busy_refused_store_untouched(tmp_path):
    # LOAD-BEARING busy-guard (D2): /new auto-switches the active project, so while a turn
    # holds the lock it must refuse and NOT call store.create — a mid-hold active-project
    # change deadlocks the parked turn (same invariant as /switch).
    proj = tmp_path / "work"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, engine = make_streaming(store, script=[HOLD], workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )

    # Drive a turn that parks on HOLD (acquires + holds the per-chat turn lock).
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    for _ in range(200):
        if session.is_busy(1):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1), "the held turn should hold the lock"

    # Spy on store.create to prove it is NOT called while busy.
    create_calls = []
    orig_create = store.create
    store.create = lambda *a, **k: create_calls.append((a, k))  # type: ignore[assignment]
    upd = make_update(1, "/new work " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", str(proj)]))
    store.create = orig_create  # type: ignore[assignment]

    reply = upd.message.reply_text.await_args.args[0]
    assert "/cancel" in reply and ("flight" in reply.lower() or "finish" in reply.lower())
    assert create_calls == [], "store.create must NOT be called while a turn is in flight"
    assert list(store.list_projects(1)) == ["alpha"]  # registry unchanged

    # Release the held turn so the task completes cleanly (no leaked task).
    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_cmd_new_no_store_is_graceful_not_crash(tmp_path):
    # RB1: ENGINE_MODE=streaming with STATE_FILE unset → store is None. /new must reply
    # gracefully, never AttributeError on a None store.
    proj = tmp_path / "work"
    proj.mkdir()
    session, _ = make_streaming(None, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new work " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", str(proj)]))  # must not raise
    assert "persistence" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_new_oneshot_streaming_only_notice():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/new work /tmp")
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", "/tmp"]))
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_new_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(999, "/new work " + str(tmp_path))
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", str(tmp_path)]))
    upd.message.reply_text.assert_not_awaited()
    assert store.list_projects(1) == {}  # nothing created for the real chat either


async def test_cmd_new_missing_path_usage(tmp_path):
    # RB1: only a name, no path → usage (handles the 1-arg case).
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new work")
    await bot.cmd_new(upd, make_cmd_ctx(args=["work"]))
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}


async def test_cmd_new_no_args_usage(tmp_path):
    # RB1: zero args → usage (handles the 0-arg case).
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new")
    await bot.cmd_new(upd, make_cmd_ctx(args=[]))
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()
    assert store.list_projects(1) == {}


# ---- /new relative-path resolution (deferred from T6 / T8 item 10) --------


async def test_cmd_new_relative_path_inside_root_resolves_against_active_cwd(tmp_path):
    # SB2 (T6 deferred): a RELATIVE <path> resolves against the ACTIVE project's cwd
    # (bot.get_cwd) and, if the result lands inside a permitted root, the project is
    # created with the RESOLVED (canonical) cwd — not the raw relative arg. Here the
    # active project sits at <root>/api; `/new sub child` must resolve to <root>/api/child.
    root = tmp_path / "root"
    root.mkdir()
    api = root / "api"
    api.mkdir()
    child = api / "child"
    child.mkdir()  # the relative target, INSIDE the root, must exist (is-a-dir check)

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(api), make_active=True)  # active project's cwd = <root>/api
    # allow_any_path=False so SB2 actually confines (the resolve base is the active cwd).
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    upd = make_update(1, "/new sub child")
    await bot.cmd_new(upd, make_cmd_ctx(args=["sub", "child"]))  # relative "child"
    # Created, and the stored cwd is the RESOLVED path under the active project's cwd.
    assert store.get_active(1) == "sub"
    assert store.get_project(1, "sub")["cwd"] == str(child.resolve())
    assert str(child.resolve()) in upd.message.reply_text.await_args.args[0]


async def test_cmd_new_relative_dotdot_escape_refused(tmp_path):
    # SB2 (T6 deferred): a relative `..`-escape that resolves OUTSIDE the permitted root
    # (against the active project's cwd) is refused and the project is NOT created — the
    # confinement holds for relative inputs, not just absolute ones.
    root = tmp_path / "root"
    root.mkdir()
    api = root / "api"
    api.mkdir()
    outside = tmp_path / "outside"  # a real dir, OUTSIDE root, reachable via ../../outside
    outside.mkdir()

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "api", str(api), make_active=True)  # resolve base = <root>/api
    session, _ = make_streaming(store, workdir=str(root))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(root), allowed_roots=(root,)),
        FakeRunner(),
        streaming=session,
    )
    # ../../outside from <root>/api == <tmp_path>/outside → escapes the root → refused.
    upd = make_update(1, "/new escape ../../outside")
    await bot.cmd_new(upd, make_cmd_ctx(args=["escape", "../../outside"]))
    assert "not allowed" in upd.message.reply_text.await_args.args[0].lower()
    assert "escape" not in store.list_projects(1)  # NOT created
    assert set(store.list_projects(1)) == {"api"}  # only the pre-existing active project


def test_build_application_registers_new_before_skill_passthrough():
    # /new is a specific CommandHandler wired BEFORE the on_skill_command COMMAND
    # passthrough — first-match-wins keeps it from being forwarded as a skill.
    from telegram.ext import CommandHandler, MessageHandler

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = bot.build_application()
    ordered = [h for group in app.handlers.values() for h in group]
    skill_passthrough_idx = None
    new_idx = None
    for i, h in enumerate(ordered):
        if isinstance(h, CommandHandler) and "new" in {c.lstrip("/").lower() for c in h.commands}:
            new_idx = i
        if isinstance(h, MessageHandler) and getattr(h.callback, "__name__", "") == "on_skill_command":
            skill_passthrough_idx = i
    assert new_idx is not None, "/new must be a registered CommandHandler"
    assert skill_passthrough_idx is not None
    assert new_idx < skill_passthrough_idx
