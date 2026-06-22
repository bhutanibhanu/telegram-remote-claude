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
from claude_tg.claude_runner import ClaudeResult
from claude_tg.config import Config
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
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"
    reply = upd.message.reply_text.await_args.args[0]
    assert "beta" in reply and "resume" in reply.lower()


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
