"""Console entry point for the ``claude-telegram-bot`` command (P7/T1, T4).

A thin shim over :func:`main.main` (the existing root ``main.py`` startup): the installed
console command, ``python -m claude_tg`` (see ``claude_tg/__main__.py``), and the
long-standing ``python main.py`` flow all start the bot identically — same ``.env``-from-CWD
load, same logging setup, same one-shot/streaming switch, same ``run_polling``. Keeping the
startup logic in one place (``main.py``, which ``tests/test_main.py`` also drives) avoids two
divergent entry points.

The only thing this wrapper adds on top of ``main.main`` is a ``--version`` / ``-V`` short
circuit (T4) so the install is identifiable without booting the bot; any other argv is passed
straight through (``main.main`` itself ignores argv and just polls).

``main`` is shipped as a top-level module (see ``[tool.setuptools] py-modules`` in
``pyproject.toml``), so ``from main import main`` resolves both in-repo (pytest's
``pythonpath = .``) and from a clean ``pip install``.
"""

from __future__ import annotations

import sys

from claude_tg import __version__
from main import main as _run

__all__ = ["main"]


def main(argv: list[str] | None = None) -> None:
    """Entry point: print the version on ``--version``/``-V``, else start the bot.

    ``argv`` defaults to ``sys.argv[1:]`` (override in tests). On the version flag we
    print and return WITHOUT importing/booting any bot machinery; otherwise we delegate
    to :func:`main.main` unchanged.
    """
    args = sys.argv[1:] if argv is None else argv
    if any(a in ("--version", "-V") for a in args):
        print(f"claude-telegram-bot {__version__}")
        return
    _run()


if __name__ == "__main__":
    main()
