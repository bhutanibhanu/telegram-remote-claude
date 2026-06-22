"""T7 bot-level streaming + SB1 callback-handler tests (mock Telegram + engine).

These cover the bot.py wiring: the ENGINE_MODE switch keeps one-shot the default; the
streaming path delegates to a StreamingSession; and — the security-critical part — the
``on_callback`` handler enforces SB1 (an explicit allowlist recheck inside the handler)
so a NON-allowlisted callback never routes a decision. No live Telegram / Claude / net.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeResult
from claude_tg.config import Config
from claude_tg.stream_session import CallbackOutcome, StreamingBusy


def make_config(allowed=(1,), engine_mode="oneshot"):
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset(allowed),
        workdir=Path("/work"),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
        engine_mode=engine_mode,
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

    async def handle_message(self, chat_id, text, *, send, edit):
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
