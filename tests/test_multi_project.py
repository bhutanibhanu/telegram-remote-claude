"""P4 / T8 — multi-project integration & SB·RB acceptance matrix.

These prove the design's *cross-task* acceptance criteria end-to-end — no single
task owns them. They favor a REAL :class:`JsonSessionStore` and a REAL
:class:`StreamingSession` (the registry CRUD + the per-project turn driver under
test) over mocks; the engine is a scripted fake (no SDK / no network). The bot-level
scenarios drive the real ``TelegramClaudeBot`` command handlers over that stack.

Grouped by the T8 brief:

* **Integration acceptance** (headline criteria): two-projects-independent-state,
  restart-resumes-both, full lifecycle, and the ⭐ busy-guard-during-an-answer-hold.
* **RB6 persistence durability:** one-shot-unchanged regression + atomic temp+replace.
* **Deferred store contracts (T2/T3):** future-version clobber, empty-string omit, and
  the malformed-doc never-crash matrix (incl. a dangling ``active``).

The relative-path ``/new`` (T6) and the ``send``-raises / ``_stop_other_started`` /
``_resume_id`` defensive branches (T7) live alongside their siblings in
``test_bot_streaming.py`` / ``test_stream_session.py`` respectively; this file owns the
genuinely cross-cutting scenarios.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_tg.bot import TelegramClaudeBot
from claude_tg.engine.types import AskEvent, ResultEvent
from claude_tg.session_store import (
    SCHEMA_VERSION,
    JsonSessionStore,
    UnknownProject,
)
from claude_tg.stream_session import StreamingSession

# Reuse the established bot+store harness (real StreamingSession over a real store, a
# HoldEngine factory, the mock-Telegram update/ctx builders) rather than re-deriving it.
from tests.test_bot_streaming import (
    HOLD,
    FakeRunner,
    make_cmd_ctx,
    make_config,
    make_ctx,
    make_streaming,
    make_update,
)

# ---------------------------------------------------------------------------
# A scripted fake engine that records the prompt(s) its send() was driven with and
# parks on a HOLD sentinel (so we can prove a turn resumed ITS OWN session, and that no
# auto-replay happens on restart). Distinct instance per cwd via make_multi_session.
# ---------------------------------------------------------------------------


class RecordingEngine:
    """Records resume id + every prompt; HOLD parks send() until resolve()/cancel()."""

    def __init__(self, script, *, session_id="sess"):
        self._script = script
        self.session_id = session_id
        self.started = False
        self.resumed: str | None = None
        self.stopped = False
        self.prompts: list[str] = []
        self._gate = asyncio.Event()

    async def start(self):
        self.started = True

    async def resume(self, session_id):
        self.resumed = session_id
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        self.prompts.append(prompt)  # the load-bearing record: WHICH prompt drove this engine
        for item in self._script:
            if item is HOLD:
                await self._gate.wait()
                self._gate.clear()
                continue
            yield item

    def resolve(self, tool_use_id, decision):
        self._gate.set()
        return True

    def cancel(self, tool_use_id=None):
        self._gate.set()
        return 1


def make_multi_session(engines_by_cwd, *, store, config=None):
    """A StreamingSession whose factory returns a DISTINCT engine per project cwd.

    Mirrors ``test_stream_session.make_multi_session`` but accepts the recording engines
    here. The same cwd always yields the same engine (a project's runtime is built once
    and reused within the process), so a test can assert which engine ran / resumed /
    stopped and with which prompt.
    """
    return StreamingSession(
        config or make_config(engine_mode="streaming", allow_any_path=True),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engines_by_cwd[cwd],
        clock=lambda: 0.0,
    )


async def _drive(session, chat_id, text):
    """Run one turn to completion via the session (bounded so a wiring bug fails fast)."""
    sends: list[dict] = []

    async def send(*, text, reply_markup=None, parse_mode=None):
        sends.append({"text": text})
        return 1

    async def edit(*, message_id, text, parse_mode=None):
        return None

    await asyncio.wait_for(
        session.handle_message(chat_id, text, send=send, edit=edit), timeout=2.0
    )
    return sends


# ===========================================================================
# 1. Two projects, independent state — a turn in A never mutates B's record.
# ===========================================================================


async def test_two_projects_keep_independent_session_and_cwd(tmp_path):
    """Create A and B with different cwds; drive a turn in each. Each project keeps its
    OWN (session_id, cwd); a turn in one never touches the other's record."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)

    eng_alpha = RecordingEngine(
        [ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="a")],
        session_id="alpha-sid",
    )
    eng_beta = RecordingEngine(
        [ResultEvent(session_id="beta-sid", is_error=False, subtype="success", result_text="b")],
        session_id="beta-sid",
    )
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )

    # Turn in A (active) → persists A's session_id, leaves B pristine.
    await _drive(session, 1, "hi alpha")
    assert store.get_project(1, "alpha")["session_id"] == "alpha-sid"
    assert store.get_project(1, "beta")["session_id"] is None  # B untouched by A's turn
    assert eng_alpha.prompts == ["hi alpha"] and eng_beta.prompts == []

    # Switch to B and drive a turn there → persists B's id; A's record is unchanged.
    store.switch(1, "beta")
    await _drive(session, 1, "hi beta")

    alpha_rec = store.get_project(1, "alpha")
    beta_rec = store.get_project(1, "beta")
    # Each project's (session_id, cwd) is independent.
    assert (alpha_rec["session_id"], alpha_rec["cwd"]) == ("alpha-sid", "/work/alpha")
    assert (beta_rec["session_id"], beta_rec["cwd"]) == ("beta-sid", "/work/beta")
    # B's turn drove B's engine with B's prompt; A's engine never saw it.
    assert eng_beta.prompts == ["hi beta"]
    assert eng_alpha.prompts == ["hi alpha"]  # A's turn record did NOT change


# ===========================================================================
# 2. Restart resumes BOTH — a fresh session over the SAME store reloads both
#    projects, preserves the active one, lazily resumes each project's OWN session,
#    and starts every project with a fresh (yolo-off / no-grant) policy.
# ===========================================================================


async def test_restart_resumes_both_projects_each_with_its_own_session(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=False)
    store.create(1, "beta", "/work/beta", make_active=True)  # beta is the active one
    store.switch(1, "alpha")
    store.update(1, session_id="alpha-sid", cwd=None)  # alpha active → its session
    store.switch(1, "beta")
    store.update(1, session_id="beta-sid", cwd=None)  # beta active → its session
    # Final on-disk state: both have distinct session_ids, beta is active.
    assert store.get_active(1) == "beta"

    # --- simulated restart: a FRESH StreamingSession over the SAME store ---
    eng_alpha = RecordingEngine(
        [ResultEvent(session_id="alpha-sid", is_error=False, subtype="success")],
        session_id="alpha-sid",
    )
    eng_beta = RecordingEngine(
        [ResultEvent(session_id="beta-sid", is_error=False, subtype="success")],
        session_id="beta-sid",
    )
    session = make_multi_session(
        {"/work/alpha": eng_alpha, "/work/beta": eng_beta}, store=store
    )

    # Both projects are present with the correct cwd/session_id, active preserved.
    assert set(store.list_projects(1)) == {"alpha", "beta"}
    assert store.get_active(1) == "beta"
    assert store.get_project(1, "alpha")["session_id"] == "alpha-sid"
    assert store.get_project(1, "beta")["session_id"] == "beta-sid"

    # Fresh policies (the transient bypass is reset on restart — D3/SB5). Resolve each
    # project's runtime explicitly and assert the fail-closed posture (yolo off, no grants).
    store.switch(1, "alpha")
    _na, rt_a = session._active_runtime(1, create_default=True)
    store.switch(1, "beta")
    _nb, rt_b = session._active_runtime(1, create_default=True)
    assert rt_a.policy.yolo is False and rt_a.policy.granted_tools() == frozenset()
    assert rt_b.policy.yolo is False and rt_b.policy.granted_tools() == frozenset()

    # A turn against the active project (beta) lazily resumes BETA's session.
    await _drive(session, 1, "back to beta")
    assert eng_beta.resumed == "beta-sid" and eng_beta.started is True

    # Switching to alpha and driving a turn lazily resumes ALPHA's own session.
    store.switch(1, "alpha")
    await _drive(session, 1, "back to alpha")
    assert eng_alpha.resumed == "alpha-sid" and eng_alpha.started is True
    # Each engine resumed ITS id (the factory records the resumed id per cwd) — proof the
    # two projects resumed independently, not against a shared/chat-global session.
    assert eng_alpha.resumed != eng_beta.resumed


# ===========================================================================
# 3. Full lifecycle via the real bot + store: /new a, /new b (auto-switch),
#    /projects (lists both, active marker), /switch a, /rm b (non-active),
#    /rm a refused (active) → /switch away → /rm a.
# ===========================================================================


async def test_full_project_lifecycle_via_bot(tmp_path):
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    store = JsonSessionStore(tmp_path / "state.json")
    session, _ = make_streaming(store, workdir=str(tmp_path))
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )

    # /new a → created + active.
    await bot.cmd_new(make_update(1, "/new a " + str(dir_a)), make_cmd_ctx(args=["a", str(dir_a)]))
    assert store.get_active(1) == "a"
    # /new b → created + auto-switched (active flips to b).
    await bot.cmd_new(make_update(1, "/new b " + str(dir_b)), make_cmd_ctx(args=["b", str(dir_b)]))
    assert store.get_active(1) == "b"
    assert set(store.list_projects(1)) == {"a", "b"}

    # /projects → lists both, marks the active (b) and not the inactive (a).
    upd = make_update(1, "/projects")
    await bot.cmd_projects(upd, make_cmd_ctx())
    reply = upd.message.reply_text.await_args.args[0]
    assert str(dir_a) in reply and str(dir_b) in reply  # both cwds listed
    # The active project's line carries the marker; the inactive one does not. R6: /projects
    # now sends HTML — the name is wrapped in <b>…</b> and the cwd in <code>…</code> so the
    # path renders as inert monospace (not tappable fake /segment command-links). Match on
    # the bolded name token ("<b>b</b>" / "<b>a</b>") rather than the old bare "<name> —"
    # shape, which the <b> wrapper now breaks.
    b_line = next(line for line in reply.splitlines() if "<b>b</b>" in line)
    a_line = next(line for line in reply.splitlines() if "<b>a</b>" in line)
    assert "→" in b_line and "→" not in a_line

    # /switch a → active flips back to a.
    await bot.cmd_switch(make_update(1, "/switch a"), make_cmd_ctx(args=["a"]))
    assert store.get_active(1) == "a"

    # /rm b → non-active removal succeeds.
    upd = make_update(1, "/rm b")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["b"]))
    assert set(store.list_projects(1)) == {"a"}
    assert "removed" in upd.message.reply_text.await_args.args[0].lower()

    # /rm a → REFUSED (a is active); switch away first.
    upd = make_update(1, "/rm a")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["a"]))
    assert "active" in upd.message.reply_text.await_args.args[0].lower()
    assert "a" in store.list_projects(1)  # not removed

    # There is no other project to switch to → create one, switch, then remove a.
    dir_c = tmp_path / "c"
    dir_c.mkdir()
    await bot.cmd_new(make_update(1, "/new c " + str(dir_c)), make_cmd_ctx(args=["c", str(dir_c)]))
    assert store.get_active(1) == "c"  # /new c auto-switched away from a
    upd = make_update(1, "/rm a")
    await bot.cmd_rm(upd, make_cmd_ctx(args=["a"]))
    assert "a" not in store.list_projects(1)  # now removable (no longer active)
    assert set(store.list_projects(1)) == {"c"}


# ===========================================================================
# 4. ⭐ FREE /switch + /new during an ANSWER-HOLD, then the held turn STILL resolves
#    (the highest-value P5 regression — the INVERSE of P4's busy-guard ⭐ test).
#
# A turn parks awaiting an interactive answer (an AskEvent → the turn loop is inside
# engine.send, alpha's lock held, is_busy True). WHILE parked, /switch to beta AND /new
# gamma now SUCCEED (the D2 relaxation): store.switch / store.create ARE called and the
# active project moves OFF alpha. THEN alpha's hold is resolved via the REAL callback
# path (resolve_callback, lock-free, id-routed by tool_use_id) and alpha's held turn
# COMPLETES — proving that switching away did NOT strand the parked turn (id-routing,
# ADR-005 D3, is the correctness guarantee that makes the relaxation safe). This is the
# load-bearing inverse of the P4 ⭐ test (which asserted the refusal): a tap for alpha
# resolves alpha even though beta/gamma became active. The cross-project never-resolve-B
# property is exhaustively pinned end-to-end in T10.
# ===========================================================================


async def test_free_switch_and_new_during_answer_hold_then_alpha_still_resolves(tmp_path):
    store = JsonSessionStore(tmp_path / "state.json")
    # beta + gamma cwds are REAL dirs inside the bot's permitted roots so /switch's and
    # /new's SB2 cwd re-validation passes (those gates survive the busy-guard relaxation).
    # alpha's cwd can stay out-of-roots: its turn is driven by the session (allow_any_path),
    # not re-validated by the bot.
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()
    gamma_dir = tmp_path / "gamma"
    gamma_dir.mkdir()
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", str(beta_dir), make_active=False)

    # The turn yields an ask, then PARKS (HOLD) awaiting the operator's answer, then
    # finishes once resolved. This is a genuine answer-hold (alpha's lock held throughout).
    ask = AskEvent(
        questions=[{"question": "Proceed?", "options": [{"label": "Yes"}, {"label": "No"}]}],
        tool_use_id="hold-tid",
    )
    session, engine = make_streaming(
        store,
        script=[ask, HOLD, ResultEvent(session_id="alpha-sid", is_error=False, subtype="success", result_text="done")],
    )
    # allow_any_path=True on the SESSION (make_streaming default) so the driver's turn path
    # is not SB2-blocked; the BOT gets real roots so /new's SB2 confinement is exercised.
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", workdir=str(tmp_path), allowed_roots=(tmp_path,)),
        FakeRunner(),
        streaming=session,
    )

    # Drive the turn; it parks at the answer-hold holding alpha's lock.
    ctx = make_ctx()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    turn = asyncio.create_task(bot.on_message(make_update(1, "go"), ctx))
    for _ in range(500):
        if session.is_busy(1, "alpha"):
            break
        await asyncio.sleep(0)
    assert session.is_busy(1, "alpha"), "the held turn must hold alpha's lock during the hold"
    # The held turn rendered the ask (its tool_use_id is in the pending index, routed to
    # the owning project) — a real answer-hold (P5 / ADR-005 D3).
    assert "hold-tid" in session._chat(1).pending_index
    assert session._chat(1).pending_index["hold-tid"].project_name == "alpha"

    # /switch beta WHILE alpha is parked → SUCCEEDS (the relaxation), active moves to beta.
    up_sw = make_update(1, "/switch beta")
    await bot.cmd_switch(up_sw, make_cmd_ctx(args=["beta"]))
    sw_reply = up_sw.message.reply_text.await_args.args[0]
    # P9: the project name is bolded + escaped (HTML), uniform with every name-bearing reply.
    assert "switched to <b>beta</b>" in sw_reply.lower(), sw_reply
    assert store.get_active(1) == "beta"

    # /new gamma WHILE alpha is parked → SUCCEEDS, gamma created + active.
    up_new = make_update(1, "/new gamma " + str(gamma_dir))
    await bot.cmd_new(up_new, make_cmd_ctx(args=["gamma", str(gamma_dir)]))
    new_reply = up_new.message.reply_text.await_args.args[0]
    # R6: /new now confirms in HTML — the name is wrapped in <b>…</b> (and the cwd in
    # <code>…</code> so the path renders as monospace, not fake /segment command-links), so
    # the old "created gamma" substring no longer matches across the tag. Assert the
    # success word + the bolded name token instead (still proves it created gamma, not a
    # busy/error reply).
    assert "created" in new_reply.lower() and "<b>gamma</b>" in new_reply, new_reply
    assert store.get_active(1) == "gamma"

    # alpha is STILL parked + busy — switching/creating did NOT disturb its run.
    assert session.is_busy(1, "alpha"), "alpha's held turn must survive the switch + new"
    assert not turn.done()

    # THEN resolve ALPHA's hold via the REAL callback path (lock-free, id-routed) — even
    # though GAMMA is now the active/foreground project. The held turn must unblock and
    # complete: switching away did NOT strand it (id-routing resolves alpha by tool_use_id,
    # not _active_engine). This is the precise behavior the P4 busy-guard existed to avoid
    # and that P5 makes safe.
    from claude_tg.render import encode_callback

    outcome = session.resolve_callback(
        1, encode_callback("a", "hold-tid", question_index=0, option_index=0)
    )
    assert outcome.handled is True
    # The parked turn now runs to completion — bounded so a deadlock regression fails fast.
    await asyncio.wait_for(turn, timeout=2.0)
    assert session.is_busy(1, "alpha") is False  # alpha's lock released cleanly at turn end


async def test_switch_succeeds_when_idle(tmp_path):
    """Sanity companion to the ⭐ test: /switch SUCCEEDS on an idle chat (it always did).

    Was the P4 ``test_busy_guard_false_pass_switch_succeeds_when_idle`` false-pass guard
    (idle → switch succeeds, proving the refusal was gated on is_busy not blanket). The
    refusal is gone in P5, so this now just pins that an idle /switch still works — the
    busy case is covered by the ⭐ test above (switch succeeds mid-hold too)."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.create(1, "alpha", "/work/alpha", make_active=True)
    store.create(1, "beta", "/work/beta", make_active=False)
    session, _ = make_streaming(store)  # no turn driven → idle
    # allow_any_path=True so the SB2 cwd re-validation no-ops for the fake /work/beta cwd.
    bot = TelegramClaudeBot(
        make_config(engine_mode="streaming", allow_any_path=True), FakeRunner(), streaming=session
    )
    assert session.is_busy(1) is False
    await bot.cmd_switch(make_update(1, "/switch beta"), make_cmd_ctx(args=["beta"]))
    assert store.get_active(1) == "beta"  # the switch went through when idle


# ===========================================================================
# 5. RB6 — one-shot UNCHANGED regression against a v2 store (complements T2).
#    Streaming registry ops must not disturb the one-shot flat view, and vice versa.
# ===========================================================================


def test_oneshot_flat_view_unchanged_by_registry_ops(tmp_path):
    """The pre-P4 one-shot contract (flat load()/update() over the active project) is
    byte-for-byte unaffected by streaming registry CRUD, and the flat update() does not
    corrupt the registry view. This is the integration-level RB6 regression."""
    store = JsonSessionStore(tmp_path / "state.json")

    # One-shot path: update()/load() behave EXACTLY as pre-P4 (a single active session).
    store.update(1, session_id="one-shot-sid", cwd="/oneshot")
    assert store.load()["1"] == {"session_id": "one-shot-sid", "cwd": "/oneshot"}

    # A streaming registry op on a DIFFERENT chat must not perturb chat 1's flat view.
    store.create(2, "proj", "/work/proj", make_active=True)
    store.update(2, session_id="streaming-sid", cwd=None)
    assert store.load()["1"] == {"session_id": "one-shot-sid", "cwd": "/oneshot"}  # untouched

    # Adding a SECOND project to chat 1 (registry op) leaves the flat view on the ACTIVE
    # (default) project unchanged — the flat view follows active, undisturbed by the add.
    store.create(1, "second", "/work/second", make_active=False)
    assert store.load()["1"] == {"session_id": "one-shot-sid", "cwd": "/oneshot"}

    # And the flat update() did not corrupt the registry: chat 1's default project still
    # carries the one-shot fields; the added project is intact and inactive.
    assert store.get_active(1) == "default"
    assert store.get_project(1, "default")["session_id"] == "one-shot-sid"
    assert store.get_project(1, "second")["cwd"] == "/work/second"
    # The clear (session_id=None) still clears ONLY the active project (pre-P4 semantics).
    store.update(1, session_id=None, cwd=None)
    assert "session_id" not in store.load()["1"]
    assert store.load()["1"]["cwd"] == "/oneshot"  # cwd preserved


# ===========================================================================
# 6. RB6 — atomicity: writes go via temp+replace; no .tmp left, target valid v2.
#    Plus a simulated interrupted write leaving the prior good file intact.
# ===========================================================================


def test_write_is_atomic_no_tmp_left_and_target_valid_v2(tmp_path):
    p = tmp_path / "state.json"
    store = JsonSessionStore(p)
    store.create(1, "alpha", "/work/alpha", make_active=True)

    tmp = p.with_name(p.name + ".tmp")
    assert not tmp.exists()  # temp was swapped in via replace(), not left behind
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["version"] == SCHEMA_VERSION
    assert on_disk["chats"]["1"]["projects"]["alpha"]["cwd"] == "/work/alpha"

    # A second write (flat update) is likewise atomic and leaves no temp residue.
    store.update(1, session_id="sid", cwd=None)
    assert not tmp.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == SCHEMA_VERSION


def test_interrupted_write_leaves_prior_good_file_intact(tmp_path, monkeypatch):
    """Simulate a crash DURING a write (after the temp is written, before/at replace):
    the prior good file must remain valid and unchanged — atomic replace never leaves a
    half-written target (RB6). The temp may linger after a crash; the target is intact."""
    p = tmp_path / "state.json"
    store = JsonSessionStore(p)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    good = p.read_text(encoding="utf-8")
    good_doc = json.loads(good)

    # Make the replace() step blow up (a crash at the worst moment — the temp is written
    # but the swap onto the target never completes). _save_raw calls tmp.replace(target).
    def boom_replace(self, target):
        raise OSError("simulated crash during atomic replace")

    monkeypatch.setattr(Path, "replace", boom_replace)
    with pytest.raises(OSError):
        store.create(1, "beta", "/work/beta", make_active=False)
    monkeypatch.undo()

    # The PRIOR good file is intact and still valid v2 — the failed write did not corrupt
    # or truncate the target (only a sibling .tmp could be left, never the target).
    assert p.is_file()
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after == good_doc  # unchanged: still only "alpha", version 2
    assert after["version"] == SCHEMA_VERSION
    # Sanity: the store reads the prior good state back without raising.
    assert store.get_active(1) == "alpha"
    assert set(store.list_projects(1)) == {"alpha"}


# ===========================================================================
# 7. Deferred (T2): update() after an UNKNOWN/FUTURE-version load starts a clean
#    v2 — it does NOT preserve the future doc (the SB6 fail-safe-clobber contract).
# ===========================================================================


def test_update_after_future_version_clobbers_to_clean_v2(tmp_path):
    p = tmp_path / "state.json"
    # A future-version doc with rich contents we must NOT carry forward.
    p.write_text(
        json.dumps(
            {
                "version": 999,
                "chats": {"1": {"active": "secret", "projects": {"secret": {"cwd": "/x"}}}},
                "extra_future_field": {"do": "not preserve"},
            }
        ),
        encoding="utf-8",
    )
    store = JsonSessionStore(p)
    # Load already fails safe to empty (T2); the deferred assertion is that a WRITE on top
    # starts a clean v2 — the future doc is clobbered, not merged.
    assert store.load() == {}
    store.update(1, session_id="fresh", cwd="/fresh")

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["version"] == SCHEMA_VERSION
    assert "extra_future_field" not in on_disk  # the future doc was NOT preserved
    # The only chat/project is the freshly written one — the "secret" project is gone.
    assert set(on_disk["chats"]) == {"1"}
    assert "secret" not in on_disk["chats"]["1"]["projects"]
    assert store.load()["1"] == {"session_id": "fresh", "cwd": "/fresh"}


def test_create_after_future_version_clobbers_to_clean_v2(tmp_path):
    """The registry CRUD path (create) on top of a future-version doc likewise starts a
    clean v2 — confirming the fail-safe-clobber holds for both write entrypoints."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 7, "keep": "nothing"}), encoding="utf-8")
    store = JsonSessionStore(p)
    store.create(1, "alpha", "/work/alpha", make_active=True)
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["version"] == SCHEMA_VERSION
    assert "keep" not in on_disk
    assert set(on_disk["chats"]["1"]["projects"]) == {"alpha"}


# ===========================================================================
# 8. Deferred (T2): an empty-string session_id / cwd on disk normalizes to ABSENT
#    in the flat view (documents the truthy-omit).
# ===========================================================================


def test_empty_string_session_id_and_cwd_are_omitted_from_flat_view(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "chats": {
                    # Empty strings for BOTH → the chat carries nothing observable → absent.
                    "1": {
                        "active": "a",
                        "projects": {"a": {"cwd": "", "session_id": "", "created_at": "t", "last_active": "t"}},
                    },
                    # Empty session_id but a real cwd → only cwd surfaces (session omitted).
                    "2": {
                        "active": "b",
                        "projects": {"b": {"cwd": "/real", "session_id": "", "created_at": "t", "last_active": "t"}},
                    },
                    # A real session_id but empty cwd → only session_id surfaces.
                    "3": {
                        "active": "c",
                        "projects": {"c": {"cwd": "", "session_id": "sid", "created_at": "t", "last_active": "t"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    flat = JsonSessionStore(p).load()
    assert "1" not in flat  # both empty → whole chat omitted (pre-P4 "pop empty")
    assert flat["2"] == {"cwd": "/real"}  # empty session_id dropped
    assert flat["3"] == {"session_id": "sid"}  # empty cwd dropped


# ===========================================================================
# 9. Deferred (T3): SB6 malformed-doc never-crash — the registry accessors degrade
#    to None / {} / UnknownProject against a hand-edited doc rather than raising:
#    non-dict chats/projects, non-str keys, and a DANGLING active (pointing at a
#    project that does not exist).
# ===========================================================================


def _store_with(tmp_path, doc) -> JsonSessionStore:
    p = tmp_path / "state.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return JsonSessionStore(p)


def test_malformed_chats_not_a_dict_never_crashes(tmp_path):
    store = _store_with(tmp_path, {"version": 2, "chats": [1, 2, 3]})  # chats is a list
    # _load_raw normalizes a non-dict chats container to empty; every accessor is benign.
    assert store.get_active(1) is None
    assert store.list_projects(1) == {}
    assert store.get_project(1, "x") is None
    assert store.load() == {}
    with pytest.raises(UnknownProject):
        store.switch(1, "x")
    with pytest.raises(UnknownProject):
        store.remove(1, "x")
    with pytest.raises(UnknownProject):
        store.touch(1, "x")


def test_malformed_projects_not_a_dict_never_crashes(tmp_path):
    store = _store_with(
        tmp_path, {"version": 2, "chats": {"1": {"active": "a", "projects": "oops"}}}
    )
    assert store.get_active(1) == "a"  # the active STRING is still returned as-is
    assert store.list_projects(1) == {}  # non-dict projects → empty, no crash
    assert store.get_project(1, "a") is None
    assert store.load() == {}  # active project unresolvable → chat omitted
    with pytest.raises(UnknownProject):
        store.switch(1, "a")
    with pytest.raises(UnknownProject):
        store.remove(1, "a")


def test_malformed_non_str_keys_and_non_dict_records_are_skipped(tmp_path, monkeypatch):
    # JSON object keys are ALWAYS strings on disk, so a genuine non-str key can only reach
    # the accessors from an in-memory raw doc (a defensive guard, not a disk shape). Drive
    # _load_raw to return such a doc and assert list_projects / get_project / _resolve_name
    # degrade — the non-str key and the non-dict record are skipped, the valid one survives,
    # and switch() on the non-str key raises UnknownProject rather than crashing.
    store = JsonSessionStore(tmp_path / "state.json")
    malformed = {
        "version": 2,
        "chats": {
            "1": {
                "active": "good",
                "projects": {
                    "good": {"cwd": "/g", "session_id": None, "created_at": "t", "last_active": "t"},
                    2: {"cwd": "/n"},  # non-str key (only expressible in memory)
                    "bad": "not-a-dict",  # non-dict record
                },
            }
        },
    }
    monkeypatch.setattr(store, "_load_raw", lambda: malformed)

    listed = store.list_projects(1)
    assert set(listed) == {"good"}  # non-str key + non-dict record both skipped
    assert store.get_project(1, "good")["cwd"] == "/g"
    assert store.get_project(1, "bad") is None  # non-dict record → None, no crash
    # touch() asserts the resolved record is a dict, so a non-dict record raises rather
    # than mutating junk — degrade, don't crash.
    with pytest.raises(UnknownProject):
        store.touch(1, "bad")
    # A truly absent name still raises UnknownProject (the resolve path is unperturbed).
    with pytest.raises(UnknownProject):
        store.switch(1, "missing")


def test_dangling_active_pointing_at_missing_project_never_crashes(tmp_path):
    # active names a project that does not exist in projects → the flat view omits the
    # chat, get_active returns the stored (dangling) name, and switch/remove/touch of the
    # dangling name raise UnknownProject (degrade, don't crash).
    store = _store_with(
        tmp_path,
        {
            "version": 2,
            "chats": {
                "1": {
                    "active": "ghost",  # dangling — no such project
                    "projects": {
                        "real": {"cwd": "/r", "session_id": "sid", "created_at": "t", "last_active": "t"}
                    },
                }
            },
        },
    )
    assert store.get_active(1) == "ghost"  # the stored active string (even if dangling)
    assert store.load() == {}  # active project unresolvable → chat omitted from flat view
    assert store.get_project(1, "ghost") is None
    assert set(store.list_projects(1)) == {"real"}  # the real project is still listed
    with pytest.raises(UnknownProject):
        store.switch(1, "ghost")
    with pytest.raises(UnknownProject):
        store.touch(1, "ghost")
    # A switch to the REAL project still works (the doc is usable, not wedged).
    store.switch(1, "real")
    assert store.get_active(1) == "real"
    assert store.load()["1"] == {"session_id": "sid", "cwd": "/r"}


def test_chat_entry_not_a_dict_never_crashes(tmp_path):
    # A chat whose value is not a dict (hand-edited junk) is skipped everywhere.
    store = _store_with(tmp_path, {"version": 2, "chats": {"1": "junk", "2": ["also junk"]}})
    assert store.get_active(1) is None
    assert store.list_projects(2) == {}
    assert store.get_project(1, "x") is None
    assert store.load() == {}
    with pytest.raises(UnknownProject):
        store.switch(1, "x")
