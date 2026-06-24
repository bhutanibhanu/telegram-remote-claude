"""Entry point shim: ``python main.py`` starts the Telegram → Claude Code bot.

The real startup logic lives INSIDE the package (:mod:`claude_tg.app`, driven by
:func:`claude_tg.cli.main`) so the installed ``claude-telegram-bot`` console script and
``python -m claude_tg`` can't be cwd-shadowed by an unrelated ``main.py`` (P7/B1). This
file just preserves the long-standing ``python main.py`` invocation by delegating to the
package entry — the dependency now points package-ward, not the other way around.
"""

from __future__ import annotations

from claude_tg.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
