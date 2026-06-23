"""P6/C2 (SB2): SDK-tool path confinement — the path layer of the permission gate.

P2's classifier is name-only (``SAFE_TOOLS`` auto-run on ANY path; a session-granted
risky tool acts on ANY path). P6/C2 adds a **path** layer: a file/search tool whose
RESOLVED target falls OUTSIDE ``allowed_roots`` must be operator-approved — even an
otherwise-auto SAFE tool (Read/Glob/LS) and even a session-granted risky one
(Write/Edit). The check runs AFTER ``/yolo`` (the explicit allow-all opt-out) and is
disabled by ``ALLOW_ANY_PATH=true`` (the other opt-out); it comes BEFORE the name-only
safe/grant short-circuit so an out-of-root call ALWAYS re-prompts.

Two surfaces:

* :func:`~claude_tg.permissions.path_needs_approval` directly — a PURE predicate:
  per-tool target extraction, canonical (``..``/symlink) resolution, fail-closed on a
  malformed-when-expected path, ``ALLOW_ANY_PATH`` opt-out.
* the full :class:`~claude_tg.engine.engine.Engine` end-to-end against a mock substrate
  (mirrors ``test_answer_hold``): proves an out-of-root tool HOLDS (emits a
  :class:`PermissionEvent`) even when name-only-safe or session-granted, that an in-root
  call auto-allows (no prompt), that ``/yolo`` bypasses, and that ``Bash`` is unchanged.

NO live Claude, NO network, NO real waits — the engine holds resolve via
``_resolve_when_pending`` (a 0-second poll), exactly as the answer-hold tests do.

The in-root cases double as a regression guard for the P2 behavior (a safe tool on an
in-root path must still auto-run; a granted risky tool on an in-root path must still be
suppressed) — confirming C2 did not change in-root semantics.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_tg.engine import Engine
from claude_tg.engine.types import PermissionDecision, PermissionEvent
from claude_tg.permissions import PermissionPolicy, path_needs_approval

# ===========================================================================
# Pure predicate: path_needs_approval (extraction + canonical resolve + SB6)
# ===========================================================================
#
# All pure tests pin ``cwd`` and a single allowed root to a real, existing tmp tree so
# the canonicalization (Path.resolve following ../symlinks) is exercised on real inodes.


@pytest.fixture()
def roots(tmp_path: Path):
    """An allowed-root tree (``<tmp>/root``) + a sibling OUT-of-root dir (``<tmp>/out``).

    Returns ``(cwd, allowed_roots)`` where cwd == the allowed root. Both exist on disk so
    ``resolve(strict=False)`` canonicalizes real paths (and a symlink test can target a
    real inode outside the root).
    """
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "out").mkdir()
    return str(root), (root,)


# ---- in-root: every path tool is allowed (no approval) ----------------------


@pytest.mark.parametrize(
    "tool,key",
    [
        ("Read", "file_path"),
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookRead", "notebook_path"),
        ("NotebookEdit", "notebook_path"),
        ("Glob", "path"),
        ("Grep", "path"),
        ("LS", "path"),
    ],
)
def test_in_root_path_tool_does_not_need_approval(tool, key, roots):
    cwd, allowed = roots
    inp = {key: str(Path(cwd) / "sub" / "f.py")}
    assert (
        path_needs_approval(tool, inp, cwd=cwd, allowed_roots=allowed, allow_any_path=False)
        is False
    )


def test_in_root_relative_path_resolved_against_cwd_is_allowed(roots):
    # A relative path is resolved against the (in-root) cwd → in-root → no approval.
    cwd, allowed = roots
    assert (
        path_needs_approval(
            "Read", {"file_path": "subdir/x.py"}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is False
    )


# ---- out-of-root: every path tool needs approval (THE core C2 fix) ----------


@pytest.mark.parametrize(
    "tool,key",
    [
        ("Read", "file_path"),
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookRead", "notebook_path"),
        ("NotebookEdit", "notebook_path"),
        ("Glob", "path"),
        ("Grep", "path"),
        ("LS", "path"),
    ],
)
def test_out_of_root_path_tool_needs_approval(tool, key, roots):
    # The CORE fix: a tool whose absolute target is outside the root requires approval —
    # including the otherwise-auto SAFE tools (Read/Glob/Grep/LS).
    cwd, allowed = roots
    inp = {key: "/etc/shadow"}
    assert (
        path_needs_approval(tool, inp, cwd=cwd, allowed_roots=allowed, allow_any_path=False)
        is True
    )


def test_notebookread_path_layer_in_root_vs_out_of_root(roots):
    # NotebookRead has a ``notebook_path`` input but was MISSING from _PATH_TOOL_KEYS, so the
    # path layer gave it no out-of-root framing (it still gated as RISKY via the name-only
    # classifier — not a hole — but the map should be exhaustive over path-bearing tools).
    # Same treatment as NotebookEdit: an out-of-root NotebookRead → approval via the path
    # layer; in-root → unchanged. Teeth: drop the NotebookRead entry from _PATH_TOOL_KEYS
    # and the out-of-root assertion goes RED (the path layer no longer governs it).
    cwd, allowed = roots
    # In-root notebook → NO approval via the path layer (unchanged).
    assert (
        path_needs_approval(
            "NotebookRead",
            {"notebook_path": str(Path(cwd) / "nb.ipynb")},
            cwd=cwd,
            allowed_roots=allowed,
            allow_any_path=False,
        )
        is False
    )
    # Out-of-root notebook → approval via the path layer (the C2 completeness fix).
    assert (
        path_needs_approval(
            "NotebookRead",
            {"notebook_path": "/etc/evil.ipynb"},
            cwd=cwd,
            allowed_roots=allowed,
            allow_any_path=False,
        )
        is True
    )
    # Required-path (like Read): a missing/None notebook_path is fail-closed → approval (SB6).
    assert (
        path_needs_approval(
            "NotebookRead", {}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )


def test_out_of_root_via_parent_traversal_is_caught(roots):
    # A ``..`` escape from the in-root cwd canonicalizes OUTSIDE the root → approval. This
    # is the resolve()-follows-".." guard (a raw string check would miss it).
    cwd, allowed = roots
    escape = "../out/secret.txt"  # <root>/../out == <tmp>/out, outside the root
    assert (
        path_needs_approval(
            "Read", {"file_path": escape}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )


def test_symlink_escape_is_caught_canonically(tmp_path: Path):
    # A symlink that lives INSIDE the root but points OUTSIDE it must be caught: resolve()
    # follows the link, so the canonical target is out-of-root → approval. A purely
    # lexical (string-prefix) check would be fooled (the link path starts with the root).
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")
    link = root / "link"  # inside root, but → outside
    link.symlink_to(outside)
    target = str(link / "loot.txt")  # <root>/link/loot.txt → <tmp>/outside/loot.txt
    assert (
        path_needs_approval(
            "Read", {"file_path": target}, cwd=str(root), allowed_roots=(root,), allow_any_path=False
        )
        is True
    )


# ---- ALLOW_ANY_PATH opt-out: the path policy is disabled --------------------


@pytest.mark.parametrize("tool,key", [("Read", "file_path"), ("Write", "file_path"), ("LS", "path")])
def test_allow_any_path_disables_path_policy(tool, key, roots):
    # ALLOW_ANY_PATH=true → the owner took the wheel; an out-of-root path no longer needs
    # approval (the path layer is off entirely, mirroring how it no-ops /cd confinement).
    cwd, allowed = roots
    inp = {key: "/etc/shadow"}
    assert (
        path_needs_approval(tool, inp, cwd=cwd, allowed_roots=allowed, allow_any_path=True)
        is False
    )


# ---- fail-closed (SB6) on a malformed-when-expected path --------------------


@pytest.mark.parametrize("missing", [{}, {"file_path": None}, {"other": "x"}])
def test_required_path_missing_or_none_fails_closed(missing, roots):
    # A required-path tool (Read/Write/…) with a missing / None path is unparseable when
    # a path is EXPECTED → fail closed → approval (never silently allowed). SB6.
    cwd, allowed = roots
    assert (
        path_needs_approval(
            "Read", missing, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )


@pytest.mark.parametrize("bad", [123, [], {}, "", b"x"])
def test_present_but_nonstr_or_empty_path_fails_closed(bad, roots):
    # A path key present but not a usable string (non-str / empty) → fail closed → approval.
    cwd, allowed = roots
    assert (
        path_needs_approval(
            "Write", {"file_path": bad}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )


def test_optional_path_tool_missing_path_defaults_to_cwd_in_root(roots):
    # Glob/Grep/LS path is OPTIONAL: a MISSING path means "search the cwd" (an allowed
    # root) → in-root by construction → NO approval (not fail-closed). This is the one
    # documented difference from the required-path tools.
    cwd, allowed = roots
    for tool in ("Glob", "Grep", "LS"):
        assert (
            path_needs_approval(tool, {}, cwd=cwd, allowed_roots=allowed, allow_any_path=False)
            is False
        ), tool
    # But Glob WITH an out-of-root path still needs approval (present path is checked).
    assert (
        path_needs_approval(
            "Glob", {"path": "/etc"}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )
    # And a present-but-None Glob path is still fail-closed (malformed-when-present).
    assert (
        path_needs_approval(
            "Glob", {"path": None}, cwd=cwd, allowed_roots=allowed, allow_any_path=False
        )
        is True
    )


# ---- no-path tools (incl. Bash) are NOT path-checked ------------------------


@pytest.mark.parametrize(
    "tool,inp",
    [
        ("Bash", {"command": "cat /etc/shadow"}),  # Bash: no static target — NOT confined
        ("TodoWrite", {"todos": []}),
        ("WebSearch", {"query": "x"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("mcp__x__y", {"file_path": "/etc/shadow"}),  # mcp tool — not a known path tool
        ("FrobnicateNew", {"file_path": "/etc/shadow"}),  # unknown — name layer governs it
    ],
)
def test_no_path_concept_tools_never_path_checked(tool, inp, roots):
    # The path layer governs ONLY the explicit-path file/search tools. Bash/TodoWrite/
    # WebSearch/WebFetch/mcp__*/unknown return False here — even a `file_path` key on an
    # mcp/unknown tool is ignored (it's not in the path-tool map). The NAME-only classifier
    # still gates the risky ones; Bash stays unconfined by design (documented C2 caveat).
    cwd, allowed = roots
    assert (
        path_needs_approval(tool, inp, cwd=cwd, allowed_roots=allowed, allow_any_path=False)
        is False
    )


# ===========================================================================
# End-to-end: Engine holds an out-of-root tool (mirrors test_answer_hold)
# ===========================================================================


class HoldingSubstrate:
    """Mock substrate whose ``send`` raises ONE tool request mid-turn (see test_answer_hold).

    Yields a pre-event, BLOCKS on ``decision_callback`` (the SDK's ``can_use_tool``),
    records the returned decision. The engine injects a :class:`PermissionEvent` onto the
    stream when it holds, which the test collects.
    """

    def __init__(self, *, tool_name, tool_input, tool_use_id):
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._tool_use_id = tool_use_id
        self.session_id = "S1"
        self.decision_callback = None
        self.last_decision = None
        self.calls = []

    async def start(self):
        self.calls.append(("start",))

    async def resume(self, session_id):
        self.session_id = session_id

    async def send(self, prompt, *, timeout=120.0):
        from claude_tg.engine.types import TextEvent

        self.calls.append(("send", prompt, timeout))
        yield TextEvent(text="working")
        decision = await self.decision_callback(
            self._tool_name, self._tool_input, self._tool_use_id
        )
        self.last_decision = decision

    async def stop(self):
        self.calls.append(("stop",))


async def drain(aiter):
    return [ev async for ev in aiter]


async def _resolve_when_pending(eng, tool_use_id, decision):
    """Resolve a held request as soon as it registers (no real wait)."""
    for _ in range(1000):
        if eng._pending.has_pending(tool_use_id):
            return eng.resolve(tool_use_id, decision)
        await asyncio.sleep(0)
    raise AssertionError(f"request {tool_use_id} never became pending")


def _engine(sub, *, cwd, allowed_roots, allow_any_path=False, policy=None):
    eng = Engine(
        sub,
        permission_policy=policy,
        cwd=cwd,
        allowed_roots=allowed_roots,
        allow_any_path=allow_any_path,
        backstop_seconds=100,  # long: prove the operator resolve wins, never the backstop
    )
    sub.decision_callback = eng.on_tool_request
    return eng


async def test_engine_out_of_root_read_holds_for_approval(roots):
    # THE core C2 fix end-to-end: an out-of-root READ (a P2-SAFE tool that today auto-runs
    # on any path) is HELD — a PermissionEvent reaches the operator. Mutation-probe: revert
    # the out-of-root check in on_tool_request and this goes RED (Read auto-allows, no event).
    cwd, allowed = roots
    sub = HoldingSubstrate(
        tool_name="Read", tool_input={"file_path": "/etc/shadow"}, tool_use_id="tu-r1"
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed)
    await eng.start()

    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-r1", PermissionDecision("allow_once"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    assert await op is True
    await eng.stop()

    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    assert len(perms) == 1
    assert perms[0].tool_name == "Read"
    assert perms[0].tool_use_id == "tu-r1"
    assert sub.last_decision.allow is True  # operator allowed it once


@pytest.mark.parametrize("tool,key", [("Glob", "path"), ("LS", "path")])
async def test_engine_out_of_root_safe_search_tool_holds(tool, key, roots):
    # Glob / LS (SAFE) on an out-of-root path also HOLD (the fix covers all safe path tools).
    cwd, allowed = roots
    sub = HoldingSubstrate(tool_name=tool, tool_input={key: "/etc"}, tool_use_id="tu-s")
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed)
    await eng.start()
    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-s", PermissionDecision("allow_once"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()
    assert [e.tool_name for e in collected if isinstance(e, PermissionEvent)] == [tool]


async def test_engine_in_root_read_auto_allows_no_prompt(roots):
    # In-root behavior is UNCHANGED (P2 regression guard): an in-root SAFE Read auto-runs —
    # no PermissionEvent, the substrate is allowed straight through.
    cwd, allowed = roots
    sub = HoldingSubstrate(
        tool_name="Read",
        tool_input={"file_path": str(Path(cwd) / "ok.py")},
        tool_use_id="tu-ok",
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed)
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # no prompt
    assert sub.last_decision.allow is True


async def test_engine_out_of_root_write_holds_even_with_session_grant(roots):
    # An out-of-root call ALWAYS re-prompts — even with a session grant for that tool name.
    # The policy is pre-granted Write (so the NAME-only gate would auto-allow), but the
    # out-of-root path forces a hold anyway. Mutation-probe: revert the out-of-root check →
    # the grant short-circuits → no PermissionEvent → RED.
    cwd, allowed = roots
    policy = PermissionPolicy()
    policy.grant_session("Write")  # name-only grant would normally suppress the prompt
    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": "/tmp/escape.py"},
        tool_use_id="tu-w",
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed, policy=policy)
    await eng.start()
    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-w", PermissionDecision("allow_once"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()
    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    assert len(perms) == 1  # held despite the grant (out-of-root re-prompts)
    assert perms[0].tool_name == "Write"


async def test_engine_in_root_write_with_session_grant_auto_allows(roots):
    # The flip side / P2 regression guard: an IN-root Write WITH a session grant still
    # auto-allows (the path layer only re-prompts OUT-of-root; in-root keeps P2 behavior).
    cwd, allowed = roots
    policy = PermissionPolicy()
    policy.grant_session("Write")
    sub = HoldingSubstrate(
        tool_name="Write",
        tool_input={"file_path": str(Path(cwd) / "in.py")},
        tool_use_id="tu-in",
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed, policy=policy)
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # grant honored in-root
    assert sub.last_decision.allow is True


async def test_engine_yolo_bypasses_out_of_root(roots):
    # /yolo is the explicit allow-all opt-out and is checked FIRST — an out-of-root tool
    # runs free under yolo (no prompt). Mutation-probe boundary: this proves the ordering
    # (yolo BEFORE the path check).
    cwd, allowed = roots
    policy = PermissionPolicy()
    policy.set_yolo(True)
    sub = HoldingSubstrate(
        tool_name="Write", tool_input={"file_path": "/etc/cron.d/x"}, tool_use_id="tu-y"
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed, policy=policy)
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # yolo bypassed
    assert sub.last_decision.allow is True


async def test_engine_allow_any_path_disables_confinement_out_of_root_auto_allows(roots):
    # ALLOW_ANY_PATH=true → an out-of-root SAFE Read auto-runs again (path layer off).
    cwd, allowed = roots
    sub = HoldingSubstrate(
        tool_name="Read", tool_input={"file_path": "/etc/shadow"}, tool_use_id="tu-aap"
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed, allow_any_path=True)
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # confinement disabled
    assert sub.last_decision.allow is True


async def test_engine_bash_still_prompts_unchanged_and_is_not_path_checked(roots):
    # Bash is RISKY (name-only) and is NOT path-checked (no static target). It still HOLDS
    # exactly as in P2 — the path layer neither tightens nor loosens it. (Its command
    # targets /etc but the engine does not parse it — the documented C2 boundary.)
    cwd, allowed = roots
    sub = HoldingSubstrate(
        tool_name="Bash", tool_input={"command": "cat /etc/shadow"}, tool_use_id="tu-b"
    )
    eng = _engine(sub, cwd=cwd, allowed_roots=allowed)  # fresh default policy → Bash gates
    await eng.start()
    op = asyncio.create_task(
        _resolve_when_pending(eng, "tu-b", PermissionDecision("deny"))
    )
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await op
    await eng.stop()
    perms = [e for e in collected if isinstance(e, PermissionEvent)]
    assert [p.tool_name for p in perms] == ["Bash"]  # prompts (unchanged)
    assert sub.last_decision.allow is False  # denied


async def test_engine_no_cwd_wired_is_a_noop_path_layer_off(roots):
    # Defaults guard: an Engine built WITHOUT the P6 path context (cwd=None) behaves exactly
    # as pre-C2 — the path layer is a no-op, so an out-of-root SAFE Read auto-allows. This
    # pins that every existing Engine(...) construction (test fakes, the sync provider) is
    # unchanged by C2.
    _cwd, allowed = roots
    sub = HoldingSubstrate(
        tool_name="Read", tool_input={"file_path": "/etc/shadow"}, tool_use_id="tu-nc"
    )
    eng = Engine(sub, cwd=None, allowed_roots=allowed, allow_any_path=False)
    sub.decision_callback = eng.on_tool_request
    await eng.start()
    collected = await asyncio.wait_for(drain(eng.send("go")), timeout=5)
    await eng.stop()
    assert not any(isinstance(e, PermissionEvent) for e in collected)  # no-op path layer
