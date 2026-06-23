import os

import pytest

from claude_tg.config import (
    Config,
    load_dotenv,
    parse_allowed_roots,
    parse_chat_ids,
    parse_chat_send_interval_seconds,
    parse_max_concurrent_runs,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_", "CLAUDE_")):
            monkeypatch.delenv(key, raising=False)
    # clean_env only clears TELEGRAM_/CLAUDE_ prefixes; the SB2 vars are NOT
    # auto-cleared, so clear them here too to stop host/CI leakage into these tests.
    monkeypatch.delenv("ALLOWED_ROOTS", raising=False)
    monkeypatch.delenv("ALLOW_ANY_PATH", raising=False)
    # MAX_CONCURRENT_RUNS (P5/T6) has no TELEGRAM_/CLAUDE_ prefix either — clear it so a
    # host/CI value can't leak into the cap parse matrix below.
    monkeypatch.delenv("MAX_CONCURRENT_RUNS", raising=False)
    # RENDER_CHAT_SEND_INTERVAL_SECONDS (P5/T8) likewise has no TELEGRAM_/CLAUDE_ prefix.
    monkeypatch.delenv("RENDER_CHAT_SEND_INTERVAL_SECONDS", raising=False)


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


# --- SB2: ALLOWED_ROOTS / ALLOW_ANY_PATH (the /cd confinement config) ---------


def test_parse_allowed_roots_defaults_to_default_when_unset(tmp_path):
    # The locked design: none given -> (default,), so confinement is ON by default
    # rooted at the workdir (never an empty tuple).
    default = tmp_path / "work"
    assert parse_allowed_roots("", default=default) == (default.resolve(),)
    assert parse_allowed_roots("   ", default=default) == (default.resolve(),)


def test_parse_allowed_roots_splits_on_comma_and_pathsep(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    # Mixed comma + os.pathsep separators (parse_chat_ids-style tolerance).
    raw = f"{a}{os.pathsep}{b},{c}"
    roots = parse_allowed_roots(raw, default=tmp_path)
    assert roots == (a.resolve(), b.resolve(), c.resolve())


def test_parse_allowed_roots_expands_user(monkeypatch, tmp_path):
    # ~ is expanded (and resolved) so an owner can list "~/projects".
    monkeypatch.setenv("HOME", str(tmp_path))
    roots = parse_allowed_roots("~/projects", default=tmp_path)
    assert roots == ((tmp_path / "projects").resolve(),)


def test_allowed_roots_default_is_workdir_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    cfg = Config.from_env(dotenv_path=None)
    # Unset ALLOWED_ROOTS -> the single default root is the (resolved) workdir.
    assert cfg.allowed_roots == (tmp_path.resolve(),)
    assert cfg.allow_any_path is False  # confinement ON by default


def test_allowed_roots_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    r1 = tmp_path / "one"
    r2 = tmp_path / "two"
    monkeypatch.setenv("ALLOWED_ROOTS", f"{r1},{r2}")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.allowed_roots == (r1.resolve(), r2.resolve())  # NOT the workdir default


def test_allow_any_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_ANY_PATH", "true")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.allow_any_path is True


# --- D6: MAX_CONCURRENT_RUNS (the concurrency cap, P5/T6) ----------------------


def test_parse_max_concurrent_runs_unset_empty_and_zero_default_to_3():
    # The locked D6 rule: unset / empty / "0" all read as "use the default" (3). A zero cap
    # would queue every turn forever (deadlock), so 0 is treated as unset, not accepted.
    assert parse_max_concurrent_runs(None) == 3
    assert parse_max_concurrent_runs("") == 3
    assert parse_max_concurrent_runs("   ") == 3
    assert parse_max_concurrent_runs("0") == 3


def test_parse_max_concurrent_runs_positive_value():
    assert parse_max_concurrent_runs("5") == 5
    assert parse_max_concurrent_runs(" 2 ") == 2  # surrounding whitespace tolerated


def test_parse_max_concurrent_runs_negative_or_non_integer_raise():
    # Negative or non-integer is a configuration error → fail loud at startup (a typo must
    # not silently change the cap). Mirrors ANSWER_BACKSTOP_SECONDS's fail-loud posture.
    with pytest.raises(ValueError):
        parse_max_concurrent_runs("-1")
    with pytest.raises(ValueError):
        parse_max_concurrent_runs("x")
    with pytest.raises(ValueError):
        parse_max_concurrent_runs("1.5")


def test_config_default_max_concurrent_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.max_concurrent_runs == 3  # unset → default


def test_config_max_concurrent_runs_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "7")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.max_concurrent_runs == 7


def test_config_bad_max_concurrent_runs_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "-2")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)


# --- D8: RENDER_CHAT_SEND_INTERVAL_SECONDS (the per-chat send budget, P5/T8) ----


def test_parse_chat_send_interval_unset_and_empty_default():
    # Unset / empty / whitespace → the ~1 s default (no new REQUIRED env — main stays
    # runnable). Note: unlike the cap, "0" is a VALID value (disables spacing), so it is
    # NOT folded into the default — see the next test.
    assert parse_chat_send_interval_seconds(None) == 1.0
    assert parse_chat_send_interval_seconds("") == 1.0
    assert parse_chat_send_interval_seconds("   ") == 1.0


def test_parse_chat_send_interval_accepts_zero_and_positive_floats():
    # 0 is a deliberate valid choice (every send goes immediately — fine for a low-traffic
    # deployment), unlike the cap where 0 would deadlock. Positive floats pass through.
    assert parse_chat_send_interval_seconds("0") == 0.0
    assert parse_chat_send_interval_seconds("2.5") == 2.5
    assert parse_chat_send_interval_seconds(" 1 ") == 1.0  # whitespace tolerated


def test_parse_chat_send_interval_negative_or_non_numeric_raise():
    # Negative or non-numeric → fail loud at startup (a typo must not silently change the
    # budget). Mirrors the other numeric keys' fail-loud posture.
    with pytest.raises(ValueError):
        parse_chat_send_interval_seconds("-1")
    with pytest.raises(ValueError):
        parse_chat_send_interval_seconds("x")


def test_config_default_chat_send_interval(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.render_chat_send_interval_seconds == 1.0  # unset → default


def test_config_chat_send_interval_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("RENDER_CHAT_SEND_INTERVAL_SECONDS", "2")
    cfg = Config.from_env(dotenv_path=None)
    assert cfg.render_chat_send_interval_seconds == 2.0


def test_config_bad_chat_send_interval_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("RENDER_CHAT_SEND_INTERVAL_SECONDS", "nope")
    with pytest.raises(ValueError):
        Config.from_env(dotenv_path=None)
