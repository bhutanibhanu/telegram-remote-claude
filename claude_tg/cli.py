"""Console entry point for the ``claude-telegram-bot`` command (P7/T1, T4; B1 hardened).

This is the REAL entry: the installed console script, ``python -m claude_tg`` (see
``claude_tg/__main__.py``), and the long-standing ``python main.py`` all funnel through
:func:`main` here. The actual startup (``.env``-from-CWD load, logging, the one-shot vs
streaming switch, ``run_polling``) lives in :mod:`claude_tg.app`.

B1 (cwd-shadow fix): startup used to live in a top-level ``main`` module and ``cli.py``
did ``from main import main``. Because ``sys.path[0]`` (the CWD) precedes site-packages,
an unrelated ``main.py`` in the user's working directory would shadow the real entry for
the installed tool. Everything is package-internal now (``from claude_tg.app import run``),
so the entry resolves the same regardless of the CWD's contents.

On top of the startup logic this entry adds only a ``--version`` / ``-V`` short circuit
(T4) so the install is identifiable without booting the bot; any other argv is ignored
(the bot itself takes no positional args — it polls).
"""

from __future__ import annotations

import sys

from claude_tg import __version__

__all__ = ["main"]


def main(argv: list[str] | None = None) -> None:
    """Entry point: print the version on ``--version``/``-V``, else start the bot.

    ``argv`` defaults to ``sys.argv[1:]`` (override in tests). On the version flag we
    print and return WITHOUT importing/booting any bot machinery; otherwise we import
    :mod:`claude_tg.app` lazily and delegate to :func:`claude_tg.app.run`.
    """
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("--version", "-V") for a in args):
        print(f"claude-telegram-bot {__version__}")
        return
    # Import lazily so ``--version`` stays light (no PTB / SDK import) and import-time
    # failures in the bot stack surface only when actually starting the bot.
    from claude_tg.app import run

    run()


if __name__ == "__main__":
    main()
