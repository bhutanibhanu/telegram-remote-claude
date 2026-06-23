from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeBusy, ClaudeResult
from claude_tg.config import Config


def make_config(allowed=(1,), *, allowed_roots=(), allow_any_path=False):
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset(allowed),
        workdir=Path("/work"),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
    )


class FakeRunner:
    def __init__(self, result=None, raises=None):
        self._result = result if result is not None else ClaudeResult(ok=True, text="ok")
        self._raises = raises
        self.run_calls = []
        self.reset_calls = []
        self.set_cwd_calls = []
        self.cwd = "/work"

    async def run(self, chat_id, text):
        self.run_calls.append((chat_id, text))
        if self._raises:
            raise self._raises
        return self._result

    def reset(self, chat_id):
        self.reset_calls.append(chat_id)

    def get_cwd(self, chat_id):
        return self.cwd

    def set_cwd(self, chat_id, path):
        self.set_cwd_calls.append((chat_id, path))
        if path == "/bad":
            raise NotADirectoryError(path)
        self.cwd = path
        return path


def make_update(chat_id=1, text="hello"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.effective_message = upd.message
    return upd


def make_ctx(args=None):
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.args = list(args or [])
    return ctx


async def test_on_message_replies():
    runner = FakeRunner(ClaudeResult(ok=True, text="the answer"))
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "do it")
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == [(1, "do it")]
    upd.message.reply_text.assert_awaited_once_with("the answer")


async def test_on_message_chunks_long_output():
    runner = FakeRunner(ClaudeResult(ok=True, text="x" * 9000))
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "go")
    await bot.on_message(upd, make_ctx())
    assert upd.message.reply_text.await_count >= 3


async def test_on_message_error_result():
    runner = FakeRunner(ClaudeResult(ok=False, text="", error="boom"))
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "go")
    await bot.on_message(upd, make_ctx())
    msg = upd.message.reply_text.await_args.args[0]
    assert "boom" in msg and msg.startswith("⚠️")


async def test_on_message_empty_result_notice():
    runner = FakeRunner(ClaudeResult(ok=True, text="   "))
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "go")
    await bot.on_message(upd, make_ctx())
    upd.message.reply_text.assert_awaited_once()
    assert "no text" in upd.message.reply_text.await_args.args[0].lower()


async def test_on_message_busy_stops_typing():
    runner = FakeRunner(raises=ClaudeBusy())
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "go")
    await bot.on_message(upd, make_ctx())  # must not raise; typing task awaited in finally
    assert "still working" in upd.message.reply_text.await_args.args[0].lower()


async def test_on_message_unauthorized_ignored():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,)), runner)
    upd = make_update(999, "hi")
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == []
    upd.message.reply_text.assert_not_awaited()


async def test_on_message_empty_text_no_run():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "   ")
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == []


async def test_cmd_cd_happy(tmp_path):
    # tmp_path is outside the /work workdir; allow_any_path=True keeps SB2 from
    # short-circuiting so this still exercises the happy set_cwd path (its intent).
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allow_any_path=True), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=[str(tmp_path)]))
    # The runner is handed the CANONICAL path (resolve() may prepend /private on macOS).
    assert runner.cwd == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in upd.message.reply_text.await_args.args[0]


async def test_cmd_cd_not_a_dir():
    # allow_any_path=True so SB2 passes and the NotADirectoryError branch is reached
    # (the original intent of this test). /bad does not exist -> runner raises.
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allow_any_path=True), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=["/bad"]))
    assert "not a directory" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_cd_default_confinement_rejects_out_of_root(tmp_path):
    # NEW (SB2): with default confinement (allowed_roots confined to /work, no opt-out),
    # a /cd to a path outside the root is REFUSED before the runner is touched.
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed_roots=(Path("/work"),)), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=[str(tmp_path)]))  # tmp_path is outside /work
    assert "not allowed" in upd.message.reply_text.await_args.args[0].lower()
    assert runner.set_cwd_calls == [], "set_cwd must NOT be called on a refused path"
    assert runner.cwd == "/work"  # unchanged


async def test_cmd_cd_usage():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=[]))
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_reset():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    await bot.cmd_reset(upd, make_ctx())
    assert runner.reset_calls == [1]


async def test_cmd_pwd():
    runner = FakeRunner()
    runner.cwd = "/some/dir"
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    await bot.cmd_pwd(upd, make_ctx())
    assert "/some/dir" in upd.message.reply_text.await_args.args[0]


async def test_cmd_pwd_path_is_code_wrapped_not_bare():
    """R6 (auto-linkify): the cwd shown to the operator MUST be wrapped in <code>…</code>
    and sent with parse_mode="HTML", so Telegram renders it as inert monospace instead of
    auto-linkifying each "/segment" of the path as a tappable fake command-link
    (a bare "/some/dir" otherwise becomes tappable "/some" "/dir" "commands"). Guards the
    one-shot /pwd reply: the path appears ONLY inside <code> tags, never bare.
    """
    runner = FakeRunner()
    runner.cwd = "/some/dir"
    bot = TelegramClaudeBot(make_config(), runner)
    upd = make_update(1, "")
    await bot.cmd_pwd(upd, make_ctx())
    text = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    assert "<code>/some/dir</code>" in text
    assert kwargs.get("parse_mode") == "HTML"
    # The path must NOT also appear bare (outside the <code> wrapper) — a bare copy would
    # still linkify. Stripping the one wrapped occurrence leaves no other "/some/dir".
    assert "/some/dir" not in text.replace("<code>/some/dir</code>", "")


def test_authorized():
    bot = TelegramClaudeBot(make_config(allowed=(1, 2)), FakeRunner())
    assert bot._authorized(make_update(1)) is True
    assert bot._authorized(make_update(3)) is False


async def test_cmd_reset_unauthorized_ignored():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,)), runner)
    upd = make_update(999, "")
    await bot.cmd_reset(upd, make_ctx())
    assert runner.reset_calls == []
    upd.message.reply_text.assert_not_awaited()
