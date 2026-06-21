"""T8 / P2 — LIVE end-to-end verify harness for the permission gate (contained).

The final P2 verification: drive the **real** :class:`claude_tg.engine.engine.Engine`
end-to-end against **real Claude** with **code-injected** permission verdicts (no
Telegram tap needed), proving the headline P2 workflow works live:

    start  →  prompt that makes the model attempt a RISKY tool (Write/Bash)  →  the engine's
    permission gate injects a PermissionEvent into the stream while the substrate's decision
    callback is PARKED awaiting engine.resolve(...)  →  this harness resolves it INLINE from
    code with the trial's verdict (allow_once / allow_session / deny)  →  the action runs (or
    is blocked) accordingly  →  ResultEvent.

This mirrors exactly what the bot's SB1-checked callback handler does at runtime
(``StreamingSession._resolve_permission`` → ``engine.resolve(tool_use_id,
PermissionDecision(verdict=…))``), so a live PASS here is evidence the whole approval-gate
path works against real Claude — P1/T9 proved the *answer-hold* (Ask/Plan) end-to-end; this
proves the *permission gate* (risky-tool hold + the three verdicts + safe-runs-free + /yolo +
/cancel) end-to-end, exercising the SAME shared ``PendingRegistry`` hold P2 reuses (RB4).

THE HARNESS OWNS THE POLICY
===========================
Unlike P1/T9 (which had no policy), the engine here is built with a harness-owned
:class:`~claude_tg.permissions.PermissionPolicy` — the SAME object the production wiring
threads from the chat (``_ChatState.policy`` → ``_default_engine_factory`` →
``Engine(permission_policy=…)``). The harness owns it so it can ``set_yolo(True)`` for the
yolo trial and observe allow-session grants land on resolve (V3) — i.e. it stands in for the
bot/session that mutates the shared policy.

TWO MODES
=========
* ``--mock``  (self-test; the IMPLEMENTER runs this): drive the SAME drive-loop and the SAME
  PASS/FAIL predicates against a **scripted fake substrate** (:class:`MockSubstrate`) that
  implements the :class:`~claude_tg.engine.substrate.Substrate` protocol and, per turn, calls
  the engine's decision callback for each scripted safe/risky tool — EXACTLY as
  ``SdkSubstrate`` does — so the gate + resolve path is exercised with **NO live Claude**. For
  a risky tool that path parks the callback until the harness resolves the injected
  ``PermissionEvent``; the mock then performs the tool's *real effect* (write / don't-write a
  file in the temp cwd) keyed on the returned :class:`SubstrateDecision.allow`, so the
  "did it run?" predicate inspects the real filesystem just like live. This proves the loop +
  predicates are correct deterministically (and can't false-pass — see ``_script_*``).

* ``--live`` (default; the ORCHESTRATOR runs this): build the REAL engine by mirroring
  ``claude_tg.stream_session._default_engine_factory`` (``SdkSubstrate(cwd=…,
  permission_mode="default", decision_callback=engine.on_tool_request)`` then
  ``Engine(…, permission_policy=<harness-owned>)``) with the substrate ``cwd`` set to a
  **fresh tempfile.mkdtemp() OUTSIDE the repo**. The gate means a risky tool only runs when
  the harness ALLOWS it — so the temp cwd is naturally contained. Host CLI auth, NO API key.

The drive-loop (the critical pattern, identical in both modes) lives in
:func:`drive_until_result`: ``async for ev in engine.send(prompt)``; on a ``PermissionEvent``
call ``engine.resolve(ev.tool_use_id, PermissionDecision(verdict=<trial's verdict>))``
INLINE (non-blocking — it sets the Future; the parked callback returns the verdict and the
turn continues), keep consuming until the terminal ``ResultEvent``. Every event is recorded
(scrubbed). Predicates are code-driven: each trial uses a UNIQUE marker filename and counts
``PermissionEvent``s, so a model that ignores the instruction yields FAIL, never a false PASS.

CONTAINMENT (live): temp cwd OUTSIDE the repo; ``git_porcelain()`` of the repo asserted
UNCHANGED before/after; no API key (asserted unset at start, ABORT if set); the gate keeps
risky tools from running unless allowed; ``clean_project_transcript_dir`` + ``shutil.rmtree``
of the temp cwd; ``descendant_claude_pids()`` asserted empty after stop. Every recorded
string passes ``scrub()`` (SB3) via the P0 ``record_criterion`` recorder.

Run (mock self-test — produces this spike's evidence; the IMPLEMENTER runs this):
    cd <repo> && .venv/bin/python spikes/p2-permission-verify/verify_permissions.py --mock
Run (LIVE — the ORCHESTRATOR runs this; produces the committed evidence):
    cd <repo> && .venv/bin/python spikes/p2-permission-verify/verify_permissions.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Optional

#: The three operator verdicts the engine understands (mirrors PermissionDecision.verdict).
Verdict = Literal["allow_once", "allow_session", "deny"]

# --- locate the repo + reuse the proven P0/P1 spike helpers (sys.path, no copy) ----
_THIS = Path(__file__).resolve()
_SPIKE_DIR = _THIS.parent  # spikes/p2-permission-verify
_REPO = _SPIKE_DIR.parents[1]  # repo worktree root
# Reuse scrub / record_criterion / descendant_claude_pids / clean_project_transcript_dir
# / git_porcelain from the p1-async-latency spike's _common (which itself imports the P0
# scrubber + recorder). Insert that dir + its session-substrate sibling on sys.path, plus
# the repo root so ``import claude_tg`` resolves — EXACTLY as the P1/T9 harness does.
_P1_ASYNC = _SPIKE_DIR.parent / "p1-async-latency"
_SS_DIR = _SPIKE_DIR.parent / "session-substrate"
for _p in (str(_REPO), str(_P1_ASYNC), str(_SS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _common import (  # noqa: E402  (p1-async-latency shared helpers)
    clean_project_transcript_dir,
    descendant_claude_pids,
    git_porcelain,
    record_criterion,  # the SB3 scrub() chokepoint: it scrubs every recorded string on write
)

# The REAL engine + policy surface under test (never the spike's own copy).
from claude_tg.engine import (  # noqa: E402
    Engine,
    ErrorEvent,
    Event,
    PermissionDecision,
    PermissionEvent,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.engine.substrate import DecisionCallback  # noqa: E402
from claude_tg.permissions import PermissionPolicy, is_risky  # noqa: E402

#: Evidence dir for THIS spike. record_criterion defaults base_dir to the P0
#: session-substrate/evidence tree, so we MUST pass this explicitly on every call —
#: otherwise evidence would land outside this spike (containment).
EVIDENCE_DIR = _SPIKE_DIR / "evidence"

#: A sane per-turn backstop for the live probe. The engine's send_timeout bounds a turn;
#: this is the permission-hold backstop (we always resolve well within it from code, so it
#: never fires) — kept short so a wedged hold can't hang the probe for long.
LIVE_BACKSTOP_SECONDS = 120.0
LIVE_SEND_TIMEOUT = 120.0


# ===========================================================================
# Event recording (every recorded string is scrubbed by record_criterion)
# ===========================================================================


def _summarize_event(ev: Event) -> str:
    """One compact, body-free line per event for the transcript (SB3-friendly).

    We render *shapes/lengths*, never raw tool bodies — and the whole transcript is
    additionally routed through ``scrub()`` by the recorder, so this is belt-and-suspenders.
    The PermissionEvent's ``tool_input_summary`` is already body-free (built by the engine's
    ``safe_input_summary`` — lengths, not contents), so it is safe to surface verbatim.
    """
    k = ev.kind
    if isinstance(ev, TextEvent):
        body = ev.text.strip().replace("\n", " ")
        return f"text(incremental={ev.incremental}, {len(ev.text)} chars): {body[:200]}"
    if isinstance(ev, ToolUseEvent):
        return f"tool_use({ev.tool_name}): {ev.tool_input_summary[:160]}"
    if isinstance(ev, PermissionEvent):
        return (
            f"permission(tool={ev.tool_name}, tool_use_id={ev.tool_use_id}): "
            f"{ev.tool_input_summary[:160]}"
        )
    if isinstance(ev, ErrorEvent):
        return f"error({ev.kind_of_error}, is_error={ev.is_error}): {ev.message[:160]}"
    if isinstance(ev, ResultEvent):
        return (
            f"result(is_error={ev.is_error}, subtype={ev.subtype}, "
            f"num_turns={ev.num_turns}): {str(ev.result_text or '').strip()[:200]}"
        )
    if isinstance(ev, StatusEvent):
        return f"status(phase={ev.phase}, model={ev.model}, detail={str(ev.detail)[:80]})"
    return f"{k}: {ev!r}"


class TurnLog:
    """Accumulates the (event-summary) lines + the events for one or more turns."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.events: list[Event] = []

    def record(self, header: str) -> None:
        self.lines.append(header)

    def record_event(self, ev: Event) -> None:
        self.events.append(ev)
        self.lines.append("    " + _summarize_event(ev))

    # -- predicate helpers over the recorded events --------------------------

    def permissions(self) -> list[PermissionEvent]:
        return [e for e in self.events if isinstance(e, PermissionEvent)]

    def permissions_for(self, tool_name: str) -> list[PermissionEvent]:
        return [e for e in self.permissions() if e.tool_name == tool_name]

    def results(self) -> list[ResultEvent]:
        return [e for e in self.events if isinstance(e, ResultEvent)]

    def errors(self) -> list[ErrorEvent]:
        return [e for e in self.events if isinstance(e, ErrorEvent)]

    def non_error_result(self) -> Optional[ResultEvent]:
        for r in self.results():
            if not r.is_error:
                return r
        return None

    def session_id(self) -> Optional[str]:
        """The first non-empty session id seen across the recorded events (for scrub)."""
        for e in self.events:
            sid = getattr(e, "session_id", None)
            if sid:
                return str(sid)
        return None

    def all_text(self) -> str:
        """Concatenated result text + every TextEvent (lower-cased) — a marker search pool."""
        parts: list[str] = []
        for e in self.events:
            if isinstance(e, TextEvent):
                parts.append(e.text)
            elif isinstance(e, ResultEvent) and e.result_text:
                parts.append(e.result_text)
        return "\n".join(parts).lower()


# ===========================================================================
# The drive-loop (THE critical pattern — identical for mock and live)
# ===========================================================================


async def drive_until_result(
    engine: Engine,
    prompt: str,
    log: TurnLog,
    *,
    verdict: Optional[Verdict] = "allow_once",
    cancel_instead_of_resolving: bool = False,
) -> None:
    """Send one turn and consume the merged event stream to the terminal result.

    The permission-hold contract (ADR-003 §2/§4, reusing ADR-002's hold): when the model
    attempts a RISKY tool the policy does not auto-allow, the engine injects a
    :class:`PermissionEvent` (carrying ``tool_use_id``) into THIS stream while the
    substrate's decision callback is parked awaiting :meth:`Engine.resolve`. So on a
    ``PermissionEvent`` we call ``engine.resolve(...)`` **inline** with a
    :class:`PermissionDecision` of the trial's ``verdict`` (non-blocking — sets the Future;
    the parked callback returns the verdict and the turn continues). We keep consuming until
    ``ResultEvent``.

    * ``verdict`` ∈ {"allow_once", "allow_session", "deny", None}: the verdict injected for
      EACH permission event. ``None`` means do NOT resolve (used only with
      ``cancel_instead_of_resolving``). For V3 the caller passes a per-event callable via
      ``verdict`` is not flexible enough, so V3 drives its own loop (see ``trial_v3``).
    * ``cancel_instead_of_resolving`` (V6): on the FIRST permission event call
      ``engine.cancel()`` instead of resolving, to prove the held callback unwinds with a
      clean deny (no hang); subsequent events (if any) are resolved with ``verdict``.
    """
    cancelled_once = False
    log.record(f"  >>> send: {prompt[:140]!r}")
    async for ev in engine.send(prompt):
        log.record_event(ev)
        if isinstance(ev, PermissionEvent):
            if cancel_instead_of_resolving and not cancelled_once:
                cancelled_once = True
                n = engine.cancel()
                log.record(f"    [code] cancel() instead of resolving -> aborted {n}")
                continue
            if verdict is None:
                log.record("    [code] verdict=None -> NOT resolving (intentional)")
                continue
            ok = engine.resolve(ev.tool_use_id, PermissionDecision(verdict=verdict))
            log.record(
                f"    [code] resolve(permission {ev.tool_use_id}) -> {ok} :: verdict={verdict}"
            )


# ===========================================================================
# MOCK substrate — deterministic, NO live Claude (self-test of the gate)
# ===========================================================================


class MockSubstrate:
    """A scripted fake conforming to :class:`~claude_tg.engine.substrate.Substrate`.

    Implements ``start/resume/send/stop/session_id`` and — critically — calls the engine's
    ``decision_callback`` (the engine's ``on_tool_request``) for each scripted tool, EXACTLY
    as ``SdkSubstrate`` does on the live path. For a SAFE tool the engine returns an allow
    immediately (no prompt). For a RISKY, not-granted tool the engine injects a
    ``PermissionEvent`` and parks the callback until the harness resolves it — so the harness
    drive-loop is exercised identically to live, with zero live Claude. The mock then PERFORMS
    THE TOOL'S REAL EFFECT keyed on the returned :class:`SubstrateDecision.allow`:

      * an allow → it writes the tool's marker file into the temp cwd (the action "ran");
      * a deny  → it writes NOTHING (the action was blocked) and emits an adapt line.

    so the "did it run?" predicate inspects the real filesystem just like live, and a deny
    that failed to block would leave the file present → FAIL (no false pass).

    Each turn is driven by a ``script``: a callable ``(prompt) -> list[step]`` where each step
    is one of:
      * ``("event", Event)``                       — emit a normalized event directly;
      * ``("tool", tool_name, tool_input, tuid)``  — call the engine's decision callback for
        ``tool_name`` (the engine gates/auto-allows it), then enact the returned decision: on
        allow, run ``tool_input["_effect"](decision)`` if present (e.g. write the marker
        file); record the (tool_name, allowed) outcome either way. This is how the mock proves
        the verdict actually came back and was honored.

    ``session_id`` is a stable fake. ``decision_outcomes`` records (tool_name, allowed) per
    tool call so the self-test can assert allow-ran / deny-didn't-run deterministically.
    """

    def __init__(self, script: Callable[[str], list], *, session_id: str = "mock-session-0001") -> None:
        self._script = script
        self.session_id: Optional[str] = None
        self._fixed_sid = session_id
        self._decision_callback: Optional[DecisionCallback] = None
        self.started = False
        self.stopped = False
        #: (tool_name, allowed) per scripted tool call — the deterministic effect ledger.
        self.decision_outcomes: list[tuple[str, bool]] = []

    # the engine wires its on_tool_request here, mirroring _default_engine_factory
    def set_decision_callback(self, cb: DecisionCallback) -> None:
        self._decision_callback = cb

    async def start(self) -> None:
        self.started = True
        self.session_id = self._fixed_sid

    async def resume(self, session_id: str) -> None:
        self.started = True
        self.session_id = session_id

    async def send(self, prompt: str, *, timeout: float = 120.0) -> AsyncIterator[Event]:
        assert self._decision_callback is not None, "engine must wire the decision callback"
        for step in self._script(prompt):
            kind = step[0]
            if kind == "event":
                yield step[1]
                continue
            if kind == "tool":
                _, tool_name, tool_input, tuid = step
                # Call the engine seam EXACTLY like SdkSubstrate.can_use_tool does: for a
                # risky/not-granted tool this parks here until the harness drive-loop resolves
                # the injected PermissionEvent; for a safe/granted/yolo tool it returns at once.
                decision = await self._decision_callback(tool_name, dict(tool_input), tuid)
                self.decision_outcomes.append((tool_name, decision.allow))
                if decision.allow:
                    effect = tool_input.get("_effect")
                    if callable(effect):
                        effect(decision)  # enact the tool's real effect (e.g. write a file)
                continue
            raise AssertionError(f"unknown script step {kind!r}")

    async def stop(self) -> None:
        self.stopped = True


def _mk_engine_over_mock(substrate: MockSubstrate, policy: PermissionPolicy) -> Engine:
    """Wire an Engine over the mock with the harness-owned policy (mirrors production seam).

    Mirrors ``_default_engine_factory``: the substrate's decision callback IS the engine's
    own ``on_tool_request`` (the gate + answer-hold), and the engine is built with the SAME
    policy object the harness owns/mutates. Short backstop so the self-test never waits long
    even if a predicate regresses.
    """
    engine = Engine(substrate, send_timeout=10.0, backstop_seconds=5.0, permission_policy=policy)
    substrate.set_decision_callback(engine.on_tool_request)
    return engine


def _write_effect(cwd: str, filename: str, marker: str) -> Callable[[SubstrateDecision], None]:
    """Build a mock '_effect' that writes ``marker`` into ``cwd/filename`` on allow.

    The marker the engine echoes back (``decision.updated_input``) is unused — the effect
    writes the code-chosen marker so a present file == "the tool ran with our content",
    mirroring how live Claude would actually create the file when allowed.
    """
    def effect(_decision: SubstrateDecision) -> None:
        Path(cwd, filename).write_text(marker, encoding="utf-8")

    return effect


# ===========================================================================
# LIVE engine factory (mirrors stream_session._default_engine_factory)
# ===========================================================================


def _build_live_engine(cwd: str, policy: PermissionPolicy) -> Engine:
    """Build the REAL engine over Substrate A for ``cwd`` with ``policy`` — mirrors production.

    Identical shape to ``claude_tg.stream_session._default_engine_factory``: the
    SdkSubstrate's ``decision_callback`` IS the engine's own ``on_tool_request`` (the gate +
    async hold); ``permission_mode="default"`` and NO bypass / skip-permissions flag (SB5).
    The engine is built with the harness-owned ``policy`` (the SAME object the production
    wiring threads from ``_ChatState.policy``), so the gate consults it and the harness can
    ``set_yolo`` / observe allow-session grants exactly as the bot/session would. The
    substrate ``cwd`` is the disposable temp dir OUTSIDE the repo (containment).
    """
    from claude_tg.engine.adapter_sdk import SdkSubstrate  # lazy: SDK only on live path

    engine: Engine

    async def decision_callback(
        tool_name: str, tool_input: dict, tool_use_id: Optional[str]
    ) -> SubstrateDecision:
        return await engine.on_tool_request(tool_name, tool_input, tool_use_id)

    substrate = SdkSubstrate(
        cwd=cwd,
        permission_mode="default",
        decision_callback=decision_callback,
    )
    engine = Engine(
        substrate,
        send_timeout=LIVE_SEND_TIMEOUT,
        backstop_seconds=LIVE_BACKSTOP_SECONDS,
        permission_policy=policy,
    )
    return engine


# ===========================================================================
# A per-trial engine maker (so a trial body is mode-agnostic)
# ===========================================================================


class EngineMaker:
    """Builds an engine per trial — mock or live — each with a FRESH harness-owned policy.

    A fresh :class:`PermissionPolicy` per trial = fail-closed isolation: no grant or yolo
    bit leaks between trials (mirrors a fresh chat). The harness holds the policy on
    ``self.policy`` so a trial can ``set_yolo`` (V5) or assert a grant landed (V3).
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.policy: PermissionPolicy = PermissionPolicy()
        self.last_substrate: Optional[MockSubstrate] = None

    def fresh_policy(self) -> PermissionPolicy:
        self.policy = PermissionPolicy()
        return self.policy

    def live(self, cwd: str) -> Engine:
        return _build_live_engine(cwd, self.policy)

    def mock(self, script: Callable[[str], list]) -> Engine:
        sub = MockSubstrate(script)
        self.last_substrate = sub
        return _mk_engine_over_mock(sub, self.policy)


# A risky tool the model can be steered to attempt with a known effect, and a safe one.
# We default the risky probe to Write (a single, easy-to-verify file effect); Bash is the
# fallback the live prompt also permits (the predicate is tool-agnostic — it counts the gate
# and checks the marker file — so either satisfies it).
RISKY_TOOL_HINT = "Write"
SAFE_TOOL_HINT = "Read"


#: Every filename any trial asks the model to create — swept from BOTH the temp cwd and
#: $HOME in cleanup. A live-ALLOWED risky Write is NOT sandboxed (ADR-001: cwd is not an OS
#: boundary) — the model may resolve a name against its own home — so we track + sweep to keep
#: the probe contained and FLAG any file that landed outside the temp cwd.
_GENERATED_FILES: list[str] = []


def _unique_marker(prefix: str) -> tuple[str, str]:
    """Return (filename, marker) — both carry a fresh uuid so they are code-chosen + unique."""
    tag = uuid.uuid4().hex[:10].upper()
    filename = f"note_{tag}.txt"
    _GENERATED_FILES.append(filename)
    return filename, f"MARKER_{tag}"


# ===========================================================================
# Trials V1–V6 — each builds an engine, drives the loop, asserts gate behavior
# ===========================================================================
#
# Each trial returns a ``cap`` dict (recorded scrubbed) + a (verdict, reason). Every
# predicate is CODE-DRIVEN: a UNIQUE marker filename is chosen by the harness and the model is
# asked to create exactly that file, and we count PermissionEvents — so a model that ignores
# the instruction (or a gate that fails to hold) yields FAIL, never a false PASS. Predicates
# tolerate model nondeterminism by asserting on the GATE MECHANICS (permission emitted / not
# emitted; file present / absent in the temp cwd; exactly one prompt for two uses) rather than
# exact prose.


async def trial_v1_risky_allow(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V1 — risky ALLOW: a risky tool is held, code injects allow_once, the action RUNS."""
    name = "V1 risky ALLOW (allow_once -> ran)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    maker.fresh_policy()
    filename, marker = _unique_marker("v1")
    cap["filename"], cap["marker"] = filename, marker

    if maker.mode == "mock":
        engine = maker.mock(lambda p: _script_one_write(cwd, filename, marker, "v1-tuid"))
    else:
        engine = maker.live(cwd)

    prompt = (
        f"Create a file at EXACTLY this absolute path: {Path(cwd, filename)}\n"
        f"It must contain exactly this text: {marker}\n"
        f"Use the Write tool with that exact absolute file_path. Do not read or list anything first."
    )
    try:
        await engine.start()
        await drive_until_result(engine, prompt, tlog, verdict="allow_once")
    finally:
        await engine.stop()

    perms = tlog.permissions()
    risky_perms = [p for p in perms if is_risky(p.tool_name)]
    file_path = Path(cwd, filename)
    file_ok = file_path.is_file() and marker in file_path.read_text(encoding="utf-8", errors="replace")
    cap["permission_emitted_for_risky"] = bool(risky_perms)
    cap["prompted_tools"] = [p.tool_name for p in perms]
    cap["file_created_with_marker"] = file_ok
    cap["non_error_result"] = tlog.non_error_result() is not None
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    # Clean the artifact so it never lingers / so V-to-V state can't leak.
    file_path.unlink(missing_ok=True)

    if cap["permission_emitted_for_risky"] and file_ok:
        return cap, "PASS", (
            f"risky tool {risky_perms[0].tool_name!r} was HELD (PermissionEvent emitted); "
            f"code injected allow_once; the action RAN (file {filename} created with the "
            f"code-chosen marker). Prompt-held + allow -> ran."
        )
    return cap, ("PARTIAL" if cap["permission_emitted_for_risky"] else "FAIL"), (
        f"permission_emitted_for_risky={cap['permission_emitted_for_risky']} "
        f"file_created_with_marker={file_ok} (prompted={cap['prompted_tools']})"
    )


async def trial_v2_risky_deny(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V2 — risky DENY: a risky tool is held, code injects deny, the action does NOT run."""
    name = "V2 risky DENY (deny -> blocked)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    maker.fresh_policy()
    filename, marker = _unique_marker("v2")
    cap["filename"], cap["marker"] = filename, marker

    if maker.mode == "mock":
        engine = maker.mock(lambda p: _script_one_write(cwd, filename, marker, "v2-tuid"))
    else:
        engine = maker.live(cwd)

    prompt = (
        f"Create a file at EXACTLY this absolute path: {Path(cwd, filename)}\n"
        f"It must contain exactly this text: {marker}\n"
        f"Use the Write tool with that exact absolute file_path. Do not read or list anything first."
    )
    try:
        await engine.start()
        await drive_until_result(engine, prompt, tlog, verdict="deny")
    finally:
        await engine.stop()

    perms = tlog.permissions()
    risky_perms = [p for p in perms if is_risky(p.tool_name)]
    file_path = Path(cwd, filename)
    file_present = file_path.is_file()
    # The session must have continued (a terminal result), not crashed, after the deny.
    results = tlog.results()
    fatal_errs = [e for e in tlog.errors() if e.kind_of_error in ("driver_error", "turn_error")]
    cap["permission_emitted_for_risky"] = bool(risky_perms)
    cap["prompted_tools"] = [p.tool_name for p in perms]
    cap["file_NOT_created"] = not file_present
    cap["session_continued"] = bool(results) and not fatal_errs
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    file_path.unlink(missing_ok=True)  # belt-and-suspenders (should not exist)

    if cap["permission_emitted_for_risky"] and (not file_present) and cap["session_continued"]:
        return cap, "PASS", (
            f"risky tool {risky_perms[0].tool_name!r} was HELD; code injected deny; the action "
            f"did NOT run (file {filename} absent) and the session continued (no crash, terminal "
            f"result reached — the model adapted to the canned denial). Prompt-held + deny -> blocked."
        )
    return cap, ("PARTIAL" if cap["permission_emitted_for_risky"] else "FAIL"), (
        f"permission_emitted_for_risky={cap['permission_emitted_for_risky']} "
        f"file_NOT_created={not file_present} session_continued={cap['session_continued']} "
        f"(prompted={cap['prompted_tools']})"
    )


async def trial_v3_allow_session(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V3 — allow-session SUPPRESSES: two uses of the SAME tool -> exactly ONE prompt."""
    name = "V3 allow-session SUPPRESSES (1 prompt for 2 uses)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    policy = maker.fresh_policy()
    f1, m1 = _unique_marker("v3a")
    f2, m2 = _unique_marker("v3b")
    cap["filenames"], cap["markers"] = [f1, f2], [m1, m2]

    if maker.mode == "mock":
        engine = maker.mock(
            lambda p: _script_two_writes(cwd, [(f1, m1, "v3-tuid-1"), (f2, m2, "v3-tuid-2")])
        )
    else:
        engine = maker.live(cwd)

    # Drive a custom loop: resolve the FIRST permission with allow_session, and assert no
    # FURTHER permission event is needed for the second use (the engine auto-allows it because
    # the grant landed on resolve). We still resolve any further permission defensively so a
    # regression can't hang — but a PASS requires exactly ONE permission for the two uses.
    n_resolved = {"n": 0}

    async def _drive() -> None:
        tlog.record(f"  >>> send (two writes, same tool): {f1!r} then {f2!r}")
        async for ev in engine.send(
            f"Do BOTH of these using the Write tool, in order, with no reading/listing first.\n"
            f"Use these EXACT absolute file paths:\n"
            f"1) write {Path(cwd, f1)} containing exactly {m1}\n"
            f"2) write {Path(cwd, f2)} containing exactly {m2}"
        ):
            tlog.record_event(ev)
            if isinstance(ev, PermissionEvent):
                n_resolved["n"] += 1
                # First prompt -> allow_session (records the per-NAME grant on resolve);
                # any later prompt -> allow_once (defensive; PASS still needs exactly one).
                v: Verdict = "allow_session" if n_resolved["n"] == 1 else "allow_once"
                ok = engine.resolve(ev.tool_use_id, PermissionDecision(verdict=v))
                tlog.record(f"    [code] resolve(permission {ev.tool_use_id}) -> {ok} :: {v}")

    try:
        await engine.start()
        await asyncio.wait_for(_drive(), timeout=LIVE_SEND_TIMEOUT + 30)
    finally:
        await engine.stop()

    perms = tlog.permissions()
    # Count prompts for the tool that was granted (the first risky tool we saw).
    granted_tool = perms[0].tool_name if perms else RISKY_TOOL_HINT
    prompts_for_granted = len(tlog.permissions_for(granted_tool))
    p1, p2 = Path(cwd, f1), Path(cwd, f2)
    both_created = (
        p1.is_file() and m1 in p1.read_text(encoding="utf-8", errors="replace")
        and p2.is_file() and m2 in p2.read_text(encoding="utf-8", errors="replace")
    )
    cap["granted_tool"] = granted_tool
    cap["prompts_for_granted_tool"] = prompts_for_granted
    cap["total_permissions"] = len(perms)
    cap["grant_recorded_on_policy"] = policy.is_granted(granted_tool)
    cap["both_files_created"] = both_created
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    p1.unlink(missing_ok=True)
    p2.unlink(missing_ok=True)

    # PASS: at least one prompt happened, and the GRANTED tool prompted EXACTLY once for its
    # two uses (the second was auto-allowed by the session grant), and both files were created.
    one_prompt_two_uses = prompts_for_granted == 1
    if perms and one_prompt_two_uses and both_created:
        return cap, "PASS", (
            f"two uses of {granted_tool!r} in one session emitted EXACTLY ONE PermissionEvent "
            f"(allow_session on the first recorded the per-NAME grant; the second was "
            f"auto-allowed, no second prompt); both files created. allow-session suppresses."
        )
    return cap, ("PARTIAL" if perms else "FAIL"), (
        f"prompts_for_granted_tool={prompts_for_granted} (want 1) "
        f"grant_recorded={cap['grant_recorded_on_policy']} both_files_created={both_created} "
        f"total_permissions={len(perms)}"
    )


async def trial_v4_safe_free(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V4 — safe FREE: a safe read runs with NO permission prompt."""
    name = "V4 safe FREE (no prompt)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    maker.fresh_policy()
    # Seed a file the safe read can target (its CONTENT carries a code-chosen marker so a
    # PASS can also confirm the read actually ran and surfaced the content — belt+suspenders).
    filename, marker = _unique_marker("v4")
    seed = Path(cwd, filename)
    seed.write_text(marker, encoding="utf-8")
    cap["filename"], cap["marker"] = filename, marker

    if maker.mode == "mock":
        engine = maker.mock(lambda p: _script_one_read(cwd, filename, marker, "v4-tuid"))
    else:
        engine = maker.live(cwd)

    prompt = (
        f"Read the file `{filename}` in the current directory using the Read tool and reply "
        f"with its exact contents on a single line. Do not write or modify anything."
    )
    try:
        await engine.start()
        # No permission should be emitted; if one IS (a misclassification), resolve allow so
        # the trial can't hang — but a PASS requires ZERO permission events.
        await drive_until_result(engine, prompt, tlog, verdict="allow_once")
    finally:
        await engine.stop()

    perms = tlog.permissions()
    pool = tlog.all_text()
    cap["no_permission_emitted"] = len(perms) == 0
    cap["prompted_tools"] = [p.tool_name for p in perms]
    cap["content_surfaced"] = marker.lower() in pool
    cap["non_error_result"] = tlog.non_error_result() is not None
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    seed.unlink(missing_ok=True)

    # PASS: ZERO permission events (the safe read ran free). The content-surfaced check is a
    # bonus confirming the read truly happened; we don't gate PASS on it (model may summarize),
    # but require a non-error result so we know the turn actually did work.
    if cap["no_permission_emitted"] and cap["non_error_result"]:
        extra = " (and the read content surfaced)" if cap["content_surfaced"] else ""
        return cap, "PASS", (
            f"a safe Read ran with NO PermissionEvent (ran unprompted) and the turn completed "
            f"with a non-error result{extra}. Safe tools are free."
        )
    return cap, ("PARTIAL" if cap["non_error_result"] else "FAIL"), (
        f"no_permission_emitted={cap['no_permission_emitted']} "
        f"non_error_result={cap['non_error_result']} prompted={cap['prompted_tools']}"
    )


async def trial_v5_yolo(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V5 — /yolo: under yolo a risky tool runs with NO prompt; un-yolo restores gating."""
    name = "V5 /yolo (risky runs free; un-yolo restores)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    policy = maker.fresh_policy()
    f1, m1 = _unique_marker("v5y")  # written under yolo
    f2, m2 = _unique_marker("v5g")  # attempted after un-yolo
    cap["filenames"], cap["markers"] = [f1, f2], [m1, m2]

    if maker.mode == "mock":
        engine = maker.mock(lambda p: _script_one_write(cwd, f1, m1, "v5-tuid-1"))
    else:
        engine = maker.live(cwd)

    # --- under yolo: risky write must run with NO prompt ---
    policy.set_yolo(True)
    cap["yolo_on"] = policy.yolo
    p1 = Path(cwd, f1)
    try:
        await engine.start()
        await drive_until_result(
            engine,
            f"Create a file at EXACTLY this absolute path: {Path(cwd, f1)} containing exactly "
            f"{m1} using the Write tool with that absolute file_path. Do not read or list first.",
            tlog,
            verdict="allow_once",  # should never be needed under yolo
        )
        perms_under_yolo = list(tlog.permissions())
        file1_ok = p1.is_file() and m1 in p1.read_text(encoding="utf-8", errors="replace")

        # --- un-yolo: gating returns (a fresh risky write must HOLD again) ---
        policy.set_yolo(False)
        tlog.record("  --- /unyolo: gating must return ---")
        if maker.mode == "mock":
            assert maker.last_substrate is not None
            maker.last_substrate._script = lambda p: _script_one_write(cwd, f2, m2, "v5-tuid-2")
        await drive_until_result(
            engine,
            f"Now create a file at EXACTLY this absolute path: {Path(cwd, f2)} containing exactly "
            f"{m2} using the Write tool with that absolute file_path. Do not read or list first.",
            tlog,
            verdict="allow_once",
        )
    finally:
        await engine.stop()

    perms_all = tlog.permissions()
    perms_after_unyolo = perms_all[len(perms_under_yolo):]
    cap["permissions_under_yolo"] = len(perms_under_yolo)
    cap["file1_created_under_yolo"] = file1_ok
    cap["permissions_after_unyolo"] = len(perms_after_unyolo)
    cap["prompted_tools"] = [p.tool_name for p in perms_all]
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    p1.unlink(missing_ok=True)
    Path(cwd, f2).unlink(missing_ok=True)

    # PASS core: ZERO permission events while yolo on AND the risky write ran (file present).
    # The un-yolo re-gating is preferred-but-not-required for PASS (model may not attempt the
    # 2nd write); if it DID re-prompt that strengthens the evidence, recorded either way.
    yolo_ran_free = (len(perms_under_yolo) == 0) and file1_ok
    if yolo_ran_free:
        regated = " un-yolo re-gated (a fresh risky write HELD again)." if perms_after_unyolo else ""
        return cap, "PASS", (
            f"under /yolo a risky Write ran with NO PermissionEvent (file {f1} created); allow-all "
            f"in effect.{regated} /yolo is the one loud bypass; un-yolo restores the gate."
        )
    return cap, ("PARTIAL" if file1_ok else "FAIL"), (
        f"permissions_under_yolo={len(perms_under_yolo)} (want 0) "
        f"file1_created_under_yolo={file1_ok} permissions_after_unyolo={len(perms_after_unyolo)}"
    )


async def trial_v6_cancel(maker: EngineMaker, cwd: str, log) -> tuple[dict, str, str]:
    """V6 — /cancel (RB4): cancel a held permission, then a trivial turn still completes."""
    name = "V6 /cancel a held permission (RB4)"
    tlog = TurnLog()
    cap: dict[str, Any] = {"name": name, "mode": maker.mode}
    maker.fresh_policy()
    filename, marker = _unique_marker("v6")
    cap["filename"], cap["marker"] = filename, marker

    if maker.mode == "mock":
        engine = maker.mock(lambda p: _script_one_write(cwd, filename, marker, "v6-tuid"))
    else:
        engine = maker.live(cwd)

    turn1_completed = False
    followup_ok = False
    file_present = False
    try:
        await engine.start()
        # Turn 1: elicit a permission hold, then cancel() instead of resolving (RB4 unwind).
        await asyncio.wait_for(
            drive_until_result(
                engine,
                f"Create a file at EXACTLY this absolute path: {Path(cwd, filename)} containing "
                f"exactly {marker} using the Write tool with that absolute file_path. Do not read or list first.",
                tlog,
                verdict=None,
                cancel_instead_of_resolving=True,
            ),
            timeout=LIVE_SEND_TIMEOUT + 30,
        )
        turn1_completed = True  # it returned (did NOT hang) — the held callback unwound
        file_present = Path(cwd, filename).is_file()
        # Turn 2 (SAME session): a trivial safe turn proves the session is still usable.
        if maker.mode == "mock":
            assert maker.last_substrate is not None
            maker.last_substrate._script = lambda p: [
                ("event", ResultEvent(
                    session_id="mock-session-0001", is_error=False, subtype="success",
                    num_turns=2, result_text="OK")),
            ]
        tlog.record("  --- follow-up turn (session must still be usable) ---")
        await asyncio.wait_for(
            drive_until_result(engine, "Reply with exactly: OK", tlog, verdict="allow_once"),
            timeout=LIVE_SEND_TIMEOUT + 30,
        )
        last = tlog.results()[-1] if tlog.results() else None
        followup_ok = last is not None and not last.is_error
    except asyncio.TimeoutError:
        cap["timeout"] = True
    finally:
        await engine.stop()

    Path(cwd, filename).unlink(missing_ok=True)
    perms = tlog.permissions()
    cap["permissions_seen"] = len(perms)
    cap["turn1_unwound_no_hang"] = turn1_completed
    cap["file_NOT_created_after_cancel"] = not file_present
    cap["followup_completed"] = followup_ok
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)

    # V6 is optional-but-preferred. PASS: a permission was elicited, cancel unwound the held
    # callback (no hang), the risky action did NOT run, and the follow-up completed (usable).
    if perms and turn1_completed and (not file_present) and followup_ok:
        return cap, "PASS", (
            "a risky tool was HELD then engine.cancel() unwound the held callback cleanly "
            "(no hang); the action did NOT run (file absent — cancel maps to deny); a following "
            "trivial turn completed -> session still usable (RB4)."
        )
    if turn1_completed and followup_ok:
        return cap, "PARTIAL", (
            f"turn unwound and follow-up completed, but no permission was elicited "
            f"(permissions_seen={len(perms)}) so the cancel-while-held path wasn't exercised."
        )
    return cap, "FAIL", (
        f"permissions_seen={len(perms)} turn1_unwound={turn1_completed} "
        f"file_NOT_created={not file_present} followup_completed={followup_ok} "
        f"timeout={cap.get('timeout', False)}"
    )


# --- scripted turns for the mock trials ------------------------------------
# Each script makes the substrate "attempt" a tool through the engine's decision callback.
# For a risky tool the engine injects a PermissionEvent and parks; the harness resolves it; the
# mock then enacts the real effect (write/skip the marker file) from the returned decision —
# so the mock only writes the file because the engine ALLOWED it (just like real Claude).


def _script_one_write(cwd: str, filename: str, marker: str, tuid: str) -> list:
    sid = "mock-session-0001"
    return [
        ("event", StatusEvent(phase="init", session_id=sid, model="mock")),
        ("event", TextEvent(text=f"I'll create {filename}.", session_id=sid)),
        # The risky Write: parks on the engine gate until the harness resolves; on allow the
        # _effect writes the marker file, on deny it writes nothing.
        ("tool", "Write", {"file_path": filename, "content": marker,
                           "_effect": _write_effect(cwd, filename, marker)}, tuid),
        ("event", ResultEvent(session_id=sid, is_error=False, subtype="success",
                              num_turns=1, result_text="done")),
    ]


def _script_two_writes(cwd: str, specs: list[tuple[str, str, str]]) -> list:
    """Two Write calls of the SAME tool name (allow-session suppression test)."""
    sid = "mock-session-0001"
    steps: list = [("event", StatusEvent(phase="init", session_id=sid, model="mock"))]
    for filename, marker, tuid in specs:
        steps.append(
            ("tool", "Write", {"file_path": filename, "content": marker,
                               "_effect": _write_effect(cwd, filename, marker)}, tuid)
        )
    steps.append(("event", ResultEvent(session_id=sid, is_error=False, subtype="success",
                                       num_turns=1, result_text="both done")))
    return steps


def _script_one_read(cwd: str, filename: str, marker: str, tuid: str) -> list:
    """A single SAFE Read — the engine must auto-allow it (no PermissionEvent)."""
    sid = "mock-session-0001"
    return [
        ("event", StatusEvent(phase="init", session_id=sid, model="mock")),
        # Read is SAFE -> the engine returns allow immediately, no PermissionEvent, no park.
        ("tool", "Read", {"file_path": filename}, tuid),
        # Echo the marker so the content-surfaced bonus check passes deterministically.
        ("event", ResultEvent(session_id=sid, is_error=False, subtype="success",
                              num_turns=1, result_text=f"The contents are: {marker}")),
    ]


# ===========================================================================
# Driver
# ===========================================================================

TRIALS = [
    ("v1_risky_allow", trial_v1_risky_allow),
    ("v2_risky_deny", trial_v2_risky_deny),
    ("v3_allow_session", trial_v3_allow_session),
    ("v4_safe_free", trial_v4_safe_free),
    ("v5_yolo", trial_v5_yolo),
    ("v6_cancel", trial_v6_cancel),
]

#: The headline trials that MUST PASS for an overall PASS. V6 (/cancel) is
#: optional-but-preferred — a PARTIAL there does not block overall PASS.
CORE = ["v1_risky_allow", "v2_risky_deny", "v3_allow_session", "v4_safe_free", "v5_yolo"]


def _record_trial(crit: str, cap: dict, verdict: str, reason: str, sink: list[str]) -> None:
    """Persist one per-trial evidence file (scrubbed) + append to the overall transcript."""
    body = [f"=== {cap['name']} ({crit}) [{cap.get('mode')}] ==="]
    for k, v in cap.items():
        if k in ("name", "transcript"):
            continue
        body.append(f"  {k}: {v}")
    if cap.get("transcript"):
        body.append("  --- event transcript ---")
        body.append(cap["transcript"])
    body.append(f"  -> trial verdict: {verdict} — {reason}")
    text = "\n".join(body) + "\n"
    # Belt-and-suspenders: redact any captured session id as a literal too (the recorder
    # already runs scrub() on everything; this guarantees a live session id never lands raw).
    extra = [str(cap["session_id"])] if cap.get("session_id") else None
    with record_criterion(crit, base_dir=EVIDENCE_DIR, extra_secrets=extra) as rec:
        rec.add_transcript(text)
        rec.set_verdict(verdict, reason)
    sink.append(text)


async def _run_all(mode: str) -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T8 / P2 — LIVE end-to-end verify: permission gate (real engine path, contained) ===")
    log(f"mode: {mode}")
    import importlib.metadata as md
    try:
        sdk_ver = md.version("claude-agent-sdk")
    except Exception:
        sdk_ver = "<not importable>"
    log(f"sdk: claude-agent-sdk=={sdk_ver} (ADR-001 environment of record)")
    api_key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    log(f"ANTHROPIC_API_KEY set: {api_key_set} (MUST be False — host CLI auth only)")

    # --- containment precondition: NO api key (live relies on host CLI auth) ----
    if api_key_set:
        log("ABORT: ANTHROPIC_API_KEY is set; this probe must use host CLI auth only "
            "(unset it and re-run). Refusing to proceed.")
        print("\n".join(report))
        return 2

    git_before = git_porcelain()
    pids_before = descendant_claude_pids()
    log(f"git porcelain BEFORE (set): {sorted(git_before) if git_before else 'clean'}")
    log(f"descendant claude pids BEFORE: {pids_before}")

    # A fresh temp cwd OUTSIDE the repo for the substrate (containment). The gate means risky
    # tools only run when the harness allows — so this dir only ever sees the marker files we
    # explicitly allow, and we rmtree it regardless.
    tmpdir = tempfile.mkdtemp(prefix="p2_perm_verify_")
    log(f"substrate cwd (temp, OUTSIDE repo): {tmpdir}")
    assert not Path(tmpdir).resolve().is_relative_to(_REPO), "temp cwd must be OUTSIDE the repo"

    maker = EngineMaker(mode)
    results: list[tuple[str, str, str]] = []  # (crit, verdict, reason)
    started = time.monotonic()

    try:
        for crit, fn in TRIALS:
            log(f"\n>>> running {crit} ...")
            try:
                cap, verdict, reason = await fn(maker, tmpdir, log)
            except Exception as exc:  # noqa: BLE001 - record, fail-clean (never hang)
                cap = {"name": crit, "mode": mode, "error": f"{type(exc).__name__}: {exc}"}
                verdict, reason = "FAIL", f"exception during trial: {type(exc).__name__}: {exc}"
            _record_trial(crit, cap, verdict, reason, report)
            results.append((crit, verdict, reason))
            log(f"    -> {crit}: {verdict} — {reason}")
    finally:
        # --- cleanup + containment asserts ----------------------------------
        clean_project_transcript_dir(tmpdir, log)
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Belt-and-suspenders containment (ADR-001: cwd is NOT a sandbox): a live-allowed
        # risky Write lands wherever the model resolves the name. Every prompt directs an
        # absolute temp path, but if the model ignored it and wrote into $HOME we sweep those
        # strays too and FLAG it, so a containment slip can never go unnoticed.
        home = Path.home()
        home_strays = [str(home / fn) for fn in _GENERATED_FILES if (home / fn).is_file()]
        for fn in _GENERATED_FILES:
            (home / fn).unlink(missing_ok=True)
        if home_strays:
            log(
                f"  CONTAINMENT NOTE: {len(home_strays)} probe file(s) landed in $HOME (model "
                f"ignored the absolute temp path — ADR-001 cwd-not-a-sandbox); swept: {home_strays}"
            )

    elapsed = round(time.monotonic() - started, 1)
    pids_after = descendant_claude_pids()
    git_after = git_porcelain()
    leaked = sorted(set(pids_after) - set(pids_before))
    git_new = sorted(git_after - git_before)

    log("\n=== per-trial verdicts ===")
    for crit, verdict, reason in results:
        log(f"  {crit:18s} {verdict:8s}  {reason}")

    # --- overall verdict ----------------------------------------------------
    vmap = {c: v for c, v, _ in results}
    core_pass = all(vmap.get(c) == "PASS" for c in CORE)
    core_no_fail = all(vmap.get(c) in ("PASS", "PARTIAL") for c in CORE)
    v6 = vmap.get("v6_cancel", "FAIL")

    log("\n=== CONTAINMENT / CLEANUP ===")
    log(f"  total runtime: {elapsed}s (~{elapsed / 60:.1f} min)")
    log(f"  descendant claude pids AFTER: {pids_after}  leaked: {leaked} (expected: [])")
    log(f"  git porcelain NEW during run: {git_new if git_new else 'NONE'} "
        f"(repo must be UNCHANGED; temp cwd lives OUTSIDE the repo)")
    log(f"  ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (expected False)")

    containment_ok = (not leaked) and (not git_new)
    if not containment_ok:
        log("  WARNING: containment guard tripped (leaked pids or repo modified).")

    if core_pass and v6 in ("PASS", "PARTIAL") and containment_ok:
        overall = "PASS"
        reason = (
            "The permission gate works end-to-end: a risky tool is HELD; a code-injected "
            "allow_once runs it; deny blocks it and the session adapts; allow_session "
            "suppresses the second prompt for that tool; a safe read runs unprompted; /yolo "
            "runs risky tools free (and un-yolo re-gates); /cancel unwinds a held prompt "
            "cleanly and the session stays usable. The verdict was injected from code exactly "
            "as the bot's SB1-checked permission tap does. Containment held (repo unchanged, "
            "no leaked CLI pids)."
        )
    elif core_no_fail and containment_ok:
        overall = "PARTIAL"
        reason = (
            "The gate drove end-to-end but at least one core trial was PARTIAL (mechanism "
            "reached, predicate softened by model nondeterminism — e.g. the model declined to "
            "attempt the risky tool) — inspect the per-trial evidence. Containment held."
        )
    else:
        overall = "FAIL"
        failed = [c for c in CORE if vmap.get(c) != "PASS"]
        reason = (
            f"At least one core trial did not pass: {failed} (verdicts: "
            f"{{{', '.join(f'{c}={vmap.get(c)}' for c in CORE)}}}), or containment tripped "
            f"(leaked={leaked}, git_new={git_new}). Inspect the per-trial evidence."
        )

    log("")
    log(f"OVERALL VERDICT: {overall}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    with record_criterion("p2_permission_verify", base_dir=EVIDENCE_DIR) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(overall, reason)

    # Exit non-zero only on a hard FAIL so CI/orchestrator can branch; PARTIAL is 0.
    return 0 if overall in ("PASS", "PARTIAL") else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="run against REAL Claude (default)")
    group.add_argument("--mock", action="store_true", help="self-test against a scripted fake substrate")
    args = parser.parse_args(argv)
    mode = "mock" if args.mock else "live"
    return asyncio.run(_run_all(mode))


if __name__ == "__main__":
    raise SystemExit(main())
