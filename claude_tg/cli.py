"""Console entry point for the ``claude-telegram-bot`` command (P7/T1).

This is a thin shim: it reuses :func:`main.main` (the existing root ``main.py``
startup) verbatim so the installed console command and the long-standing
``python main.py`` flow are byte-for-byte identical — same ``.env``-from-CWD load,
same logging setup, same one-shot/streaming switch, same ``run_polling``. Keeping the
startup logic in one place (``main.py``, which ``tests/test_main.py`` also drives)
avoids two divergent entry points.

``main`` is shipped as a top-level module (see ``[tool.setuptools] py-modules`` in
``pyproject.toml``), so ``from main import main`` resolves both in-repo (pytest's
``pythonpath = .``) and from a clean ``pip install``.
"""

from __future__ import annotations

from main import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
