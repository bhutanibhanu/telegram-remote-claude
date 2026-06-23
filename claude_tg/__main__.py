"""``python -m claude_tg`` entry point (P7/T4).

Delegates to :func:`claude_tg.cli.main` — the same entry the ``claude-telegram-bot``
console script targets, which (apart from a ``--version`` short circuit) reuses the root
``main.main()`` verbatim. So ``python -m claude_tg``, the installed ``claude-telegram-bot``
command, and ``python main.py`` all start the bot identically (``.env`` loaded from the CWD,
then ``run_polling``). This module exists so the launchd/systemd keep-alive can fall back to
``python -m claude_tg`` when the console script isn't on PATH.
"""

from __future__ import annotations

from claude_tg.cli import main

if __name__ == "__main__":
    main()
