import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import main


def test_main_installs_event_loop_for_polling(monkeypatch):
    app = Mock()

    def run_polling(**kwargs):
        assert asyncio.get_event_loop() is not None
        assert kwargs == {"allowed_updates": ["message"]}

    app.run_polling.side_effect = run_polling
    config = SimpleNamespace(
        state_file=None,
        allowed_chat_ids={123},
        workdir="/tmp",
        model=None,
        skip_permissions=True,
    )
    monkeypatch.setattr(main.Config, "from_env", lambda: config)
    monkeypatch.setattr(main, "ClaudeRunner", Mock())
    bot = Mock()
    bot.build_application.return_value = app
    monkeypatch.setattr(main, "TelegramClaudeBot", Mock(return_value=bot))

    main.main()

    app.run_polling.assert_called_once_with(allowed_updates=["message"])
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
