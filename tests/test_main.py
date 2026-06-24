import asyncio
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from claude_tg import app as app_mod
from claude_tg import cli as cli_mod

# B1 (cwd-shadow fix): the bot startup logic moved out of the root `main` module and into
# `claude_tg.app` (driven by `claude_tg.cli.main`). These tests now patch `claude_tg.app.*`
# and drive `claude_tg.cli.main` directly. Coverage is preserved verbatim — the loud
# allow-all WARNING, the event-loop/polling install, and the gated-default-stays-INFO
# assertions — plus a new shadow-proof test (see test_entry_is_not_cwd_shadowed).


def _run_main_with_config(monkeypatch, config):
    """Drive ``claude_tg.cli.main()`` with a stub config + fully-mocked PTB app/bot,
    returning the mock app. Shared by the polling + SB5 startup-surface tests."""
    app = Mock()

    def run_polling(**kwargs):
        assert asyncio.get_event_loop() is not None

    app.run_polling.side_effect = run_polling
    monkeypatch.setattr(app_mod.Config, "from_env", lambda: config)
    monkeypatch.setattr(app_mod, "ClaudeRunner", Mock())
    bot = Mock()
    bot.build_application.return_value = app
    monkeypatch.setattr(app_mod, "TelegramClaudeBot", Mock(return_value=bot))
    # Drive the REAL console entry (no args => start the bot, not the --version short
    # circuit). Exercises cli.main -> app.run end to end.
    cli_mod.main([])
    return app


def test_main_installs_event_loop_for_polling(monkeypatch):
    config = SimpleNamespace(
        state_file=None,
        allowed_chat_ids={123},
        workdir="/tmp",
        model=None,
        # SB5/C1: the gated (secure) default — a startup with the bypass OFF must NOT
        # emit the allow-all WARNING (see test_main_warns_loudly_when_bypass_enabled).
        skip_permissions=False,
    )
    app = _run_main_with_config(monkeypatch, config)

    app.run_polling.assert_called_once_with(allowed_updates=["message"])
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_main_warns_loudly_when_bypass_enabled(monkeypatch, caplog):
    """SB5/C1: when the operator opts INTO the bypass (``skip_permissions=True``) the
    startup must be LOUD — a WARNING-level record clearly stating the operator approval
    gate is DISABLED (allow-all). RED on the pre-C1 code, which logged the bypass state
    only inside the single INFO startup line (no WARNING surface).
    """
    config = SimpleNamespace(
        state_file=None,
        allowed_chat_ids={123},
        workdir="/tmp",
        model=None,
        skip_permissions=True,
    )
    with caplog.at_level(logging.WARNING, logger="claude_tg"):
        _run_main_with_config(monkeypatch, config)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an allow-all startup must emit at least one WARNING"
    blob = " ".join(r.getMessage() for r in warnings).lower()
    # The warning must name the disabled gate + the allow-all consequence (not a vague
    # "skip_permissions: True" — an operator scanning logs must see the danger).
    assert "disabled" in blob or "no approval" in blob or "allow-all" in blob
    assert "approval" in blob or "permission" in blob


def test_main_gated_default_stays_info_no_warning(monkeypatch, caplog):
    """SB5/C1: the normal (gated) startup stays at INFO — no spurious WARNING when the
    gate is ON. Keeps the loud surface meaningful (it fires ONLY for the dangerous
    allow-all posture, so it can't be tuned out as noise)."""
    config = SimpleNamespace(
        state_file=None,
        allowed_chat_ids={123},
        workdir="/tmp",
        model=None,
        skip_permissions=False,
    )
    with caplog.at_level(logging.INFO, logger="claude_tg"):
        _run_main_with_config(monkeypatch, config)

    # No record may warn about the permission gate when it is ON.
    offenders = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("approval" in r.getMessage().lower() or "permission" in r.getMessage().lower())
    ]
    assert not offenders, f"gated startup must not warn about the gate: {offenders}"
    # The gated startup is still announced at INFO (the normal startup line).
    info_blob = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.INFO
    ).lower()
    assert "starting" in info_blob


def test_root_main_shim_delegates_to_package(monkeypatch):
    """B1: root ``main.py`` is a thin shim — ``main.main`` IS ``claude_tg.cli.main`` (the
    dependency points package-ward now, not the other way). Guards against the shim
    re-growing its own startup logic (which would reintroduce the cwd-shadow footgun)."""
    import main as root_main

    assert root_main.main is cli_mod.main


def test_entry_is_not_cwd_shadowed(tmp_path, monkeypatch):
    """B1 RED→GREEN: ``python -m claude_tg --version`` must print the SHIPPED version even
    when the CWD contains an unrelated ``main.py``.

    The package import path (``claude_tg.cli`` -> ``claude_tg.app``) is fully self-contained,
    so a decoy ``main.py`` in ``sys.path[0]`` (the CWD) can't shadow the entry. This is RED
    on the pre-B1 code (``cli.py`` did ``from main import main``, which resolves the decoy
    because the CWD precedes site-packages on ``sys.path``) and GREEN after.

    Run in a child interpreter from inside the decoy dir so ``sys.path[0]`` is that dir,
    faithfully reproducing the installed-tool scenario (package on the path, decoy in CWD).
    """
    decoy = tmp_path / "main.py"
    decoy.write_text(
        textwrap.dedent(
            """\
            # Unrelated decoy main.py. If the entry is cwd-shadowed this is imported
            # instead of the shipped startup; its sentinel version would then print.
            __version__ = "DECOY-SHADOWED-9.9.9"

            def main(argv=None):
                print(f"claude-telegram-bot {__version__}")
            """
        )
    )
    repo_root = Path(__file__).resolve().parent.parent
    env = {
        **__import__("os").environ,
        # Put the package on the path WITHOUT relying on the CWD (mirrors site-packages).
        "PYTHONPATH": str(repo_root),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "claude_tg", "--version"],
        cwd=str(decoy.parent),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    out = proc.stdout.strip()
    assert out == "claude-telegram-bot 0.1.0", f"shadowed? got {out!r} (stderr: {proc.stderr})"
    assert "DECOY" not in out
