import asyncio
import json
from pathlib import Path

import pytest

from claude_tg.claude_runner import ClaudeBusy, ClaudeRunner
from claude_tg.config import Config
from claude_tg.session_store import JsonSessionStore


def make_config(**kw):
    base = dict(
        bot_token="t",
        allowed_chat_ids=frozenset({1}),
        workdir=Path("/tmp"),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
    )
    base.update(kw)
    return Config(**base)


def ok_json(result="hi", session_id="sess-1"):
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "session_id": session_id,
        }
    )


async def test_first_run_builds_cmd_and_captures_session(monkeypatch):
    runner = ClaudeRunner(make_config())
    captured = {}

    async def fake(cmd, stdin, cwd):
        captured.update(cmd=cmd, stdin=stdin, cwd=cwd)
        return 0, ok_json(result="hello", session_id="S1"), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "do thing")
    assert res.ok and res.text == "hello"
    assert "--dangerously-skip-permissions" in captured["cmd"]
    assert "--output-format" in captured["cmd"] and "json" in captured["cmd"]
    assert "--resume" not in captured["cmd"]
    assert captured["stdin"] == "do thing"
    assert captured["cwd"] == "/tmp"
    assert runner._sessions[1] == "S1"


async def test_second_run_resumes(monkeypatch):
    runner = ClaudeRunner(make_config())
    calls = []

    async def fake(cmd, stdin, cwd):
        calls.append(cmd)
        return 0, ok_json(session_id="S1"), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    await runner.run(1, "first")
    await runner.run(1, "second")
    assert "--resume" not in calls[0]
    assert "--resume" in calls[1] and "S1" in calls[1]


async def test_model_flag(monkeypatch):
    runner = ClaudeRunner(make_config(model="claude-opus-4-8"))
    captured = {}

    async def fake(cmd, stdin, cwd):
        captured["cmd"] = cmd
        return 0, ok_json(), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    await runner.run(1, "x")
    assert "--model" in captured["cmd"] and "claude-opus-4-8" in captured["cmd"]


async def test_skip_permissions_false(monkeypatch):
    runner = ClaudeRunner(make_config(skip_permissions=False))
    captured = {}

    async def fake(cmd, stdin, cwd):
        captured["cmd"] = cmd
        return 0, ok_json(), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    await runner.run(1, "x")
    assert "--dangerously-skip-permissions" not in captured["cmd"]


async def test_error_json(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        return 0, json.dumps(
            {"type": "result", "subtype": "error_during_execution",
             "is_error": True, "result": "boom", "session_id": "S"}
        ), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert not res.ok and "boom" in (res.error or "")


async def test_nonzero_exit_no_json(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        return 1, "", "some stderr"

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert not res.ok and "some stderr" in (res.error or "")


async def test_plain_text_fallback(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        return 0, "just plain text, not json", ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert res.ok and "plain text" in res.text


async def test_timeout(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert not res.ok and "timed out" in (res.error or "").lower()


async def test_binary_not_found(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        raise FileNotFoundError()

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert not res.ok and "not found" in (res.error or "").lower()


async def test_empty_prompt():
    runner = ClaudeRunner(make_config())
    res = await runner.run(1, "   ")
    assert not res.ok


async def test_busy_rejects_concurrent(monkeypatch):
    runner = ClaudeRunner(make_config())
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake(cmd, stdin, cwd):
        started.set()
        await release.wait()
        return 0, ok_json(), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    task = asyncio.create_task(runner.run(1, "first"))
    await started.wait()
    with pytest.raises(ClaudeBusy):
        await runner.run(1, "second")
    release.set()
    await task


def test_reset_clears_session():
    runner = ClaudeRunner(make_config())
    runner._sessions[1] = "S1"
    runner.reset(1)
    assert 1 not in runner._sessions


def test_set_cwd_validates(tmp_path):
    runner = ClaudeRunner(make_config())
    with pytest.raises(NotADirectoryError):
        runner.set_cwd(1, str(tmp_path / "does-not-exist"))
    runner.set_cwd(1, str(tmp_path))
    assert runner.get_cwd(1) == str(tmp_path.resolve())


async def test_persistence_roundtrip(tmp_path, monkeypatch):
    store = JsonSessionStore(tmp_path / "s.json")
    runner = ClaudeRunner(make_config(), session_store=store)

    async def fake(cmd, stdin, cwd):
        return 0, ok_json(session_id="PERSIST"), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    await runner.run(5, "hi")

    runner2 = ClaudeRunner(make_config(), session_store=store)
    assert runner2._sessions[5] == "PERSIST"


async def test_stale_session_retries_fresh(monkeypatch):
    runner = ClaudeRunner(make_config())
    runner._sessions[1] = "OLD"
    calls = []

    async def fake(cmd, stdin, cwd):
        calls.append(cmd)
        if "--resume" in cmd:
            return 1, "", "No conversation found with session ID: OLD"
        return 0, ok_json(result="recovered", session_id="NEW"), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "hi")
    assert res.ok and res.text == "recovered"
    assert runner._sessions[1] == "NEW"
    assert len(calls) == 2
    assert "--resume" in calls[0] and "--resume" not in calls[1]


async def test_genuine_error_does_not_retry(monkeypatch):
    runner = ClaudeRunner(make_config())
    runner._sessions[1] = "S"
    calls = []

    async def fake(cmd, stdin, cwd):
        calls.append(cmd)
        return 0, json.dumps(
            {"type": "result", "subtype": "error_during_execution",
             "is_error": True, "result": "model said no", "session_id": "S"}
        ), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "hi")
    assert not res.ok
    assert len(calls) == 1  # a real content error must NOT trigger a fresh retry


async def test_parse_prefers_result_over_trailing_log(monkeypatch):
    runner = ClaudeRunner(make_config())
    combined = ok_json(result="real answer", session_id="S") + "\n" + json.dumps(
        {"level": "warn", "msg": "stray log line"}
    )

    async def fake(cmd, stdin, cwd):
        return 0, combined, ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert res.ok and res.text == "real answer"
    assert runner._sessions[1] == "S"


async def test_different_chats_not_blocked(monkeypatch):
    runner = ClaudeRunner(make_config())
    started = asyncio.Event()
    gate = asyncio.Event()

    async def fake(cmd, stdin, cwd):
        if stdin == "block":
            started.set()
            await gate.wait()
        return 0, ok_json(), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    t1 = asyncio.create_task(runner.run(1, "block"))
    await started.wait()
    res2 = await runner.run(2, "go")  # different chat must not be blocked by chat 1
    assert res2.ok
    gate.set()
    await t1


async def test_empty_result_is_ok(monkeypatch):
    runner = ClaudeRunner(make_config())

    async def fake(cmd, stdin, cwd):
        return 0, ok_json(result="", session_id="S"), ""

    monkeypatch.setattr(runner, "_invoke", fake)
    res = await runner.run(1, "x")
    assert res.ok and res.text == ""
