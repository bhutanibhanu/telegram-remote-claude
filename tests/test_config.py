import os

import pytest

from claude_tg.config import Config, load_dotenv, parse_chat_ids


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_", "CLAUDE_")):
            monkeypatch.delenv(key, raising=False)


def test_parse_chat_ids():
    assert parse_chat_ids("1, 2 ;3") == {1, 2, 3}
    assert parse_chat_ids("") == set()
    with pytest.raises(ValueError):
        parse_chat_ids("abc")


def test_requires_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


def test_requires_chat_ids(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


def test_defaults(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42, 43")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.bot_token == "tok"
    assert cfg.allowed_chat_ids == frozenset({42, 43})
    assert cfg.claude_bin == "claude"
    assert cfg.model is None
    assert cfg.skip_permissions is True
    assert cfg.timeout_seconds == 600


def test_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CLAUDE_SKIP_PERMISSIONS", "false")
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "30")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.workdir == tmp_path
    assert cfg.model == "claude-opus-4-8"
    assert cfg.skip_permissions is False
    assert cfg.timeout_seconds == 30


def test_bad_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "abc")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


def test_load_dotenv_no_override(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_BOT_TOKEN="fromfile"\n# comment\nFOO=bar\n')
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fromenv")
    load_dotenv(env)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "fromenv"  # existing env wins
    assert os.environ["FOO"] == "bar"
