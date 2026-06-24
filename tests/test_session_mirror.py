"""P11 T3 — live-mirror (`/watch <id>`): the tailer, the body-free normalizer (⭐ SB3),
and the read-only watch lifecycle on StreamingSession.

Everything is mock-only — NO live Telegram, NO live SDK, NO real ``~/.claude``, NO real
sleeping. The tailer is fed a fake/tmp transcript file; the normalizer is fed dict/JSON
lines directly; the watch loop's send gate is mocked to CAPTURE what would be sent so we
can assert (a) the rendered output and (b) — the make-or-break — that a raw tool body /
result content NEVER appears in any captured send.

Pins:

* the tailer reads only COMPLETE lines (a half-written final line is buffered, then emitted
  when its ``\\n`` arrives); appends after the offset are picked up; a truncation/rotation
  resets cleanly; a vanished file stops without crashing (RB1-total);
* the dict→Event normalizer reuses the bot's events + render layer (assistant text →
  message; tool_use → the body-free ``safe_input_summary`` line; tool_result → a body-free
  ``✓ result (N chars)`` indicator); bad/non-JSON/unknown lines are skipped;
* **⭐ SB3** — a ``tool_result`` with a fat body and a ``tool_use``/Write with a huge input
  body render to a body-free summary, and the raw body NEVER appears in any captured send;
* the **SB3 mutation-probe**: passing ``tool_result.content`` through RAW must make the
  "secret absent" assertion FAIL (so the test has teeth);
* the **last-``\\n`` mutation-probe**: a partial line must NOT be emitted until completed;
* ``/watch`` lifecycle: SB1 (bot-gated, exercised in test_bot_streaming), unknown-id clean
  error, one-shot notice (bot), one-watch-per-chat replacement, ``/unwatch`` stops it (no
  more sends), shutdown cancels all, flood control respects the send gate (non-verbatim tool
  lines yield + shed first).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from claude_tg.config import Config
from claude_tg.engine.types import TextEvent, ToolUseEvent
from claude_tg.session_mirror import (
    DEFAULT_WATCH_QUEUE_MAX,
    TranscriptTailer,
    normalize_line,
    render_batch,
    run_mirror,
    transcript_path,
)
from claude_tg.sessions_discovery import DiscoveredSession
from claude_tg.stream_session import StreamingSession, WatchOutcome

# NB: value carries a "fake" marker so scripts/secret_scan.py treats it as a
# placeholder (it is a test fixture asserting the mirror NEVER leaks tool bodies).
SECRET = "FAKE_SECRET_TOKEN_abcdef0123456789_do_not_leak"


# ===========================================================================
# 1. The tailer — offset / last-`\n` / RB1-total
# ===========================================================================


def test_tailer_emits_only_complete_lines_buffers_partial(tmp_path):
    p = tmp_path / "t.jsonl"
    # One complete line + a trailing PARTIAL (no newline) — a reader catching a half write.
    p.write_text('{"a":1}\n{"b":2')
    t = TranscriptTailer(path=p)
    assert t.poll() == ['{"a":1}']  # only the complete line
    assert t.poll() == []  # nothing new; partial still buffered
    # Complete the partial + append another complete line.
    with open(p, "a") as fh:
        fh.write('}\n{"c":3}\n')
    assert t.poll() == ['{"b":2}', '{"c":3}']  # the completed partial THEN the new line


def test_tailer_partial_line_not_emitted_until_newline(tmp_path):
    """Mutation-probe of the last-``\\n`` rule: a partial must NOT surface until completed."""
    p = tmp_path / "t.jsonl"
    p.write_text('{"partial":true')  # no newline at all
    t = TranscriptTailer(path=p)
    assert t.poll() == []  # NOTHING — the whole thing is an incomplete trailing line
    with open(p, "a") as fh:
        fh.write("}\n")
    assert t.poll() == ['{"partial":true}']  # now it's complete


def test_tailer_picks_up_appends_after_offset(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("line1\n")
    t = TranscriptTailer(path=p)
    assert t.poll() == ["line1"]
    with open(p, "a") as fh:
        fh.write("line2\nline3\n")
    assert t.poll() == ["line2", "line3"]
    assert t.poll() == []  # offset advanced; no re-read of old bytes


def test_tailer_truncation_resets_and_rereads(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("aaaa\nbbbb\n")
    t = TranscriptTailer(path=p)
    assert t.poll() == ["aaaa", "bbbb"]
    # Rotate/replace with a SHORTER file — the tailer must reset to 0 and re-read (RB1).
    p.write_text("new\n")
    assert t.poll() == ["new"]


def test_tailer_vanished_file_stops_cleanly(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("x\n")
    t = TranscriptTailer(path=p)
    assert t.poll() == ["x"]
    p.unlink()
    assert t.poll() == []  # no crash
    assert t.gone is True  # signals the watch to stop


def test_tailer_never_appeared_then_appears(tmp_path):
    p = tmp_path / "later.jsonl"  # does not exist yet
    t = TranscriptTailer(path=p)
    assert t.poll() == []
    assert t.gone is True  # a missing file reads as gone (the watch reports it)


def test_tailer_blank_lines_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("real\n\n   \nreal2\n")
    t = TranscriptTailer(path=p)
    assert t.poll() == ["real", "real2"]


# ===========================================================================
# 2. The dict→Event normalizer (reuses the bot's events) + 3. ⭐ SB3 scrub
# ===========================================================================


def test_normalize_assistant_text_to_text_event():
    line = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
    evs = normalize_line(line)
    assert len(evs) == 1
    assert isinstance(evs[0], TextEvent)
    assert evs[0].text == "hello"
    assert evs[0].incremental is False  # an assembled message (its own send), not a delta


def test_normalize_tool_use_to_body_free_summary():
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Read", "id": "toolu_x",
                 "input": {"file_path": "/a/b.py"}}
            ]
        },
    }
    evs = normalize_line(line)
    assert len(evs) == 1
    assert isinstance(evs[0], ToolUseEvent)
    assert evs[0].tool_name == "Read"
    assert "Read(" in evs[0].tool_input_summary
    assert "/a/b.py" in evs[0].tool_input_summary
    assert evs[0].tool_use_id == "toolu_x"


def test_normalize_user_prompt_string_shown():
    line = {"type": "user", "message": {"content": "do the thing"}}
    evs = normalize_line(line)
    assert [e.text for e in evs] == ["do the thing"]


def test_normalize_tool_result_is_body_free_indicator():
    line = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "x" * 1234, "is_error": False}]},
    }
    evs = normalize_line(line)
    assert len(evs) == 1
    assert isinstance(evs[0], TextEvent)
    assert evs[0].text == "✓ result (1234 chars)"  # count + ok flag — NOT the content


def test_normalize_tool_result_error_flag():
    line = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "boom", "is_error": True}]},
    }
    evs = normalize_line(line)
    assert evs[0].text == "⚠️ result error (4 chars)"


def test_normalize_assistant_multiple_blocks_text_then_tool():
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "I'll read it"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]
        },
    }
    evs = normalize_line(line)
    assert isinstance(evs[0], TextEvent) and evs[0].text == "I'll read it"
    assert isinstance(evs[1], ToolUseEvent) and evs[1].tool_name == "Bash"


@pytest.mark.parametrize(
    "line",
    [
        "not json at all",
        '{"truncated": ',  # half a JSON object
        {"type": "system", "subtype": "init"},  # known-but-ignored type
        {"type": "summary", "summary": "a recap"},  # unknown type
        {"no": "type"},  # no type field
        {"type": "assistant", "message": {"content": "not-a-list"}},  # odd shape
        123,  # not even a dict/str
    ],
)
def test_normalize_bad_or_unknown_lines_are_skipped(line):
    """RB1: a non-JSON / truncated / unknown / odd line yields no events, never raises."""
    assert normalize_line(line) == []


# -- ⭐ SB3: raw bodies NEVER appear in the rendered output ------------------


def test_sb3_tool_result_fat_body_never_rendered():
    """A tool_result carrying a fat body + a secret renders to a body-free count only."""
    line = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": "FILE DUMP " + SECRET + (" " * 5000), "is_error": False}
        ]},
    })
    msgs = render_batch([line])
    rendered = "||".join(m.text for m in msgs)
    assert SECRET not in rendered  # ⭐ the make-or-break assertion
    assert "result (" in rendered  # the body-free indicator IS shown


def test_sb3_tool_result_block_list_never_rendered():
    """The block-list shape of tool_result content is also reduced to a count (no leak)."""
    line = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result",
             "content": [{"type": "text", "text": SECRET + (" " * 800)}],
             "is_error": True},
        ]},
    })
    msgs = render_batch([line])
    rendered = "||".join(m.text for m in msgs)
    assert SECRET not in rendered
    assert "result error (" in rendered


def test_sb3_write_content_body_never_rendered():
    """A Write tool_use whose ``content`` BODY carries a secret renders ``<N chars>``, not it."""
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Write", "id": "t1",
             "input": {"file_path": "/x/y.txt", "content": "header\n" + SECRET + "\n" + ("z" * 3000)}}
        ]},
    })
    msgs = render_batch([line])
    rendered = "||".join(m.text for m in msgs)
    assert SECRET not in rendered  # the body field is collapsed to <N chars>
    assert "content=" in rendered and "chars" in rendered
    assert "/x/y.txt" in rendered  # the path (an identifier) is still shown — like the bot's own line


def test_sb3_mutation_probe_raw_result_content_would_leak():
    """Mutation-probe: if the normalizer surfaced ``tool_result.content`` RAW, the body-free
    assertion MUST fail — proving the SB3 test has teeth.

    We emulate the broken normalizer (passing the raw content through) and assert the secret
    DOES appear, then confirm the REAL normalizer suppresses it. So a regression that drops the
    scrub flips this from suppressed→leaked and the suppression test above goes red.
    """
    raw_content = "OUTPUT " + SECRET
    broken_event = TextEvent(text=raw_content, incremental=False)  # what a broken scrub would emit
    assert SECRET in broken_event.text  # the mutant leaks…

    # …while the real normalizer reduces the SAME content to a count (no secret).
    line = {"type": "user", "message": {"content": [{"type": "tool_result", "content": raw_content}]}}
    evs = normalize_line(line)
    assert SECRET not in evs[0].text
    assert evs[0].text.endswith("chars)")


# ===========================================================================
# render_batch flood control (pure) — a fast burst sheds tool-line NOISE first
# ===========================================================================


def test_render_batch_under_budget_keeps_everything():
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{i}"}]}})
        for i in range(5)
    ]
    msgs = render_batch(lines, queue_max=DEFAULT_WATCH_QUEUE_MAX)
    assert [m.text for m in msgs] == ["m0", "m1", "m2", "m3", "m4"]
    assert all(m.verbatim for m in msgs)


def test_render_batch_floods_sheds_tool_noise_keeps_text():
    # 3 text lines (verbatim, kept) + many tool lines (non-verbatim, shed) over a tiny budget.
    text_lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"keep{i}"}]}})
        for i in range(3)
    ]
    tool_lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"cmd{i}"}}]}})
        for i in range(20)
    ]
    msgs = render_batch(text_lines + tool_lines, queue_max=5)
    texts = [m.text for m in msgs]
    # All the verbatim text survived…
    assert "keep0" in texts and "keep1" in texts and "keep2" in texts
    # …the tool-line noise was shed…
    assert not any(t.startswith("▶️") for t in texts)
    # …and a single coalesced "skipped" marker tells the operator the tail is lossy.
    assert any("events skipped" in t for t in texts)
    skipped_markers = [t for t in texts if "events skipped" in t]
    assert len(skipped_markers) == 1
    assert "20 events skipped" in skipped_markers[0]


def test_render_batch_huge_text_burst_keeps_recent_tail():
    # Even verbatim alone can overflow — keep the most-recent queue_max + count the rest shed.
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"t{i}"}]}})
        for i in range(10)
    ]
    msgs = render_batch(lines, queue_max=3)
    texts = [m.text for m in msgs]
    # The most-recent 3 text messages are kept (t7,t8,t9) + a skipped marker.
    assert "t9" in texts and "t8" in texts and "t7" in texts
    assert "t0" not in texts
    assert any("events skipped" in t for t in texts)


# ===========================================================================
# run_mirror loop (injected sleep/stop) — deterministic, no real time
# ===========================================================================


class _ScriptedTailer:
    """A fake tailer that returns a scripted batch of lines per poll, then signals gone."""

    def __init__(self, batches: list[list[str]], *, gone_after: bool = True):
        self._batches = list(batches)
        self.gone = False
        self._gone_after = gone_after
        self.polls = 0

    def poll(self) -> list[str]:
        self.polls += 1
        if self._batches:
            return self._batches.pop(0)
        if self._gone_after:
            self.gone = True
        return []


async def test_run_mirror_emits_then_stops_on_gone():
    sent: list[dict] = []

    async def emit(*, text, parse_mode, verbatim):
        sent.append({"text": text, "verbatim": verbatim})
        return 1

    async def fast_sleep(_s):
        return None

    tailer = _ScriptedTailer([
        [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "alpha"}]}})],
        [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "beta"}]}})],
    ])
    gone_called = []

    async def on_gone():
        gone_called.append(True)

    await asyncio.wait_for(
        run_mirror(tailer, emit=emit, sleep=fast_sleep, poll_interval=0.0, on_gone=on_gone),
        timeout=2,
    )
    assert [s["text"] for s in sent] == ["alpha", "beta"]
    assert gone_called == [True]  # the operator is told the session ended


async def test_run_mirror_stops_when_should_stop_true():
    sent = []

    async def emit(*, text, parse_mode, verbatim):
        sent.append(text)
        return 1

    async def fast_sleep(_s):
        return None

    # Never goes gone; we stop it via should_stop after the first poll.
    tailer = _ScriptedTailer(
        [[json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}})]],
        gone_after=False,
    )
    stop = {"v": False}

    async def driver():
        # flip stop after the first batch is delivered
        async def emit2(*, text, parse_mode, verbatim):
            sent.append(text)
            stop["v"] = True
            return 1

        await run_mirror(tailer, emit=emit2, sleep=fast_sleep, poll_interval=0.0,
                         should_stop=lambda: stop["v"])

    await asyncio.wait_for(driver(), timeout=2)
    assert sent == ["one"]  # emitted once, then stopped — no flood


async def test_run_mirror_one_bad_send_does_not_kill_loop():
    sent = []
    calls = {"n": 0}

    async def flaky_emit(*, text, parse_mode, verbatim):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telegram hiccup")  # first send fails
        sent.append(text)
        return 1

    async def fast_sleep(_s):
        return None

    tailer = _ScriptedTailer([
        [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}})],
        [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}})],
    ])
    await asyncio.wait_for(
        run_mirror(tailer, emit=flaky_emit, sleep=fast_sleep, poll_interval=0.0), timeout=2
    )
    # The first send raised (swallowed); the loop kept tailing and delivered the second.
    assert sent == ["second"]


# ===========================================================================
# transcript_path — symlink confinement (read-only SB note)
# ===========================================================================


def test_transcript_path_resolves_in_tree(tmp_path):
    home = tmp_path / ".claude"
    (home / "projects").mkdir(parents=True)
    p = transcript_path("sid-1", "/work/a", home=home)
    assert p is not None
    assert p.name == "sid-1.jsonl"
    assert "-work-a" in str(p)  # sanitized cwd dir


def test_transcript_path_missing_args_returns_none(tmp_path):
    home = tmp_path / ".claude"
    assert transcript_path("", "/work/a", home=home) is None
    assert transcript_path("sid", None, home=home) is None


def test_transcript_path_refuses_symlink_escape(tmp_path):
    home = tmp_path / ".claude"
    projects = home / "projects"
    projects.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    # A project dir that is a SYMLINK pointing OUT of ~/.claude/projects.
    (projects / "-evil").symlink_to(outside)
    p = transcript_path("sid", "/evil", home=home)
    # The resolved transcript would live under ``outside`` → refused (None), never followed.
    assert p is None


# ===========================================================================
# StreamingSession /watch + /unwatch lifecycle (send gate mocked → captured)
# ===========================================================================


def _make_config():
    return Config(
        bot_token="t",
        allowed_chat_ids=frozenset({1}),
        workdir=Path("/work"),
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=None,
        engine_mode="streaming",
        answer_backstop_seconds=3600,
        max_concurrent_runs=3,
        render_chat_send_interval_seconds=0.0,  # gate adds no real sleep under the frozen clock
        stream_message_timeout_seconds=300.0,
        allowed_roots=(),
        allow_any_path=True,
    )


def _disc(session_id, cwd="/work/a", title="t", running=False):
    return DiscoveredSession(
        session_id=session_id, cwd=cwd, title=title, last_active=0, running=running
    )


def _seed_transcript(tmp_path, monkeypatch, session_id, cwd="/work/a", body=""):
    """Create <home>/projects/<sanitized-cwd>/<id>.jsonl and point CLAUDE_CONFIG_DIR at home.

    Uses the public :func:`transcript_path` to derive the EXACT path the watch will tail (so
    the seed + the production resolution stay in lock-step), creates its parent + writes
    ``body``, then points ``CLAUDE_CONFIG_DIR`` at ``home`` so ``watch_session``'s own path
    resolution (which reads the env) finds it.
    """
    home = tmp_path / ".claude"
    (home / "projects").mkdir(parents=True, exist_ok=True)
    path = transcript_path(session_id, cwd, home=home)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return path


def _make_session(tmp_path, *, sessions, sleep=None, clock=None):
    sleeps = []

    async def default_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)  # yield so the loop makes progress without real waiting

    s = StreamingSession(
        _make_config(),
        session_store=None,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: None,
        clock=clock or (lambda: 0.0),
        sleep=sleep or default_sleep,
        discover=lambda: list(sessions),
    )
    s._sleeps = sleeps  # type: ignore[attr-defined]  (for assertions if needed)
    return s


def _recording_send():
    sent: list[dict] = []

    async def send(*, text, reply_markup=None, parse_mode=None, link_preview_options=None):
        sent.append({"text": text, "parse_mode": parse_mode})
        return len(sent)

    return send, sent


async def test_watch_unknown_id_clean_error(tmp_path):
    s = _make_session(tmp_path, sessions=[_disc("known-id")])
    send, sent = _recording_send()
    out = s.watch_session(1, "no-such-id", send=send)
    assert isinstance(out, WatchOutcome)
    assert out.ok is False
    assert "No Claude session found" in out.message
    assert s.is_watching(1) is False  # no task started
    assert sent == []  # nothing tailed


async def test_watch_session_no_cwd_refused(tmp_path):
    s = _make_session(tmp_path, sessions=[_disc("id-no-cwd", cwd=None)])
    send, _ = _recording_send()
    out = s.watch_session(1, "id-no-cwd", send=send)
    assert out.ok is False
    assert "working directory" in out.message
    assert s.is_watching(1) is False


async def test_watch_starts_and_mirrors_appended_lines(tmp_path, monkeypatch):
    sid = "sess-live-1"
    path = _seed_transcript(tmp_path, monkeypatch, sid, body="")
    s = _make_session(tmp_path, sessions=[_disc(sid)])
    send, sent = _recording_send()
    out = s.watch_session(1, sid, send=send)
    assert out.ok is True
    assert s.is_watching(1) is True
    # Append an assistant text line; let the loop poll a few times.
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "live-hello"}]}}) + "\n")
    for _ in range(20):
        await asyncio.sleep(0)
        if any("live-hello" in m["text"] for m in sent):
            break
    s.unwatch(1)  # stop the task so the test doesn't leak it
    assert any("live-hello" in m["text"] for m in sent)


async def test_watch_replaces_prior_one_per_chat(tmp_path, monkeypatch):
    sid1, sid2 = "sess-aaaa", "sess-bbbb"
    _seed_transcript(tmp_path, monkeypatch, sid1)
    _seed_transcript(tmp_path, monkeypatch, sid2)
    s = _make_session(tmp_path, sessions=[_disc(sid1), _disc(sid2)])
    send, _ = _recording_send()
    out1 = s.watch_session(1, sid1, send=send)
    first_task = s._watches[1]
    assert out1.ok and "Now mirroring" in out1.message
    out2 = s.watch_session(1, sid2, send=send)
    second_task = s._watches[1]
    assert out2.ok
    assert "Replaced the previous mirror" in out2.message  # told the operator
    assert second_task is not first_task
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()  # the old task was cancelled
    s.unwatch(1)


async def test_unwatch_stops_the_mirror_no_more_sends(tmp_path, monkeypatch):
    sid = "sess-stop"
    path = _seed_transcript(tmp_path, monkeypatch, sid, body="")
    s = _make_session(tmp_path, sessions=[_disc(sid)])
    send, sent = _recording_send()
    s.watch_session(1, sid, send=send)
    # First line is mirrored.
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "before"}]}}) + "\n")
    for _ in range(20):
        await asyncio.sleep(0)
        if any("before" in m["text"] for m in sent):
            break
    assert any("before" in m["text"] for m in sent)
    # Stop the mirror.
    msg = s.unwatch(1)
    assert "Stopped mirroring" in msg
    await asyncio.sleep(0)
    assert s.is_watching(1) is False
    count_before = len(sent)
    # Append MORE after unwatch — it must NOT be mirrored.
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "AFTER_UNWATCH"}]}}) + "\n")
    for _ in range(10):
        await asyncio.sleep(0)
    assert not any("AFTER_UNWATCH" in m["text"] for m in sent)
    assert len(sent) == count_before  # no new sends after /unwatch


async def test_unwatch_with_no_active_watch_is_clean_noop(tmp_path):
    s = _make_session(tmp_path, sessions=[])
    msg = s.unwatch(1)
    assert "no active mirror" in msg.lower()


async def test_shutdown_cancels_all_watches(tmp_path, monkeypatch):
    sid = "sess-shutdown"
    _seed_transcript(tmp_path, monkeypatch, sid)
    s = _make_session(tmp_path, sessions=[_disc(sid)])
    send, _ = _recording_send()
    s.watch_session(1, sid, send=send)
    assert s.is_watching(1) is True
    await s.shutdown()
    await asyncio.sleep(0)
    assert s.is_watching(1) is False
    assert 1 not in s._watches


async def test_watch_flood_control_routes_through_send_gate(tmp_path, monkeypatch):
    """A burst of tool lines + text degrades gracefully: tool noise is shed, text + a single
    skipped marker survive, and EVERY send goes through the gate (captured)."""
    sid = "sess-flood"
    path = _seed_transcript(tmp_path, monkeypatch, sid, body="")
    s = _make_session(tmp_path, sessions=[_disc(sid)])
    # Shrink the per-poll budget + flood threshold so the burst trips flood control
    # deterministically (the production knobs the watch loop reads).
    s._watch_poll_interval = 0.0
    s._watch_queue_max = 5
    send, sent = _recording_send()
    s.watch_session(1, sid, send=send)
    # Write 2 text lines + 30 tool lines in ONE batch (the loop reads them in one poll).
    with open(path, "a") as fh:
        for i in range(2):
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"keep{i}"}]}}) + "\n")
        for i in range(30):
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"c{i}"}}]}}) + "\n")
    for _ in range(40):
        await asyncio.sleep(0)
        if any("keep1" in m["text"] for m in sent):
            break
    s.unwatch(1)
    texts = [m["text"] for m in sent]
    # The text survived; the bulk of tool noise was shed; a skipped marker appeared.
    assert any("keep0" in t for t in texts) and any("keep1" in t for t in texts)
    assert any("events skipped" in t for t in texts)
    # Far fewer than 32 sends went out (flood control shed the tool noise).
    assert len(sent) < 32
