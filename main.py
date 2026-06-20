"""Entry point: start the Telegram → Claude Code bot."""

from __future__ import annotations

import logging

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeRunner
from claude_tg.config import Config
from claude_tg.session_store import JsonSessionStore

log = logging.getLogger("claude_tg")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    store = JsonSessionStore(config.state_file) if config.state_file else None
    runner = ClaudeRunner(config, session_store=store)
    bot = TelegramClaudeBot(config, runner)
    app = bot.build_application()

    log.info(
        "Starting Claude Telegram bot | %d allowed chat(s) | workdir: %s | model: %s | skip_permissions: %s",
        len(config.allowed_chat_ids),
        config.workdir,
        config.model or "(default)",
        config.skip_permissions,
    )
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
