"""T2 / P3 — LIVE end-to-end verify harness for the skill-launch passthrough (contained).

The final P3 verification: prove the headline workflow — **type `/grill` in Telegram and
the skill runs in the live Claude session** — composes end-to-end. P1 shipped the
interactive relay (AskUserQuestion → buttons / "Other"; the async answer-hold), P2 added
per-tool permission gating, and T1 added ``on_skill_command`` to ``bot.py`` (any
*unregistered* slash-command is forwarded **verbatim** to the active session via the shared
``_run_turn``). T2 proves the **launch path drives the full loop**: that ``/grill`` actually
reaches the engine and runs a real interactive ``/grill`` loop
(AskUserQuestion → answers, file writes gated) to a written doc. ADR-001 C5 already PASSED
(a slash-command skill IS invocable in-session), so this is expected to work.

TWO MODES (mirrors ``spikes/p2-permission-verify/verify_permissions.py``)
========================================================================
* **(1) Launch-smoke (deterministic; runs in ``--mock``; NO live Claude).** The IMPLEMENTER
  runs this. It bridges T1's unit test (the bot boundary — ``on_skill_command`` forwards the
  verbatim text) DOWN to ``engine.send``: it constructs the REAL :class:`TelegramClaudeBot`
  wired to a REAL :class:`~claude_tg.stream_session.StreamingSession` whose engine is built
  over a **recording fake substrate** (:class:`RecordingSubstrate` — a stub conforming to the
  :class:`~claude_tg.engine.substrate.Substrate` protocol whose ``send(prompt, …)`` RECORDS
  the prompt and yields a minimal ``ResultEvent``-terminated stream). It then calls
  ``bot.on_skill_command(<update text "/grill build me X">, ctx)`` and asserts the fake
  substrate's ``send`` received the prompt **VERBATIM** (``"/grill build me X"`` — leading
  ``/`` + args intact). A second assertion confirms a *registered* bot command (``/reset``)
  is NOT forwarded as a skill (it goes to its own ``CommandHandler``, so the substrate sees
  nothing) — proving bot commands still win (D1).

* **(1b) Scripted drive-loop (deterministic; ``--mock``).** Drives the SAME ask→answer→
  permission→result drive-loop the live probe uses, against a scripted fake substrate that —
  exactly like ``SdkSubstrate`` — surfaces an ``AskUserQuestion`` and a risky ``Write``
  through the engine's decision callback. This proves the loop + PASS predicates are correct
  deterministically (an ``AskEvent`` is surfaced; we answer the first option; a
  ``PermissionEvent`` is ``allow_once``'d; a ``ResultEvent`` ends it) with ZERO live Claude,
  and that they can't false-pass (a script that never asks would FAIL the ≥1-ask predicate).

* **(2) Live ``/grill`` loop (``--live``; the ORCHESTRATOR runs this).** Build the REAL
  engine over a real ``SdkSubstrate`` with ``cwd = tempfile.mkdtemp()`` OUTSIDE the repo and a
  harness-owned :class:`~claude_tg.permissions.PermissionPolicy` (mirrors
  ``stream_session._default_engine_factory``). Send ``/grill <concrete idea>`` (leading ``/``
  forwarded verbatim — that IS the point) and drive the loop: on ``AskEvent`` answer with the
  first option of each question; on ``PermissionEvent`` ``allow_once`` (the doc write); on
  ``ResultEvent`` stop. Bounded by an ask-round + wall-clock budget so it can never hang.

THE DRIVE-LOOP (the critical pattern — identical for the scripted and live runs)
================================================================================
:func:`drive_grill_until_result`: ``async for ev in engine.send(prompt)``;

  * ``AskEvent`` → build ``answers = {q["question"]: q["options"][0]["label"] for q in
    ev.questions}`` (pick the FIRST option per question; for a multiSelect one label is fine)
    and ``engine.resolve(ev.tool_use_id, QuestionAnswer(answers=answers))`` INLINE
    (non-blocking — sets the Future; the parked callback returns the native answers-map and
    the turn continues). This is EXACTLY how ``StreamingSession._resolve_ask_option`` resolves
    an ask (``answers_from_ask`` → ``QuestionAnswer`` → ``engine.resolve``).
  * ``PermissionEvent`` → ``engine.resolve(ev.tool_use_id,
    PermissionDecision(verdict="allow_once"))`` so a ``/grill``-written doc is allowed
    (CONTAINED — the sweep below catches any stray). Mirrors
    ``StreamingSession._resolve_permission``.
  * ``ResultEvent`` → the turn is done.

PASS PREDICATES (tolerant of model nondeterminism — assert on MECHANICS, never prose)
=====================================================================================
  * the ``/grill`` command reached the engine and the skill LAUNCHED — i.e. **≥ 1
    ``AskEvent`` was surfaced** (an unregistered slash-command, forwarded verbatim, caused the
    skill to run and drive the relay). THIS is the core proof.
  * the loop ran to a ``ResultEvent`` without hanging after answers were injected.
  * **bonus (recorded, NOT hard-failed if the model varies):** a doc/brief file was written
    under the temp cwd. If it landed outside, the sweep FLAGS it.
  A model that never asks a question, or a launch that never reaches the engine, yields
  **FAIL / PARTIAL — never a false PASS.**

CONTAINMENT / HYGIENE (the load-bearing lesson — mirrors P2 exactly)
====================================================================
Temp cwd via ``mkdtemp()`` OUTSIDE the repo, ``rmtree``'d regardless of outcome. **A
live-ALLOWED write is NOT sandboxed (ADR-001: cwd is not an OS boundary)** — ``/grill`` may
write its brief to an absolute path or ``$HOME`` — so we maintain a **SWEEP set** of the doc
filenames grill is likely to produce and in cleanup sweep BOTH the temp cwd AND ``$HOME`` AND
``~/.claude`` for them, removing + FLAGGING any that landed outside the temp cwd. The live
prompt instructs grill to write its brief to an ABSOLUTE path inside the temp cwd to minimize
leakage. ``git_porcelain()`` of the repo is asserted UNCHANGED before/after;
``clean_project_transcript_dir`` cleans the temp cwd's ``~/.claude/projects/…``;
``descendant_claude_pids()`` is asserted empty after ``engine.stop()``. **Every recorded
string is scrubbed** (SB3) via the P0 ``record_criterion`` recorder. The harness only
**imports** ``claude_tg`` (bot / engine / stream_session) + reuses the p1 ``_common.py``; it
modifies NO production file. Host CLI auth — NO API key (asserted unset at start; ABORT if
set). No ``--dangerously-skip-permissions`` anywhere.

Run (mock self-test — launch-smoke + scripted drive-loop; the IMPLEMENTER runs this):
    cd <repo> && .venv/bin/python spikes/p3-skill-launch-verify/verify_skill_launch.py --mock
Run (LIVE — the ORCHESTRATOR runs this; produces the committed evidence):
    cd <repo> && .venv/bin/python spikes/p3-skill-launch-verify/verify_skill_launch.py --live
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
from typing import Any, AsyncIterator, Callable, Optional

# --- locate the repo + reuse the proven P0/P1 spike helpers (sys.path, no copy) ----
# Mirror the P2 harness's exact insert + import pattern: the p1-async-latency _common
# (which itself imports the P0 scrubber + recorder) plus the session-substrate sibling
# and the repo root so ``import claude_tg`` resolves. We touch NO production file and
# copy nothing.
_THIS = Path(__file__).resolve()
_SPIKE_DIR = _THIS.parent  # spikes/p3-skill-launch-verify
_REPO = _SPIKE_DIR.parents[1]  # repo worktree root
_P1_ASYNC = _SPIKE_DIR.parent / "p1-async-latency"
_SS_DIR = _SPIKE_DIR.parent / "session-substrate"
for _p in (str(_REPO), str(_P1_ASYNC), str(_SS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _common import (  # noqa: E402  (p1-async-latency shared helpers)
    clean_project_transcript_dir,
    descendant_claude_pids,
    git_porcelain,
    record_criterion,  # the SB3 scrub() chokepoint: scrubs every recorded string on write
)

# The REAL surface under test (never the spike's own copy): the engine + its types, the
# permission policy, the substrate protocol's decision-callback alias, the bot + the
# streaming session that the launch-smoke drives.
from claude_tg.bot import TelegramClaudeBot  # noqa: E402
from claude_tg.config import Config  # noqa: E402
from claude_tg.engine import (  # noqa: E402
    AskEvent,
    Engine,
    ErrorEvent,
    Event,
    PermissionDecision,
    PermissionEvent,
    PlanEvent,
    QuestionAnswer,
    ResultEvent,
    StatusEvent,
    SubstrateDecision,
    TextEvent,
    ToolUseEvent,
)
from claude_tg.engine.substrate import DecisionCallback  # noqa: E402
from claude_tg.permissions import PermissionPolicy  # noqa: E402
from claude_tg.stream_session import StreamingSession  # noqa: E402

#: Evidence dir for THIS spike. record_criterion defaults base_dir to the P0
#: session-substrate/evidence tree, so we MUST pass this explicitly on every call —
#: otherwise evidence would land outside this spike (containment).
EVIDENCE_DIR = _SPIKE_DIR / "evidence"

#: Live budgets so the probe can NEVER hang. The engine's send_timeout bounds a turn; the
#: ask-round cap stops a runaway interrogation; the wall-clock cap is the outer backstop. We
#: always resolve from code well within these, so they fire only on a genuine wedge.
LIVE_SEND_TIMEOUT = 180.0
LIVE_BACKSTOP_SECONDS = 180.0
MAX_ASK_ROUNDS = 12  # ≤ 12 ask rounds (per the T2 spec) — a runaway grill is FAILED, not hung
WALL_CLOCK_CAP_SECONDS = 600.0  # outer backstop for the whole live drive-loop

#: The doc/brief filenames ``/grill`` is likely to produce. The live prompt directs grill to
#: write its brief to an ABSOLUTE path inside the temp cwd (the unique name below), but an
#: ALLOWED write is NOT sandboxed (ADR-001: cwd is not an OS boundary), so the model MAY
#: resolve a name against $HOME / ~/.claude. We sweep ALL of these from BOTH the temp cwd AND
#: $HOME AND ~/.claude in cleanup and FLAG any that landed outside the temp cwd. The first
#: entry (a fresh uuid name) is the one we instruct; the rest are grill's common default doc
#: names, swept defensively in case the model picks its own.
_GRILL_DOC_NAME = f"PROJECT_BRIEF_{uuid.uuid4().hex[:10].upper()}.md"
SWEEP_NAMES: list[str] = [
    _GRILL_DOC_NAME,
    "PROJECT_BRIEF.md",
    "design.md",
    "DESIGN.md",
    "brief.md",
    "BRIEF.md",
    "grill.md",
    "GRILL.md",
    "project_brief.md",
]


# ===========================================================================
# Event recording (every recorded string is scrubbed by record_criterion)
# ===========================================================================


def _summarize_event(ev: Event) -> str:
    """One compact, body-free line per event for the transcript (SB3-friendly).

    We render *shapes / lengths / labels*, never raw tool bodies — and the whole transcript
    is additionally routed through ``scrub()`` by the recorder (belt-and-suspenders). For an
    ``AskEvent`` we surface only the question text + option LABELS (the choices the operator
    sees — not secrets), so the evidence shows the relay actually ran. The
    ``PermissionEvent``'s ``tool_input_summary`` is already body-free (built by the engine's
    ``safe_input_summary`` — lengths, not contents), so it is safe verbatim.
    """
    k = ev.kind
    if isinstance(ev, TextEvent):
        body = ev.text.strip().replace("\n", " ")
        return f"text(incremental={ev.incremental}, {len(ev.text)} chars): {body[:160]}"
    if isinstance(ev, ToolUseEvent):
        return f"tool_use({ev.tool_name}): {ev.tool_input_summary[:160]}"
    if isinstance(ev, AskEvent):
        qs = []
        for q in ev.questions:
            opts = ", ".join(str(o.get("label", "")) for o in q.get("options", []))
            qs.append(f"{str(q.get('question', ''))[:80]} -> [{opts[:120]}]")
        return f"ask(tool_use_id={ev.tool_use_id}, {len(ev.questions)} question(s)): " + " | ".join(qs)
    if isinstance(ev, PlanEvent):
        return f"plan(tool_use_id={ev.tool_use_id}, {len(ev.plan)} chars)"
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
            f"num_turns={ev.num_turns}): {str(ev.result_text or '').strip()[:160]}"
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

    def asks(self) -> list[AskEvent]:
        return [e for e in self.events if isinstance(e, AskEvent)]

    def permissions(self) -> list[PermissionEvent]:
        return [e for e in self.events if isinstance(e, PermissionEvent)]

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


# ===========================================================================
# The drive-loop (THE critical pattern — identical for the scripted + live runs)
# ===========================================================================


async def drive_grill_until_result(
    engine: Engine,
    prompt: str,
    log: TurnLog,
    *,
    max_ask_rounds: int = MAX_ASK_ROUNDS,
) -> None:
    """Send one ``/grill`` turn and consume the merged stream to the terminal result.

    Mirrors P2's ``drive_until_result`` but for the **ask/answer + permission** path the
    ``/grill`` loop drives. The hold contract (ADR-002 answer-hold + ADR-003 permission
    gate, one shared ``PendingRegistry``): when the model raises an ``AskUserQuestion`` or
    attempts a risky tool, the engine injects an :class:`AskEvent` / :class:`PermissionEvent`
    (carrying ``tool_use_id``) into THIS stream while the substrate's decision callback is
    parked awaiting :meth:`Engine.resolve`. So:

      * on an :class:`AskEvent` we build the native answers-map by picking the FIRST option of
        each question (``{q["question"]: q["options"][0]["label"]}``) and call
        ``engine.resolve(ev.tool_use_id, QuestionAnswer(answers=…))`` INLINE — the EXACT shape
        ``StreamingSession._resolve_ask_option`` uses (``answers_from_ask`` →
        ``QuestionAnswer`` → ``engine.resolve``). Non-blocking: it sets the Future, the parked
        callback returns the answers-map, the turn continues.
      * on a :class:`PermissionEvent` we ``engine.resolve(ev.tool_use_id,
        PermissionDecision(verdict="allow_once"))`` so a ``/grill``-written brief is allowed
        (mirrors ``StreamingSession._resolve_permission``).
      * a :class:`PlanEvent` (grill may propose a plan) is approved so the loop proceeds.

    Bounded by ``max_ask_rounds`` (a runaway interrogation is FAILED, never hung) — exceeding
    it cancels the engine so the held callback unwinds cleanly and ``send`` returns.
    """
    ask_rounds = 0
    log.record(f"  >>> send: {prompt[:160]!r}")
    async for ev in engine.send(prompt):
        log.record_event(ev)
        if isinstance(ev, AskEvent):
            ask_rounds += 1
            if ask_rounds > max_ask_rounds:
                n = engine.cancel()
                log.record(
                    f"    [code] ask-round budget ({max_ask_rounds}) exceeded -> cancel() "
                    f"aborted {n} pending (FAIL-clean, no hang)"
                )
                continue
            # Pick the FIRST option of EACH question -> the native answers-map keyed by
            # verbatim question text (the proven C3 path; QuestionAnswer rides the allow
            # channel as updated_input["answers"]). For a multiSelect a single label is fine.
            answers: dict[str, str] = {}
            for q in ev.questions:
                options = q.get("options") or []
                if not options:
                    continue
                answers[str(q.get("question", ""))] = str(options[0].get("label", ""))
            ok = engine.resolve(ev.tool_use_id, QuestionAnswer(answers=answers))
            log.record(
                f"    [code] resolve(ask {ev.tool_use_id}) -> {ok} :: "
                f"answered first option of {len(answers)} question(s)"
            )
        elif isinstance(ev, PermissionEvent):
            ok = engine.resolve(ev.tool_use_id, PermissionDecision(verdict="allow_once"))
            log.record(
                f"    [code] resolve(permission {ev.tool_use_id}, tool={ev.tool_name}) -> {ok} "
                f":: verdict=allow_once (CONTAINED; sweep catches any stray)"
            )
        elif isinstance(ev, PlanEvent):
            # grill may propose a plan before writing — approve so the loop proceeds.
            from claude_tg.engine import PlanVerdict  # local import: keeps the top tidy

            ok = engine.resolve(ev.tool_use_id, PlanVerdict(approve=True))
            log.record(f"    [code] resolve(plan {ev.tool_use_id}) -> {ok} :: approved")


# ===========================================================================
# (1) Launch-smoke — deterministic; the REAL bot + StreamingSession over a
#     RECORDING fake substrate. Proves /grill reaches engine.send VERBATIM.
# ===========================================================================


class RecordingSubstrate:
    """A recording fake conforming to :class:`~claude_tg.engine.substrate.Substrate`.

    Implements ``start/resume/send/stop/session_id`` and — critically — its ``send`` RECORDS
    every prompt it is handed, then yields a minimal ``ResultEvent``-terminated stream. It
    does NOT call the decision callback (no tool use in the launch-smoke); the point of THIS
    fake is purely the *prompt-arrival* assertion: that the verbatim slash-command text the
    bot forwarded actually reaches ``Substrate.send`` (one hop below ``engine.send``). The
    scripted drive-loop below uses a DIFFERENT fake (:class:`ScriptedGrillSubstrate`) that
    exercises the ask/permission path.
    """

    def __init__(self, *, session_id: str = "rec-session-0001") -> None:
        self.session_id: Optional[str] = None
        self._fixed_sid = session_id
        self._decision_callback: Optional[DecisionCallback] = None
        self.started = False
        self.stopped = False
        #: Every prompt handed to ``send`` — the launch-smoke asserts on this VERBATIM.
        self.sent_prompts: list[str] = []

    def set_decision_callback(self, cb: DecisionCallback) -> None:
        self._decision_callback = cb

    async def start(self) -> None:
        self.started = True
        self.session_id = self._fixed_sid

    async def resume(self, session_id: str) -> None:
        self.started = True
        self.session_id = session_id

    async def send(self, prompt: str, *, timeout: float = 120.0) -> AsyncIterator[Event]:
        self.sent_prompts.append(prompt)  # record VERBATIM — the launch-smoke's whole point
        yield ResultEvent(
            session_id=self._fixed_sid, is_error=False, subtype="success",
            num_turns=1, result_text="ok",
        )

    async def stop(self) -> None:
        self.stopped = True


async def launch_smoke(log) -> tuple[dict, str, str]:
    """(1) Prove ``on_skill_command`` forwards ``/grill …`` down to ``engine.send`` VERBATIM.

    Build the REAL :class:`TelegramClaudeBot` over a REAL :class:`StreamingSession` whose
    engine is built (via the injected ``engine_factory``) over a :class:`RecordingSubstrate`.
    Drive ``bot.on_skill_command(<update text "/grill build me X">, ctx)`` and assert the fake
    substrate's ``send`` received ``"/grill build me X"`` — leading ``/`` + args intact. This
    is the bridge from T1's unit test (the bot boundary) down to ``engine.send``: T1 asserted
    the bot forwards verbatim to ``StreamingSession.handle_message``; THIS asserts that text
    reaches the substrate one hop below ``engine.send``.

    Also asserts a *registered* bot command (``/reset``) does NOT reach the substrate as a
    skill (bot commands win — D1): we route a ``/reset`` through the bot's own
    ``cmd_reset`` and confirm the recording substrate saw no new prompt.
    """
    name = "launch-smoke (/grill -> engine.send verbatim; bot command wins)"
    cap: dict[str, Any] = {"name": name, "mode": "mock"}

    # A captured RecordingSubstrate so we can read back what reached send(). The engine is
    # built EXACTLY as production does (decision_callback IS engine.on_tool_request), only the
    # substrate is the recording fake.
    captured: dict[str, RecordingSubstrate] = {}

    def factory(*, cwd: str, backstop_seconds: float, permission_policy: PermissionPolicy) -> Engine:
        engine: Engine

        async def decision_callback(
            tool_name: str, tool_input: dict, tool_use_id: Optional[str]
        ) -> SubstrateDecision:
            return await engine.on_tool_request(tool_name, tool_input, tool_use_id)

        sub = RecordingSubstrate()
        sub.set_decision_callback(decision_callback)
        captured["sub"] = sub
        engine = Engine(
            sub,
            send_timeout=10.0,
            backstop_seconds=5.0,
            permission_policy=permission_policy,
        )
        return engine

    config = Config(
        bot_token="t",
        allowed_chat_ids=frozenset({1}),
        workdir=Path(tempfile.gettempdir()),  # never used for I/O here; the fake records only
        engine_mode="streaming",
    )
    streaming = StreamingSession(config, engine_factory=factory)
    # The one-shot runner is unused in streaming mode but the bot requires one; a stub is fine.
    bot = TelegramClaudeBot(config, _StubRunner(), streaming=streaming)

    # --- drive /grill through the REAL passthrough handler -----------------
    grill_text = "/grill build me X"
    sends: list[dict] = []

    update = _FakeUpdate(chat_id=1, text=grill_text, sends=sends)
    ctx = _FakeContext()
    await bot.on_skill_command(update, ctx)

    sub = captured.get("sub")
    sent = list(sub.sent_prompts) if sub is not None else []
    cap["substrate_send_prompts"] = sent
    cap["expected_prompt"] = grill_text
    forwarded_verbatim = sent == [grill_text]
    cap["forwarded_verbatim"] = forwarded_verbatim

    # --- bot command WINS: /reset must NOT reach the substrate as a skill ---
    # Route /reset through the bot's OWN command handler (cmd_reset). It resets the session
    # (drops the engine) and replies; it must NOT forward "/reset" to engine.send. Because
    # reset() drops the engine, a fresh RecordingSubstrate is built on the next turn — so we
    # assert by confirming /reset never landed in ANY substrate's sent_prompts. We re-read the
    # SAME captured substrate (cmd_reset does not start a turn, so no new substrate is built).
    update_reset = _FakeUpdate(chat_id=1, text="/reset", sends=sends)
    await bot.cmd_reset(update_reset, _FakeContext())
    sub_after = captured.get("sub")
    sent_after = list(sub_after.sent_prompts) if sub_after is not None else []
    cap["substrate_send_prompts_after_reset"] = sent_after
    # /reset reached cmd_reset (it replied) and did NOT forward "/reset" to the substrate.
    reset_replied = any("fresh" in str(s.get("text", "")).lower() for s in sends)
    bot_command_won = ("/reset" not in sent_after) and reset_replied
    cap["bot_command_won"] = bot_command_won
    cap["reset_replied"] = reset_replied

    cap["transcript"] = "\n".join(
        [f"  on_skill_command({grill_text!r}) -> substrate.send saw: {sent!r}"]
        + [f"  cmd_reset('/reset') -> replied={reset_replied}; substrate saw: {sent_after!r}"]
    )

    if forwarded_verbatim and bot_command_won:
        return cap, "PASS", (
            f"on_skill_command forwarded {grill_text!r} VERBATIM (leading / + args intact) all "
            f"the way down to Substrate.send (one hop below engine.send) — the launch path "
            f"composes the bot→StreamingSession→Engine→Substrate chain; and a registered bot "
            f"command (/reset) was handled by its own CommandHandler and NOT forwarded as a "
            f"skill (bot commands win, D1)."
        )
    return cap, "FAIL", (
        f"forwarded_verbatim={forwarded_verbatim} (substrate saw {sent!r}; expected "
        f"[{grill_text!r}]) bot_command_won={bot_command_won} (reset_replied={reset_replied}, "
        f"substrate_after_reset={sent_after!r})"
    )


class _StubRunner:
    """Minimal one-shot runner stub (unused in streaming mode; the bot requires one)."""

    async def run(self, chat_id: int, text: str):  # pragma: no cover - not reached in streaming
        raise AssertionError("one-shot runner must not be used in streaming mode")

    def reset(self, chat_id: int) -> None:
        pass

    def get_cwd(self, chat_id: int) -> str:
        return str(tempfile.gettempdir())

    def set_cwd(self, chat_id: int, path: str) -> str:
        return path


class _FakeMessage:
    """A minimal stand-in for ``update.message`` recording reply_text calls."""

    def __init__(self, text: str, sends: list[dict]) -> None:
        self.text = text
        self._sends = sends

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self._sends.append({"text": text, **kwargs})


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    """A minimal stand-in for ``telegram.Update`` at the handler-method level.

    Carries ``effective_chat.id`` + a ``message`` with ``text`` and an async ``reply_text``,
    enough for ``on_skill_command`` / ``cmd_reset`` (which only read those). No PTB routing is
    involved here — the launch-smoke calls the handler methods directly (T1's unit tests prove
    PTB's first-match-wins routing against the REAL handler list; this proves the *delivery*
    down to the substrate).
    """

    def __init__(self, chat_id: int, text: str, sends: list[dict]) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage(text, sends)
        self.effective_message = self.message


class _FakeContext:
    """A minimal ``ContextTypes.DEFAULT_TYPE`` stand-in: a ``bot`` with the async I/O the
    streaming send/edit closures call, plus ``args``."""

    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.args: list[str] = []


class _FakeBot:
    def __init__(self) -> None:
        self._mid = 0

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None, parse_mode=None):
        self._mid += 1
        return type("Msg", (), {"message_id": self._mid})()

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, parse_mode=None):
        return None

    async def send_chat_action(self, *, chat_id: int, action) -> None:
        return None


# ===========================================================================
# (1b) Scripted drive-loop — deterministic; a scripted fake substrate that
#      surfaces an ask + a risky Write through the engine's decision callback,
#      exactly like SdkSubstrate. Proves the drive-loop + predicates.
# ===========================================================================


class ScriptedGrillSubstrate:
    """A scripted fake conforming to :class:`Substrate` that simulates a tiny ``/grill`` loop.

    Per turn it: emits a status + some prose, raises ONE ``AskUserQuestion`` through the
    engine's decision callback (parking until the harness answers it — exactly as
    ``SdkSubstrate`` does), then attempts a risky ``Write`` (parking until allowed), then —
    if the Write was allowed — performs the write's real effect (creating the brief file in
    the temp cwd), then emits a terminal ``ResultEvent``. This drives the SAME
    ask→answer→permission→result loop the live probe drives, with ZERO live Claude, so the
    drive-loop + PASS predicates are exercised deterministically and provably can't false-pass
    (a script with no ask would FAIL the ≥1-ask predicate).

    The AskUserQuestion's ``answers`` map (the engine's mapped result) is observed via the
    returned :class:`SubstrateDecision.updated_input` so we can confirm the native answers
    actually rode back. The Write only happens if ``decision.allow`` — exactly like the real
    substrate honoring the operator's verdict.
    """

    def __init__(self, *, cwd: str, doc_name: str, session_id: str = "grill-session-0001") -> None:
        self._cwd = cwd
        self._doc_name = doc_name
        self.session_id: Optional[str] = None
        self._fixed_sid = session_id
        self._decision_callback: Optional[DecisionCallback] = None
        self.started = False
        self.stopped = False
        #: Observability for the predicates / evidence.
        self.ask_answers_seen: Optional[dict] = None
        self.write_allowed: Optional[bool] = None

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
        sid = self._fixed_sid
        yield StatusEvent(phase="init", session_id=sid, model="scripted")
        yield TextEvent(text="Let me interrogate this project idea.", session_id=sid)

        # 1) Raise an AskUserQuestion through the decision callback. The engine injects the
        #    AskEvent onto the stream and PARKS here until the harness drive-loop resolves it.
        ask_input = {
            "questions": [
                {
                    "question": "What kind of app is this?",
                    "header": "Scope",
                    "options": [
                        {"label": "CLI tool", "description": "a command-line app"},
                        {"label": "Web app", "description": "a browser app"},
                    ],
                    "multiSelect": False,
                }
            ]
        }
        ask_decision = await self._decision_callback("AskUserQuestion", ask_input, "grill-ask-1")
        # The engine maps the QuestionAnswer onto the allow channel as updated_input["answers"].
        self.ask_answers_seen = (ask_decision.updated_input or {}).get("answers")
        yield TextEvent(
            text=f"Got it: {self.ask_answers_seen}. Writing the brief.", session_id=sid
        )

        # 2) Attempt a risky Write through the decision callback (parks until allow_once).
        doc_path = str(Path(self._cwd, self._doc_name))
        write_input = {"file_path": doc_path, "content": "# Project Brief\n\nA tiny CLI todo app.\n"}
        write_decision = await self._decision_callback("Write", write_input, "grill-write-1")
        self.write_allowed = write_decision.allow
        if write_decision.allow:
            # Enact the write's REAL effect — exactly as live Claude would on allow.
            Path(doc_path).write_text(write_input["content"], encoding="utf-8")

        yield ResultEvent(
            session_id=sid, is_error=False, subtype="success", num_turns=1,
            result_text="Brief written.",
        )

    async def stop(self) -> None:
        self.stopped = True


async def scripted_drive_loop(cwd: str, log) -> tuple[dict, str, str]:
    """(1b) Drive the ask→answer→permission→result loop against a scripted fake substrate.

    Builds a REAL :class:`Engine` over :class:`ScriptedGrillSubstrate` with a harness-owned
    policy (mirrors the production seam: the substrate's decision callback IS the engine's
    ``on_tool_request``), then runs :func:`drive_grill_until_result`. Asserts the SAME
    predicates the live probe asserts: ≥ 1 ``AskEvent`` surfaced (skill launched + relay
    drove), a non-error ``ResultEvent`` reached (no hang), and the doc was written under the
    temp cwd (bonus). Also confirms the answers-map actually rode back to the substrate (the
    native-answer round-trip) and the Write only happened because we allowed it.
    """
    name = "scripted drive-loop (ask -> answer -> permission allow_once -> result -> doc)"
    cap: dict[str, Any] = {"name": name, "mode": "mock"}
    tlog = TurnLog()
    policy = PermissionPolicy()
    doc_name = _GRILL_DOC_NAME

    engine: Engine

    async def decision_callback(
        tool_name: str, tool_input: dict, tool_use_id: Optional[str]
    ) -> SubstrateDecision:
        return await engine.on_tool_request(tool_name, tool_input, tool_use_id)

    sub = ScriptedGrillSubstrate(cwd=cwd, doc_name=doc_name)
    sub.set_decision_callback(decision_callback)
    engine = Engine(sub, send_timeout=10.0, backstop_seconds=5.0, permission_policy=policy)

    prompt = "/grill a tiny CLI todo app in Python"
    try:
        await engine.start()
        await asyncio.wait_for(
            drive_grill_until_result(engine, prompt, tlog), timeout=30.0
        )
    finally:
        await engine.stop()

    asks = tlog.asks()
    perms = tlog.permissions()
    doc_path = Path(cwd, doc_name)
    doc_written = doc_path.is_file()
    cap["ask_events"] = len(asks)
    cap["permission_events"] = len(perms)
    cap["answers_rode_back_to_substrate"] = sub.ask_answers_seen
    cap["write_allowed"] = sub.write_allowed
    cap["doc_written_in_temp_cwd"] = doc_written
    cap["non_error_result"] = tlog.non_error_result() is not None
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)
    doc_path.unlink(missing_ok=True)  # clean the scripted artifact

    ask_surfaced = len(asks) >= 1
    ran_to_result = tlog.non_error_result() is not None
    answers_ok = bool(sub.ask_answers_seen)  # the native answers-map rode back on allow
    if ask_surfaced and ran_to_result and doc_written and answers_ok:
        return cap, "PASS", (
            f"the scripted /grill loop surfaced {len(asks)} AskEvent(s) (relay drove), the "
            f"first option of each was answered (answers rode back to the substrate as "
            f"{sub.ask_answers_seen!r}), the risky Write was allow_once'd ({len(perms)} "
            f"PermissionEvent), and the loop ran to a non-error ResultEvent with the brief "
            f"written under the temp cwd. The ask→answer→permission→result drive-loop + "
            f"predicates are correct and can't false-pass (no ask -> FAIL)."
        )
    return cap, ("PARTIAL" if ask_surfaced and ran_to_result else "FAIL"), (
        f"ask_events={len(asks)} (want >=1) non_error_result={ran_to_result} "
        f"doc_written={doc_written} answers_rode_back={answers_ok} permission_events={len(perms)}"
    )


# ===========================================================================
# LIVE engine factory (mirrors stream_session._default_engine_factory)
# ===========================================================================


def _build_live_engine(cwd: str, policy: PermissionPolicy) -> Engine:
    """Build the REAL engine over Substrate A for ``cwd`` with ``policy`` — mirrors production.

    Identical shape to ``claude_tg.stream_session._default_engine_factory``: the
    ``SdkSubstrate``'s ``decision_callback`` IS the engine's own ``on_tool_request`` (the
    answer-hold + the P2 gate); ``permission_mode="default"`` and NO bypass /
    skip-permissions flag (SB5). The substrate ``cwd`` is the disposable temp dir OUTSIDE the
    repo (containment).
    """
    from claude_tg.engine.adapter_sdk import SdkSubstrate  # lazy: SDK only on the live path

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


async def live_grill_loop(cwd: str, log) -> tuple[dict, str, str]:
    """(2) Drive a REAL ``/grill`` loop against real Claude through the launch path (contained).

    Build the REAL engine over a real ``SdkSubstrate`` (cwd = the temp dir OUTSIDE the repo)
    with a harness-owned policy, then send ``/grill <concrete idea>`` — the leading ``/``
    forwarded VERBATIM (that IS the point of the passthrough). Drive
    :func:`drive_grill_until_result`: answer the first option of every ``AskEvent``,
    ``allow_once`` the doc ``Write``, stop on ``ResultEvent``. The drive-loop is the SAME code
    the scripted self-test exercises.

    PASS (mechanics, never prose): ≥ 1 ``AskEvent`` was surfaced (the unregistered ``/grill``,
    forwarded verbatim, LAUNCHED the skill and drove the relay) AND the loop ran to a
    non-error ``ResultEvent`` without hanging. Bonus (recorded, not hard-failed): the brief was
    written under the temp cwd. A launch that never reaches the engine, or a model that never
    asks, is FAIL/PARTIAL — never a false PASS.
    """
    name = "LIVE /grill loop (launch -> ask/answer -> gated write -> doc)"
    cap: dict[str, Any] = {"name": name, "mode": "live"}
    tlog = TurnLog()
    policy = PermissionPolicy()
    doc_path = Path(cwd, _GRILL_DOC_NAME)
    cap["instructed_doc_path"] = str(doc_path)

    engine = _build_live_engine(cwd, policy)

    # Forward the leading "/" VERBATIM (the whole point), with a short concrete idea in the
    # SAME message so grill has something to interrogate. We also instruct grill to write its
    # brief to the ABSOLUTE temp path to minimize leakage (the sweep catches any stray).
    prompt = (
        f"/grill a tiny CLI todo app in Python. Keep it brief: ask me at most two or three "
        f"AskUserQuestion rounds, then write the project brief to EXACTLY this absolute path "
        f"using the Write tool: {doc_path}"
    )

    started = time.monotonic()
    try:
        await engine.start()
        await asyncio.wait_for(
            drive_grill_until_result(engine, prompt, tlog),
            timeout=WALL_CLOCK_CAP_SECONDS,
        )
    except asyncio.TimeoutError:
        cap["timeout"] = True
        engine.cancel()
        tlog.record("    [code] WALL-CLOCK CAP hit -> cancel() (FAIL-clean, no hang)")
    finally:
        await engine.stop()

    elapsed = round(time.monotonic() - started, 1)
    asks = tlog.asks()
    perms = tlog.permissions()
    doc_written_in_cwd = doc_path.is_file()
    cap["elapsed_seconds"] = elapsed
    cap["ask_events"] = len(asks)
    cap["permission_events"] = len(perms)
    cap["permission_tools"] = [p.tool_name for p in perms]
    cap["doc_written_in_temp_cwd"] = doc_written_in_cwd
    cap["non_error_result"] = tlog.non_error_result() is not None
    cap["timeout"] = cap.get("timeout", False)
    cap["session_id"] = tlog.session_id()
    cap["transcript"] = "\n".join(tlog.lines)

    ask_surfaced = len(asks) >= 1
    ran_to_result = tlog.non_error_result() is not None and not cap["timeout"]
    if ask_surfaced and ran_to_result:
        bonus = (
            f" A brief was written under the temp cwd ({_GRILL_DOC_NAME})."
            if doc_written_in_cwd
            else " (No brief file found under the temp cwd; the sweep below FLAGS any stray.)"
        )
        return cap, "PASS", (
            f"the unregistered /grill command, forwarded VERBATIM, reached the engine and "
            f"LAUNCHED the skill: {len(asks)} AskEvent(s) were surfaced and answered (first "
            f"option each), {len(perms)} risky tool(s) were gated + allow_once'd, and the loop "
            f"ran to a non-error ResultEvent in {elapsed}s without hanging.{bonus} The full "
            f"P1 relay + P2 gating compose under the P3 launch path."
        )
    return cap, ("PARTIAL" if ran_to_result or ask_surfaced else "FAIL"), (
        f"ask_events={len(asks)} (want >=1 — the core launch proof) "
        f"non_error_result={tlog.non_error_result() is not None} timeout={cap['timeout']} "
        f"doc_written_in_temp_cwd={doc_written_in_cwd} permission_tools={cap['permission_tools']}"
    )


# ===========================================================================
# Driver
# ===========================================================================


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


def _sweep(home: Path, claude_dir: Path, tmpdir: str, log) -> list[str]:
    """Sweep $HOME AND ~/.claude for any SWEEP-set doc file; remove + FLAG strays.

    An ALLOWED write is NOT sandboxed (ADR-001: cwd is not an OS boundary) — ``/grill`` may
    resolve its brief name against its own home rather than the absolute temp path we
    instructed. We instructed the absolute temp path to minimize this, but we still sweep both
    $HOME and ~/.claude for EVERY name in :data:`SWEEP_NAMES`, remove any found, and FLAG it so
    a containment slip can never go unnoticed. Files inside the temp cwd are NOT strays (the
    rmtree handles them); only the ones that escaped to $HOME / ~/.claude are flagged.
    """
    strays: list[str] = []
    for base in (home, claude_dir):
        for fn in SWEEP_NAMES:
            candidate = base / fn
            try:
                if candidate.is_file() and not candidate.resolve().is_relative_to(Path(tmpdir).resolve()):
                    strays.append(str(candidate))
                    candidate.unlink(missing_ok=True)
            except Exception:
                # Never let a sweep error mask the run; record nothing we couldn't remove.
                pass
    if strays:
        log(
            f"  CONTAINMENT NOTE: {len(strays)} brief file(s) landed OUTSIDE the temp cwd "
            f"(model ignored the absolute temp path — ADR-001 cwd-not-a-sandbox); swept: {strays}"
        )
    return strays


async def _run(mode: str) -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T2 / P3 — LIVE end-to-end verify: skill-launch passthrough (full loop, contained) ===")
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

    # A fresh temp cwd OUTSIDE the repo (containment). rmtree'd regardless of outcome.
    tmpdir = tempfile.mkdtemp(prefix="p3_skill_launch_verify_")
    log(f"substrate/grill cwd (temp, OUTSIDE repo): {tmpdir}")
    assert not Path(tmpdir).resolve().is_relative_to(_REPO), "temp cwd must be OUTSIDE the repo"

    results: list[tuple[str, str, str]] = []  # (crit, verdict, reason)
    session_ids: list[str] = []  # live session id(s) -> also scrubbed from the OVERALL file (SB3)
    started = time.monotonic()
    home = Path.home()
    claude_dir = home / ".claude"

    try:
        if mode == "mock":
            # The IMPLEMENTER's self-test: launch-smoke + scripted drive-loop. Both
            # deterministic, NO live Claude. Each must PASS for an overall PASS.
            trials: list[tuple[str, Any]] = [
                ("launch_smoke", lambda: launch_smoke(log)),
                ("scripted_drive_loop", lambda: scripted_drive_loop(tmpdir, log)),
            ]
        else:
            # The ORCHESTRATOR's live probe: one real /grill loop end-to-end.
            trials = [("live_grill_loop", lambda: live_grill_loop(tmpdir, log))]

        for crit, fn in trials:
            log(f"\n>>> running {crit} ...")
            try:
                cap, verdict, reason = await fn()
            except Exception as exc:  # noqa: BLE001 - record, fail-clean (never hang)
                cap = {"name": crit, "mode": mode, "error": f"{type(exc).__name__}: {exc}"}
                verdict, reason = "FAIL", f"exception during trial: {type(exc).__name__}: {exc}"
            _record_trial(crit, cap, verdict, reason, report)
            if cap.get("session_id"):
                session_ids.append(str(cap["session_id"]))
            results.append((crit, verdict, reason))
            log(f"    -> {crit}: {verdict} — {reason}")
    finally:
        # --- cleanup + containment asserts ----------------------------------
        clean_project_transcript_dir(tmpdir, log)
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Belt-and-suspenders containment (ADR-001: cwd is NOT a sandbox): sweep $HOME and
        # ~/.claude for any brief file that escaped the temp cwd; remove + FLAG strays.
        strays = _sweep(home, claude_dir, tmpdir, log)

    elapsed = round(time.monotonic() - started, 1)
    pids_after = descendant_claude_pids()
    git_after = git_porcelain()
    leaked = sorted(set(pids_after) - set(pids_before))
    git_new = sorted(git_after - git_before)

    log("\n=== per-trial verdicts ===")
    for crit, verdict, reason in results:
        log(f"  {crit:22s} {verdict:8s}  {reason}")

    # --- overall verdict ----------------------------------------------------
    vmap = {c: v for c, v, _ in results}
    all_pass = bool(results) and all(v == "PASS" for _, v, _ in results)
    no_fail = bool(results) and all(v in ("PASS", "PARTIAL") for _, v, _ in results)

    log("\n=== CONTAINMENT / CLEANUP ===")
    log(f"  total runtime: {elapsed}s (~{elapsed / 60:.1f} min)")
    log(f"  descendant claude pids AFTER: {pids_after}  leaked: {leaked} (expected: [])")
    log(f"  git porcelain NEW during run: {git_new if git_new else 'NONE'} "
        f"(repo must be UNCHANGED; temp cwd lives OUTSIDE the repo)")
    log(f"  brief files swept from $HOME / ~/.claude (strays): {strays if strays else 'NONE'}")
    log(f"  ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (expected False)")

    containment_ok = (not leaked) and (not git_new)
    if not containment_ok:
        log("  WARNING: containment guard tripped (leaked pids or repo modified).")

    if all_pass and containment_ok:
        overall = "PASS"
        if mode == "mock":
            reason = (
                "Launch-smoke PASSED — on_skill_command forwards /grill VERBATIM all the way "
                "down to Substrate.send (bridging T1's bot-boundary test to engine.send), and a "
                "registered bot command (/reset) is NOT forwarded as a skill (bot commands win). "
                "Scripted drive-loop PASSED — the ask→answer→permission(allow_once)→result loop "
                "drove to a written brief and the native answers rode back. The launch path "
                "composes the full loop deterministically; predicates can't false-pass. "
                "Containment held (repo unchanged, no leaked CLI pids)."
            )
        else:
            reason = (
                "The live /grill loop PASSED: an unregistered slash-command, forwarded verbatim, "
                "reached the engine and LAUNCHED the skill (>=1 AskEvent surfaced + answered), "
                "risky writes were gated + allow_once'd, and the loop ran to a non-error result "
                "without hanging — P1 relay + P2 gating compose under the P3 launch path. "
                "Containment held (repo unchanged, no leaked CLI pids; any stray brief swept)."
            )
    elif no_fail and containment_ok:
        overall = "PARTIAL"
        reason = (
            "The launch path drove end-to-end but at least one trial was PARTIAL (mechanism "
            "reached, predicate softened by model nondeterminism — e.g. the model declined to "
            "ask a question or never wrote the doc) — inspect the per-trial evidence. "
            "Containment held."
        )
    else:
        overall = "FAIL"
        failed = [c for c, v, _ in results if v != "PASS"]
        reason = (
            f"At least one trial did not pass: {failed} (verdicts: "
            f"{{{', '.join(f'{c}={v}' for c, v, _ in results)}}}), or containment tripped "
            f"(leaked={leaked}, git_new={git_new}). Inspect the per-trial evidence."
        )

    log("")
    log(f"OVERALL VERDICT: {overall}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    with record_criterion(
        "p3_skill_launch_verify", base_dir=EVIDENCE_DIR, extra_secrets=session_ids or None
    ) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(overall, reason)

    # Exit non-zero only on a hard FAIL so CI/orchestrator can branch; PARTIAL is 0.
    return 0 if overall in ("PASS", "PARTIAL") else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="run a REAL /grill loop against Claude (orchestrator)")
    group.add_argument(
        "--mock", action="store_true",
        help="self-test: launch-smoke + scripted drive-loop, NO live Claude (implementer)",
    )
    args = parser.parse_args(argv)
    mode = "live" if args.live else "mock"  # default to the SAFE self-test (no network)
    return asyncio.run(_run(mode))


if __name__ == "__main__":
    raise SystemExit(main())
