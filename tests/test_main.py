import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import main


def _run_main_with_config(monkeypatch, config):
    """Drive ``main.main()`` with a stub config + fully-mocked PTB app/bot, returning
    the mock app. Shared by the polling + SB5 startup-surface tests."""
    app = Mock()

    def run_polling(**kwargs):
        assert asyncio.get_event_loop() is not None

    app.run_polling.side_effect = run_polling
    monkeypatch.setattr(main.Config, "from_env", lambda: config)
    monkeypatch.setattr(main, "ClaudeRunner", Mock())
    bot = Mock()
    bot.build_application.return_value = app
    monkeypatch.setattr(main, "TelegramClaudeBot", Mock(return_value=bot))
    main.main()
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
