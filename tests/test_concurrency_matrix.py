"""P5-T10 — the cross-cutting concurrency ACCEPTANCE matrix (RB7 + cross-project
routing + RB6), proving the design's *cross-task* criteria end-to-end.

No single task owns these: they are the properties that only emerge once T2's id-routing,
T4's per-project live-turn state, T5's per-project lock, T6's cap/queue, T8's notification
routing + per-chat send gate, and T9's free-text routing are all in place. Every test here
is **non-vacuous** — it would fail on a real regression of the criterion it pins (the three
highest-value ones — cross-project routing, concurrent-multi-q independence, no-slot-leak —
were mutation-probed during authoring; see the per-test docstrings).

**Tests ONLY — no production code.** Everything is mock-only (NO live Telegram, NO live
Claude, NO network): the engine is a scripted fake whose ``send()`` PARKS on a ``HOLD``
sentinel until ``resolve()``/``cancel()`` fires, so the real concurrency the live path has
is exercised — the turn loop iterates ``engine.send`` while the callback handler calls
``engine.resolve`` on the same loop to unblock it. Every hold is bounded by
``asyncio.wait_for`` so a wiring bug fails fast rather than hanging the suite.

Reuses the proven harnesses:

* ``tests.test_bot_streaming`` — the REAL bot callback path: ``TelegramClaudeBot`` +
  ``make_update`` / ``make_callback_update`` / ``make_ctx`` / ``make_cmd_ctx`` drive a button
  tap through ``on_callback`` (the SB1 ``_authorized`` boundary) and a command through
  ``cmd_*``; the ``HOLD`` park sentinel; ``_wait`` spins the loop until a predicate (a turn
  has parked + holds its lock) holds.
* A local ``make_matrix_session`` builder (below) for the matrix's lower-level scenarios — a
  real ``StreamingSession`` over a real ``JsonSessionStore`` whose factory returns a distinct
  scripted ``MatrixEngine`` per project cwd, each able to carry its OWN ``session_id`` (so the
  RB6 no-clobber + the cross-project routing defense-in-depth are real, not collapsed onto one
  shared id), and an injectable recording clock/sleep for the RB7 send-gate bound.

Acceptance-criterion → test map (one ⭐ per the task's headline criteria):

1. ⭐ Cross-project routing E2E (SB1 property P6 re-verifies) — ``test_cross_project_*``
2.   Two CONCURRENT multi-question asks accumulate independently — ``test_two_concurrent_multi_question_*``
3.   Foreground→background mid-stream flip (inline→ping) — ``test_foreground_to_background_*``
4.   Multi-question BACKGROUND ask end-to-end — ``test_background_multi_question_ask_*``
5.   SB1 under concurrency (non-allowlisted / forged tap) — ``test_sb1_*``
6.   RB6 concurrent persist (no clobber) — ``test_rb6_*``
7.   RB1/RB2 isolation (one turn raising) — ``test_rb1_*``
8.   RB3 restart with concurrent state — ``test_rb3_*``
9. ⭐ Cap/queue E2E (FIFO, no slot leak) — ``test_cap_queue_*``
10.  One-shot unchanged (regression) — ``test_oneshot_*``
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_tg.bot import TelegramClaudeBot
from claude_tg.engine.types import (
    AskEvent,
    ErrorEvent,
    PermissionDecision,
    PermissionEvent,
    QuestionAnswer,
    ResultEvent,
    TextEvent,
)
from claude_tg.render import encode_callback
from claude_tg.session_store import JsonSessionStore
from claude_tg.stream_session import StreamingSession
from tests.test_bot_streaming import (
    HOLD,
    FakeRunner,
    _wait,
    make_callback_update,
    make_cmd_ctx,
    make_config,
    make_ctx,
    make_update,
)

# ---------------------------------------------------------------------------
# A scripted multi-engine session builder for the matrix's lower-level scenarios.
#
# Distinct engine per project cwd (so a test can assert WHICH engine ran / was resolved /
# cancelled), each able to carry its OWN session_id (so RB6 no-clobber + the cross-project
# session-match defense-in-depth are real, not collapsed onto one shared id). The engine
# parks on HOLD exactly like HoldEngine but also records WHAT was resolved (id + decision)
# and exposes its events' session ids — the live path's concurrency, reproduced with mocks.
# ---------------------------------------------------------------------------


class MatrixEngine:
    """A scripted fake engine: yields its script, parks on ``HOLD`` until resolve/cancel.

    Records ``resolve``/``cancel`` calls so a test can prove WHICH project's engine a tap
    routed to (the cross-project property), and which decision it carried. ``raise_at`` (an
    index into the script) makes ``send`` raise mid-stream AFTER yielding that many events —
    so a turn that errors while another runs concurrently can be driven (RB1/RB2 isolation).
    """

    def __init__(self, script, *, session_id="sess", resolve_result=True, raise_at=None):
        self._script = list(script)
        self.session_id = session_id
        self.started = False
        self.resumed: str | None = None
        self.stopped = False
        self.resolve_calls: list[tuple[str, object]] = []
        self.cancel_calls: list = []
        self._resolve_result = resolve_result
        self._raise_at = raise_at
        self._gate = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def resume(self, session_id: str) -> None:
        self.resumed = session_id
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, prompt: str, *, timeout=None):
        yielded = 0
        for item in self._script:
            if item is HOLD:
                await self._gate.wait()
                self._gate.clear()
                continue
            yield item
            yielded += 1
            if self._raise_at is not None and yielded >= self._raise_at:
                raise RuntimeError("engine blew up mid-stream")

    def resolve(self, tool_use_id: str, decision) -> bool:
        self.resolve_calls.append((tool_use_id, decision))
        self._gate.set()
        return self._resolve_result

    def cancel(self, tool_use_id=None) -> int:
        self.cancel_calls.append(tool_use_id)
        self._gate.set()
        return 1

    def release(self) -> None:
        """Release a parked ``HOLD`` WITHOUT recording a resolve/cancel (test plumbing).

        Used to advance an engine past a *foreground* HOLD (so the test can flip the
        foreground / start a concurrent turn before the next event renders) without
        polluting ``resolve_calls`` — those asserts must reflect only real operator taps.
        """
        self._gate.set()


class RecordingClock:
    """A controllable monotonic clock + a recording ``sleep`` (no real time elapses).

    Used by the RB7 send-gate bound: the :class:`~claude_tg.render.ChatSendGate` decides a
    wait off this clock, and ``sleep`` RECORDS the waits the session honors without actually
    sleeping (so the test asserts the gate spaced the combined cross-project send rate). Time
    only advances when the test calls :meth:`advance`.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    async def sleep(self, seconds: float) -> None:
        # Record the requested wait; do NOT actually sleep (deterministic, no real time).
        self.sleeps.append(seconds)


class Recorder:
    """Captures the send/edit/delete calls a driver performs (mirrors test_stream_session)."""

    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.deletes: list[dict] = []
        self._next_id = 100

    async def send(self, *, text, reply_markup=None, parse_mode=None) -> int:
        self.sends.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        self._next_id += 1
        return self._next_id

    async def edit(self, *, message_id, text, parse_mode=None) -> None:
        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})

    async def delete(self, *, message_id) -> None:
        self.deletes.append({"message_id": message_id})


def make_matrix_session(
    engines_by_cwd: dict,
    *,
    store,
    config=None,
    clock=None,
    sleep=None,
    chat_send_interval=None,
) -> StreamingSession:
    """A real :class:`StreamingSession` whose factory returns a DISTINCT engine per cwd.

    Same shape as ``test_stream_session.make_multi_session`` but lets a test inject a
    recording clock + sleep + an explicit per-chat send interval (for the RB7 gate bound),
    and route a distinct :class:`MatrixEngine` per project cwd (the same cwd always yields
    the same engine — a project's runtime is built once + reused).
    """
    return StreamingSession(
        config or make_config(engine_mode="streaming", allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engines_by_cwd[cwd],
        clock=clock or (lambda: 0.0),
        sleep=sleep or AsyncMock(),
        chat_send_interval=chat_send_interval,
    )


def two_project_store(tmp_path, *, a="alpha", b="beta", active="alpha"):
    """A real v2 store with two projects (``a`` active by default), distinct cwds."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, a, f"/work/{a}", make_active=(active == a))
    store.create(1, b, f"/work/{b}", make_active=(active == b))
    return store


# ===========================================================================
# 1. ⭐ Cross-project routing E2E (the headline; SB1 property P6 re-verifies).
#
#    Project A parked at a hold while B is foreground + running; a button tap carrying A's
#    tool_use_id — through the REAL on_callback / resolve_callback path (SB1-checked) —
#    resolves A, NEVER B. Conversely B's id resolves B, never A. Mutation-sensitive: if the
#    index router regressed to _active_engine (P4 behavior), the tap would hit the FOREGROUND
#    project (B) and these would fail loudly.
# ===========================================================================


async def _park_two_holds(tmp_path):
    """Set up: A holds a permission, B holds a permission, B is foreground. Returns the
    pieces a cross-project routing test needs (bot, session, both engines, both turns)."""
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    # Each engine emits a permission hold (distinct tool_use_id) then parks. Distinct
    # session_ids so the defense-in-depth session-match is genuinely exercised (the held
    # event carries no session_id → match is permissive, but the engine ids still differ).
    perm_a = PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="A-perm")
    perm_b = PermissionEvent(tool_name="Write", tool_input_summary="Write(...)", tool_use_id="B-perm")
    eng_a = MatrixEngine([perm_a, HOLD], session_id="sess-A")
    eng_b = MatrixEngine([perm_b, HOLD], session_id="sess-B")
    session = make_matrix_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b}, store=store
    )
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    # Start A's turn (parks holding alpha's lock), then start B's turn (parks holding beta's
    # lock) — two engines live at once. Switch foreground to beta so A is BACKGROUND.
    rec = make_ctx()
    rec.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn_a = asyncio.create_task(bot.on_message(make_update(1, "do A"), rec))
    await _wait(lambda: session.is_busy(1, "alpha"))
    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"  # beta is now the foreground
    turn_b = asyncio.create_task(bot.on_message(make_update(1, "do B"), rec))
    await _wait(lambda: session.is_busy(1, "beta"))
    return bot, session, eng_a, eng_b, turn_a, turn_b


async def test_cross_project_tap_for_A_resolves_A_never_B_while_B_foreground(tmp_path):
    """⭐ THE headline. A held permission in A (background) + B foreground & running: a REAL
    button tap carrying A's tool_use_id resolves A's engine and NEVER B's.

    Mutation probe (done while authoring): pointing ``_engine_for_pending`` at the active
    project (the P4 ``_active_engine`` collapse) makes this tap hit B → ``eng_b.resolve_calls``
    becomes non-empty and ``eng_a.resolve_calls`` empty → the asserts below fail. So this
    pins the id-routing, not just "a resolve happened".
    """
    bot, session, eng_a, eng_b, turn_a, turn_b = await _park_two_holds(tmp_path)

    # Tap A's permission (allow once) through the REAL on_callback (SB1 _authorized → decode →
    # index[A-perm] → owning project alpha → alpha's engine). Chat 1 IS allowlisted.
    upd = make_callback_update(chat_id=1, data=encode_callback("m", "A-perm", payload="o"))
    await bot.on_callback(upd, make_ctx())

    # A's engine resolved with allow_once; B's engine was NEVER touched (cross-project safety).
    assert eng_a.resolve_calls == [("A-perm", PermissionDecision(verdict="allow_once"))]
    assert eng_b.resolve_calls == [], "a tap for A must NEVER resolve B's request"
    # The callback query was answered (spinner stops).
    upd.callback_query.answer.assert_awaited()

    # A's held turn unblocks + finishes; B is still parked. Then resolve B to drain it.
    await asyncio.wait_for(turn_a, timeout=2.0)
    assert not turn_b.done()
    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)


async def test_cross_project_tap_for_B_resolves_B_never_A(tmp_path):
    """The converse: a tap for the FOREGROUND project B resolves B, never the background A —
    so the property is symmetric (id routes to the owner, regardless of foreground)."""
    bot, session, eng_a, eng_b, turn_a, turn_b = await _park_two_holds(tmp_path)

    upd = make_callback_update(chat_id=1, data=encode_callback("m", "B-perm", payload="s"))
    await bot.on_callback(upd, make_ctx())

    assert eng_b.resolve_calls == [("B-perm", PermissionDecision(verdict="allow_session"))]
    assert eng_a.resolve_calls == [], "a tap for B must NEVER resolve A's request"

    await asyncio.wait_for(turn_b, timeout=2.0)
    assert not turn_a.done()
    eng_a.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)


async def test_cross_project_stale_id_after_resolve_noops_in_both(tmp_path):
    """A re-tap of A's now-resolved id (its index entry dropped on the first resolve) is a
    benign no-op — it resolves nothing in A or B (RB1 / SB6: a stale button never re-fires)."""
    bot, session, eng_a, eng_b, turn_a, turn_b = await _park_two_holds(tmp_path)
    data = encode_callback("m", "A-perm", payload="o")
    await bot.on_callback(make_callback_update(chat_id=1, data=data), make_ctx())
    await asyncio.wait_for(turn_a, timeout=2.0)  # A resolved + finished → its entry gone
    a_calls_after_first = list(eng_a.resolve_calls)

    # Re-tap the same (now stale) id: no new resolve in either engine.
    await bot.on_callback(make_callback_update(chat_id=1, data=data), make_ctx())
    assert eng_a.resolve_calls == a_calls_after_first  # no extra resolve on A
    assert eng_b.resolve_calls == []  # and never B

    eng_b.cancel()
    await asyncio.wait_for(turn_b, timeout=2.0)


# ===========================================================================
# 2. Two CONCURRENT multi-question asks accumulate independently (T2-review gap).
#
#    A and B each hold a MULTI-question ask. Answering A's questions accumulates only in A's
#    per-id index entry (ref.ask_answers) and resolves A once ALL A's questions are answered;
#    it NEVER contaminates B's accumulator, and B resolves on its OWN questions. Pins the
#    per-id accumulator (a chat-global accumulator would cross-contaminate).
# ===========================================================================


async def _park_two_multi_question_asks(tmp_path):
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    ask_a = AskEvent(
        questions=[
            {"question": "A-Q1", "options": [{"label": "A1a"}, {"label": "A1b"}]},
            {"question": "A-Q2", "options": [{"label": "A2a"}, {"label": "A2b"}]},
        ],
        tool_use_id="ask-A",
    )
    ask_b = AskEvent(
        questions=[
            {"question": "B-Q1", "options": [{"label": "B1a"}, {"label": "B1b"}]},
            {"question": "B-Q2", "options": [{"label": "B2a"}, {"label": "B2b"}]},
        ],
        tool_use_id="ask-B",
    )
    eng_a = MatrixEngine([ask_a, HOLD, ResultEvent(session_id="sess-A", is_error=False, subtype="success", result_text="A done")], session_id="sess-A")
    eng_b = MatrixEngine([ask_b, HOLD, ResultEvent(session_id="sess-B", is_error=False, subtype="success", result_text="B done")], session_id="sess-B")
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)
    rec = Recorder()

    turn_a = asyncio.create_task(
        session.handle_message(1, "go A", send=rec.send, edit=rec.edit)
    )
    await _wait(lambda: session.is_busy(1, "alpha"))
    # Switch to beta and start its turn so both asks are held concurrently.
    store.switch(1, "beta")
    turn_b = asyncio.create_task(
        session.handle_message(1, "go B", send=rec.send, edit=rec.edit)
    )
    await _wait(lambda: session.is_busy(1, "beta"))
    return session, eng_a, eng_b, turn_a, turn_b


async def test_two_concurrent_multi_question_asks_accumulate_independently(tmp_path):
    """Answering A's two questions resolves A with A's OWN answers map; B's accumulator is
    untouched and B resolves only when B's OWN questions are all answered.

    Mutation probe (done while authoring): collapsing ``ask_answers`` onto a chat-global slot
    makes A's first answer and B's first answer share one accumulator → A resolves with a
    B-contaminated map (or resolves early) → ``eng_a.resolve_calls`` / ``eng_b.resolve_calls``
    diverge from the per-id maps below. So this pins per-id independence.
    """
    session, eng_a, eng_b, turn_a, turn_b = await _park_two_multi_question_asks(tmp_path)

    # Answer A-Q1 (not complete — A-Q2 still open). Interleave a B-Q1 answer in between to
    # prove the two accumulators don't bleed into each other.
    session.resolve_callback(1, encode_callback("a", "ask-A", question_index=0, option_index=0))  # A-Q1=A1a
    assert eng_a.resolve_calls == []  # A not resolved (1 of 2)
    session.resolve_callback(1, encode_callback("a", "ask-B", question_index=0, option_index=1))  # B-Q1=B1b
    assert eng_b.resolve_calls == []  # B not resolved (1 of 2)

    # Finish A (A-Q2) → A resolves with ONLY A's answers; B still unresolved.
    session.resolve_callback(1, encode_callback("a", "ask-A", question_index=1, option_index=1))  # A-Q2=A2b
    assert eng_a.resolve_calls == [
        ("ask-A", QuestionAnswer(answers={"A-Q1": "A1a", "A-Q2": "A2b"}))
    ]
    assert eng_b.resolve_calls == [], "answering A's questions must not resolve B"

    # Finish B (B-Q2) → B resolves with ONLY B's answers (no A contamination).
    session.resolve_callback(1, encode_callback("a", "ask-B", question_index=1, option_index=0))  # B-Q2=B2a
    assert eng_b.resolve_calls == [
        ("ask-B", QuestionAnswer(answers={"B-Q1": "B1b", "B-Q2": "B2a"}))
    ]

    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)


# ===========================================================================
# 3. Foreground→background mid-stream flip (T8-review gap (a)).
#
#    A turn that is FOREGROUND when it starts (renders inline) → /switch makes it BACKGROUND
#    mid-stream → its SUBSEQUENT attention (permission) renders as a 🔔 ping (not inline) and
#    its terminal as a ✅ ping. Pins the per-event re-read of foreground in _drive_turn.
# ===========================================================================


async def test_foreground_to_background_flip_attention_becomes_bell_ping(tmp_path):
    """alpha starts foreground (its status line renders inline), then /switch beta makes it
    background; alpha's later permission hold then arrives as a ``🔔 alpha —`` ping carrying
    the permission keyboard (not an inline permission card), and its completion as ``✅ alpha``.

    Non-vacuous: if _drive_turn read foreground ONCE at turn start (instead of per event),
    alpha would still render its permission inline → no 🔔 ping → the bell assert fails.
    """
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    perm = PermissionEvent(tool_name="Bash", tool_input_summary="Bash(...)", tool_use_id="A-perm")
    # alpha: a bit of foreground status, HOLD #1 (we /switch during it), then a permission
    # hold + HOLD #2 (asserted as a background ping), then a clean result.
    eng_a = MatrixEngine(
        [
            TextEvent(text="thinking", incremental=True),  # inline status while foreground
            HOLD,  # park #1 — operator switches away here
            perm,  # now background → must become a 🔔 ping
            HOLD,  # park #2 — held permission
            ResultEvent(session_id="sess-A", is_error=False, subtype="success", result_text="A done"),
        ],
        session_id="sess-A",
    )
    eng_b = MatrixEngine([], session_id="sess-B")
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)
    rec = Recorder()

    turn = asyncio.create_task(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit, delete=rec.delete)
    )
    # Let the inline status render while foreground (park #1). While foreground, NO 🔔 ping
    # has been sent — the status is inline (a send and/or an edit of the status line).
    await _wait(lambda: any("thinking" in (s["text"] or "") for s in rec.sends) or rec.edits != [])
    assert not any(s["text"].startswith("🔔") for s in rec.sends), (
        "while foreground, status renders inline — no bell ping yet"
    )

    # Switch foreground to beta MID-STREAM, then release park #1 so alpha continues backgrounded.
    store.switch(1, "beta")
    eng_a.release()  # release park #1 (the gate); the script advances to `perm`
    await _wait(lambda: any(s["text"].startswith("🔔") for s in rec.sends))

    # alpha's permission arrived as a name-prefixed bell ping (NOT an inline permission card),
    # carrying the permission keyboard so the tap still routes by id.
    bell = next(s for s in rec.sends if s["text"].startswith("🔔"))
    assert "alpha" in bell["text"]
    assert bell["reply_markup"] is not None, "the background permission ping must carry its keyboard"

    # Answer the backgrounded permission via the index → alpha resumes + completes.
    out = session.resolve_callback(1, encode_callback("m", "A-perm", payload="o"))
    assert out.handled is True
    await asyncio.wait_for(turn, timeout=2.0)

    # The terminal arrived as a ✅ background ping (alpha is not foreground), not inline.
    assert any(s["text"].startswith("✅") and "alpha" in s["text"] for s in rec.sends)
    # And the inline result body ("A done") was NOT sent inline (backgrounded run is silent
    # for non-hold/terminal — it is summarized by the ✅ ping, D4).
    assert not any("A done" in s["text"] for s in rec.sends)


# ===========================================================================
# 4. Multi-question BACKGROUND ask end-to-end (T8-review gap (b)).
#
#    A background project raises a >1-question ask → a 🔔 ping + EACH question's keyboard is
#    sent, and EACH is answerable via the index → full resolution. Pins _notify_background_ask
#    (one keyboard per question for a backgrounded multi-q ask).
# ===========================================================================


async def test_background_multi_question_ask_pings_and_each_question_answerable(tmp_path):
    """A background project's 2-question ask → a ``🔔`` ping + two keyboarded messages (one
    per question); answering question 0 does NOT resolve, answering question 1 resolves the
    whole ask with both answers — all while the project is NOT foreground.

    Non-vacuous: if a backgrounded ask sent a single stacked keyboard (or no per-question
    keyboards), ``keyboarded`` below would not be 2 and the per-question answer taps could not
    address both questions.
    """
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")  # alpha starts fg
    ask = AskEvent(
        questions=[
            {"question": "Storage?", "options": [{"label": "JSON"}, {"label": "SQLite"}]},
            {"question": "CLI?", "options": [{"label": "argparse"}, {"label": "Typer"}]},
        ],
        tool_use_id="bg-ask",
    )
    # A foreground HOLD #1 first (so we can /switch alpha to BACKGROUND while it is parked),
    # THEN the multi-question ask — which therefore renders only after alpha is backgrounded.
    eng_a = MatrixEngine(
        [HOLD, ask, HOLD, ResultEvent(session_id="sess-A", is_error=False, subtype="success", result_text="done")],
        session_id="sess-A",
    )
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": MatrixEngine([])}, store=store)
    rec = Recorder()

    # Start alpha's turn; it parks at HOLD #1 while still foreground.
    turn = asyncio.create_task(session.handle_message(1, "go", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "alpha"))
    # Switch foreground to beta, then release HOLD #1 so the ask is emitted while alpha is BG.
    store.switch(1, "beta")
    eng_a.release()

    # Wait for the background ask rendering (the 🔔 line + per-question keyboards).
    await _wait(lambda: any(s["text"].startswith("🔔") for s in rec.sends))
    assert any(s["text"].startswith("🔔") and "alpha" in s["text"] for s in rec.sends)
    keyboarded = [s for s in rec.sends if s["reply_markup"] is not None]
    assert len(keyboarded) == 2, "a backgrounded multi-q ask sends one keyboard per question"
    assert any("Storage?" in s["text"] for s in keyboarded)
    assert any("CLI?" in s["text"] for s in keyboarded)

    # Answer each question via the index. Q0 alone does NOT resolve; Q1 completes it.
    session.resolve_callback(1, encode_callback("a", "bg-ask", question_index=0, option_index=0))  # JSON
    assert eng_a.resolve_calls == []
    session.resolve_callback(1, encode_callback("a", "bg-ask", question_index=1, option_index=1))  # Typer
    assert eng_a.resolve_calls == [
        ("bg-ask", QuestionAnswer(answers={"Storage?": "JSON", "CLI?": "Typer"}))
    ]
    await asyncio.wait_for(turn, timeout=2.0)


# ===========================================================================
# 5. SB1 under concurrency: a NON-allowlisted callback tap never resolves ANY project.
#
#    The on_callback _authorized recheck is the authoritative gate. A tap from a chat NOT on
#    the allowlist — even carrying a valid in-index tool_use_id — resolves nothing in either
#    concurrent project. Also: a forged/foreign callback_data from an authorized chat decodes
#    to None and resolves nothing.
# ===========================================================================


async def test_sb1_unauthorized_tap_resolves_no_project_under_concurrency(tmp_path):
    """SB1 with TEETH: a tap from a NON-allowlisted chat resolves nothing **even when that
    chat's pending-index is NON-empty** — the ``_authorized`` recheck must reject the tap
    BEFORE ``resolve_callback`` can fire.

    The two holds (``A-perm`` / ``B-perm``) are primed under **chat 1's** index. We then
    REVOKE chat 1's allowlist entry (rebuild ``bot.config`` so chat 1 is no longer allowed)
    and tap from chat 1 with a VALID, in-index id. So the tapping chat genuinely owns those
    pendings — the only thing standing between the tap and ``eng_a.resolve`` is the
    ``_authorized`` gate.

    Mutation probe (verified while authoring, instance monkeypatch in a /tmp throwaway): with
    ``_authorized`` bypassed (forced True), this de-allowlisted chat-1 tap reaches
    ``resolve_callback(1, "A-perm")``, hits chat 1's NON-empty index, and resolves ``eng_a`` →
    ``eng_a.resolve_calls`` becomes ``[("A-perm", allow_once)]`` → the asserts below FAIL. With
    the recheck in place the tap is answered + dropped. So this pins the allowlist boundary
    itself, not the index miss. (The earlier chat-999 framing was VACUOUS: a 999 tap routes
    against chat 999's EMPTY index, so it resolved nothing regardless of ``_authorized``.)
    """
    bot, session, eng_a, eng_b, turn_a, turn_b = await _park_two_holds(tmp_path)
    # _park_two_holds built chat 1 with both pendings indexed (A-perm, B-perm). Now make chat
    # 1 NON-allowlisted (Config is frozen → rebuild it; _authorized reads it live at tap time).
    # The session keeps its own config + index — only the SB1 gate's view of the allowlist
    # changes, so chat 1's pending-index stays NON-empty while the chat is no longer allowed.
    bot.config = make_config(allowed=(), engine_mode="streaming", allow_any_path=True)
    assert not bot._authorized(make_callback_update(chat_id=1, data="x"))  # chat 1 now blocked

    # A tap from the now-NON-allowlisted chat 1 — a VALID id whose entry IS in chat 1's index.
    upd = make_callback_update(chat_id=1, data=encode_callback("m", "A-perm", payload="o"))
    await bot.on_callback(upd, make_ctx())

    upd.callback_query.answer.assert_awaited()  # spinner stops…
    assert eng_a.resolve_calls == []  # …but NOTHING resolved in A (the recheck blocked it)
    assert eng_b.resolve_calls == []  # …nor B (SB1 holds across both concurrent projects)

    # Belt-and-braces: a tap from a foreign chat 999 (empty index, also not allowlisted) is
    # likewise inert — both the allowlist gate AND the per-chat index miss reject it.
    upd999 = make_callback_update(chat_id=999, data=encode_callback("m", "A-perm", payload="o"))
    await bot.on_callback(upd999, make_ctx())
    upd999.callback_query.answer.assert_awaited()
    assert eng_a.resolve_calls == []
    assert eng_b.resolve_calls == []

    # Drain both holds.
    eng_a.cancel()
    eng_b.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)


async def test_sb1_forged_callback_from_authorized_chat_resolves_nothing(tmp_path):
    """An authorized chat's tap whose callback_data is garbage / foreign decodes to None →
    resolves nothing in either concurrent project (defense-in-depth past the allowlist)."""
    bot, session, eng_a, eng_b, turn_a, turn_b = await _park_two_holds(tmp_path)

    for bad in ["garbage", "a|A-perm", "x|A-perm|0.0", ""]:
        await bot.on_callback(make_callback_update(chat_id=1, data=bad), make_ctx())
    assert eng_a.resolve_calls == []
    assert eng_b.resolve_calls == []

    eng_a.cancel()
    eng_b.cancel()
    await asyncio.wait_for(turn_a, timeout=2.0)
    await asyncio.wait_for(turn_b, timeout=2.0)


# ===========================================================================
# 6. RB6 concurrent persist: two projects' turns persist their session_ids concurrently →
#    each project's record is correct, no clobber (cross-ref per-project set_session_id).
# ===========================================================================


async def test_rb6_two_concurrent_runs_persist_distinct_session_ids_no_clobber(tmp_path):
    """A and B each run a turn concurrently to a ResultEvent carrying its OWN session_id; the
    store ends with A's id on A's record and B's id on B's — neither clobbers the other.

    Non-vacuous: persisting to the ACTIVE project (instead of the turn's CAPTURED project)
    would write whichever was active when each result landed — under concurrency that
    clobbers one project's id with the other's. This pins the per-project, captured-name
    persist (the lock-P/drive-Q persist-drift fix).
    """
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    eng_a = MatrixEngine(
        [HOLD, ResultEvent(session_id="A-session-xyz", is_error=False, subtype="success", result_text="A done")],
        session_id="A-session-xyz",
    )
    eng_b = MatrixEngine(
        [HOLD, ResultEvent(session_id="B-session-789", is_error=False, subtype="success", result_text="B done")],
        session_id="B-session-789",
    )
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)
    rec = Recorder()

    # Start A (parks), switch active to beta, start B (parks) — both turns captured their OWN
    # project at message time, so each must persist to its own record even though `active`
    # moved to beta before either result lands.
    turn_a = asyncio.create_task(session.handle_message(1, "go A", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go B", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "beta"))

    # Release B first (while active==beta), then A (active is still beta) — A must STILL write
    # to alpha's record, not the active beta.
    eng_b.release()
    await asyncio.wait_for(turn_b, timeout=2.0)
    eng_a.release()
    await asyncio.wait_for(turn_a, timeout=2.0)

    assert store.get_project(1, "alpha")["session_id"] == "A-session-xyz"
    assert store.get_project(1, "beta")["session_id"] == "B-session-789"


# ===========================================================================
# 7. RB1/RB2 isolation: one project's turn raising/erroring does NOT break a concurrent
#    project's turn or wedge the chat (the gate/queue stay usable); the finally frees its slot.
# ===========================================================================


async def test_rb1_one_turn_raising_does_not_break_concurrent_turn_or_wedge_chat(tmp_path):
    """alpha's turn raises mid-stream (an engine exception) while beta runs concurrently; beta
    completes cleanly, alpha's lock + run slot are freed (finally), and the chat stays usable
    — a fresh turn on alpha runs afterward.

    Non-vacuous: if alpha's exception escaped without releasing its slot/lock, ``_running``
    would leak (capacity shrinks) and ``is_busy(1, "alpha")`` would stay True → the later
    re-run on alpha would StreamingBusy or never start. This pins the contained-failure +
    finally-release.
    """
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    # alpha raises after yielding 1 event; beta parks then completes cleanly.
    eng_a = MatrixEngine([TextEvent(text="boom-soon", incremental=False)], session_id="sess-A", raise_at=1)
    eng_b = MatrixEngine(
        [HOLD, ResultEvent(session_id="sess-B", is_error=False, subtype="success", result_text="B ok")],
        session_id="sess-B",
    )
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)
    rec = Recorder()

    # Start beta first (parks), switch active back to alpha, then drive alpha (which raises).
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go B", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "beta"))
    store.switch(1, "alpha")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.handle_message(1, "go A", send=rec.send, edit=rec.edit), timeout=2.0)

    # alpha's failure is CONTAINED: its lock + slot are freed, beta is untouched + still running.
    assert session.is_busy(1, "alpha") is False, "the raised turn must release alpha's lock"
    assert session.is_busy(1, "beta") is True, "beta's concurrent run is unaffected"
    assert session._running == 1, "only beta holds a slot now (alpha's was released — no leak)"

    # The chat is not wedged: beta finishes and a FRESH alpha turn runs (no zombie state).
    eng_b.release()
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert store.get_project(1, "beta")["session_id"] == "sess-B"  # beta still persisted cleanly

    # A fresh alpha engine (the runtime rebuilds) runs to completion — chat usable again.
    eng_a2 = MatrixEngine([ResultEvent(session_id="sess-A2", is_error=False, subtype="success", result_text="A retry ok")], session_id="sess-A2")
    session._engine_factory = lambda *, cwd, backstop_seconds, permission_policy: (
        eng_a2 if cwd == "/work/alpha" else MatrixEngine([])
    )
    store.switch(1, "alpha")
    # alpha's runtime still holds the dead engine; _ensure_engine discards a non-started one.
    state = session._chats[1]
    state.runtimes["alpha"].engine = None
    state.runtimes["alpha"].started = False
    await asyncio.wait_for(session.handle_message(1, "go A again", send=rec.send, edit=rec.edit), timeout=2.0)
    assert any("A retry ok" in s["text"] for s in rec.sends)


async def test_rb2_concurrent_error_event_pings_without_breaking_other(tmp_path):
    """A background project's ErrorEvent renders a body-free ``⚠️`` ping (not a crash, no raw
    body) while a concurrent foreground project keeps working — RB2 + SB3 under concurrency.

    Non-vacuous: if the terminal-ping path leaked ``event.message`` (the raw body) instead of
    the body-free ``kind_of_error``, the stand-in secret below WOULD appear in a send → the
    SB3 assert fails. And the ⚠️ ping presence pins that a background error still surfaces.
    """
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")  # alpha starts fg
    err = ErrorEvent(kind_of_error="tool_error", message="SECRET-LEAK-token-abc123")
    # A foreground HOLD first (so we switch alpha to BACKGROUND while parked), THEN the error +
    # an is_error result. The error therefore renders while alpha is backgrounded → a ⚠️ ping.
    eng_a = MatrixEngine(
        [HOLD, err, ResultEvent(session_id="sess-A", is_error=True, subtype="error")],
        session_id="sess-A",
    )
    eng_b = MatrixEngine(
        [HOLD, ResultEvent(session_id="sess-B", is_error=False, subtype="success", result_text="B ok")],
        session_id="sess-B",
    )
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)
    rec = Recorder()

    # Start alpha (parks fg), then start beta (parks) so beta runs concurrently. Switch
    # foreground to beta, release alpha's HOLD so its error renders backgrounded.
    turn_a = asyncio.create_task(session.handle_message(1, "go A", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go B", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "beta"))
    eng_a.release()  # release alpha → its error + is_error result render (bg)
    await asyncio.wait_for(turn_a, timeout=2.0)

    # alpha's background error surfaced as a body-free ⚠️ ping — and the raw message (a
    # stand-in secret) NEVER appears in any send (SB3 body-free).
    assert any(s["text"].startswith("⚠️") and "alpha" in s["text"] for s in rec.sends)
    assert not any("SECRET-LEAK-token-abc123" in s["text"] for s in rec.sends)

    # beta (the concurrent foreground run) is unaffected — still parked + usable; release it.
    assert session.is_busy(1, "beta") is True
    eng_b.release()
    await asyncio.wait_for(turn_b, timeout=2.0)
    assert store.get_project(1, "beta")["session_id"] == "sess-B"


# ===========================================================================
# 8. RB3 restart with concurrent state: a fresh StreamingSession over a store with multiple
#    projects resumes each independently (lazy), transient concurrency state reset.
# ===========================================================================


async def test_rb3_restart_resumes_each_project_idle_then_lazily_no_inflight(tmp_path):
    """After a "restart" (a brand-new StreamingSession over the same store with persisted
    session_ids for both projects): every project comes back IDLE (no in-flight runs, the
    in-memory queue/run-count/index do not persist), and a turn on each LAZILY resumes ITS
    OWN session id independently.

    Non-vacuous: if the new session carried over in-flight/busy state, ``is_busy`` would be
    True at boot or a project would resume the WRONG id; if resume were not per-project, both
    turns would resume the same id.
    """
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    store.set_session_id(1, "alpha", "alpha-prev")
    store.set_session_id(1, "beta", "beta-prev")

    # The "restart": a fresh session + fresh engines (the old process's runtimes are gone).
    eng_a = MatrixEngine([ResultEvent(session_id="alpha-prev", is_error=False, subtype="success", result_text="A ok")], session_id="alpha-prev")
    eng_b = MatrixEngine([ResultEvent(session_id="beta-prev", is_error=False, subtype="success", result_text="B ok")], session_id="beta-prev")
    session = make_matrix_session({"/work/alpha": eng_a, "/work/beta": eng_b}, store=store)

    # Boot state: NO in-flight runs, NO busy projects, the run counter is 0 (transient reset).
    assert session.is_busy(1) is False
    assert session._running == 0
    assert session.project_status(1, "alpha") == "idle"
    assert session.project_status(1, "beta") == "idle"

    rec = Recorder()
    # A turn on the active project (alpha) LAZILY resumes alpha-prev.
    await asyncio.wait_for(session.handle_message(1, "hi A", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng_a.resumed == "alpha-prev"
    assert eng_b.resumed is None  # beta untouched so far

    # Switch + a turn on beta lazily resumes beta-prev — independently of alpha.
    store.switch(1, "beta")
    await asyncio.wait_for(session.handle_message(1, "hi B", send=rec.send, edit=rec.edit), timeout=2.0)
    assert eng_b.resumed == "beta-prev"


# ===========================================================================
# 9. ⭐ Cap/queue E2E: with MAX_CONCURRENT_RUNS small, >cap projects → queued (status
#    `queued`) → dequeue FIFO on completion; slot never leaks.
# ===========================================================================


async def test_cap_queue_third_run_queues_then_dequeues_fifo_no_slot_leak(tmp_path):
    """⭐ With cap=1: the first project runs, the second + third QUEUE (FIFO, status
    ``queued`` + a one-time ⏳ notice). Finishing the running one dequeues the OLDEST waiter
    first (FIFO), then the next; the run counter never exceeds the cap and never leaks.

    Mutation probe (done while authoring): if ``_release_slot`` decremented instead of
    transferring to a waiter (a slot leak / no dequeue), the queued turns would never start →
    they'd stay ``queued`` and the final asserts (they completed) fail. If FIFO were LIFO, the
    completion ORDER below would invert. So this pins both the FIFO dequeue and the slot
    accounting.
    """
    store = JsonSessionStore(tmp_path / "state.json")
    for name in ("p1", "p2", "p3"):
        store.create(1, name, f"/work/{name}", make_active=(name == "p1"))
    # p1 holds (occupies the only slot); p2, p3 each complete immediately ONCE they get a slot.
    eng1 = MatrixEngine([HOLD, ResultEvent(session_id="s1", is_error=False, subtype="success", result_text="p1 done")], session_id="s1")
    eng2 = MatrixEngine([ResultEvent(session_id="s2", is_error=False, subtype="success", result_text="p2 done")], session_id="s2")
    eng3 = MatrixEngine([ResultEvent(session_id="s3", is_error=False, subtype="success", result_text="p3 done")], session_id="s3")
    config = make_config(engine_mode="streaming", allow_any_path=True, max_concurrent_runs=1)
    session = make_matrix_session(
        {"/work/p1": eng1, "/work/p2": eng2, "/work/p3": eng3}, store=store, config=config
    )
    rec = Recorder()

    # p1 runs (takes the only slot) and parks.
    turn1 = asyncio.create_task(session.handle_message(1, "go1", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "p1"))
    assert session._running == 1

    # p2 then p3 are started while AT the cap → each queues (FIFO). Drive them as tasks.
    store.switch(1, "p2")
    turn2 = asyncio.create_task(session.handle_message(1, "go2", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.project_status(1, "p2") == "queued")
    store.switch(1, "p3")
    turn3 = asyncio.create_task(session.handle_message(1, "go3", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.project_status(1, "p3") == "queued")

    # Still only ONE running (the cap held); two are queued; a ⏳ notice was sent for each.
    assert session._running == 1
    assert len([s for s in rec.sends if s["text"].startswith("⏳")]) == 2
    assert not turn2.done() and not turn3.done()

    # Finish p1 → its slot transfers to the OLDEST waiter (p2, FIFO). p2 completes; its slot
    # then transfers to p3; p3 completes. The counter never exceeds 1.
    eng1.release()
    await asyncio.wait_for(turn1, timeout=2.0)
    await asyncio.wait_for(turn2, timeout=2.0)
    await asyncio.wait_for(turn3, timeout=2.0)

    # All three ran to completion (FIFO dequeue worked; no waiter was stranded — a stranded
    # waiter would have timed out the wait_for above). Each project's completion shows in the
    # send stream as a ✅ <name> background ping (p1, p2 — backgrounded once active moved) or
    # the inline result text (p3 — it ended up the foreground project). The ORDER of p2's then
    # p3's completion markers is the FIFO dequeue order: p2 queued BEFORE p3, so when p1's slot
    # freed it went to p2 first → p2 completes before p3.
    def completion_index(marker_substrings) -> int:
        for i, s in enumerate(rec.sends):
            if any(m in s["text"] for m in marker_substrings):
                return i
        raise AssertionError(f"no completion marker {marker_substrings} found in {[s['text'] for s in rec.sends]}")

    # p1 completed (its ✅ ping is present — it was backgrounded by the time it finished).
    assert any(s["text"].startswith("✅") and "p1" in s["text"] for s in rec.sends)
    p2_done = completion_index(("✅ p2", "p2 done"))
    p3_done = completion_index(("✅ p3", "p3 done"))
    assert p2_done < p3_done, (
        f"FIFO: p2 (queued first) must dequeue/complete before p3, got "
        f"{[s['text'] for s in rec.sends]}"
    )
    # Slot accounting is clean: nothing left running, no leak (counter back to 0).
    assert session._running == 0
    assert session.is_busy(1) is False


async def test_cap_queue_same_project_second_message_is_busy_not_queued(tmp_path):
    """A project is never queued behind ITSELF: a second message to the SAME running project
    raises StreamingBusy (checked before a slot/queue entry is taken), even at the cap."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "solo", "/work/solo", make_active=True)
    eng = MatrixEngine([HOLD, ResultEvent(session_id="s", is_error=False, subtype="success")], session_id="s")
    config = make_config(engine_mode="streaming", allow_any_path=True, max_concurrent_runs=1)
    session = make_matrix_session({"/work/solo": eng}, store=store, config=config)
    rec = Recorder()

    turn = asyncio.create_task(session.handle_message(1, "first", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "solo"))
    from claude_tg.stream_session import StreamingBusy

    with pytest.raises(StreamingBusy):
        await session.handle_message(1, "second", send=rec.send, edit=rec.edit)
    # The queue stayed empty (a busy project never consumes a queue entry).
    assert len(session._chats[1].run_queue) == 0

    eng.cancel()
    await asyncio.wait_for(turn, timeout=2.0)


# ===========================================================================
#    RB7 (RB5 under concurrency): several projects emitting status bursts concurrently
#    produce a BOUNDED per-chat send rate and NO dropped verbatim. The per-chat ChatSendGate
#    serializes ALL outbound; verbatim (ask/result) is never starved by status churn.
# ===========================================================================


async def test_rb7_concurrent_status_bursts_bounded_rate_and_no_dropped_verbatim(tmp_path):
    """Two concurrent turns each emit a status burst + a verbatim result through ONE per-chat
    send gate (interval=1.0, a recording clock/sleep): EVERY verbatim survives (rate-ordered,
    never dropped) and the combined send rate is BOUNDED (the gate computed ≥1-interval waits
    for the contended sends — they did not all fire in the same instant).

    Non-vacuous on two axes: (1) if the per-chat gate were bypassed (concurrent projects' sends
    not funneled through one gate), the recorded ``sleeps`` would be all-zero (every send
    immediate) → the rate-bound assert fails; (2) both verbatim results are asserted present —
    a gate that DROPPED a contended verbatim instead of ordering it (the D8 anti-pattern) would
    lose one. Status bursts coalescing to a single leading-edge edit each is the expected RB5
    throttle (so a starved status line is acceptable; a starved/dropped verbatim is not)."""
    store = two_project_store(tmp_path, a="alpha", b="beta", active="alpha")
    clk = RecordingClock()
    # alpha: a status burst, a foreground HOLD (we start beta concurrently during it), then its
    # verbatim result. beta: its own status burst + verbatim result. Each project is foreground
    # during its OWN turn (active is switched to it), so each renders its verbatim INLINE — and
    # ALL of it funnels through the single per-chat ChatSendGate (the RB5/D8 surface under test).
    eng_a = MatrixEngine(
        [
            TextEvent(text="a1", incremental=True),
            TextEvent(text="a2", incremental=True),
            HOLD,  # park alpha so beta can run concurrently
            ResultEvent(session_id="sess-A", is_error=False, subtype="success", result_text="ALPHA-RESULT"),
        ],
        session_id="sess-A",
    )
    eng_b = MatrixEngine(
        [
            TextEvent(text="b1", incremental=True),
            TextEvent(text="b2", incremental=True),
            ResultEvent(session_id="sess-B", is_error=False, subtype="success", result_text="BETA-RESULT"),
        ],
        session_id="sess-B",
    )
    session = make_matrix_session(
        {"/work/alpha": eng_a, "/work/beta": eng_b},
        store=store,
        clock=clk.now,
        sleep=clk.sleep,
        chat_send_interval=1.0,
    )
    rec = Recorder()

    # Start alpha (foreground; renders inline status, then parks at HOLD). Switch active to beta
    # and run beta concurrently to completion. Switch back to alpha and release it so BOTH turns'
    # sends went through the one gate while both engines were live.
    turn_a = asyncio.create_task(session.handle_message(1, "go A", send=rec.send, edit=rec.edit))
    await _wait(lambda: session.is_busy(1, "alpha"))
    store.switch(1, "beta")
    turn_b = asyncio.create_task(session.handle_message(1, "go B", send=rec.send, edit=rec.edit))
    await asyncio.wait_for(turn_b, timeout=3.0)  # beta runs to completion concurrently
    assert session.is_busy(1, "alpha"), "alpha's run is still live (parked) while beta ran"
    store.switch(1, "alpha")
    eng_a.release()  # release alpha's HOLD → its verbatim result renders
    await asyncio.wait_for(turn_a, timeout=3.0)

    # NO verbatim dropped: BOTH concurrent turns' results survived through the one gate (D8 —
    # verbatim is rate-ordered, never discarded).
    assert any("ALPHA-RESULT" in s["text"] for s in rec.sends), "alpha verbatim must survive"
    assert any("BETA-RESULT" in s["text"] for s in rec.sends), "beta verbatim must survive"
    # The combined rate was BOUNDED: the gate computed non-zero waits for the contended sends
    # (so the concurrent sends were spaced ≥ interval apart, not all fired at once). With
    # interval=1.0 and a frozen clock, each subsequent send reserves ≥1.0s behind the prior.
    assert any(w >= 1.0 for w in clk.sleeps), "the per-chat gate must space concurrent sends (RB5/D8)"


# ===========================================================================
# 10. One-shot unchanged (regression): one-shot mode behaves exactly as pre-P5.
#
#     The streaming concurrency machinery (cap/queue/index/notifications/send-gate) is gated
#     behind ENGINE_MODE=streaming. In one-shot the bot drives the legacy runner and the
#     schema-v2 store's flat load()/update() contract is byte-for-byte unaffected.
# ===========================================================================


async def test_oneshot_message_drives_legacy_runner_not_streaming(tmp_path):
    """In one-shot mode an inbound message drives the legacy ClaudeRunner (NOT the streaming
    session / its concurrency machinery) — the pre-P5 path, unchanged.

    Non-vacuous: if the bot routed one-shot messages into the streaming session, the runner's
    ``run_calls`` would be empty and the streaming ``handle_message`` would have fired instead.
    """
    runner = FakeRunner()
    # A streaming session is constructed but must NOT be used in one-shot mode.
    streaming = MagicMock()
    streaming.handle_message = AsyncMock()
    streaming.store = None
    bot = TelegramClaudeBot(
        make_config(engine_mode="oneshot", allow_any_path=True), runner, streaming=streaming
    )
    upd = make_update(1, "hello there")
    await bot.on_message(upd, make_ctx())

    # The legacy runner ran; the streaming session's turn driver was NOT invoked.
    assert runner.run_calls == [(1, "hello there")]
    streaming.handle_message.assert_not_awaited()


def test_oneshot_flat_store_contract_unchanged_by_v2_schema(tmp_path):
    """The pre-P5 one-shot flat ``update()``/``load()`` contract is byte-for-byte unaffected by
    the v2 multi-project schema: a one-shot chat reads back exactly the (session_id, cwd) it
    wrote, with no project structure leaking into its flat view (RB6 regression)."""
    store = JsonSessionStore(tmp_path / "state.json")
    # One-shot writes via the flat update() (no project name).
    store.update(1, session_id="one-shot-sess", cwd="/work/here")
    flat = store.load()
    assert flat["1"]["session_id"] == "one-shot-sess"
    assert flat["1"]["cwd"] == "/work/here"
    # A concurrent streaming chat's project CRUD on a DIFFERENT chat does not perturb chat 1's
    # flat view (isolation — the v2 store keys per chat).
    store.create(2, "proj", "/work/proj", make_active=True)
    store.set_session_id(2, "proj", "streaming-sess")
    flat2 = store.load()
    assert flat2["1"] == {"session_id": "one-shot-sess", "cwd": "/work/here"}
