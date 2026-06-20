"""Telegram transport: routes allowlisted messages to Claude and replies."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude_runner import ClaudeBusy, ClaudeRunner
from .config import Config
from .util import split_message

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 *Claude Code remote*\n\n"
    "Just send me a message and I'll run it through Claude Code on the Mac and reply.\n\n"
    "Commands:\n"
    "/help — this help\n"
    "/reset — start a fresh Claude session (forget context)\n"
    "/pwd — show the current working directory\n"
    "/cd <path> — change the working directory\n"
)


class TelegramClaudeBot:
    def __init__(self, config: Config, runner: ClaudeRunner):
        self.config = config
        self.runner = runner

    # ---- auth ---------------------------------------------------------------
    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id in self.config.allowed_chat_ids

    async def _ok(self, update: Update) -> bool:
        if self._authorized(update):
            return True
        chat = update.effective_chat
        log.warning("ignoring update from unauthorized chat %s", chat.id if chat else "?")
        return False

    # ---- commands -----------------------------------------------------------
    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

    async def cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        self.runner.reset(update.effective_chat.id)
        await update.message.reply_text("🔄 Fresh Claude session started.")

    async def cmd_pwd(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        await update.message.reply_text(f"📁 {self.runner.get_cwd(update.effective_chat.id)}")

    async def cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_text("Usage: /cd <path>")
            return
        try:
            new_cwd = self.runner.set_cwd(update.effective_chat.id, arg)
        except NotADirectoryError:
            await update.message.reply_text(f"❌ Not a directory: {arg}")
            return
        await update.message.reply_text(f"📁 Working directory set to:\n{new_cwd}")

    # ---- messages -----------------------------------------------------------
    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id

        stop = asyncio.Event()
        typing = asyncio.create_task(self._keep_typing(ctx, chat_id, stop))
        try:
            result = await self.runner.run(chat_id, text)
        except ClaudeBusy:
            await update.message.reply_text(
                "⏳ Still working on your previous message — it'll reply when done. "
                "Send one message at a time."
            )
            return
        finally:
            stop.set()
            await asyncio.gather(typing, return_exceptions=True)

        if result.ok:
            await self._reply_chunked(update, result.text or "✅ (Claude returned no text.)")
        else:
            await self._reply_chunked(update, f"⚠️ {result.error or 'Something went wrong.'}")

    async def _keep_typing(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event) -> None:
        """Show the 'typing…' indicator until ``stop`` is set (Claude can be slow)."""
        try:
            while not stop.is_set():
                try:
                    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:  # never let a transient API hiccup kill the turn
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover
            pass

    async def _reply_chunked(self, update: Update, text: str) -> None:
        for chunk in split_message(text):
            if not chunk.strip():
                continue
            await update.message.reply_text(chunk)

    # ---- errors -------------------------------------------------------------
    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("unhandled error while processing update", exc_info=ctx.error)
        try:
            if isinstance(update, Update) and update.effective_message and self._authorized(update):
                await update.effective_message.reply_text("⚠️ Internal error — check the bot logs on the Mac.")
        except Exception:
            pass

    # ---- wiring -------------------------------------------------------------
    def build_application(self) -> Application:
        app = ApplicationBuilder().token(self.config.bot_token).build()
        allowed = filters.Chat(chat_id=list(self.config.allowed_chat_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=allowed))
        app.add_handler(CommandHandler("reset", self.cmd_reset, filters=allowed))
        app.add_handler(CommandHandler("pwd", self.cmd_pwd, filters=allowed))
        app.add_handler(CommandHandler("cd", self.cmd_cd, filters=allowed))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_message))
        app.add_error_handler(self.on_error)
        return app
