"""P11 T1 — session discovery adapter + composite liveness (no real SDK / ps / ~/.claude).

Every external dependency is INJECTED, so these tests are pure and machine-independent: a
fake session lister, a fake ``ps`` snapshot, a fake process registry, a frozen clock, and a
``tmp_path`` ``~/.claude`` for the transcript-mtime signal. We pin:

* the SDK adapter maps ``SDKSessionInfo`` → ``_RawSession`` and survives a missing/odd SDK (RB1);
* the COMPOSITE liveness — each of the three signals fires INDEPENDENTLY, the OR combines
  them, and the pid-recycling guard validates start time by EPOCH (the TZ-robust check);
* ``discover_sessions`` glues them, marks running/idle correctly, and never crashes when a
  seam fails;
* mutation-probes on the liveness composite (drop a signal → the right sessions go idle) and
  on the recycling guard (a recycled pid must NOT count as live).
"""

from __future__ import annotations

import unittest.mock as mock
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import claude_tg.sessions_discovery as disc_mod
from claude_tg.sessions_discovery import (
    LIVE_MTIME_WINDOW_SECONDS,
    DiscoveredSession,
    SessionDiscovery,
    _ProcInfo,
    _RawSession,
    probe_liveness,
    scan_claude_processes,
    sdk_list_sessions,
    transcript_mtime,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeSDKInfo:
    """Mimics the SDK's ``SDKSessionInfo`` dataclass (attribute-only access)."""

    session_id: str
    summary: str = ""
    last_modified: int = 0
    custom_title: Optional[str] = None
    first_prompt: Optional[str] = None
    git_branch: Optional[str] = None
    cwd: Optional[str] = None


def _raw(session_id="s1", cwd="/work/a", title="t", last_modified=1000, git_branch=None):
    return _RawSession(
        session_id=session_id,
        cwd=cwd,
        title=title,
        last_modified=last_modified,
        git_branch=git_branch,
    )


def _proc(pid, started, command, started_epoch=None):
    return _ProcInfo(pid=pid, started=started, command=command, started_epoch=started_epoch)


# A fixed start instant used across the pid-recycling tests (epoch + matching strings).
_START_EPOCH = datetime(2026, 6, 24, 2, 33, 31).timestamp()
_START_STR = datetime(2026, 6, 24, 2, 33, 31).strftime("%a %b %d %H:%M:%S %Y")


# ---------------------------------------------------------------------------
# 1. The SDK adapter (the only SDK touch point) — mapping + RB1
# ---------------------------------------------------------------------------


def test_sdk_adapter_maps_fields_and_picks_best_title(monkeypatch):
    infos = [
        FakeSDKInfo(session_id="a", custom_title="Custom", first_prompt="fp", summary="sum",
                    last_modified=10, cwd="/x", git_branch="main"),
        FakeSDKInfo(session_id="b", first_prompt="just a prompt", summary="sum2", last_modified=20),
        FakeSDKInfo(session_id="c", summary="only summary", last_modified=30),
    ]

    class FakeSDKModule:
        @staticmethod
        def list_sessions():
            return infos

    # Patch the import target: `from claude_agent_sdk import list_sessions`.
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", FakeSDKModule)
    out = sdk_list_sessions()
    assert [r.session_id for r in out] == ["a", "b", "c"]
    # Title precedence: custom_title → first_prompt → summary.
    assert out[0].title == "Custom"
    assert out[1].title == "just a prompt"
    assert out[2].title == "only summary"
    assert out[0].cwd == "/x" and out[0].git_branch == "main" and out[0].last_modified == 10


def test_sdk_adapter_skips_entry_without_id_and_survives_call_failure(monkeypatch):
    class Boom:
        @staticmethod
        def list_sessions():
            raise RuntimeError("disk on fire")

    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", Boom)
    assert sdk_list_sessions() == []  # RB1: a raising SDK → empty, not a crash

    class WithBadEntry:
        @staticmethod
        def list_sessions():
            return [FakeSDKInfo(session_id=""), FakeSDKInfo(session_id="ok", last_modified=5)]

    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", WithBadEntry)
    out = sdk_list_sessions()
    assert [r.session_id for r in out] == ["ok"]  # the id-less entry is dropped


def test_sdk_adapter_missing_module_returns_empty(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "claude_agent_sdk":
            raise ImportError("no SDK here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert sdk_list_sessions() == []  # RB1 / SB pin: a missing/renamed SDK degrades cleanly


def test_epoch_seconds_normalizes_millis_and_passthrough_seconds():
    # LIVE BUG: the SDK's last_modified is epoch MILLISECONDS (~1.78e12 for 2026); treating it
    # as seconds made every /sessions row read "just now" + broke the recency sort. The adapter
    # normalizes ms → s; an already-seconds value (a hypothetical future SDK) passes through.
    from claude_tg.sessions_discovery import _epoch_seconds

    ms_2026 = 1782300357915  # epoch ms
    assert _epoch_seconds(ms_2026) == 1782300357  # ms → s (÷1000, truncated)
    s_2026 = 1782300357  # already seconds
    assert _epoch_seconds(s_2026) == 1782300357  # passthrough (below the ms threshold)
    # Defensive (RB1): missing / non-numeric / boolean → None (never crash, never mis-sort).
    assert _epoch_seconds(None) is None
    assert _epoch_seconds("nope") is None
    assert _epoch_seconds(True) is None


def test_sdk_adapter_normalizes_last_modified_to_seconds(monkeypatch):
    # End-to-end: a FakeSDKInfo with a millisecond last_modified surfaces as epoch SECONDS on
    # the _RawSession (so relative_age / the sort see seconds, not ms).
    class FakeSDKModule:
        @staticmethod
        def list_sessions():
            return [FakeSDKInfo(session_id="a", last_modified=1782300357915, summary="s")]

    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", FakeSDKModule)
    out = sdk_list_sessions()
    assert out[0].last_modified == 1782300357  # normalized to seconds


# ---------------------------------------------------------------------------
# 2. Transcript path + mtime signal
# ---------------------------------------------------------------------------


def test_transcript_mtime_reads_sanitized_path(tmp_path):
    # The transcript lives at <home>/projects/<sanitized-cwd>/<id>.jsonl with every
    # non-alphanumeric char in the cwd replaced by '-'.
    home = tmp_path / ".claude"
    cwd = "/work/My Proj"
    sanitized = "-work-My-Proj"
    proj = home / "projects" / sanitized
    proj.mkdir(parents=True)
    f = proj / "sid123.jsonl"
    f.write_text("{}")
    mt = transcript_mtime("sid123", cwd, home=home)
    assert mt is not None and abs(mt - f.stat().st_mtime) < 0.001
    # Missing file / missing cwd → None (no signal), never an exception.
    assert transcript_mtime("nope", cwd, home=home) is None
    assert transcript_mtime("sid123", None, home=home) is None


def test_transcript_mtime_degraded_sink_distinguishes_missing_from_errored(tmp_path):
    """P11 T2: a MISSING transcript is a confident no-signal (degraded sink untouched); a
    NON-not-found stat error (PermissionError) flags the sink (we couldn't read the signal)."""
    home = tmp_path / ".claude"
    cwd = "/work/x"
    # 1. Missing transcript → None, and the degraded sink is NOT touched (confident no-signal).
    sink: list[bool] = []
    assert transcript_mtime("gone", cwd, home=home, degraded=sink) is None
    assert sink == []
    # 2. A PermissionError on stat → None AND the sink is flagged (uncertain).
    def deny(self, *a, **k):
        raise PermissionError("denied")

    sink2: list[bool] = []
    with mock.patch.object(disc_mod.Path, "stat", deny):
        assert transcript_mtime("sid", cwd, home=home, degraded=sink2) is None
    assert sink2 == [True]


# ---------------------------------------------------------------------------
# 3. ps scan parsing + helper filtering
# ---------------------------------------------------------------------------


def test_scan_parses_pid_lstart_command_and_filters_helpers():
    canned = "\n".join(
        [
            "  4301 Sun Jun 21 20:05:59 2026 /Users/ray/.vscode/extensions/anthropic.claude-code/native/claude --output-format stream-json --resume abc",
            "24647 Wed Jun 24 02:33:31 2026 claude --continue --dangerously-skip-permissions",
            "22350 Wed Jun 24 02:21:59 2026 /opt/.../claude.exe daemon run --origin transient",  # daemon → filtered
            "  999 Wed Jun 24 02:00:00 2026 /usr/bin/python my_app.py",  # no 'claude' → skipped
            "  111 Wed Jun 24 02:00:00 2026 /repo/claude-telegram-bot/.venv/bin/python -m claude_tg",  # self → filtered
        ]
    )
    procs = scan_claude_processes(runner=lambda: canned)
    pids = {p.pid for p in procs}
    assert pids == {4301, 24647}  # daemon + non-claude + self all filtered out
    p4301 = next(p for p in procs if p.pid == 4301)
    assert p4301.started == "Sun Jun 21 20:05:59 2026"
    assert "stream-json" in p4301.command and p4301.started_epoch is not None


def test_scan_returns_empty_on_runner_failure():
    def boom():
        raise FileNotFoundError("no ps")

    assert scan_claude_processes(runner=boom) == []  # RB1


# ---------------------------------------------------------------------------
# 4. Composite liveness — each signal INDEPENDENTLY, plus the OR
# ---------------------------------------------------------------------------


def test_liveness_signal_a_transcript_mtime(tmp_path):
    home = tmp_path / ".claude"
    cwd = "/work/a"
    proj = home / "projects" / "-work-a"
    proj.mkdir(parents=True)
    (proj / "sid.jsonl").write_text("{}")
    now = (proj / "sid.jsonl").stat().st_mtime + 1  # 1s after the write → within the window
    rs = _raw(session_id="sid", cwd=cwd)
    assert probe_liveness(rs, procs=[], registry=[], now=now, home=home) is True
    # A stale transcript (older than the window) with NO other signal → idle.
    stale_now = now + LIVE_MTIME_WINDOW_SECONDS + 100
    assert probe_liveness(rs, procs=[], registry=[], now=stale_now, home=home) is False


def test_liveness_signal_b_process_argv_resume_id(tmp_path):
    rs = _raw(session_id="abc123", cwd="/no/transcript")
    procs = [_proc(18031, _START_STR, "claude.exe --resume abc123 --model opus")]
    # argv names this id → running, even with no transcript and an empty registry.
    assert probe_liveness(rs, procs=procs, registry=[], now=1e12, home=tmp_path) is True
    # A claude proc bound to a DIFFERENT id does NOT mark this one running.
    other = [_proc(18031, _START_STR, "claude.exe --resume zzz999")]
    assert probe_liveness(rs, procs=other, registry=[], now=1e12, home=tmp_path) is False


def test_liveness_stream_json_without_id_is_not_attributed(tmp_path):
    # A bare stream-json runner with NO explicit id can't be attributed to a specific
    # session — it must not false-positive across all sessions.
    rs = _raw(session_id="abc123", cwd="/no/transcript")
    procs = [_proc(1, _START_STR, "claude --output-format stream-json --verbose")]
    assert probe_liveness(rs, procs=procs, registry=[], now=1e12, home=tmp_path) is False
    # But stream-json WITH a matching id is a strong match.
    procs2 = [_proc(1, _START_STR, "claude --output-format stream-json --resume abc123")]
    assert probe_liveness(rs, procs=procs2, registry=[], now=1e12, home=tmp_path) is True


def test_liveness_signal_c_registry_validated_by_epoch_catches_orchestrator(tmp_path):
    # THE "even this one" case: the live orchestrator runs as bare `claude --continue` with
    # NO id in argv. The process REGISTRY names its sessionId + pid; we validate the pid by
    # EPOCH (startedAt ms) against ps lstart — robust to the registry's procStart string
    # being in a different timezone.
    rs = _raw(session_id="orch-id", cwd="/no/transcript")
    procs = [_proc(24647, _START_STR, "claude --continue --dangerously-skip-permissions",
                   started_epoch=_START_EPOCH)]
    registry = [{
        "pid": 24647,
        "sessionId": "orch-id",
        "startedAt": int(_START_EPOCH * 1000) + 650,  # epoch-ms, ~0.65s rounding skew
        "procStart": "Wed Jun 24 06:33:31 2026",  # WRONG-TZ string (+4h) — must be ignored
    }]
    assert probe_liveness(rs, procs=procs, registry=registry, now=1e12, home=tmp_path) is True


def test_liveness_all_signals_absent_is_idle(tmp_path):
    rs = _raw(session_id="quiet", cwd="/no/transcript")
    assert probe_liveness(rs, procs=[], registry=[], now=1e12, home=tmp_path) is False


# ---------------------------------------------------------------------------
# 5. ⭐ Mutation-probe: the pid-RECYCLING guard (start-time validation is load-bearing)
# ---------------------------------------------------------------------------


def test_recycled_pid_does_not_count_as_live(tmp_path):
    # The registry says session "old-sess" ran as pid 24647 starting at _START_EPOCH. But the
    # pid 24647 ALIVE NOW started at a DIFFERENT time (the pid was recycled to another program)
    # → it must NOT be reported running. This is the os.kill(pid,0) trap the design forbids.
    rs = _raw(session_id="old-sess", cwd="/no/transcript")
    recycled = _proc(24647, "Wed Jun 24 09:00:00 2026",
                     "claude --continue", started_epoch=datetime(2026, 6, 24, 9, 0, 0).timestamp())
    registry = [{"pid": 24647, "sessionId": "old-sess", "startedAt": int(_START_EPOCH * 1000)}]
    assert probe_liveness(rs, procs=[recycled], registry=registry, now=1e12, home=tmp_path) is False
    # Sanity: the SAME registry entry against the MATCHING-start process IS live (proves the
    # guard isn't just always-false).
    matching = _proc(24647, _START_STR, "claude --continue", started_epoch=_START_EPOCH)
    assert probe_liveness(rs, procs=[matching], registry=registry, now=1e12, home=tmp_path) is True


def test_registry_pid_not_in_ps_is_not_live(tmp_path):
    # A registry entry whose pid is NOT in the ps snapshot (process is gone) → idle.
    rs = _raw(session_id="dead", cwd="/no/transcript")
    registry = [{"pid": 55555, "sessionId": "dead", "startedAt": int(_START_EPOCH * 1000)}]
    assert probe_liveness(rs, procs=[], registry=registry, now=1e12, home=tmp_path) is False


# ---------------------------------------------------------------------------
# 6. discover_sessions glue + RB1 across seams
# ---------------------------------------------------------------------------


def test_discover_marks_running_idle_from_injected_seams(tmp_path):
    sessions = [_raw(session_id="run", cwd="/c1"), _raw(session_id="idle", cwd="/c2")]
    procs = [_proc(1, _START_STR, "claude --resume run", started_epoch=_START_EPOCH)]
    disc = SessionDiscovery(
        lister=lambda: sessions,
        proc_scan=lambda: procs,
        registry=lambda: [],
        clock=lambda: 1e12,
        home=tmp_path,
    )
    out = disc.discover()
    assert [(s.session_id, s.running) for s in out] == [("run", True), ("idle", False)]
    assert all(isinstance(s, DiscoveredSession) for s in out)
    # P11 T2: a CLEAN run (every signal executed, no error) is CONFIDENT — not degraded —
    # even for the idle session. A clean negative is a confident continue, not "couldn't tell".
    assert all(s.liveness_degraded is False for s in out)


def test_discover_total_when_lister_raises(tmp_path):
    disc = SessionDiscovery(lister=lambda: (_ for _ in ()).throw(RuntimeError("x")),
                            proc_scan=lambda: [], registry=lambda: [], clock=lambda: 0.0)
    assert disc.discover() == []  # RB1: a broken lister → empty, never raises


def test_discover_total_when_proc_and_registry_raise(tmp_path):
    sessions = [_raw(session_id="s", cwd="/c")]

    def boom():
        raise OSError("scan failed")

    disc = SessionDiscovery(lister=lambda: sessions, proc_scan=boom, registry=boom,
                            clock=lambda: 1e12, home=tmp_path)
    out = disc.discover()
    # The session is still listed; with both liveness scans down it just reads idle.
    assert [(s.session_id, s.running) for s in out] == [("s", False)]
    # P11 T2: but the negative is NOT confident — the scans ERRORED, so liveness is degraded.
    # A write/adopt action reads this and forks on doubt (never co-drives a possibly-live id).
    assert out[0].liveness_degraded is True


def test_discover_scan_failure_with_stale_mtime_is_degraded(tmp_path):
    """P11 T2 (fork-on-doubt): proc_scan + registry both raise and the mtime is stale/absent →
    the session reads idle (running=False) but UNCERTAIN (liveness_degraded=True). This is the
    exact shape attach_session must fork on, distinguished from a clean confident idle."""
    sessions = [_raw(session_id="s", cwd="/nope-no-transcript")]

    def boom():
        raise OSError("scan failed")

    disc = SessionDiscovery(
        lister=lambda: sessions, proc_scan=boom, registry=boom,
        clock=lambda: 1e12, home=tmp_path,  # no transcript under home → mtime absent → idle
    )
    out = disc.discover()
    assert out[0].running is False
    assert out[0].liveness_degraded is True


def test_discover_mtime_permission_error_is_degraded(tmp_path):
    """P11 T2: a NON-not-found transcript stat error (a PermissionError) degrades the per-
    session liveness even when the scans are clean — we could not read the mtime signal. A
    MISSING transcript (FileNotFoundError) would NOT degrade (a confident no-signal)."""
    sessions = [_raw(session_id="s", cwd="/c")]
    # Build the transcript path the probe will stat, then make stat raise PermissionError.
    real_stat = disc_mod.Path.stat

    def deny_stat(self, *a, **k):
        if self.name == "s.jsonl":
            raise PermissionError("denied")
        return real_stat(self, *a, **k)

    disc = SessionDiscovery(
        lister=lambda: sessions, proc_scan=lambda: [], registry=lambda: [],
        clock=lambda: 1e12, home=tmp_path,
    )
    with mock.patch.object(disc_mod.Path, "stat", deny_stat):
        out = disc.discover()
    assert out[0].running is False
    assert out[0].liveness_degraded is True  # the stat ERRORED (not not-found) → uncertain


def test_discover_empty_when_no_sessions():
    disc = SessionDiscovery(lister=lambda: [], proc_scan=lambda: [], registry=lambda: [],
                            clock=lambda: 0.0)
    assert disc.discover() == []


def test_discover_sessions_real_is_total_and_typed():
    # The zero-config entry point must never raise on the real machine (it may or may not find
    # sessions depending on the CI box) and returns DiscoveredSession instances.
    from claude_tg.sessions_discovery import discover_sessions

    out = discover_sessions()
    assert isinstance(out, list)
    assert all(isinstance(s, DiscoveredSession) for s in out)


# ===========================================================================
# P11 T2 / B1 — degradation surfaced from INSIDE the scans (swallowed real failures),
# the single-session re-probe (probe_one), and the SDK-internal dedupe-by-id.
# ===========================================================================

import functools  # noqa: E402

from claude_tg.sessions_discovery import read_process_registry  # noqa: E402


def test_scan_claude_processes_swallowed_failure_flags_degraded():
    """B1: a real ``ps`` failure is swallowed to ``[]`` for the listing, but the ``degraded``
    sink is flagged so the caller knows the proc signal could not be gathered. A CLEAN run
    (ran fine, no claude procs) does NOT flag it (confident negative)."""
    # A runner that raises (ps missing / nonzero / timeout) → [] AND degraded flagged.
    sink: list[bool] = []
    assert scan_claude_processes(runner=lambda: (_ for _ in ()).throw(RuntimeError("ps boom")),
                                 degraded=sink) == []
    assert sink == [True]
    # A clean empty ps (no claude procs) → [] and NOT degraded.
    sink2: list[bool] = []
    assert scan_claude_processes(runner=lambda: "", degraded=sink2) == []
    assert sink2 == []


def test_read_process_registry_corrupt_entry_flags_degraded(tmp_path):
    """B1: a corrupt/unreadable registry entry is skipped for the listing, but flags the
    ``degraded`` sink (a live process's entry might be the unreadable one). A MISSING dir or a
    cleanly-parsed dir does NOT flag it."""
    home = tmp_path / ".claude"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    # One valid entry + one corrupt (non-JSON) entry.
    (sessions / "111.json").write_text('{"sessionId": "s-ok", "pid": 111}')
    (sessions / "222.json").write_text("{ this is not json ")
    sink: list[bool] = []
    out = read_process_registry(home=home, degraded=sink)
    assert any(d.get("sessionId") == "s-ok" for d in out)  # the valid one is returned
    assert sink == [True]  # the corrupt one degraded the signal

    # A MISSING sessions dir → [] and NOT degraded (confident "no registry").
    sink2: list[bool] = []
    assert read_process_registry(home=tmp_path / "no-such", degraded=sink2) == []
    assert sink2 == []


def test_discover_swallowed_scan_failure_is_degraded(tmp_path):
    """⭐ B1 end-to-end: with the PRODUCTION-shaped seams (scan_claude_processes /
    read_process_registry, which accept ``degraded=``) a swallowed ``ps`` failure makes every
    session ``liveness_degraded=True`` even though ``discover``'s own try/except never fired
    (the callable returned ``[]`` rather than raising). This is the path the Codex blocker hit:
    before B1 the swallowed failure read as a CONFIDENT negative → attach co-drove on doubt."""
    sessions = [_raw(session_id="s", cwd="/c")]
    # proc_scan is the REAL scan with a runner that raises internally (swallowed → [] + sink).
    disc = SessionDiscovery(
        lister=lambda: sessions,
        proc_scan=functools.partial(
            scan_claude_processes, runner=lambda: (_ for _ in ()).throw(RuntimeError("ps boom"))
        ),
        registry=lambda: [],
        clock=lambda: 1e12,
        home=tmp_path,
    )
    out = disc.discover()
    assert out[0].running is False
    assert out[0].liveness_degraded is True  # B1: the swallowed ps failure surfaced


def test_probe_one_running_idle_uncertain(tmp_path):
    """probe_one re-runs the composite signals for ONE (id, cwd) → (running, degraded):
    a matching live ``ps`` proc → running; a clean empty scan → confidently idle; a swallowed
    scan failure → uncertain (degraded)."""
    # 1) A ps proc bound to the id → running, not degraded.
    procs_str = f"{1} {_START_STR} claude --resume sess-x"
    disc_live = SessionDiscovery(
        lister=lambda: [], proc_scan=lambda: scan_claude_processes(runner=lambda: procs_str),
        registry=lambda: [], clock=lambda: 1e12, home=tmp_path,
    )
    assert disc_live.probe_one("sess-x", "/c") == (True, False)

    # 2) A clean empty scan (no procs, no registry, no transcript) → confidently idle.
    disc_idle = SessionDiscovery(
        lister=lambda: [], proc_scan=lambda: [], registry=lambda: [],
        clock=lambda: 1e12, home=tmp_path,
    )
    assert disc_idle.probe_one("sess-x", "/c") == (False, False)

    # 3) A swallowed ps failure → uncertain (degraded True). Production-shaped partial.
    disc_unc = SessionDiscovery(
        lister=lambda: [],
        proc_scan=functools.partial(
            scan_claude_processes, runner=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
        registry=lambda: [], clock=lambda: 1e12, home=tmp_path,
    )
    assert disc_unc.probe_one("sess-x", "/c") == (False, True)


def test_probe_one_empty_id_is_uncertain():
    """probe_one with no id can't confirm idle → uncertain (caller forks on doubt)."""
    disc = SessionDiscovery(lister=lambda: [], proc_scan=lambda: [], registry=lambda: [], clock=lambda: 0.0)
    assert disc.probe_one("", "/c") == (False, True)


def test_discover_dedupes_by_session_id(tmp_path):
    """The SDK may return duplicate session ids; discover dedupes by id, keeping the FIRST
    (most-recent) occurrence so a session appears exactly once."""
    sessions = [
        _raw(session_id="dup", cwd="/c1", title="first"),
        _raw(session_id="dup", cwd="/c2", title="second"),  # a later dup — dropped
        _raw(session_id="other", cwd="/c3"),
    ]
    disc = SessionDiscovery(lister=lambda: sessions, proc_scan=lambda: [], registry=lambda: [],
                            clock=lambda: 1e12, home=tmp_path)
    out = disc.discover()
    ids = [s.session_id for s in out]
    assert ids == ["dup", "other"]  # one "dup" row (the first), order preserved
    # The kept row is the FIRST occurrence (cwd /c1).
    dup_row = next(s for s in out if s.session_id == "dup")
    assert dup_row.cwd == "/c1"


# ---- mutation probe (B1): ignoring the swallowed-failure signal -----------

def test_mutation_probe_b1_swallowed_failure_must_degrade(tmp_path):
    """MUTATION PROBE (B1) — a SWALLOWED scan failure MUST degrade liveness. INTACT: the
    production-shaped seam's internal failure surfaces as ``liveness_degraded=True``. If
    someone reverted the scans to swallow failures WITHOUT signalling (the pre-B1 behavior),
    this would read ``liveness_degraded=False`` and FAIL — catching the regression that lets
    attach co-drive a possibly-live session on the strength of a probe that silently failed.
    The CLEAN-empty control proves it's not just always-degrading."""
    sessions = [_raw(session_id="s", cwd="/c")]
    # Swallowed failure → MUST degrade.
    disc_fail = SessionDiscovery(
        lister=lambda: sessions,
        proc_scan=functools.partial(
            scan_claude_processes, runner=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
        registry=lambda: [], clock=lambda: 1e12, home=tmp_path,
    )
    assert disc_fail.discover()[0].liveness_degraded is True, \
        "a swallowed scan failure MUST surface as degraded (the mutation under test)"
    # Control: a CLEAN empty scan must NOT degrade (confident negative).
    disc_clean = SessionDiscovery(
        lister=lambda: sessions,
        proc_scan=functools.partial(scan_claude_processes, runner=lambda: ""),
        registry=lambda: [], clock=lambda: 1e12, home=tmp_path,
    )
    assert disc_clean.discover()[0].liveness_degraded is False, \
        "a clean empty scan is a confident negative (not degraded)"
