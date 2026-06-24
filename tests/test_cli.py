"""Tests for the console entry wrapper (P7/T4; B1-updated).

``claude_tg.cli.main`` adds exactly one thing on top of the bot startup
(:func:`claude_tg.app.run`): a ``--version``/``-V`` short circuit that prints and returns
WITHOUT booting the bot. Any other argv must fall through to ``app.run`` unchanged. These
tests pin both, and that ``python -m claude_tg`` routes through the same wrapper.

B1 (cwd-shadow fix): the startup moved into ``claude_tg.app`` and ``cli.main`` imports it
LAZILY (``from claude_tg.app import run``) so ``--version`` stays light. The patch target
is therefore ``claude_tg.app.run`` (resolved at call time), not a module-level ``cli._run``.
"""

from __future__ import annotations

from unittest.mock import patch

import claude_tg.cli as cli
from claude_tg import __version__


def test_version_flag_prints_and_does_not_run(capsys):
    # Patch app.run: cli.main does a lazy `from claude_tg.app import run`, so the attribute
    # is resolved on claude_tg.app at call time. On --version it must never be reached.
    with patch("claude_tg.app.run") as run:
        cli.main(["--version"])
    assert run.call_count == 0  # never boots the bot
    out = capsys.readouterr().out
    assert __version__ in out
    assert "claude-telegram-bot" in out


def test_short_version_flag(capsys):
    with patch("claude_tg.app.run") as run:
        cli.main(["-V"])
    assert run.call_count == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_delegates_to_run(capsys):
    with patch("claude_tg.app.run") as run:
        cli.main([])
    run.assert_called_once_with()
    assert capsys.readouterr().out == ""  # nothing printed on the start path


def test_module_entry_uses_cli_main():
    # python -m claude_tg imports claude_tg.__main__, whose `main` IS cli.main.
    import claude_tg.__main__ as module_entry

    assert module_entry.main is cli.main
