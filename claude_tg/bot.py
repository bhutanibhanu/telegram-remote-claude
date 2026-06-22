"""Telegram transport: routes allowlisted messages to Claude and replies."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude_runner import ClaudeBusy, ClaudeRunner
from .config import Config
from .paths import PathNotAllowed, resolve_within_roots
from .render import yolo_banner
from .stream_session import StreamingBusy, StreamingSession
from .util import split_message

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 *Claude Code remote*\n\n"
    "Just send me a message and I'll run it through Claude Code on the Mac and reply.\n\n"
    "Commands:\n"
    "/help — this help\n"
    "/reset — start a fresh Claude session (forget context)\n"
    "/cancel — abort the in-flight run (streaming mode)\n"
    "/yolo — run every tool with NO approval prompt this session (streaming mode)\n"
    "/unyolo — restore the per-tool permission gate (streaming mode)\n"
    "/pwd — show the current working directory\n"
    "/cd <path> — change the working directory (confined to the permitted roots)\n"
    "\nAny *other* slash-command (e.g. /grill, /pipeline, /scaffold) is forwarded "
    "verbatim and runs as a skill in the Claude session.\n"
)


class TelegramClaudeBot:
    def __init__(
        self,
        config: Config,
        runner: ClaudeRunner,
        *,
        streaming: StreamingSession | None = None,
    ):
        self.config = config
        self.runner = runner
        # S4 switch: the streaming collaborator is constructed (by main.py) ONLY when
        # ENGINE_MODE=streaming. In oneshot mode it is None and EVERY path below behaves
        # exactly as before — the live one-shot bot is untouched until the owner flips
        # the flag. Streaming-mode methods delegate to this driver.
        self.streaming = streaming if config.engine_mode == "streaming" else None

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
        if self.streaming is not None:
            self.streaming.reset(update.effective_chat.id)
        await update.message.reply_text("🔄 Fresh Claude session started.")

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Abort the in-flight run cleanly (RB4). Streaming mode only; oneshot is a no-op."""
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Nothing to cancel — one-shot mode runs each message to completion."
            )
            return
        aborted = self.streaming.handle_cancel(update.effective_chat.id)
        if aborted:
            await update.message.reply_text(f"🛑 Cancelled ({aborted} pending request(s) aborted).")
        else:
            await update.message.reply_text("Nothing in flight to cancel.")

    async def cmd_yolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn ON ``/yolo`` — every tool runs with NO approval prompt this session (P2, D6).

        Streaming mode only (the permission gate is a streaming-engine concept; one-shot
        has no per-tool gating). Mirrors :meth:`cmd_cancel`: the ``_ok`` allowlist guard
        first, then delegate to the session. The reply is the LOUD enable banner
        (``render.yolo_banner`` — carries the ``⚠️`` glyph) so allow-all is never silent
        at toggle time; the session keeps it loud throughout each turn (D6).
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Permission gating (and /yolo) applies to streaming mode only — "
                "one-shot mode has no per-tool approval prompts."
            )
            return
        self.streaming.set_yolo(update.effective_chat.id, True)
        await update.message.reply_text(yolo_banner())

    async def cmd_unyolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn OFF ``/yolo`` — restore the fail-closed per-tool permission gate (P2, D6).

        Streaming mode only (mirrors :meth:`cmd_yolo`). After this, risky tools are held
        for approval again. A clear confirmation so the operator knows gating is back on.
        """
        if not await self._ok(update) or update.message is None:
            return
        if self.streaming is None:
            await update.message.reply_text(
                "Permission gating (and /yolo) applies to streaming mode only — "
                "one-shot mode has no per-tool approval prompts."
            )
            return
        self.streaming.set_yolo(update.effective_chat.id, False)
        await update.message.reply_text(
            "✅ Gating restored — risky tools will ask for approval again (/yolo is off)."
        )

    async def cmd_pwd(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        await update.message.reply_text(f"📁 {self.runner.get_cwd(update.effective_chat.id)}")

    async def cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ok(update) or update.message is None:
            return
        chat_id = update.effective_chat.id
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_text("Usage: /cd <path>")
            return
        # SB2: canonicalize (resolves symlinks AND ..) and confine to ALLOWED_ROOTS
        # BEFORE touching the runner. A path that escapes the permitted roots is
        # refused here and never reaches set_cwd — this guard holds for BOTH oneshot
        # and streaming modes (cmd_cd is shared). ALLOW_ANY_PATH=true is the opt-out.
        try:
            target = resolve_within_roots(
                arg,
                cwd=self.runner.get_cwd(chat_id),
                allowed_roots=self.config.allowed_roots,
                allow_any=self.config.allow_any_path,
            )
        except PathNotAllowed:
            await update.message.reply_text(
                f"❌ Path not allowed (outside the permitted roots): {arg}"
            )
            return
        try:
            new_cwd = self.runner.set_cwd(chat_id, str(target))
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
        await self._run_turn(update, ctx, update.effective_chat.id, text)

    async def on_skill_command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward any *unregistered* slash-command verbatim to the active session (P3, D1).

        Registered as ``MessageHandler(allowed & filters.COMMAND, …)`` **after** the
        specific ``CommandHandler``s, so PTB's first-match-wins routing means a real bot
        command (``/reset``, ``/cd``, …) is consumed by its own handler and only an
        *unregistered* command (``/grill``, ``/pipeline``, …) falls through to here. The
        text is then run as an ordinary turn — the same dispatch path :meth:`on_message`
        uses — so the slash-command launches that skill in the live session.

        SB1: this is a new inbound surface, so it carries the SAME guards as every other
        handler — the ``allowed`` filter on the registration AND the ``_ok`` recheck below
        (defense in depth). A non-allowlisted chat reaches neither the runner nor the
        streaming session. RB1: a missing/empty/whitespace command no-ops (after strip),
        and ``/`` only / unicode garbage is just forwarded as a turn — the handler never
        raises and the session stays usable. The command is forwarded VERBATIM (leading
        ``/`` and args intact); we do not validate, rewrite, or allowlist skill names (D1/D2).
        """
        if not await self._ok(update) or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        await self._run_turn(update, ctx, update.effective_chat.id, text)

    async def _run_turn(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> None:
        """Run ``text`` as one turn for ``chat_id`` — the shared dispatch both the message
        handler and the skill-launch passthrough route through (one path, no duplication).

        Streaming mode hands the turn to the :class:`StreamingSession`; one-shot mode runs
        it through the runner and replies (chunked). Callers MUST have already done the
        ``_ok`` allowlist recheck and the empty-text guard.
        """
        if self.streaming is not None:
            await self._on_message_streaming(update, ctx, chat_id, text)
            return

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
            reply = result.text if (result.text and result.text.strip()) else "✅ (Claude returned no text.)"
            await self._reply_chunked(update, reply)
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

    # ---- streaming mode (ENGINE_MODE=streaming) -----------------------------
    async def _on_message_streaming(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
    ) -> None:
        """Drive the streaming engine for one message (delegates to StreamingSession).

        Binds send/edit closures to this chat (the actual Telegram I/O the render layer
        deferred), then hands the turn to the driver. A second concurrent message raises
        :class:`StreamingBusy` (one active turn per chat — the harvested ClaudeBusy
        invariant) and we reply the same "still working" notice as one-shot mode. SB4: the
        text is the engine's prompt, never interpolated into a shell command/argument.
        """
        assert self.streaming is not None
        bot = ctx.bot

        async def send(*, text: str, reply_markup=None, parse_mode=None) -> int | None:
            msg = await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            return getattr(msg, "message_id", None)

        async def edit(*, message_id: int, text: str, parse_mode=None) -> None:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode
            )

        async def delete(*, message_id: int) -> None:
            # Clear the transient "💭 Claude is thinking…" status line at turn end so a
            # stale one does not linger (best-effort; the session swallows failures).
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

        try:
            await self.streaming.handle_message(
                chat_id, text, send=send, edit=edit, delete=delete
            )
        except StreamingBusy:
            await update.message.reply_text(
                "⏳ Still working on your previous message — it'll reply when done. "
                "Send one message at a time."
            )

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline-keyboard tap handler — **the SB1 security boundary**.

        A callback tap is new attack surface (SB1). PTB's ``CallbackQueryHandler`` cannot
        be chat-filtered the way ``MessageHandler`` is (it filters by callback_data
        pattern), so THIS explicit :meth:`_authorized` recheck is the authoritative
        allowlist gate: an unauthorized / forged callback NEVER routes a decision (it
        cannot approve a plan or answer a question) — if the chat is not allowlisted we
        silently answer the callback query and return WITHOUT touching the engine. For an
        authorized chat the
        decode + routing lives in :meth:`StreamingSession.resolve_callback`, which ignores
        any ``callback_data`` that fails to decode (foreign/stale/malformed → None) and
        resolves nothing in that case (RB1). The callback query is ALWAYS answered (so the
        client's spinner stops), even when ignored.
        """
        query = update.callback_query
        if query is None:
            return
        # SB1: explicit allowlist recheck inside the handler (the filter is the first
        # gate; this is defense in depth). An unauthorized tap is answered + dropped —
        # never resolved.
        if not self._authorized(update) or self.streaming is None:
            await self._answer_callback(query)
            return
        chat = update.effective_chat
        try:
            outcome = self.streaming.resolve_callback(chat.id, query.data)
        except Exception:  # RB1: a bad/garbage callback must never crash the handler
            log.exception("error routing callback for chat %s", chat.id if chat else "?")
            await self._answer_callback(query)
            return
        await self._answer_callback(query, outcome.note if outcome.handled else None)
        if outcome.expects_text and outcome.note:
            try:
                await query.message.reply_text(f"✏️ {outcome.note}…")
            except Exception:
                pass

    @staticmethod
    async def _answer_callback(query, text: str | None = None) -> None:
        """Answer a callback query (stops the client spinner); never raise (RB1)."""
        try:
            if text:
                await query.answer(text=text)
            else:
                await query.answer()
        except Exception:
            pass

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
        # concurrent_updates(True) is REQUIRED by the answer-hold design: a streaming turn
        # parks its handler *inside* engine.send awaiting the operator's answer, and the
        # inline-keyboard tap that supplies that answer arrives as a SEPARATE update. With
        # PTB's default sequential processing the tap would queue behind the parked turn
        # handler — a deadlock (the turn waits for the tap; the tap waits for the turn to
        # return). Concurrent dispatch lets the callback handler run while the turn is held
        # (resolve_callback is intentionally lock-free for exactly this). One turn per chat
        # is still enforced by the StreamingBusy guard.
        app = ApplicationBuilder().token(self.config.bot_token).concurrent_updates(True).build()
        allowed = filters.Chat(chat_id=list(self.config.allowed_chat_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=allowed))
        app.add_handler(CommandHandler("reset", self.cmd_reset, filters=allowed))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel, filters=allowed))
        app.add_handler(CommandHandler("yolo", self.cmd_yolo, filters=allowed))
        app.add_handler(CommandHandler("unyolo", self.cmd_unyolo, filters=allowed))
        app.add_handler(CommandHandler("pwd", self.cmd_pwd, filters=allowed))
        app.add_handler(CommandHandler("cd", self.cmd_cd, filters=allowed))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_message))
        # P3 skill-launch passthrough (D1): forward any *unregistered* slash-command verbatim
        # to the session. Registered AFTER the specific CommandHandlers above so PTB's
        # first-match-wins routing lets a real bot command (/reset, /cd, …) be consumed by
        # its own handler — only an unregistered command (/grill, /pipeline, …) falls through
        # here. SB1: same `allowed` chat filter as every other handler (the `_ok` recheck
        # inside on_skill_command is defense in depth).
        app.add_handler(MessageHandler(allowed & filters.COMMAND, self.on_skill_command))
        # SB1 (callback taps): PTB's CallbackQueryHandler filters by callback_data
        # *pattern*, not by chat (no `filters=` like MessageHandler), so the authoritative
        # allowlist gate for a tap is the explicit `_authorized` recheck inside
        # on_callback — a non-allowlisted / forged tap is answered and dropped there,
        # never routed to a decision. (allowed_updates also only enables callback_query
        # in streaming mode; see main.py.)
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=None))
        app.add_error_handler(self.on_error)
        return app
