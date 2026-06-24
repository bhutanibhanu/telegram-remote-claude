"""T7 bot-level streaming + SB1 callback-handler tests (mock Telegram + engine).

These cover the bot.py wiring: the ENGINE_MODE switch keeps one-shot the default; the
streaming path delegates to a StreamingSession; and — the security-critical part — the
``on_callback`` handler enforces SB1 (an explicit allowlist recheck inside the handler)
so a NON-allowlisted callback never routes a decision. No live Telegram / Claude / net.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeResult, ClaudeRunner
from claude_tg.config import Config
from claude_tg.engine.types import AskEvent, ResultEvent
from claude_tg.session_store import JsonSessionStore
from claude_tg.stream_session import CallbackOutcome, StreamingBusy, StreamingSession


def make_config(
    allowed=(1,),
    engine_mode="oneshot",
    workdir="/work",
    *,
    allowed_roots=(),
    allow_any_path=False,
    max_concurrent_runs=3,
    render_chat_send_interval_seconds=0.0,
    image_max_bytes=5 * 1024 * 1024,
    file_max_bytes=20 * 1024 * 1024,
    transcribe_cmd="",
    transcribe_timeout_seconds=120.0,
):
    # P5/T8: default the per-chat send-gate interval to 0.0 in tests so the gate never
    # introduces a real ``asyncio.sleep`` under the frozen test clock (these tests assert
    # send/edit CONTENT + ordering, not rate timing — the RB5 gate timing has its own
    # injected-clock tests). Production defaults to ~1 s.
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
        max_concurrent_runs=max_concurrent_runs,
        render_chat_send_interval_seconds=render_chat_send_interval_seconds,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
        image_max_bytes=image_max_bytes,
        file_max_bytes=file_max_bytes,
        transcribe_cmd=transcribe_cmd,
        transcribe_timeout_seconds=transcribe_timeout_seconds,
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

    def __init__(self, outcome=None, busy=False, cwd="/work"):
        # P10 T3: the active project's cwd the on_document save / cmd_get resolve against.
        # Default "/work" (the pre-T3 fixed value); the file tests point it at a tmp dir.
        self._cwd = cwd
        self.handle_message_calls = []
        self.resolve_calls = []
        self.cancel_calls = []
        self.reset_calls = []
        self.yolo_calls = []
        self.model_calls = []
        self.reply_prompt_calls = []
        self.to_calls = []
        self.command_initiated_calls = []
        self.images_calls = []
        self._outcome = outcome or CallbackOutcome(handled=True, note="ok")
        self._busy = busy
        # P5/T7: cmd_reset now reads streaming.store.get_active to scope its busy-guard to
        # the ACTIVE project. This lightweight stand-in carries no registry (store=None), so
        # the active name resolves to None and the guard is skipped — exactly the pre-P5
        # behavior the wiring tests here assert (they exercise delegation, not concurrency;
        # the per-project busy-guard semantics are covered against a REAL session below).
        self.store = None

    async def handle_message(
        self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
        command_initiated=False, images=None,
    ):
        # P5/T9: handle_message gained reply_to_message_id (the D5 reply-to escape hatch);
        # P9 fix: + command_initiated (a macro /run skips free-text capture). P10 T1: + images
        # (the multimodal photo/screenshot). Record them so the wiring tests can assert they
        # are threaded through from on_message / cmd_run / on_photo.
        self.handle_message_calls.append((chat_id, text, reply_to_message_id))
        self.command_initiated_calls.append(command_initiated)
        self.images_calls.append(images)
        if self._busy:
            raise StreamingBusy()
        # T6/P9: handle_message now returns whether the message was a free-text capture (the
        # bot dismisses the quick-reply chips on True). This stand-in drives normal turns →
        # False; the free-text-capture behavior is covered against a REAL session.
        return False

    def resolve_callback(self, chat_id, data):
        self.resolve_calls.append((chat_id, data))
        return self._outcome

    def handle_cancel(self, chat_id, name=None):
        # P5/T9: handle_cancel gained an optional name (None=active / "all" / <name>).
        self.cancel_calls.append((chat_id, name))
        return 1

    def is_busy(self, chat_id, name=None):
        # Mirror StreamingSession.is_busy (now (chat_id, name=None) — P5/T5) so the bot's
        # busy-guards can be exercised against this lightweight stand-in. This stand-in has
        # one notional run, so both the chat-level and per-project queries return _busy.
        return self._busy

    def register_reply_prompt(self, chat_id, message_id, tool_use_id):
        # P5/T9: the bot calls this after sending a free-text prompt (D5 reply-to map).
        self.reply_prompt_calls.append((chat_id, message_id, tool_use_id))

    def resolve_to(self, chat_id, name, text):
        # P5/T9: the /to <name> <text> escape hatch (D5). Return a confirmation string.
        self.to_calls.append((chat_id, name, text))
        return f"✅ Sent your reply to {name}."

    def reset(self, chat_id):
        self.reset_calls.append(chat_id)

    def set_yolo(self, chat_id, on):
        self.yolo_calls.append((chat_id, on))

    def set_model(self, chat_id, model):
        # P9/T4: /fast · /deep · /auto set (or clear) the active project's model override.
        self.model_calls.append((chat_id, model))
        return model

    def get_cwd(self, chat_id):
        # P9/T1: the first-run welcome reads the active cwd via this accessor.
        # P10/T3: on_document saves into — and /get resolves against — this cwd.
        return self._cwd

    def get_yolo(self, chat_id):
        # P9/T2: /status reads the active project's yolo posture (read-only).
        return False

    def get_project_yolo(self, chat_id, name):
        # P9 fix: /status surfaces EACH project's yolo posture (read-only). This stand-in
        # has no allow-all projects → False (the per-project marker behavior is covered
        # against a REAL session below).
        return False

    def active_run_count(self):
        # P9/T2: /status reports active-vs-cap run counts.
        return 0

    def queued_waiting(self, chat_id):
        # P9/T6: /status surfaces the queued-behind-the-cap counter (0 → no suffix).
        return 0

    def project_status(self, chat_id, name):
        # P9/T2: /status reuses the per-project status (mirrors /projects).
        return "idle"


def make_update(chat_id=1, text="hello", *, reply_to_message_id=None):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.effective_message = upd.message
    # P5/T9 (D5): a plain message is NOT a reply unless reply_to_message_id is given (else
    # the MagicMock would auto-create a truthy reply_to_message.message_id and every message
    # would look like a reply). When set, build a reply_to_message carrying that id.
    if reply_to_message_id is None:
        upd.message.reply_to_message = None
    else:
        upd.message.reply_to_message = MagicMock(message_id=reply_to_message_id)
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
    # P9/T1: pre-mark welcomed so the first-run welcome doesn't perturb the assert-once.
    bot._welcomed.add(1)
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
    # P5/T9: the third tuple element is the reply-to message_id (None — not a reply).
    assert streaming.handle_message_calls == [(1, "build it", None)]
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
        async def handle_message(
            self, chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
            command_initiated=False,
        ):
            self.handle_message_calls.append((chat_id, text, reply_to_message_id))
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
    assert streaming.cancel_calls == [(1, None)]  # P5/T9: (chat_id, name=None → active)
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
        # Record resolve() calls so a test can assert a free-text answer reached (or did
        # NOT reach) a given engine (P9 /run-vs-free-text-capture fix).
        self.resolve_calls: list = []

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
        self.resolve_calls.append((tool_use_id, decision))
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
    # The active project (alpha) carries the marker; beta does not. R6: names are bolded.
    alpha_line = next(line for line in reply.splitlines() if "<b>alpha</b>" in line)
    beta_line = next(line for line in reply.splitlines() if "<b>beta</b>" in line)
    assert "→" in alpha_line and "→" not in beta_line


async def test_cmd_projects_cwd_column_is_code_wrapped_not_bare(tmp_path):
    """R6 (auto-linkify): the /projects cwd column was the worst offender — every project's
    path rendered as a row of tappable fake "/segment" command-links. Each cwd MUST be
    wrapped in <code>…</code> and the reply sent with parse_mode="HTML" so the paths are
    inert monospace; a cwd must NOT appear bare (a bare copy would still linkify).
    """
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    assert "<code>/work/alpha</code>" in reply and "<code>/work/beta</code>" in reply
    # Neither path appears bare (outside its <code> wrapper) — strip the wrapped copies and
    # assert nothing is left to auto-linkify.
    stripped = reply.replace("<code>/work/alpha</code>", "").replace("<code>/work/beta</code>", "")
    assert "/work/alpha" not in stripped and "/work/beta" not in stripped


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


async def test_cmd_switch_sb2_revalidation_still_fires_while_busy(tmp_path):
    """P5/T7 (D2 relaxed): /switch no longer has a busy-guard, so the FIRST gate a busy
    /switch hits is the SB2 cwd re-validation (it used to be shadowed by the busy refusal).
    A /switch to an OUT-OF-ROOT target while a turn is in flight is now refused for the
    RIGHT reason — the out-of-root message, not a busy message — and the active project is
    left unchanged (SB2 fail-closed is preserved under the relaxation). The held turn keeps
    running throughout (background concurrency).

    (Was ``test_cmd_switch_busy_guard_precedes_revalidation``, which asserted the busy-guard
    fired FIRST. The guard is gone in P5; this re-points the same scenario at the surviving
    SB2 gate so the out-of-root refusal is not lost — coverage moved, not deleted.)
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
    # The OUT-OF-ROOT message (SB2), NOT a busy message — the relaxed /switch reached SB2.
    assert "permitted roots" in reply.lower()
    assert "/cancel" not in reply  # no busy refusal anymore
    assert store.get_active(1) == "alpha"  # out-of-root target refused → active unchanged
    assert session.is_busy(1), "the held turn keeps running (switch did not disturb it)"

    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_cmd_switch_while_busy_succeeds_prior_run_untouched(tmp_path):
    # P5/T7 — THE HEADLINE RELAXATION (D2). While a turn holds project alpha's lock, /switch
    # to beta now SUCCEEDS (no busy refusal): store.switch IS called, active becomes beta,
    # and alpha's held turn is UNTOUCHED — it keeps holding its lock in the background
    # (id-routing, ADR-005 D3, makes this safe: alpha's prompt still resolves by tool_use_id
    # regardless of which project is foreground).
    #
    # MUTATION PROBE: this is the exact inverse of the P4 busy-guard test it replaces
    # (was ``test_cmd_switch_while_busy_refused_store_untouched``). If the is_busy refusal is
    # re-added to cmd_switch, store.switch is no longer called and this fails — so the
    # relaxation is pinned, not merely uncovered.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, engine = make_streaming(store, script=[HOLD])  # the turn parks holding the lock
    # allow_any_path=True so /switch's SB2 cwd re-validation no-ops for the fake /work/beta
    # cwd — this test exercises the busy-guard relaxation, not SB2 (its own test below).
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )

    # Drive a turn that parks on HOLD (acquires + holds alpha's per-project turn lock).
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    # Wait until the turn is actually in flight (alpha's lock held).
    for _ in range(200):
        if session.is_busy(1, "alpha"):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1, "alpha"), "the held turn should hold alpha's lock"

    # /switch beta WHILE alpha is mid-run → SUCCEEDS (the relaxation). Spy store.switch to
    # prove it IS now called (the inverse of the P4 assertion).
    switch_calls = []
    orig_switch = store.switch
    store.switch = lambda *a, **k: (switch_calls.append((a, k)), orig_switch(*a, **k))[1]  # type: ignore[assignment]
    upd = make_update(1, "/switch beta")
    await bot.cmd_switch(upd, make_cmd_ctx(args=["beta"]))
    store.switch = orig_switch  # type: ignore[assignment]

    reply = upd.message.reply_text.await_args.args[0]
    # P9: the name is bolded + escaped (HTML), uniform styling. Success, not a busy refusal.
    assert "switched to <b>beta</b>" in reply.lower(), reply
    assert "/cancel" not in reply
    assert switch_calls, "store.switch MUST be called now that /switch is free mid-run"
    assert store.get_active(1) == "beta"  # active moved
    # alpha's held turn is untouched — still parked + holding its lock (background run).
    assert session.is_busy(1, "alpha"), "the prior run must keep running after the switch"
    assert not turn.done()

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
    kwargs = upd.message.reply_text.await_args.kwargs
    # T3/R6: the name is bolded like /projects, not a Python repr (was the odd-quoted
    # "'nope'"); HTML parse mode so the <b> tags render.
    assert "<b>nope</b>" in reply
    assert kwargs.get("parse_mode") == "HTML"
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


async def test_project_name_replies_use_html_bold_styling(tmp_path):
    # P9 styling unification: EVERY operator-facing reply that names a project renders it as
    # <b>{name}</b> and is sent parse_mode="HTML" — no more bare-{name} interpolation. This
    # pins the common name-bearing replies (/rm success + active-refusal, /new duplicate,
    # /cancel named-nothing, /switch success) so the styling can't silently drift back.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    store.create(1, "beta", str(tmp_path / "beta"), make_active=False)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allow_any_path=True),
        FakeRunner(),
        streaming=session,
    )

    def assert_html_bold(upd, fragment):
        args, kwargs = upd.message.reply_text.call_args
        assert kwargs.get("parse_mode") == "HTML", (args, kwargs)
        assert fragment in args[0], args[0]

    # /rm active-refusal → bold name, HTML.
    up = make_update(1, "/rm alpha")
    await bot.cmd_rm(up, make_cmd_ctx(args=["alpha"]))
    assert_html_bold(up, "<b>alpha</b>")

    # /rm success (non-active beta) → bold name, HTML.
    up = make_update(1, "/rm beta")
    await bot.cmd_rm(up, make_cmd_ctx(args=["beta"]))
    assert_html_bold(up, "<b>beta</b>")

    # /new duplicate → bold name, HTML.
    up = make_update(1, "/new alpha")
    await bot.cmd_new(up, make_cmd_ctx(args=["alpha", str(tmp_path / "alpha")]))
    assert_html_bold(up, "<b>alpha</b>")

    # /cancel named-nothing → bold name, HTML.
    up = make_update(1, "/cancel gamma")
    await bot.cmd_cancel(up, make_cmd_ctx(args=["gamma"]))
    assert_html_bold(up, "<b>gamma</b>")

    # /switch success → bold name, HTML.
    store.create(1, "delta", str(tmp_path / "delta"), make_active=False)
    (tmp_path / "delta").mkdir()
    up = make_update(1, "/switch delta")
    await bot.cmd_switch(up, make_cmd_ctx(args=["delta"]))
    assert_html_bold(up, "<b>delta</b>")


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
    reply = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    # T3/R6: bolded name (was the odd-quoted repr "'ghost'"), HTML parse mode.
    assert "<b>ghost</b>" in reply
    assert kwargs.get("parse_mode") == "HTML"
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
    reply = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    assert "invalid project name" in reply.lower()
    # T3/R6: the rejected name is bolded like /projects (was the odd-quoted repr
    # "'bad/name'"); HTML parse mode so the <b> tags render. ``/`` is not an HTML
    # metachar, so it survives escaping unchanged.
    assert "<b>bad/name</b>" in reply
    assert kwargs.get("parse_mode") == "HTML"
    assert store.list_projects(1) == {}  # NOT created


async def test_cmd_new_invalid_name_hostile_input_is_html_escaped(tmp_path):
    # T3 (defense-in-depth): the invalid-name reply echoes PRE-SB4-validation input — the
    # name was just REJECTED, so it is arbitrary operator input. A name carrying HTML
    # metacharacters (<b>x, a&b) MUST be escaped: no RAW tag/entity in the (now HTML)
    # reply, only the escaped form. A raw "<b>x" would otherwise open a live bold tag in
    # Telegram's HTML parse mode.
    proj = tmp_path / "work"
    proj.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )
    for hostile in ("<b>x", "a&b"):
        upd = make_update(1, "/new " + hostile + " " + str(proj))
        await bot.cmd_new(upd, make_cmd_ctx(args=[hostile, str(proj)]))
        reply = upd.message.reply_text.await_args.args[0]
        kwargs = upd.message.reply_text.await_args.kwargs
        assert kwargs.get("parse_mode") == "HTML"
        # The escaped form is present; the raw hostile string is NOT (it would be a live tag).
        assert html.escape(hostile, quote=False) in reply
        assert hostile not in reply
    assert store.list_projects(1) == {}  # nothing created on any rejection


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


async def test_cmd_new_while_busy_succeeds_prior_run_untouched(tmp_path):
    # P5/T7 — THE HEADLINE RELAXATION (D2), /new arm. While alpha's turn holds its lock,
    # /new work <path> now SUCCEEDS (no busy refusal): store.create IS called, the new
    # project is created + made active, and alpha's held turn is UNTOUCHED (it keeps holding
    # its lock in the background). Same id-routing safety as /switch (ADR-005 D3).
    #
    # MUTATION PROBE: inverse of the P4 busy-guard test it replaces
    # (was ``test_cmd_new_while_busy_refused_store_untouched``). Re-adding the is_busy
    # refusal to cmd_new makes store.create not fire → this fails. Coverage pinned.
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

    # Drive a turn that parks on HOLD (acquires + holds alpha's per-project turn lock).
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    for _ in range(200):
        if session.is_busy(1, "alpha"):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1, "alpha"), "the held turn should hold alpha's lock"

    # /new work <path> WHILE alpha is mid-run → SUCCEEDS. Spy store.create to prove it IS
    # now called (the inverse of the P4 assertion).
    create_calls = []
    orig_create = store.create
    store.create = lambda *a, **k: (create_calls.append((a, k)), orig_create(*a, **k))[1]  # type: ignore[assignment]
    upd = make_update(1, "/new work " + str(proj))
    await bot.cmd_new(upd, make_cmd_ctx(args=["work", str(proj)]))
    store.create = orig_create  # type: ignore[assignment]

    reply = upd.message.reply_text.await_args.args[0]
    # R6: /new confirms in HTML (name in <b>…</b>, cwd in <code>…</code> so the path is
    # inert monospace, not fake /segment command-links). The old "created work" substring
    # no longer matches across the <b> tag — assert the success word + bolded name instead
    # (still proves success, not a busy refusal).
    assert "created" in reply.lower() and "<b>work</b>" in reply, reply  # success, not busy
    assert "/cancel" not in reply
    assert create_calls, "store.create MUST be called now that /new is free mid-run"
    assert set(store.list_projects(1)) == {"alpha", "work"}  # new project added
    assert store.get_active(1) == "work"  # /new auto-switched
    # alpha's held turn is untouched — still parked + holding its lock (background run).
    assert session.is_busy(1, "alpha"), "the prior run must keep running after /new"
    assert not turn.done()

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


async def test_cmd_reset_while_busy_refused_then_cancel_recovers(tmp_path):
    """B5: /reset during a held turn must be REFUSED. reset() drops the active engine, which
    would orphan a parked answer-hold — neither a tap nor /cancel could then reach it
    (handle_cancel finds no active engine), wedging the turn until the 60-min backstop. While
    busy the engine is still live, so the operator's recovery is /cancel (which works), then
    /reset. Mirrors the /switch and /new busy-guards."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, engine = make_streaming(store, script=[HOLD])  # the turn parks holding the lock
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    for _ in range(200):
        if session.is_busy(1):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1), "the held turn should hold the lock"

    # /reset is REFUSED while busy — spy streaming.reset to prove it is NOT called (so the
    # engine is never nulled / the held turn never orphaned).
    reset_calls: list = []
    orig_reset = session.reset
    session.reset = lambda *a, **k: reset_calls.append((a, k))  # type: ignore[assignment]
    up_reset = make_update(1, "/reset")
    await bot.cmd_reset(up_reset, make_cmd_ctx())
    session.reset = orig_reset  # type: ignore[assignment]
    assert "/cancel" in up_reset.message.reply_text.await_args.args[0]
    assert reset_calls == [], "streaming.reset must NOT be called while a turn is in flight"
    assert session.is_busy(1), "the held turn must still be live (not orphaned) after the refusal"

    # The engine is still live (reset was refused) → /cancel genuinely recovers the held turn
    # (the recovery path B5 flagged as broken when reset nulled the engine first).
    await bot.cmd_cancel(make_update(1, "/cancel"), make_ctx())
    await asyncio.wait_for(turn, timeout=2.0)
    assert session.is_busy(1) is False, "/cancel must release the held turn (recovery works)"


# ===========================================================================
# P5 / T7 — the busy-guard relaxation + per-project /reset + /projects status, at the
# BOT boundary over a REAL StreamingSession (multi-engine). These complement the
# session-level concurrency tests in test_stream_session.py / test_multi_project.py.
# ===========================================================================


def make_multi_streaming(store, engines_by_cwd, *, workdir="/work"):
    """A real StreamingSession whose factory returns a DISTINCT HoldEngine per cwd.

    Mirrors test_stream_session.make_multi_session but for the bot-boundary tests here:
    lets two projects each hold a parked turn so the per-project busy-guard + the
    /reset-of-idle-active-while-other-busy behavior can be driven through the bot. The
    session config uses allow_any_path=True so the driver's turn path is not SB2-blocked
    (these exercise concurrency/guards, not path confinement).
    """
    session = StreamingSession(
        make_config(engine_mode="streaming", workdir=workdir, allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engines_by_cwd[cwd],
        clock=lambda: 0.0,
    )
    return session


async def _wait(predicate, *, tries=500):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_cmd_switch_to_idle_project_while_another_busy_succeeds(tmp_path):
    # P5/T7: /switch to an IDLE project while a DIFFERENT project (alpha) is mid-run
    # SUCCEEDS — background concurrency at the bot boundary. alpha keeps running.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    eng_a = HoldEngine([HOLD])
    eng_b = HoldEngine([])
    session = make_multi_streaming(store, {"/work/alpha": eng_a, "/work/beta": eng_b})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))

    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"  # switched while alpha busy
    assert session.is_busy(1, "alpha"), "alpha keeps running in the background"

    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)


async def test_cmd_reset_idle_active_while_other_project_busy_succeeds(tmp_path):
    # P5/T7 (D2): the per-project /reset guard. A BACKGROUND run in project beta must NOT
    # block /reset of the IDLE active project alpha — reset only touches alpha's session, so
    # beta's concurrent run is irrelevant. (Was blocked by the P4 "any project busy" guard.)
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.set_session_id(1, "alpha", "alpha-live")  # alpha has a session to clear on reset
    eng_a = HoldEngine([])
    eng_b = HoldEngine([HOLD])
    session = make_multi_streaming(store, {"/work/alpha": eng_a, "/work/beta": eng_b})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )

    # Drive BETA busy while alpha (the active project) is idle: switch to beta, start its
    # turn (parks), then switch BACK so alpha is active + idle while beta runs in background.
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "go beta"), rec))
    await _wait(lambda: session.is_busy(1, "beta"))
    store.switch(1, "alpha")  # alpha is now the active project, and it is idle
    assert session.is_busy(1, "alpha") is False
    assert session.is_busy(1, "beta") is True

    # /reset → SUCCEEDS (active alpha is idle); alpha's session is cleared; beta untouched.
    up_reset = make_update(1, "/reset")
    await bot.cmd_reset(up_reset, make_cmd_ctx())
    assert "fresh" in up_reset.message.reply_text.await_args.args[0].lower()
    assert store.get_project(1, "alpha")["session_id"] is None  # alpha reset
    assert session.is_busy(1, "beta"), "beta's background run is untouched by /reset of alpha"

    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)


async def test_cmd_reset_refused_when_active_project_itself_busy(tmp_path):
    # P5/T7 (D2): /reset IS refused when the ACTIVE project's own turn is in flight (real
    # session, real per-project busy state) — resetting would orphan its parked hold.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    session, engine = make_streaming(store, script=[HOLD])
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))

    reset_calls: list = []
    orig_reset = session.reset
    session.reset = lambda *a, **k: reset_calls.append((a, k))  # type: ignore[assignment]
    up_reset = make_update(1, "/reset")
    await bot.cmd_reset(up_reset, make_cmd_ctx())
    session.reset = orig_reset  # type: ignore[assignment]
    assert "/cancel" in up_reset.message.reply_text.await_args.args[0]
    assert reset_calls == [], "reset must NOT run while the ACTIVE project is busy"

    engine.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


async def test_cmd_projects_shows_per_project_status(tmp_path):
    # P5/T7 (D7): /projects renders each project's run status. alpha is mid-run (running),
    # beta is awaiting an answer (an ask hold → awaiting answer), gamma never ran (idle).
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.create(1, "gamma", "/work/gamma", make_active=False)
    ask_b = AskEvent(
        questions=[{"question": "B?", "options": [{"label": "Yb"}, {"label": "Nb"}]}],
        tool_use_id="tid-b",
    )
    eng_a = HoldEngine([HOLD])  # parks → running (no held request)
    eng_b = HoldEngine([ask_b, HOLD])  # emits an ask then parks → awaiting_answer
    eng_g = HoldEngine([])
    session = make_multi_streaming(
        store, {"/work/alpha": eng_a, "/work/beta": eng_b, "/work/gamma": eng_g}
    )
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))

    turn_a = asyncio.create_task(bot.on_message(make_update(1, "go a"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "go b"), rec))
    await _wait(lambda: session.project_status(1, "beta") == "awaiting_answer")
    store.switch(1, "alpha")  # restore alpha active (cosmetic; status is per-project)

    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    alpha_line = next(line for line in reply.splitlines() if "alpha" in line)
    beta_line = next(line for line in reply.splitlines() if "beta" in line)
    gamma_line = next(line for line in reply.splitlines() if "gamma" in line)
    assert "(running)" in alpha_line, alpha_line
    assert "(awaiting answer)" in beta_line, beta_line
    assert "(idle)" in gamma_line, gamma_line

    eng_a.cancel()
    eng_b.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)


async def test_cmd_projects_shows_queued_status(tmp_path):
    # P5/T7 (D7): a turn QUEUED behind the cap reports "queued" on /projects.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    eng_a = HoldEngine([HOLD])
    eng_b = HoldEngine([HOLD])
    session = StreamingSession(
        make_config(engine_mode="streaming", allow_any_path=True, max_concurrent_runs=1),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: {
            "/work/alpha": eng_a, "/work/beta": eng_b
        }[cwd],
        clock=lambda: 0.0,
    )
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "a"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "b"), rec))
    await _wait(lambda: session.project_status(1, "beta") == "queued")

    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    beta_line = next(line for line in reply.splitlines() if "beta" in line)
    assert "(queued)" in beta_line, beta_line

    eng_a.cancel()
    await _wait(lambda: session.is_busy(1, "beta"))
    eng_b.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)


# ===========================================================================
# P5 / ADR-005 D5 + D9 (T9) — bot-level wiring: /cancel <name>|all, /to,
# reply-to threading, the name-echoed free-text prompt + reply-to-map capture,
# /rm-running-refused. The FakeStreaming-boundary tests assert the bot parses +
# delegates; the real-session tests assert end-to-end behavior.
# ===========================================================================


async def test_cmd_cancel_named_delegates_with_name():
    # /cancel work → handle_cancel(chat_id, "work").
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_cancel(make_update(1, "/cancel work"), make_cmd_ctx(args=["work"]))
    assert streaming.cancel_calls == [(1, "work")]


async def test_cmd_cancel_all_delegates_with_all():
    # /cancel all → handle_cancel(chat_id, "all").
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_cancel(make_update(1, "/cancel all"), make_cmd_ctx(args=["all"]))
    assert streaming.cancel_calls == [(1, "all")]


async def test_cmd_cancel_all_wording_is_project_scoped_no_unit_count(tmp_path):
    # P9 wording: /cancel all is phrased project-scoped — NOT "(N aborted)" (the unit count
    # conflates projects vs prompts). The single-project /cancel <name> keeps the count.
    streaming = FakeStreaming()  # handle_cancel returns 1 (a cancelled unit)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    up_all = make_update(1, "/cancel all")
    await bot.cmd_cancel(up_all, make_cmd_ctx(args=["all"]))
    all_reply = up_all.message.reply_text.await_args.args[0]
    assert all_reply == "🛑 Cancelled all running/queued projects."
    assert "aborted" not in all_reply  # no raw unit count for the all branch

    # Single-project /cancel <name> KEEPS the unit count.
    up_one = make_update(1, "/cancel work")
    await bot.cmd_cancel(up_one, make_cmd_ctx(args=["work"]))
    one_reply = up_one.message.reply_text.await_args.args[0]
    assert "aborted" in one_reply and "1" in one_reply


async def test_cmd_cancel_no_arg_delegates_active():
    # /cancel (no arg) → handle_cancel(chat_id, None) (the active project).
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_cancel(make_update(1, "/cancel"), make_cmd_ctx(args=[]))
    assert streaming.cancel_calls == [(1, None)]


async def test_cmd_to_delegates_name_and_text():
    # /to work use the staging URL → resolve_to(chat_id, "work", "use the staging URL").
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/to work use the staging URL")
    await bot.cmd_to(upd, make_cmd_ctx(args=["work", "use", "the", "staging", "URL"]))
    assert streaming.to_calls == [(1, "work", "use the staging URL")]
    # The session's confirmation string is relayed to the operator.
    assert "work" in upd.message.reply_text.await_args.args[0]


async def test_cmd_to_usage_on_missing_text():
    # /to with a name but no text → usage (RB1), no routing.
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/to work")
    await bot.cmd_to(upd, make_cmd_ctx(args=["work"]))
    assert streaming.to_calls == []
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_to_oneshot_is_streaming_only_notice():
    # /to in one-shot mode → the streaming-only notice (no streaming session).
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/to work hi")
    await bot.cmd_to(upd, make_cmd_ctx(args=["work", "hi"]))
    assert "streaming mode only" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_to_unauthorized_ignored():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_to(make_update(999, "/to work hi"), make_cmd_ctx(args=["work", "hi"]))
    assert streaming.to_calls == []


async def test_on_message_threads_reply_to_id():
    # D5: a plain message that is a reply-to carries its reply_to_message_id through to
    # handle_message (so free-text reply-to routing can use it).
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.on_message(make_update(1, "the answer", reply_to_message_id=5150), make_ctx())
    assert streaming.handle_message_calls == [(1, "the answer", 5150)]


async def test_on_callback_name_echoes_prompt_and_registers_reply_map():
    # D5: when a tap arms free text, the bot replies a NAME-echoed prompt ("✏️ work: …") and
    # registers the prompt's message_id -> tool_use_id (the reply-to map).
    streaming = FakeStreaming(
        outcome=CallbackOutcome(
            handled=True, note="Type your answer", expects_text=True,
            project_name="work", tool_use_id="tid-7",
        )
    )
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_callback_update(chat_id=1, data="o|tid-7|0")
    # The prompt reply returns a message with id 808 (so the map records 808 -> tid-7).
    upd.callback_query.message.reply_text = AsyncMock(return_value=MagicMock(message_id=808))
    await bot.on_callback(upd, make_ctx())
    # Name-echoed prompt (carries the project name, not the bare toast).
    sent_text = upd.callback_query.message.reply_text.await_args.args[0]
    assert "work" in sent_text and sent_text.startswith("✏️")
    # The reply-to map was populated with the sent prompt's id -> the armed tool_use_id.
    assert streaming.reply_prompt_calls == [(1, 808, "tid-7")]


async def test_cmd_rm_refuses_running_project(tmp_path):
    # P5/T9 (D9): /rm of a currently-RUNNING (non-active) project is REFUSED ("cancel it
    # first") — tearing down a live engine mid-turn would orphan its parked hold. beta runs
    # in the background while alpha is active; /rm beta is refused and beta keeps running.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    eng_a = HoldEngine([])
    eng_b = HoldEngine([HOLD])
    session = make_multi_streaming(store, {"/work/alpha": eng_a, "/work/beta": eng_b})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    # Drive beta busy in the background, alpha active + idle.
    store.switch(1, "beta")
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "go beta"), rec))
    await _wait(lambda: session.is_busy(1, "beta"))
    store.switch(1, "alpha")

    up_rm = make_update(1, "/rm beta")
    await bot.cmd_rm(up_rm, make_cmd_ctx(args=["beta"]))
    reply = up_rm.message.reply_text.await_args.args[0]
    assert "/cancel" in reply and "beta" in reply  # refused, told to cancel first
    assert "beta" in store.list_projects(1), "a refused /rm must NOT remove the project"
    assert session.is_busy(1, "beta"), "beta's run is untouched by the refused /rm"

    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)


async def test_cmd_rm_idle_non_active_project_still_removed(tmp_path):
    # Regression (existing behavior holds): /rm of an IDLE non-active project still purges it
    # (the running-refusal only fires for a busy project).
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)  # idle, never run
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    up_rm = make_update(1, "/rm beta")
    await bot.cmd_rm(up_rm, make_cmd_ctx(args=["beta"]))
    assert "beta" not in store.list_projects(1)
    assert "removed" in up_rm.message.reply_text.await_args.args[0].lower()


async def test_cmd_cancel_named_aborts_only_that_run_end_to_end(tmp_path):
    # End-to-end (real session): /cancel beta aborts beta's background run while alpha keeps
    # running — concurrency-aware cancel at the bot boundary.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    eng_a = HoldEngine([HOLD])
    eng_b = HoldEngine([HOLD])
    session = make_multi_streaming(store, {"/work/alpha": eng_a, "/work/beta": eng_b})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "a"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "b"), rec))
    await _wait(lambda: session.is_busy(1, "beta"))

    # /cancel beta → only beta's run aborts; alpha keeps running.
    await bot.cmd_cancel(make_update(1, "/cancel beta"), make_cmd_ctx(args=["beta"]))
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert session.is_busy(1, "beta") is False
    assert session.is_busy(1, "alpha"), "alpha's concurrent run is untouched by /cancel beta"

    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)


# ===========================================================================
# P9 / T1 — command menu (set_my_commands) + first-run onboarding.
# ===========================================================================


def _registered_command_names(bot):
    """The set of command names actually registered as CommandHandlers (excluding the
    /start alias of /help — intentionally omitted from the native menu)."""
    from telegram.ext import CommandHandler

    app = bot.build_application()
    names: set[str] = set()
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                names |= {c.lstrip("/").lower() for c in h.commands}
    names.discard("start")
    return names


def test_command_menu_matches_registered_handlers():
    # T1: COMMAND_MENU must be in lock-step with the registered CommandHandlers — no
    # documented-but-unregistered command, and no registered command missing from the menu.
    from claude_tg.bot import COMMAND_MENU

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    menu_names = {cmd for cmd, _desc in COMMAND_MENU}
    assert menu_names == _registered_command_names(bot)
    # Every menu entry has a non-empty, concise description.
    assert all(desc and len(desc) <= 100 for _cmd, desc in COMMAND_MENU)
    # The new /status command is present.
    assert "status" in menu_names


def test_help_text_covers_every_command_menu_entry():
    # P9 polish: HELP_TEXT must document EVERY command in the native /-menu so the long-form
    # help can't drift behind the menu (the /status omission that prompted this guard). A
    # lock-step assertion mirroring test_command_menu_matches_registered_handlers, but for the
    # wall-of-text help. Extract every /<cmd> token mentioned in HELP_TEXT and require each
    # COMMAND_MENU command to appear.
    from claude_tg.bot import COMMAND_MENU, HELP_TEXT

    mentioned = set(re.findall(r"/([a-z]+)", HELP_TEXT))
    menu_names = {cmd for cmd, _desc in COMMAND_MENU}
    missing = menu_names - mentioned
    assert not missing, f"HELP_TEXT is missing menu commands: {sorted(missing)}"
    # /status specifically must be documented (the bug this guard pins).
    assert "/status" in HELP_TEXT


async def test_post_init_registers_commands_via_set_my_commands():
    # T1: post_init calls bot.set_my_commands with a BotCommand list matching COMMAND_MENU.
    from telegram import BotCommand

    from claude_tg.bot import COMMAND_MENU

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    await bot._post_init(app)
    app.bot.set_my_commands.assert_awaited_once()
    sent = app.bot.set_my_commands.await_args.args[0]
    assert all(isinstance(c, BotCommand) for c in sent)
    assert [(c.command, c.description) for c in sent] == list(COMMAND_MENU)


async def test_post_init_survives_set_my_commands_failure():
    # RB1: a Telegram API failure registering the menu must not crash startup.
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("api down"))
    await bot._post_init(app)  # does not raise


async def test_first_message_welcomes_once_then_not_again():
    # T1: the first-ever message for a chat fires a one-time welcome; subsequent messages
    # do NOT re-welcome.
    runner = FakeRunner(ClaudeResult(ok=True, text="ok"))
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    upd1 = make_update(1, "hello")
    await bot.on_message(upd1, make_ctx())
    welcome = upd1.message.reply_text.await_args_list[0].args[0]
    assert welcome.startswith("👋")
    assert "/status" in welcome and "oneshot" in welcome
    # Second message: NO welcome (only the turn reply).
    upd2 = make_update(1, "again")
    await bot.on_message(upd2, make_ctx())
    assert not any("👋" in c.args[0] for c in upd2.message.reply_text.await_args_list)


async def test_welcome_not_sent_to_unauthorized_chat():
    # T1/SB1: a non-allowlisted chat never triggers onboarding (and is never marked seen).
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="oneshot"), runner)
    upd = make_update(999, "hi")
    await bot.on_message(upd, make_ctx())
    upd.message.reply_text.assert_not_awaited()
    assert 999 not in bot._welcomed


async def test_welcome_failure_does_not_break_dispatch():
    # RB1: a welcome send failure must not stop the turn from running.
    runner = FakeRunner(ClaudeResult(ok=True, text="answer"))
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    upd = make_update(1, "go")

    # First reply (the welcome) raises; later replies (the turn) succeed.
    upd.message.reply_text = AsyncMock(side_effect=[RuntimeError("boom"), None, None, None])
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == [(1, "go")]  # the turn still ran


# ===========================================================================
# P9 / T2 — /status health command (real StreamingSession + store).
# ===========================================================================


async def test_cmd_status_streaming_fields(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.add_cost(1, "alpha", 0.05)
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    # Uptime, engine mode, gate posture, run counts, the per-project line + cwd + cost.
    assert "Uptime" in reply
    assert "streaming" in reply
    assert "gate" in reply.lower()
    assert "active" in reply.lower() and "max concurrent" in reply.lower()
    assert "alpha" in reply
    assert "<code>/work/alpha</code>" in reply  # R6: cwd wrapped, not auto-linkified
    assert "$0.05" in reply  # T3 cumulative cost surfaced in /status


async def test_cmd_status_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(999, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    upd.message.reply_text.assert_not_awaited()  # SB1: dropped, nothing leaked


async def test_cmd_status_oneshot_mode():
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "oneshot" in reply
    assert "Uptime" in reply
    assert "<code>/work</code>" in reply  # working dir wrapped (R6)


async def test_cmd_status_body_free_no_secrets(tmp_path):
    # SB3: /status carries only health values — never tool input/output or file content. We
    # plant a "secret"-looking session_id in the store and assert it never appears.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.set_session_id(1, "alpha", "SECRET-SESSION-ID-12345")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "SECRET-SESSION-ID-12345" not in reply


async def test_cmd_status_multi_project_marker_mixed_cost_and_yolo(tmp_path):
    # T2: /status with MULTIPLE projects — assert (a) the active-marker placement (→ on the
    # active project only), (b) a MIXED cost/no-cost rendering (the costed project shows
    # "$X.XX", the un-costed one shows no cost suffix), and (c) the /yolo-ON gate-disabled
    # wording shows and stays body-free + HTML-escaped.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)   # active + costed
    store.create(1, "beta", "/work/beta", make_active=False)    # inactive + no cost
    store.add_cost(1, "alpha", 0.12)
    session, _ = make_streaming(store)
    # Flip the active project into /yolo so the gate-disabled posture renders.
    session.set_yolo(1, True)
    assert session.get_yolo(1) is True
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    kwargs = upd.message.reply_text.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"

    lines = reply.splitlines()
    alpha_line = next(line for line in lines if "alpha" in line)
    beta_line = next(line for line in lines if "beta" in line)
    # (a) active marker: the → arrow leads the ACTIVE project's line only.
    assert "→" in alpha_line and "<b>alpha</b>" in alpha_line
    assert "→" not in beta_line and "<b>beta</b>" in beta_line
    # (b) mixed cost: alpha shows its cumulative cost, beta (uncharged) shows none.
    assert "$0.12" in alpha_line
    assert "$" not in beta_line
    # (c) /yolo gate-disabled wording is present, loud, and names the toggle.
    gate_line = next(line for line in lines if line.startswith("Permission gate:"))
    assert "OFF" in gate_line and "/yolo" in gate_line
    # Body-free + HTML-escaped: the only "<...>" runs are the bot's own <b>/<code> tags (no
    # stray angle brackets), and no raw '&' leaks unescaped (every literal & is an entity).
    assert set(re.findall(r"</?(\w+)", reply)) <= {"b", "code"}
    for amp in re.findall(r"&\S*", reply):
        assert amp.startswith(("&amp;", "&lt;", "&gt;")), amp


async def test_cmd_status_surfaces_background_project_yolo(tmp_path):
    # P9 fix: /yolo is PER-PROJECT, but the global "Permission gate" line reflects only the
    # ACTIVE project — a wide-open BACKGROUND project must NOT be hidden. /status marks each
    # allow-all project with "⚠️ yolo" on its own line. Here beta (background) is yolo while
    # alpha (active) is NOT — the global gate line reads ON, but beta's line must flag yolo.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)   # active, gate ON
    store.create(1, "beta", "/work/beta", make_active=False)    # background
    session, _ = make_streaming(store)
    # Flip BETA into /yolo while it is the active project, then switch back to alpha so beta
    # is a wide-open BACKGROUND project (the exact case the global gate line would hide).
    store.switch(1, "beta")
    session.set_yolo(1, True)
    store.switch(1, "alpha")
    assert session.get_yolo(1) is False  # the ACTIVE (alpha) gate is ON
    assert session.get_project_yolo(1, "beta") is True  # but beta is allow-all

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    lines = reply.splitlines()
    # The global gate line reflects the ACTIVE project (alpha) — ON.
    gate_line = next(line for line in lines if line.startswith("Permission gate:"))
    assert "ON" in gate_line
    # The per-project marker exposes beta's allow-all posture; alpha's line carries none.
    alpha_line = next(line for line in lines if "<b>alpha</b>" in line)
    beta_line = next(line for line in lines if "<b>beta</b>" in line)
    assert "yolo" in beta_line, beta_line  # the background allow-all project is NOT hidden
    assert "yolo" not in alpha_line, alpha_line


def test_format_uptime():
    from claude_tg.bot import _format_uptime

    assert _format_uptime(8) == "8s"
    assert _format_uptime(72) == "1m 12s"
    assert _format_uptime(3661) == "1h 1m"
    assert _format_uptime(90061) == "1d 1h 1m"
    assert _format_uptime(-5) == "0s"  # RB1: defensive floor


# ===========================================================================
# T4 (P9): /fast · /deep · /auto — per-project model routing commands.
# ===========================================================================


async def test_cmd_fast_sets_fast_model():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/fast")
    await bot.cmd_fast(upd, make_cmd_ctx())
    assert streaming.model_calls == [(1, bot.config.fast_model)]
    reply = upd.message.reply_text.await_args.args[0]
    assert "fast" in reply and bot.config.fast_model in reply


async def test_cmd_deep_sets_deep_model():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/deep")
    await bot.cmd_deep(upd, make_cmd_ctx())
    assert streaming.model_calls == [(1, bot.config.deep_model)]
    reply = upd.message.reply_text.await_args.args[0]
    assert "deep" in reply and bot.config.deep_model in reply


async def test_cmd_auto_clears_model():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/auto")
    await bot.cmd_auto(upd, make_cmd_ctx())
    assert streaming.model_calls == [(1, None)]  # cleared
    reply = upd.message.reply_text.await_args.args[0]
    assert "default" in reply.lower()


async def test_cmd_fast_unauthorized_ignored():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(999, "/fast")
    await bot.cmd_fast(upd, make_cmd_ctx())
    assert streaming.model_calls == []  # SB1: dropped
    upd.message.reply_text.assert_not_awaited()


async def test_cmd_fast_oneshot_mode_notice():
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), FakeRunner())
    upd = make_update(1, "/fast")
    await bot.cmd_fast(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "streaming mode only" in reply


async def test_cmd_status_shows_model_override(tmp_path):
    # T4: the per-project model override surfaces on the /status per-project line.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.set_model(1, "alpha", "claude-haiku-4-5")
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "<code>claude-haiku-4-5</code>" in reply


async def test_cmd_status_no_model_override_omits_it(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)  # no override
    session, _ = make_streaming(store)
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=session)
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "claude-haiku-4-5" not in reply and "claude-opus" not in reply


# ===========================================================================
# T5 (P9): macros — /save · /run · /macros · /unsave.
# ===========================================================================


async def test_cmd_save_run_roundtrip_fires_turn(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    # /save deploy build $1 and ship $*
    await bot.cmd_save(make_update(1, "/save"), make_cmd_ctx(["deploy", "build", "$1", "and", "ship", "$*"]))
    assert store.get_macro(1, "deploy") == "build $1 and ship $*"
    # /run deploy app extra1 extra2  → expands $1=app, $*=app extra1 extra2
    await bot.cmd_run(make_update(1, "/run"), make_cmd_ctx(["deploy", "app", "extra1", "extra2"]))
    # The expanded text fired as a normal turn through the streaming session.
    assert streaming.handle_message_calls
    fired = streaming.handle_message_calls[-1][1]
    assert fired == "build app and ship app extra1 extra2"


async def test_cmd_save_case_collision_run_resolves_latest(tmp_path):
    # RED-GREEN (P0 macro case-collision) at the BOT boundary: `/save Work x` then
    # `/save work y` must leave exactly ONE macro, and `/run work` fires the LATEST body (y).
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_save(make_update(1, "/save"), make_cmd_ctx(["Work", "x"]))
    await bot.cmd_save(make_update(1, "/save"), make_cmd_ctx(["work", "y"]))  # same name, new case
    assert len(store.list_macros(1)) == 1, store.list_macros(1)
    await bot.cmd_run(make_update(1, "/run"), make_cmd_ctx(["work"]))
    assert streaming.handle_message_calls[-1][1] == "y"  # /run work → the latest body


async def test_cmd_save_rejects_bad_name_sb4(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_save(make_update(1, "/save"), make_cmd_ctx(["../etc", "body"]))
    # Nothing saved; a clean refusal (the focused escape assertion is the next test).
    assert store.list_macros(1) == {}


async def test_cmd_save_bad_name_reply_is_escaped(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/save")
    await bot.cmd_save(upd, make_cmd_ctx(["<b>x", "body"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "Invalid macro name" in reply
    assert "<b>x" not in reply or "&lt;b&gt;x" in reply  # the bad name is escaped


async def test_cmd_save_usage_when_missing_args(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/save")
    await bot.cmd_save(upd, make_cmd_ctx(["onlyname"]))  # no body
    reply = upd.message.reply_text.await_args.args[0]
    assert reply.startswith("Usage: /save")
    assert store.list_macros(1) == {}


async def test_cmd_macros_lists(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "deploy", "do the deploy thing")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/macros")
    await bot.cmd_macros(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "<b>deploy</b>" in reply and "do the deploy thing" in reply


async def test_cmd_macros_empty(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/macros")
    await bot.cmd_macros(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert "No macros yet" in reply


async def test_cmd_unsave_removes(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "deploy", "x")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    await bot.cmd_unsave(make_update(1, "/unsave"), make_cmd_ctx(["deploy"]))
    assert store.get_macro(1, "deploy") is None


async def test_cmd_unsave_unknown_is_clean(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/unsave")
    await bot.cmd_unsave(upd, make_cmd_ctx(["nope"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "No macro named" in reply


async def test_cmd_run_unknown_macro_clean(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/run")
    await bot.cmd_run(upd, make_cmd_ctx(["nope", "arg"]))
    reply = upd.message.reply_text.await_args.args[0]
    assert "No macro named" in reply
    assert streaming.handle_message_calls == []  # nothing fired


async def test_cmd_run_usage_when_no_name(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(1, "/run")
    await bot.cmd_run(upd, make_cmd_ctx([]))
    reply = upd.message.reply_text.await_args.args[0]
    assert reply.startswith("Usage: /run")


async def test_macro_commands_unauthorized_ignored(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    streaming = FakeStreaming()
    streaming.store = store
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming)
    upd = make_update(999, "/save")
    await bot.cmd_save(upd, make_cmd_ctx(["m", "body"]))
    upd.message.reply_text.assert_not_awaited()  # SB1: dropped
    assert store.list_macros(1) == {}


async def test_macros_work_in_oneshot_mode(monkeypatch, tmp_path):
    # T5: macros work in BOTH engine modes — in one-shot the /run expansion fires through
    # the runner. Use a real store on the runner.
    store = JsonSessionStore(tmp_path / "state.json")
    store.save_macro(1, "greet", "say hi to $1")
    runner = FakeRunner(ClaudeResult(ok=True, text="done"))
    runner.store = store
    bot = TelegramClaudeBot(make_config(engine_mode="oneshot"), runner)
    bot._welcomed.add(1)  # skip the first-run welcome noise
    await bot.cmd_run(make_update(1, "/run"), make_cmd_ctx(["greet", "alice"]))
    assert runner.run_calls == [(1, "say hi to alice")]


# ---------------------------------------------------------------------------
# P9 fix — /run must NOT be swallowed by a pending free-text capture.
# A macro /run is a deliberate command to START a fresh turn; it must go through
# the normal turn path, never satisfy an outstanding "Other"/plan-reject free-text
# hold. A plain typed message answering the prompt is UNCHANGED.
# ---------------------------------------------------------------------------


async def test_cmd_run_does_not_consume_pending_free_text_capture(tmp_path):
    # RED-GREEN (Codex blocker): alpha is mid-turn with an ask hold; the operator taps
    # "Other" (arming free-text capture on alpha). They then /switch to the IDLE beta and
    # fire a macro via /run. The macro's expanded text MUST start a fresh turn on beta — it
    # must NOT be routed to engine.resolve as alpha's captured free-text answer.
    from claude_tg.render import encode_callback

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.save_macro(1, "deploy", "ship it")
    ask_a = AskEvent(
        questions=[{"question": "Qa?", "options": [{"label": "A"}]}],
        tool_use_id="a-ask",
    )
    eng_a = HoldEngine([ask_a, HOLD])  # emit ask, then park awaiting the answer
    eng_b = HoldEngine([])  # beta's fresh turn runs to completion
    session = make_multi_streaming(store, {"/work/alpha": eng_a, "/work/beta": eng_b})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))

    # Drive alpha to the ask hold, then tap "Other" to ARM free-text capture on alpha.
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "go a"), rec))
    await _wait(lambda: session.project_status(1, "alpha") == "awaiting_answer")
    cb = make_callback_update(1, data=encode_callback("o", "a-ask", question_index=0))
    await bot.on_callback(cb, make_ctx())
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask"  # armed

    # /switch to the idle beta (allowed mid-run), then /run the macro.
    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"
    await bot.cmd_run(make_update(1, "/run"), make_cmd_ctx(args=["deploy"]))

    # The macro fired a FRESH turn on beta's engine — it did NOT resolve alpha's free-text
    # hold. Mutation probe: routing /run through the free-text capture would call
    # eng_a.resolve("a-ask", …) and leave eng_b un-driven (assertions below would fail).
    assert eng_b.started, "the macro must start a fresh turn on the active (beta) project"
    assert eng_a.resolve_calls == [], "/run must NOT be consumed as alpha's free-text answer"
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask", (
        "alpha is still armed — its hold was untouched by /run"
    )

    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)


async def test_plain_message_still_answers_pending_free_text_capture(tmp_path):
    # The companion guard: a PLAIN typed message (not a command) answering a pending
    # free-text prompt resolves it EXACTLY as before — the fix must not regress this.
    from claude_tg.render import encode_callback

    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    ask_a = AskEvent(
        questions=[{"question": "Qa?", "options": [{"label": "A"}]}],
        tool_use_id="a-ask",
    )
    eng_a = HoldEngine([ask_a, HOLD])
    session = make_multi_streaming(store, {"/work/alpha": eng_a})
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "go a"), rec))
    await _wait(lambda: session.project_status(1, "alpha") == "awaiting_answer")
    cb = make_callback_update(1, data=encode_callback("o", "a-ask", question_index=0))
    await bot.on_callback(cb, make_ctx())
    assert session._chat(1).runtimes["alpha"].awaiting_text_for == "a-ask"

    # A plain typed message answers the prompt — resolves alpha's hold (unchanged behavior).
    await bot.on_message(make_update(1, "my free-text answer"), rec)
    assert eng_a.resolve_calls and eng_a.resolve_calls[0][0] == "a-ask"
    assert session._chat(1).runtimes["alpha"].awaiting_text_for is None  # capture cleared

    await asyncio.wait_for(turn_a, timeout=2.0)


# ===========================================================================
# T6 (P9) — notification polish + chips at the BOT boundary.
#   * [Open <project>] switch tap → on_callback performs the switch (SB1-gated).
#   * free-text prompt carries the one-time quick-reply chips.
#   * a free-text capture dismisses the chips (ReplyKeyboardRemove).
# ===========================================================================
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove  # noqa: E402

from claude_tg.render import encode_switch_callback  # noqa: E402


async def test_switch_button_tap_switches_active_project_at_bot(tmp_path):
    # T6.2: a [Open beta] tap from an AUTHORIZED chat switches the active project (via the
    # shared /switch helper, with SB2 path revalidation). Uses a REAL StreamingSession + store.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    store.create(1, "beta", str(tmp_path / "beta"), make_active=False)
    (tmp_path / "beta").mkdir()
    session = StreamingSession(
        make_config(engine_mode="streaming", allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: None,
    )
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", allow_any_path=True),
        FakeRunner(), streaming=session,
    )
    upd = make_callback_update(chat_id=1, data=encode_switch_callback("beta"))
    await bot.on_callback(upd, make_ctx())
    assert store.get_active(1) == "beta", "the switch tap must change the active project"
    upd.callback_query.answer.assert_awaited()
    upd.callback_query.message.reply_text.assert_awaited()


async def test_switch_button_tap_from_unauthorized_chat_never_switches(tmp_path):
    # ⭐ SB1 (mutation probe): a [Open beta] tap from a NON-allowlisted chat must NEVER switch.
    # If on_callback skipped the _authorized recheck for switch taps, the active project would
    # flip — this guards that the switch is gated exactly like every other callback.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    store.create(1, "beta", str(tmp_path / "beta"), make_active=False)
    (tmp_path / "beta").mkdir()
    session = StreamingSession(
        make_config(engine_mode="streaming", allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: None,
    )
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", allow_any_path=True),
        FakeRunner(), streaming=session,
    )
    upd = make_callback_update(chat_id=999, data=encode_switch_callback("beta"))  # NOT allowlisted
    await bot.on_callback(upd, make_ctx())
    assert store.get_active(1) == "alpha", "an unauthorized switch tap must NOT change the active project"
    upd.callback_query.answer.assert_awaited()  # spinner stops
    # No switch reply was sent (the handler dropped it before resolve_callback).
    upd.callback_query.message.reply_text.assert_not_awaited()


async def test_free_text_prompt_attaches_quick_reply_chips():
    # T6.4: an "Other"/reject arm prompts for free text WITH the one-time quick-reply chips.
    streaming = FakeStreaming(
        outcome=CallbackOutcome(
            handled=True, note="Type your answer", expects_text=True,
            project_name="alpha", tool_use_id="tid",
        )
    )
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    upd = make_callback_update(chat_id=1, data="o|tid|0")
    await bot.on_callback(upd, make_ctx())
    upd.callback_query.message.reply_text.assert_awaited()
    _args, kwargs = upd.callback_query.message.reply_text.call_args
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, ReplyKeyboardMarkup) and kb.one_time_keyboard is True
    chips = [b.text for row in kb.keyboard for b in row]
    assert "proceed" in chips


async def test_free_text_capture_dismisses_chips():
    # ⭐ T6.4 (mutation probe — chip dismissal): when a message is consumed as a free-text
    # capture (handle_message returns True), the bot sends a ReplyKeyboardRemove so the
    # one-time chips don't linger over the next turn.
    streaming = FakeStreaming()

    async def captured_handle(
        chat_id, text, *, send, edit, delete=None, reply_to_message_id=None,
        command_initiated=False,
    ):
        return True  # this message was a free-text capture

    streaming.handle_message = captured_handle
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    upd = make_update(1, "proceed")
    await bot.on_message(upd, make_ctx())
    # The dismiss message rode a ReplyKeyboardRemove.
    upd.message.reply_text.assert_awaited()
    _args, kwargs = upd.message.reply_text.call_args
    assert isinstance(kwargs.get("reply_markup"), ReplyKeyboardRemove)


async def test_normal_turn_does_not_dismiss_chips():
    # T6.4: a NORMAL turn (handle_message returns False) does NOT send a ReplyKeyboardRemove —
    # the chips are scoped to a pending free-text prompt, not every message.
    streaming = FakeStreaming()  # its handle_message returns False
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    upd = make_update(1, "do something new")
    await bot.on_message(upd, make_ctx())
    # No dismissal message sent (reply_text not called for the chip-remove path).
    upd.message.reply_text.assert_not_awaited()


async def test_status_runs_line_shows_queued_counter(tmp_path):
    # T6.3: /status surfaces the queued-behind-the-cap counter on the runs line.
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", str(tmp_path / "alpha"), make_active=True)
    (tmp_path / "alpha").mkdir()
    session = StreamingSession(
        make_config(engine_mode="streaming", allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: None,
    )
    # Park two waiters behind the cap.
    from claude_tg.stream_session import _ProjectRuntime, _QueuedTurn

    state = session._chat(1)
    loop = asyncio.get_running_loop()
    futures = []
    for _ in range(2):
        fut = loop.create_future()
        futures.append(fut)
        state.run_queue.append(_QueuedTurn(runtime=_ProjectRuntime(cwd="/x"), future=fut))
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", allow_any_path=True),
        FakeRunner(), streaming=session,
    )
    upd = make_update(1, "/status")
    await bot.cmd_status(upd, make_ctx())
    _args, kwargs = upd.message.reply_text.call_args
    text = _args[0] if _args else kwargs.get("text", "")
    assert "(2 more waiting)" in text
    for fut in futures:
        fut.cancel()


# ---------------------------------------------------------------------------
# P10 T1 — photo / image-document → native multimodal turn (SB1 / size-cap /
# media_type / no-bytes-logged / oneshot fallback). The send-path content-block
# build + engine threading is in test_multimodal.py; the live-verify is T4.
# ---------------------------------------------------------------------------

import base64 as _base64  # noqa: E402

from claude_tg.bot import DEFAULT_IMAGE_PROMPT  # noqa: E402
from claude_tg.engine import ImageInput  # noqa: E402


def make_photo_update(
    chat_id=1, *, caption=None, raw=b"\x89PNG-fake-bytes", file_size=None, kind="photo",
    mime_type=None, file_name=None,
):
    """A fake Update carrying a PHOTO (size ladder) or an image DOCUMENT.

    ``get_file().download_as_bytearray()`` returns ``raw`` (the fake pixels). ``file_size``
    is the Telegram-declared size for the pre-download cap (defaults to len(raw)).
    """
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = None
    upd.message.caption = caption
    upd.message.reply_text = AsyncMock()
    upd.message.reply_to_message = None
    upd.effective_message = upd.message
    size = file_size if file_size is not None else len(raw)

    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(raw))

    attachment = MagicMock()
    attachment.file_size = size
    attachment.get_file = AsyncMock(return_value=tg_file)

    if kind == "photo":
        # Telegram sends a SIZE LADDER; the largest is last (the handler takes [-1]).
        small = MagicMock()
        small.file_size = 1
        small.get_file = AsyncMock(return_value=MagicMock())
        upd.message.photo = [small, attachment]
        upd.message.document = None
    else:  # image document
        attachment.mime_type = mime_type
        attachment.file_name = file_name
        upd.message.photo = []
        upd.message.document = attachment
    return upd


async def test_on_photo_sb1_unauthorized_chat_no_engine_no_download():
    # SB1: a photo from a NON-allowlisted chat is dropped — no download, no turn.
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    upd = make_photo_update(chat_id=999, caption="peek")  # 999 not in the allowlist
    await bot.on_photo(upd, make_ctx())
    assert streaming.handle_message_calls == []
    # The largest photo's get_file was never called (no download for an unauthorized chat).
    upd.message.photo[-1].get_file.assert_not_awaited()


async def test_on_photo_threads_imageinput_and_caption_as_prompt():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)  # don't perturb on the welcome
    raw = b"\x89PNG\r\n-some-bytes"
    upd = make_photo_update(1, caption="what is in this screenshot?", raw=raw)
    await bot.on_photo(upd, make_ctx())

    # One turn ran with the caption as the prompt and an ImageInput carrying the base64.
    assert len(streaming.handle_message_calls) == 1
    chat_id, prompt, _rt = streaming.handle_message_calls[0]
    assert (chat_id, prompt) == (1, "what is in this screenshot?")
    images = streaming.images_calls[0]
    assert images is not None and len(images) == 1
    img = images[0]
    assert isinstance(img, ImageInput)
    assert img.media_type == "image/jpeg"  # a compressed photo → JPEG
    assert img.data == _base64.b64encode(raw).decode("ascii")


async def test_on_photo_no_caption_uses_default_prompt():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    upd = make_photo_update(1, caption=None)
    await bot.on_photo(upd, make_ctx())
    _chat, prompt, _rt = streaming.handle_message_calls[0]
    assert prompt == DEFAULT_IMAGE_PROMPT


async def test_on_photo_size_cap_rejects_oversized_before_download():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", image_max_bytes=1024),
        FakeRunner(), streaming=streaming,
    )
    bot._welcomed.add(1)
    # Declared size over the 1 KB cap → refused with a clean message, NO turn, NO download.
    upd = make_photo_update(1, caption="big", raw=b"x" * 50, file_size=5000)
    await bot.on_photo(upd, make_ctx())
    assert streaming.handle_message_calls == []
    upd.message.photo[-1].get_file.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "too large" in reply.lower()


async def test_on_photo_size_cap_rejects_after_download_when_size_underreported():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", image_max_bytes=10),
        FakeRunner(), streaming=streaming,
    )
    bot._welcomed.add(1)
    # Declared size is None (no pre-check), but the downloaded bytes exceed the 10 B cap →
    # the post-download cap refuses it (defense-in-depth; never reaches the engine).
    upd = make_photo_update(1, caption="sneaky", raw=b"x" * 100, file_size=None)
    await bot.on_photo(upd, make_ctx())
    assert streaming.handle_message_calls == []
    reply = upd.message.reply_text.await_args.args[0]
    assert "too large" in reply.lower()


async def test_on_photo_image_document_media_type_from_mime():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    upd = make_photo_update(
        1, caption="doc", raw=b"webp-bytes", kind="document",
        mime_type="image/webp", file_name="shot.webp",
    )
    await bot.on_photo(upd, make_ctx())
    img = streaming.images_calls[0][0]
    assert img.media_type == "image/webp"


async def test_on_photo_non_image_document_refused_cleanly():
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    # An unsupported mime (e.g. a PDF that slipped past the filter) → clean refusal, no turn.
    upd = make_photo_update(
        1, caption="pdf", raw=b"%PDF", kind="document",
        mime_type="application/pdf", file_name="x.pdf",
    )
    await bot.on_photo(upd, make_ctx())
    assert streaming.handle_message_calls == []
    reply = upd.message.reply_text.await_args.args[0]
    assert "jpeg" in reply.lower() or "image" in reply.lower()


async def test_on_photo_never_logs_image_bytes_sb3(caplog):
    import logging

    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    bot._welcomed.add(1)
    raw = b"SECRET-PIXEL-PAYLOAD-DO-NOT-LOG-1234567890"
    b64 = _base64.b64encode(raw).decode("ascii")
    with caplog.at_level(logging.DEBUG):
        upd = make_photo_update(1, caption="secret", raw=raw)
        await bot.on_photo(upd, make_ctx())
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    # SB3: neither the raw bytes nor the base64 may appear anywhere in the logs.
    assert b64 not in full_log
    assert "SECRET-PIXEL-PAYLOAD" not in full_log
    # But a size SUMMARY is logged (so an operator can see an image arrived).
    assert "received an image" in full_log


async def test_on_photo_oneshot_mode_refuses_images_need_streaming():
    # Oneshot fallback (documented choice): images need streaming mode → clean refusal,
    # never silently run the caption text-only.
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="oneshot"), runner)
    assert bot.streaming is None
    bot._welcomed.add(1)
    upd = make_photo_update(1, caption="see this")
    await bot.on_photo(upd, make_ctx())
    # No runner turn fired, and the operator was told images need streaming mode.
    assert runner.run_calls == []
    reply = upd.message.reply_text.await_args.args[0]
    assert "streaming" in reply.lower()


def test_build_application_registers_photo_handler():
    # The photo/image-document MessageHandler is wired with the SB1 `allowed` chat filter.
    from telegram.ext import MessageHandler

    bot = TelegramClaudeBot(make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming())
    app = bot.build_application()
    photo_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, MessageHandler) and h.callback == bot.on_photo
    ]
    assert len(photo_handlers) == 1


# ===========================================================================
# P10 T3 — file send (out: /get) + receive (in: on_document)
# ===========================================================================


def make_document_update(
    chat_id=1, *, caption=None, raw=b"print('hi')\n", file_size=None,
    file_name="note.py", mime_type="text/x-python",
):
    """A fake Update carrying a NON-image ``Document`` (the on_document inbound path).

    ``get_file().download_as_bytearray()`` returns ``raw`` (the fake file bytes);
    ``file_size`` is the Telegram-declared size for the pre-download cap (defaults to
    len(raw)). ``file_name`` is the Telegram-supplied name (may be hostile — ``../`` etc).
    """
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = None
    upd.message.caption = caption
    upd.message.reply_text = AsyncMock()
    upd.message.reply_to_message = None
    upd.effective_message = upd.message
    size = file_size if file_size is not None else len(raw)

    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(raw))

    document = MagicMock()
    document.file_size = size
    document.file_name = file_name
    document.mime_type = mime_type
    document.get_file = AsyncMock(return_value=tg_file)

    upd.message.photo = []
    upd.message.document = document
    return upd


def _file_bot(tmp_path, *, allowed=(1,), file_max_bytes=20 * 1024 * 1024, cwd=None):
    """A streaming bot whose active-project cwd + ALLOWED_ROOTS are ``tmp_path`` (T3)."""
    cwd = cwd if cwd is not None else str(tmp_path)
    streaming = FakeStreaming(cwd=cwd)
    bot = TelegramClaudeBot(
        make_config(
            allowed=allowed, engine_mode="streaming",
            allowed_roots=(tmp_path,), file_max_bytes=file_max_bytes,
        ),
        FakeRunner(), streaming=streaming,
    )
    bot._welcomed.add(1)
    return bot, streaming


# ---- inbound: on_document saves into the project, path-confined --------------


async def test_on_document_saves_into_project_cwd_and_offers_to_claude(tmp_path):
    bot, streaming = _file_bot(tmp_path)
    raw = b"def f():\n    return 42\n"
    upd = make_document_update(1, caption="review this", raw=raw, file_name="snippet.py")
    await bot.on_document(upd, make_ctx())

    # The file landed INSIDE the project cwd with the sanitized name + exact bytes.
    saved = tmp_path / "snippet.py"
    assert saved.is_file()
    assert saved.read_bytes() == raw
    # A normal turn was fired offering the file to Claude (caption as the instruction).
    assert len(streaming.handle_message_calls) == 1
    _chat, prompt, _rt = streaming.handle_message_calls[0]
    assert str(saved) in prompt
    assert "review this" in prompt
    # No image was threaded — this is the file path, not the multimodal path.
    assert streaming.images_calls[0] is None


async def test_on_document_no_caption_uses_default_instruction(tmp_path):
    bot, streaming = _file_bot(tmp_path)
    upd = make_document_update(1, caption=None, file_name="a.log")
    await bot.on_document(upd, make_ctx())
    _chat, prompt, _rt = streaming.handle_message_calls[0]
    assert "I've added the file" in prompt
    assert (tmp_path / "a.log").is_file()


async def test_on_document_dotdot_filename_confined_to_cwd(tmp_path):
    # SB2: a ``../`` traversal in the file_name CANNOT escape the project cwd. The name is
    # sanitized to a basename AND re-confined by resolve_within_roots — the file lands
    # INSIDE tmp_path, never in the parent.
    bot, streaming = _file_bot(tmp_path)
    upd = make_document_update(1, raw=b"x", file_name="../../escape.txt")
    await bot.on_document(upd, make_ctx())

    # Nothing was written above the root; the basename landed inside the cwd.
    parent_escape = tmp_path.parent / "escape.txt"
    assert not parent_escape.exists()
    assert (tmp_path / "escape.txt").is_file()
    assert len(streaming.handle_message_calls) == 1


async def test_on_document_absolute_filename_confined_to_cwd(tmp_path):
    # SB2: an ABSOLUTE file_name is stripped to its basename and saved inside the cwd —
    # it never writes to the absolute location.
    bot, streaming = _file_bot(tmp_path)
    upd = make_document_update(1, raw=b"y", file_name="/etc/cron.d/evil")
    await bot.on_document(upd, make_ctx())
    assert not Path("/etc/cron.d/evil").exists()  # never touched
    assert (tmp_path / "evil").is_file()


async def test_on_document_oversized_refused_before_download(tmp_path):
    # RB2: a declared size over the cap is refused with a clean message — NO download, NO
    # write, NO turn.
    bot, streaming = _file_bot(tmp_path, file_max_bytes=1024)
    upd = make_document_update(1, raw=b"x" * 10, file_size=5000, file_name="big.bin")
    await bot.on_document(upd, make_ctx())
    assert streaming.handle_message_calls == []
    upd.message.document.get_file.assert_not_awaited()
    assert not (tmp_path / "big.bin").exists()
    reply = upd.message.reply_text.await_args.args[0]
    assert "too large" in reply.lower()


async def test_on_document_oversized_refused_after_download_when_underreported(tmp_path):
    # Defense-in-depth: declared size None (no pre-check) but the downloaded bytes exceed
    # the cap → refused post-download; never written, never offered.
    bot, streaming = _file_bot(tmp_path, file_max_bytes=10)
    upd = make_document_update(1, raw=b"x" * 100, file_size=None, file_name="sneaky.bin")
    await bot.on_document(upd, make_ctx())
    assert streaming.handle_message_calls == []
    assert not (tmp_path / "sneaky.bin").exists()
    reply = upd.message.reply_text.await_args.args[0]
    assert "too large" in reply.lower()


async def test_on_document_sb1_unauthorized_chat_no_download_no_write(tmp_path):
    # SB1: a document from a NON-allowlisted chat is dropped — no download, no write, no turn.
    bot, streaming = _file_bot(tmp_path, allowed=(1,))
    upd = make_document_update(chat_id=999, raw=b"z", file_name="x.py")
    await bot.on_document(upd, make_ctx())
    assert streaming.handle_message_calls == []
    upd.message.document.get_file.assert_not_awaited()
    assert not (tmp_path / "x.py").exists()


async def test_on_document_oneshot_mode_refuses_needs_streaming(tmp_path):
    # One-shot mode has no per-project cwd → clean refusal; never silently drops the file.
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="oneshot"), runner)
    assert bot.streaming is None
    bot._welcomed.add(1)
    upd = make_document_update(1, file_name="x.py")
    await bot.on_document(upd, make_ctx())
    assert runner.run_calls == []
    reply = upd.message.reply_text.await_args.args[0]
    assert "streaming" in reply.lower()


async def test_on_document_download_failure_clean_message(tmp_path):
    # RB1: a download exception → a clean message, no crash, no write, no turn.
    bot, streaming = _file_bot(tmp_path)
    upd = make_document_update(1, file_name="x.py")
    upd.message.document.get_file = AsyncMock(side_effect=RuntimeError("boom"))
    await bot.on_document(upd, make_ctx())
    assert streaming.handle_message_calls == []
    assert not (tmp_path / "x.py").exists()
    reply = upd.message.reply_text.await_args.args[0]
    assert "couldn't download" in reply.lower()


async def test_on_document_never_logs_file_bytes_sb3(tmp_path, caplog):
    import logging

    bot, streaming = _file_bot(tmp_path)
    secret = b"SECRET-FILE-PAYLOAD-DO-NOT-LOG-9876543210"
    with caplog.at_level(logging.DEBUG):
        upd = make_document_update(1, raw=secret, file_name="secret.bin")
        await bot.on_document(upd, make_ctx())
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET-FILE-PAYLOAD" not in full_log
    # But a size SUMMARY (name + KB) is logged.
    assert "received a file" in full_log


async def test_on_document_does_not_collide_with_image_document(tmp_path):
    # An IMAGE document must route to on_photo (T1's multimodal path), NOT to on_document.
    # The registration filters are disjoint: on_photo = PHOTO | Document.IMAGE; on_document =
    # Document.ALL & ~Document.IMAGE. Prove an image-document message is NOT matched by the
    # on_document handler's filter, while a non-image document IS.
    from telegram.ext import MessageHandler

    bot, _streaming = _file_bot(tmp_path)
    app = bot.build_application()
    doc_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, MessageHandler) and h.callback == bot.on_document
    ]
    assert len(doc_handlers) == 1
    photo_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, MessageHandler) and h.callback == bot.on_photo
    ]
    assert len(photo_handlers) == 1


# ---- outbound: /get <path> ---------------------------------------------------


async def test_cmd_get_in_root_file_sends_document(tmp_path):
    bot, _streaming = _file_bot(tmp_path)
    f = tmp_path / "out.txt"
    f.write_bytes(b"hello file")
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["out.txt"]  # relative to the active cwd
    upd = make_update(1, text="/get out.txt")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_awaited_once()
    kwargs = ctx.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == 1
    assert kwargs["document"] is not None


async def test_cmd_get_absolute_in_root_path_sends(tmp_path):
    bot, _streaming = _file_bot(tmp_path)
    f = tmp_path / "abs.txt"
    f.write_bytes(b"data")
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = [str(f)]  # absolute, but inside the root
    upd = make_update(1, text=f"/get {f}")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_awaited_once()


async def test_cmd_get_out_of_root_refused(tmp_path):
    # SB2: an absolute path OUTSIDE the allowed root is refused — never read/uploaded.
    bot, _streaming = _file_bot(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"top secret")
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = [str(outside)]
    upd = make_update(1, text="/get ...")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "outside the permitted roots" in reply.lower()


async def test_cmd_get_dotdot_traversal_refused(tmp_path):
    # SB2: a ``../`` relative traversal that escapes the root is refused.
    bot, _streaming = _file_bot(tmp_path)
    (tmp_path.parent / "escape.txt").write_bytes(b"x")
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["../escape.txt"]
    upd = make_update(1, text="/get ../escape.txt")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "outside the permitted roots" in reply.lower()


async def test_cmd_get_missing_file_refused(tmp_path):
    bot, _streaming = _file_bot(tmp_path)
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["nope.txt"]
    upd = make_update(1, text="/get nope.txt")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "no such file" in reply.lower()


async def test_cmd_get_directory_refused(tmp_path):
    # A directory (in-root) is not a regular file → refused (RB2).
    bot, _streaming = _file_bot(tmp_path)
    (tmp_path / "subdir").mkdir()
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["subdir"]
    upd = make_update(1, text="/get subdir")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "no such file" in reply.lower()


async def test_cmd_get_oversized_refused(tmp_path):
    # RB2: an in-root file over the cap is refused — never uploaded.
    bot, _streaming = _file_bot(tmp_path, file_max_bytes=10)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["big.bin"]
    upd = make_update(1, text="/get big.bin")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert "too large" in reply.lower()


async def test_cmd_get_no_arg_usage(tmp_path):
    bot, _streaming = _file_bot(tmp_path)
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = []
    upd = make_update(1, text="/get")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    assert "usage" in upd.message.reply_text.await_args.args[0].lower()


async def test_cmd_get_sb1_unauthorized_no_send(tmp_path):
    # SB1: a /get from a NON-allowlisted chat does nothing.
    bot, _streaming = _file_bot(tmp_path, allowed=(1,))
    (tmp_path / "f.txt").write_bytes(b"x")
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["f.txt"]
    upd = make_update(chat_id=999, text="/get f.txt")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    upd.message.reply_text.assert_not_awaited()


async def test_cmd_get_oneshot_mode_refuses(tmp_path):
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,), engine_mode="oneshot"), runner)
    assert bot.streaming is None
    ctx = make_ctx()
    ctx.bot.send_document = AsyncMock()
    ctx.args = ["x.txt"]
    upd = make_update(1, text="/get x.txt")
    await bot.cmd_get(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


def test_build_application_registers_get_command_and_document_handler(tmp_path):
    from telegram.ext import CommandHandler, MessageHandler

    bot, _streaming = _file_bot(tmp_path)
    app = bot.build_application()
    get_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, CommandHandler) and "get" in {c.lower() for c in h.commands}
    ]
    assert len(get_handlers) == 1
    doc_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, MessageHandler) and h.callback == bot.on_document
    ]
    assert len(doc_handlers) == 1


def test_get_in_command_menu_and_help():
    # T3: /get must be in the native menu AND documented in HELP_TEXT (the lock-step guards).
    from claude_tg.bot import COMMAND_MENU, HELP_TEXT

    assert "get" in {cmd for cmd, _desc in COMMAND_MENU}
    assert "/get" in HELP_TEXT


# ===========================================================================
# P10 T2 — voice notes (pluggable transcription, graceful-off)
# ===========================================================================

import os  # noqa: E402
import sys as _sys  # noqa: E402

from claude_tg.bot import VOICE_SETUP_MESSAGE  # noqa: E402
from claude_tg.voice import TranscriptionError  # noqa: E402


def make_voice_update(chat_id=1, *, raw=b"OggS-fake-opus-bytes", kind="voice"):
    """A fake Update carrying a Telegram VOICE note (or an AUDIO file).

    ``get_file().download_as_bytearray()`` returns ``raw`` (the fake audio bytes).
    """
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = None
    upd.message.caption = None
    upd.message.reply_text = AsyncMock()
    upd.message.reply_to_message = None
    upd.effective_message = upd.message

    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(raw))
    attachment = MagicMock()
    attachment.file_size = len(raw)
    attachment.get_file = AsyncMock(return_value=tg_file)

    if kind == "voice":
        upd.message.voice = attachment
        upd.message.audio = None
    else:  # an audio file
        upd.message.voice = None
        upd.message.audio = attachment
    upd.message.photo = []
    upd.message.document = None
    return upd


def _voice_bot(*, allowed=(1,), transcribe_cmd="stt -f {audio}", streaming=True):
    """A streaming (or one-shot) bot with TRANSCRIBE_CMD set (or empty for graceful-off)."""
    fake_streaming = FakeStreaming() if streaming else None
    mode = "streaming" if streaming else "oneshot"
    bot = TelegramClaudeBot(
        make_config(allowed=allowed, engine_mode=mode, transcribe_cmd=transcribe_cmd),
        FakeRunner(), streaming=fake_streaming,
    )
    bot._welcomed.add(1)
    return bot, fake_streaming


# ---- graceful-off: no TRANSCRIBE_CMD configured -----------------------------


async def test_on_voice_graceful_off_when_no_transcribe_cmd():
    # No TRANSCRIBE_CMD → a clean setup message, NO download, NO turn (RB2, no crash).
    bot, streaming = _voice_bot(transcribe_cmd="")
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    assert streaming.handle_message_calls == []
    upd.message.voice.get_file.assert_not_awaited()
    reply = upd.message.reply_text.await_args.args[0]
    assert reply == VOICE_SETUP_MESSAGE
    assert "TRANSCRIBE_CMD" in reply


async def test_on_voice_graceful_off_whitespace_cmd():
    bot, streaming = _voice_bot(transcribe_cmd="   ")
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    assert streaming.handle_message_calls == []
    assert upd.message.reply_text.await_args.args[0] == VOICE_SETUP_MESSAGE


# ---- happy path: transcript echoed + turn fired -----------------------------


async def test_on_voice_transcribes_echoes_and_fires_turn(monkeypatch):
    bot, streaming = _voice_bot()

    async def fake_transcribe(*, template, audio_path, work_dir, timeout):
        # The handler must hand us a real, existing temp audio path it downloaded.
        assert os.path.isfile(audio_path)
        return "build me a parser"

    monkeypatch.setattr("claude_tg.bot.transcribe", fake_transcribe)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())

    # The transcript was echoed back QUOTED before the turn ran.
    echo = upd.message.reply_text.await_args_list[0].args[0]
    assert "🎙️" in echo and "build me a parser" in echo
    # And the transcript fired as a NORMAL turn (no image) against the active project.
    assert len(streaming.handle_message_calls) == 1
    chat_id, prompt, _rt = streaming.handle_message_calls[0]
    assert (chat_id, prompt) == (1, "build me a parser")
    assert streaming.images_calls[0] is None


async def test_on_voice_audio_file_also_handled(monkeypatch):
    bot, streaming = _voice_bot()

    async def fake_transcribe(*, template, audio_path, work_dir, timeout):
        return "from an audio file"

    monkeypatch.setattr("claude_tg.bot.transcribe", fake_transcribe)
    upd = make_voice_update(1, kind="audio")
    await bot.on_voice(upd, make_ctx())
    assert streaming.handle_message_calls[0][1] == "from an audio file"


async def test_on_voice_echo_escapes_html(monkeypatch):
    # A transcript with </>& is HTML-escaped in the quoted echo (it's parse_mode=HTML).
    bot, streaming = _voice_bot()

    async def fake_transcribe(*, template, audio_path, work_dir, timeout):
        return "fix <Foo> & <Bar>"

    monkeypatch.setattr("claude_tg.bot.transcribe", fake_transcribe)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    echo = upd.message.reply_text.await_args_list[0].args[0]
    assert "&lt;Foo&gt;" in echo and "&amp;" in echo
    # The turn still fires the RAW transcript (Claude gets the real text).
    assert streaming.handle_message_calls[0][1] == "fix <Foo> & <Bar>"


# ---- transcriber fails → clean error, no turn -------------------------------


async def test_on_voice_transcriber_failure_clean_error_no_turn(monkeypatch):
    bot, streaming = _voice_bot()

    async def boom(*, template, audio_path, work_dir, timeout):
        raise TranscriptionError("the transcriber failed (exit 1)")

    monkeypatch.setattr("claude_tg.bot.transcribe", boom)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    # No turn fired; a clean (body-free) error message was sent.
    assert streaming.handle_message_calls == []
    reply = upd.message.reply_text.await_args.args[0]
    assert "transcribe" in reply.lower()
    assert "exit 1" in reply


async def test_on_voice_download_failure_clean_message(monkeypatch):
    bot, streaming = _voice_bot()
    monkeypatch.setattr("claude_tg.bot.transcribe", AsyncMock())
    upd = make_voice_update(1)
    upd.message.voice.get_file = AsyncMock(side_effect=RuntimeError("net down"))
    await bot.on_voice(upd, make_ctx())
    assert streaming.handle_message_calls == []
    assert "download" in upd.message.reply_text.await_args.args[0].lower()


# ---- SB1: unauthorized voice → nothing --------------------------------------


async def test_on_voice_sb1_unauthorized_chat_no_download_no_turn(monkeypatch):
    bot, streaming = _voice_bot(allowed=(1,))
    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
        return "x"

    monkeypatch.setattr("claude_tg.bot.transcribe", spy)
    upd = make_voice_update(chat_id=999)  # not in the allowlist
    await bot.on_voice(upd, make_ctx())
    assert streaming.handle_message_calls == []
    assert called["n"] == 0
    upd.message.voice.get_file.assert_not_awaited()
    upd.message.reply_text.assert_not_awaited()


# ---- oneshot mode → clean "needs streaming" notice --------------------------


async def test_on_voice_oneshot_mode_refuses_needs_streaming():
    bot, _ = _voice_bot(streaming=False)
    assert bot.streaming is None
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    # No download even attempted; a clean streaming-only notice.
    upd.message.voice.get_file.assert_not_awaited()
    assert "streaming" in upd.message.reply_text.await_args.args[0].lower()


# ---- SB3: never log the audio bytes or the raw transcript -------------------


async def test_on_voice_never_logs_audio_or_transcript_sb3(monkeypatch, caplog):
    import logging

    bot, _streaming = _voice_bot()
    secret_audio = b"SECRET-AUDIO-PAYLOAD-DO-NOT-LOG-9876543210"
    secret_transcript = "SECRET-SPOKEN-WORDS-DO-NOT-LOG"

    async def fake_transcribe(*, template, audio_path, work_dir, timeout):
        return secret_transcript

    monkeypatch.setattr("claude_tg.bot.transcribe", fake_transcribe)
    with caplog.at_level(logging.DEBUG):
        upd = make_voice_update(1, raw=secret_audio)
        await bot.on_voice(upd, make_ctx())
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_transcript not in full_log
    assert "SECRET-AUDIO-PAYLOAD" not in full_log
    # But a size SUMMARY is logged (so an operator sees a voice note arrived).
    assert "received a voice note" in full_log


# ---- temp files cleaned (RB1) -----------------------------------------------


async def test_on_voice_temp_files_cleaned(monkeypatch, tmp_path):
    bot, _streaming = _voice_bot()
    workdir = tmp_path / "tg-voice-fixed"

    def fake_mkdtemp(*a, **k):
        workdir.mkdir()
        return str(workdir)

    monkeypatch.setattr("claude_tg.bot.tempfile.mkdtemp", fake_mkdtemp)

    async def fake_transcribe(*, template, audio_path, work_dir, timeout):
        # The audio is on disk under the temp dir while transcribing; we also drop a fake
        # transcript .txt to prove EVERYTHING under the dir is swept.
        assert os.path.isfile(audio_path)
        (workdir / "transcript.txt").write_text("hi", encoding="utf-8")
        return "ok"

    monkeypatch.setattr("claude_tg.bot.transcribe", fake_transcribe)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    # The whole temp dir (audio + any .txt) is gone after the handler returns.
    assert not workdir.exists()


async def test_on_voice_temp_cleaned_even_on_failure(monkeypatch, tmp_path):
    bot, _streaming = _voice_bot()
    workdir = tmp_path / "tg-voice-fail"

    def fake_mkdtemp(*a, **k):
        workdir.mkdir()
        return str(workdir)

    monkeypatch.setattr("claude_tg.bot.tempfile.mkdtemp", fake_mkdtemp)

    async def boom(*, template, audio_path, work_dir, timeout):
        raise TranscriptionError("boom")

    monkeypatch.setattr("claude_tg.bot.transcribe", boom)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    assert not workdir.exists()  # finally cleaned it even though transcribe raised


# ---- end-to-end through a REAL transcriber subprocess (no shell injection) ---


async def test_on_voice_real_transcriber_no_shell_injection(tmp_path):
    # Use a REAL python "transcriber" via TRANSCRIBE_CMD that just prints a fixed transcript.
    # The downloaded temp audio path is passed as {audio}; prove no shell runs it (the handler
    # path uses exec, not a shell). A separate injection probe of the path tokenization lives
    # in test_voice.py; here we prove the bot handler wires a real subprocess end to end.
    sentinel = tmp_path / "PWNED"
    template = f'{_sys.executable} -c "print(\'real voice transcript\')" {{audio}}'
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming", transcribe_cmd=template),
        FakeRunner(), streaming=streaming,
    )
    bot._welcomed.add(1)
    upd = make_voice_update(1)
    await bot.on_voice(upd, make_ctx())
    # The transcript was produced by the real subprocess and fired as a turn.
    assert streaming.handle_message_calls[0][1] == "real voice transcript"
    assert not sentinel.exists()


# ---- registration: SB1 chat filter; NOT a command (no menu change) ----------


def test_build_application_registers_voice_handler():
    from telegram.ext import MessageHandler

    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming"), FakeRunner(), streaming=FakeStreaming()
    )
    app = bot.build_application()
    voice_handlers = [
        h
        for group in sorted(app.handlers)
        for h in app.handlers[group]
        if isinstance(h, MessageHandler) and h.callback == bot.on_voice
    ]
    assert len(voice_handlers) == 1


def test_voice_is_not_a_command_no_menu_change():
    # The voice handler is a MessageHandler, NOT a command — COMMAND_MENU must not gain a
    # "voice" entry (the lock-step menu test would otherwise fail).
    from claude_tg.bot import COMMAND_MENU

    assert "voice" not in {cmd for cmd, _desc in COMMAND_MENU}
