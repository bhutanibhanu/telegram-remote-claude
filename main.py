"""Entry point: start the Telegram → Claude Code bot."""

from __future__ import annotations

import asyncio
import logging

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeRunner
from claude_tg.config import Config
from claude_tg.session_store import JsonSessionStore
from claude_tg.stream_session import StreamingSession

log = logging.getLogger("claude_tg")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTPX logs Telegram request URLs, which contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = Config.from_env()
    store = JsonSessionStore(config.state_file) if config.state_file else None
    runner = ClaudeRunner(config, session_store=store)
    # S4 switch: build the streaming collaborator only in streaming mode; oneshot keeps
    # the one-shot runner path untouched (the live bot is unaffected until the flag flips).
    streaming = (
        StreamingSession(config, session_store=store)
        if getattr(config, "engine_mode", "oneshot") == "streaming"
        else None
    )
    bot = TelegramClaudeBot(config, runner, streaming=streaming)
    app = bot.build_application()

    log.info(
        "Starting Claude Telegram bot | %d allowed chat(s) | workdir: %s | model: %s | "
        "skip_permissions: %s | engine_mode: %s",
        len(config.allowed_chat_ids),
        config.workdir,
        config.model or "(default)",
        config.skip_permissions,
        getattr(config, "engine_mode", "oneshot"),
    )
    # SB5 / C1: make an allow-all posture LOUD. When the operator opted into the bypass
    # (CLAUDE_SKIP_PERMISSIONS=true) the per-tool approval gate is DISABLED and Claude
    # runs every tool unattended — surface that as a prominent WARNING (not buried in the
    # INFO startup line) so it is obvious in the logs. The gated default stays at INFO.
    if config.skip_permissions:
        log.warning(
            "⚠️  SECURITY: operator approval gate is DISABLED "
            "(CLAUDE_SKIP_PERMISSIONS=true) — Claude runs ALL tools with NO approval "
            "prompt (allow-all / --dangerously-skip-permissions). Unset "
            "CLAUDE_SKIP_PERMISSIONS to restore the gate."
        )
    # Python 3.14 no longer creates a default event loop for synchronous callers.
    # Streaming mode also needs callback_query updates (the inline-keyboard taps);
    # one-shot mode only needs messages.
    allowed_updates = ["message"]
    if streaming is not None:
        allowed_updates = ["message", "callback_query"]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run_polling(allowed_updates=allowed_updates)
    finally:
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()


if __name__ == "__main__":
    main()
