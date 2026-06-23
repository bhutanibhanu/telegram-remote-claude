"""Tests for the console entry wrapper (P7/T4).

``claude_tg.cli.main`` adds exactly one thing on top of ``main.main``: a
``--version``/``-V`` short circuit that prints and returns WITHOUT booting the bot. Any
other argv must fall through to ``main.main`` unchanged. These tests pin both, and that
``python -m claude_tg`` routes through the same wrapper.
"""

from __future__ import annotations

from unittest.mock import patch

import claude_tg.cli as cli
from claude_tg import __version__


def test_version_flag_prints_and_does_not_run(capsys):
    with patch.object(cli, "_run") as run:
        cli.main(["--version"])
    assert run.call_count == 0  # never boots the bot
    out = capsys.readouterr().out
    assert __version__ in out
    assert "claude-telegram-bot" in out


def test_short_version_flag(capsys):
    with patch.object(cli, "_run") as run:
        cli.main(["-V"])
    assert run.call_count == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_delegates_to_main(capsys):
    with patch.object(cli, "_run") as run:
        cli.main([])
    run.assert_called_once_with()
    assert capsys.readouterr().out == ""  # nothing printed on the start path


def test_module_entry_uses_cli_main():
    # python -m claude_tg imports claude_tg.__main__, whose `main` IS cli.main.
    import claude_tg.__main__ as module_entry

    assert module_entry.main is cli.main
