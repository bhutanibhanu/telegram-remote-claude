"""T1 skill-launch passthrough tests (mock Telegram + engine, no live Claude/net).

P3's only new surface: ``on_skill_command`` forwards any *unregistered* slash-command
verbatim to the active session — so ``/grill``, ``/pipeline``, … launch that skill in
the live session. These cover the T1 acceptance criteria:

* Passthrough forwards the VERBATIM command text (``/`` + args) to the same turn path
  ``on_message`` uses — in BOTH one-shot (``runner.run``) and streaming
  (``StreamingSession.handle_message``) engine modes.
* Bot commands WIN: PTB first-match-wins routing (exercised against the REAL handler list
  from :meth:`build_application`) sends a registered command (``/reset``, ``/cd``) to its
  own ``CommandHandler``, never the passthrough — confirmed via PTB's own ``check_update``.
* SB1: a non-allowlisted chat launches NO skill — the ``allowed`` filter drops it at the
  routing layer AND the ``_ok`` recheck drops it inside the handler (no session call).
* RB1: empty / whitespace / ``/``-only / unicode-garbage / missing-message text never
  raises and leaves the session usable.

Substrate (runner + streaming session) is mocked; behavior is asserted, not internals.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, Message, MessageEntity, Update
from telegram.ext import CommandHandler, MessageHandler

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeResult
from claude_tg.config import Config


def make_config(allowed=(1,), *, engine_mode="oneshot"):
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
    """One-shot runner stand-in — records what it is asked to run."""

    def __init__(self, result=None):
        self._result = result if result is not None else ClaudeResult(ok=True, text="ok")
        self.run_calls: list[tuple[int, str]] = []

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
    """StreamingSession stand-in at the bot boundary — records handle_message calls."""

    def __init__(self):
        self.handle_message_calls: list[tuple[int, str]] = []

    async def handle_message(
        self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
        command_initiated=False,
    ):
        # P5/T9: handle_message gained reply_to_message_id (D5); P9 fix added
        # command_initiated (a macro /run skips free-text capture). The skill-launch tests
        # don't exercise either, so we keep recording just (chat_id, text).
        self.handle_message_calls.append((chat_id, text))

    def reset(self, chat_id):
        pass


def make_update(chat_id=1, text="/grill do X"):
    """A mock command update (handler-method level — no PTB routing needed)."""
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.effective_message = upd.message
    return upd


def make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.args = []
    return ctx


# ---------------------------------------------------------------------------
# (a) Passthrough forwards the verbatim command text to the session turn path.
# ---------------------------------------------------------------------------


async def test_skill_command_forwards_verbatim_oneshot():
    """A non-bot command forwards the VERBATIM text (cmd + args, leading /) to runner.run."""
    runner = FakeRunner(ClaudeResult(ok=True, text="launched"))
    bot = TelegramClaudeBot(make_config(), runner)
    # P9/T1: pre-mark welcomed so the first-run welcome doesn't perturb the assert-once.
    bot._welcomed.add(1)
    upd = make_update(1, "/grill do X")
    await bot.on_skill_command(upd, make_ctx())
    assert runner.run_calls == [(1, "/grill do X")]  # verbatim — / and args intact
    upd.message.reply_text.assert_awaited_once_with("launched")


async def test_skill_command_forwards_verbatim_streaming():
    """(e) Streaming mode routes the forwarded command to StreamingSession.handle_message."""
    runner = FakeRunner()
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), runner, streaming=streaming)
    upd = make_update(1, "/pipeline")
    await bot.on_skill_command(upd, make_ctx())
    assert streaming.handle_message_calls == [(1, "/pipeline")]  # verbatim to the session
    assert runner.run_calls == []  # one-shot runner NOT used in streaming mode


async def test_skill_command_with_args_preserved_streaming():
    """Args after the command are preserved verbatim when forwarded (streaming)."""
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/scaffold a new python cli")
    await bot.on_skill_command(upd, make_ctx())
    assert streaming.handle_message_calls == [(1, "/scaffold a new python cli")]


# ---------------------------------------------------------------------------
# (b) Bot commands win — exercised against the REAL handler list + PTB routing.
# ---------------------------------------------------------------------------

RESERVED = ["start", "help", "reset", "cancel", "yolo", "unyolo", "pwd", "cd"]


def _command_update(text: str, chat_id: int = 1) -> Update:
    """A REAL telegram Update carrying a BOT_COMMAND entity, bound to a stub bot.

    This lets PTB's genuine ``CommandHandler``/``MessageHandler`` ``check_update`` run —
    so the first-match-wins ordering is exercised for real, not asserted on registration.
    """
    first = text.split()[0]
    ent = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(first))
    msg = Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=chat_id, type="private"),
        text=text,
        entities=[ent],
    )
    bot = MagicMock()
    bot.username = "mybot"
    msg.set_bot(bot)
    return Update(update_id=1, message=msg)


def _ordered_handlers(bot: TelegramClaudeBot):
    app = bot.build_application()
    # Group 0 holds the message/command handlers in registration order.
    return [h for group in sorted(app.handlers) for h in app.handlers[group]]


def _first_match(handlers, update: Update):
    """Mirror PTB's first-match-wins: the first handler whose check_update is truthy."""
    for h in handlers:
        check = h.check_update(update)
        if check is None or check is False:
            continue
        return h
    return None


def test_registered_command_routes_to_its_command_handler_not_passthrough():
    """(b) /reset is claimed by its own CommandHandler — the passthrough never sees it.

    Drives PTB's real first-match-wins over the actual handler list from
    build_application(): the winner for /reset must be the cmd_reset CommandHandler, even
    though the passthrough MessageHandler would ALSO match /reset (which is exactly why
    the passthrough is registered AFTER the CommandHandlers).
    """
    bot = TelegramClaudeBot(make_config(), FakeRunner())
    handlers = _ordered_handlers(bot)
    winner = _first_match(handlers, _command_update("/reset"))
    assert isinstance(winner, CommandHandler)
    assert "reset" in winner.commands  # the dedicated /reset handler, not the passthrough


def test_cd_with_args_routes_to_command_handler_not_passthrough():
    """(b) /cd /tmp is claimed by cmd_cd's CommandHandler, not the passthrough."""
    bot = TelegramClaudeBot(make_config(), FakeRunner())
    handlers = _ordered_handlers(bot)
    winner = _first_match(handlers, _command_update("/cd /tmp"))
    assert isinstance(winner, CommandHandler)
    assert "cd" in winner.commands


def test_all_reserved_bot_commands_win_over_passthrough():
    """(b) EVERY reserved bot command routes to a CommandHandler, never the passthrough."""
    bot = TelegramClaudeBot(make_config(), FakeRunner())
    handlers = _ordered_handlers(bot)
    for name in RESERVED:
        winner = _first_match(handlers, _command_update(f"/{name}"))
        assert isinstance(winner, CommandHandler), f"/{name} should win via CommandHandler"
        assert name in winner.commands, f"/{name} routed to the wrong handler: {winner.commands}"


def test_unregistered_command_falls_through_to_passthrough():
    """(b/a) An unregistered command (/grill) is claimed by the passthrough MessageHandler.

    No CommandHandler matches, so the first truthy handler is the passthrough — confirming
    only *unregistered* commands reach on_skill_command.
    """
    bot = TelegramClaudeBot(make_config(), FakeRunner())
    handlers = _ordered_handlers(bot)
    winner = _first_match(handlers, _command_update("/grill"))
    assert isinstance(winner, MessageHandler)
    # Bound methods compare equal (==) but each access is a new object, so avoid `is`.
    assert winner.callback == bot.on_skill_command


def test_passthrough_registered_after_command_handlers():
    """The passthrough MessageHandler is registered AFTER every CommandHandler (ordering)."""
    bot = TelegramClaudeBot(make_config(), FakeRunner())
    handlers = _ordered_handlers(bot)
    passthrough_idx = next(
        i for i, h in enumerate(handlers)
        if isinstance(h, MessageHandler) and h.callback == bot.on_skill_command
    )
    last_command_idx = max(i for i, h in enumerate(handlers) if isinstance(h, CommandHandler))
    assert passthrough_idx > last_command_idx


# ---------------------------------------------------------------------------
# (c) SB1 — a non-allowlisted chat launches NO skill.
# ---------------------------------------------------------------------------


async def test_skill_command_unauthorized_no_session_call_oneshot():
    """SB1: a non-allowlisted chat's slash-command never reaches the runner (no skill)."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,)), runner)
    upd = make_update(chat_id=999, text="/grill do harm")  # NOT allowlisted
    await bot.on_skill_command(upd, make_ctx())
    assert runner.run_calls == []  # no turn started -> no skill launched
    upd.message.reply_text.assert_not_awaited()  # nothing leaked back


async def test_skill_command_unauthorized_no_session_call_streaming():
    """SB1: a non-allowlisted chat's slash-command never reaches the streaming session."""
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    upd = make_update(chat_id=999, text="/pipeline")  # NOT allowlisted
    await bot.on_skill_command(upd, make_ctx())
    assert streaming.handle_message_calls == []  # engine NEVER touched
    upd.message.reply_text.assert_not_awaited()


def test_sb1_passthrough_filter_drops_unauthorized_at_routing_layer():
    """SB1 (defense in depth): the `allowed` filter drops a non-allowlisted command at the
    PTB routing layer too — the passthrough MessageHandler's check_update is False, so the
    handler is never even dispatched for chat 999."""
    bot = TelegramClaudeBot(make_config(allowed=(1,)), FakeRunner())
    handlers = _ordered_handlers(bot)
    winner = _first_match(handlers, _command_update("/grill", chat_id=999))
    assert winner is None  # no handler claims an unauthorized command


# ---------------------------------------------------------------------------
# (d) RB1 — malformed / empty command never raises; session stays usable.
# ---------------------------------------------------------------------------


async def test_skill_command_empty_text_no_run():
    """RB1: empty message text no-ops (after strip) — no turn, no raise."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == []


async def test_skill_command_whitespace_text_no_run():
    """RB1: whitespace-only text no-ops (after strip)."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "   \n\t ")
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == []


async def test_skill_command_slash_only_forwards_as_turn():
    """RB1: a bare '/' is non-empty after strip -> forwarded verbatim as an ordinary turn
    (no validation/allowlist, D1/D2) — and the handler does not raise."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "/")
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == [(1, "/")]


async def test_skill_command_unicode_garbage_forwards_no_raise():
    """RB1: unicode garbage after the slash is just forwarded as a turn — never raises."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "/💥🔥 ​ ⚠️")
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == [(1, "/💥🔥 ​ ⚠️".strip())]


async def test_skill_command_missing_message_no_raise():
    """RB1: a missing message (update.message is None) no-ops — never raises."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = MagicMock()
    upd.effective_chat.id = 1
    upd.message = None
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == []


async def test_skill_command_none_text_no_raise():
    """RB1: update.message.text is None -> treated as empty, no-ops, never raises."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    upd.message.text = None
    await bot.on_skill_command(upd, make_ctx())  # must not raise
    assert runner.run_calls == []


async def test_skill_command_session_still_usable_after_garbage():
    """RB1: after a no-op/garbage command, a normal command still launches (session usable)."""
    runner = FakeRunner(ClaudeResult(ok=True, text="ok"))
    bot = TelegramClaudeBot(make_config(), runner)
    await bot.on_skill_command(make_update(1, "   "), make_ctx())  # no-op
    await bot.on_skill_command(make_update(1, "/grill"), make_ctx())  # still works
    assert runner.run_calls == [(1, "/grill")]


# ---------------------------------------------------------------------------
# HELP_TEXT mentions that other slash-commands run as skills.
# ---------------------------------------------------------------------------


def test_help_text_mentions_skill_passthrough():
    from claude_tg.bot import HELP_TEXT

    low = HELP_TEXT.lower()
    assert "skill" in low and "slash-command" in low
