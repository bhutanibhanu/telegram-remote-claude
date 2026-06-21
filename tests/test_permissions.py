"""Unit tests for ``claude_tg.permissions`` — the fail-closed classifier + policy (P2/T2).

Maps each test to the ADR-003 rule it pins (D1/D2 safe-allowlist, D4 per-name grants,
D6 yolo, D7 clear, SB6 fail-closed). The fail-closed cases (unknown / ``mcp__*`` /
``WebFetch`` / empty / ``None`` / non-str -> RISKY) are written so they would FAIL if the
classifier ever flipped its default to "allow unknown" — see
``test_unknown_tool_is_risky_failclosed_guard`` for the explicit no-false-pass note.
"""

from __future__ import annotations

import pytest

from claude_tg.permissions import SAFE_TOOLS, PermissionPolicy, is_risky

# --- is_risky: the safe allowlist (D1/D2) ------------------------------------


@pytest.mark.parametrize("name", ["Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch"])
def test_safe_tools_are_not_risky(name):
    # D1/D2: exactly the six local-read/search tools auto-run (is_risky -> False).
    assert is_risky(name) is False


@pytest.mark.parametrize(
    "name", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch"]
)
def test_known_mutating_or_egress_tools_are_risky(name):
    # D1/D2: edits/exec gate; WebFetch (arbitrary-URL egress) gates even though
    # WebSearch (its sibling) is safe — the distinction is deliberate.
    assert is_risky(name) is True


def test_websearch_safe_but_webfetch_risky():
    # Pin the D2 split directly so the two are never accidentally lumped together.
    assert is_risky("WebSearch") is False
    assert is_risky("WebFetch") is True


# --- is_risky: fail-closed cases (SB6) ---------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "mcp__github__create_issue",
        "mcp__filesystem__write_file",
        "mcp__",  # bare prefix, still an MCP-shaped name -> risky
    ],
)
def test_mcp_tools_are_risky(name):
    # SB6/D1: ANY mcp__* tool gates (not in the allowlist).
    assert is_risky(name) is True


def test_unknown_tool_is_risky():
    # SB6: a tool the classifier has never heard of gates by default.
    assert is_risky("FrobnicateAll") is True


@pytest.mark.parametrize("bad", ["", None, 123, 4.5, [], {}, object()])
def test_empty_none_or_nonstr_name_is_risky(bad):
    # SB6 fail-closed: empty / None / any non-str name is RISKY, never safe — a
    # malformed substrate payload can never be waved through.
    assert is_risky(bad) is True  # type: ignore[arg-type]


def test_tool_input_is_ignored_by_classifier():
    # D4: the verdict is name-only; passing an input never changes safe<->risky
    # (no Bash command-aware classification, no per-resource scoping in P2).
    assert is_risky("Bash", {"command": "rm -rf /"}) is True
    assert is_risky("Bash", None) is True
    assert is_risky("Read", {"file_path": "/etc/passwd"}) is False


def test_unknown_tool_is_risky_failclosed_guard():
    # NO-FALSE-PASS GUARD. This asserts the *direction* of the default: unknown,
    # mcp__*, and a non-str name are ALL risky. If the classifier were ever changed
    # to "allow unknown" (fail-OPEN), every assert below flips and this test fails —
    # which is exactly the regression we want CI to catch.
    assert is_risky("SomeBrandNewToolWeHaveNeverSeen") is True
    assert is_risky("mcp__anything__at_all") is True
    assert is_risky(None) is True  # type: ignore[arg-type]


# --- SAFE_TOOLS guard --------------------------------------------------------


def test_safe_tools_is_exactly_the_intended_six():
    # Guard against accidental widening: if someone adds a tool to SAFE_TOOLS, this
    # fails and forces a deliberate review (a widened allowlist is a fail-OPEN risk).
    assert SAFE_TOOLS == frozenset(
        {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch"}
    )
    assert len(SAFE_TOOLS) == 6
    # The two egress siblings are on the correct sides of the line.
    assert "WebSearch" in SAFE_TOOLS
    assert "WebFetch" not in SAFE_TOOLS


# --- PermissionPolicy: defaults (D6) -----------------------------------------


def test_policy_yolo_off_by_default():
    # D6: a fresh policy is NOT in allow-all mode.
    assert PermissionPolicy().yolo is False


def test_policy_no_grants_by_default():
    # A fresh policy has no allow-session grants.
    assert PermissionPolicy().granted_tools() == frozenset()


def test_policy_safe_tool_needs_no_approval():
    # D1/D2: a safe tool never prompts, regardless of grants.
    assert PermissionPolicy().needs_approval("Read") is False


def test_policy_risky_ungranted_tool_needs_approval():
    # The default fail-closed path: a risky, ungranted tool pauses for approval.
    assert PermissionPolicy().needs_approval("Bash") is True


# --- PermissionPolicy: allow-session grants are per NAME (D4) -----------------


def test_grant_session_suppresses_that_tool_only():
    # D4: granting Bash makes Bash auto-allow, but a DIFFERENT risky tool (Write)
    # still gates — one grant greenlights nothing else (the ADR-001 caveat).
    policy = PermissionPolicy()
    policy.grant_session("Bash")
    assert policy.is_granted("Bash") is True
    assert policy.needs_approval("Bash") is False  # granted -> no prompt
    assert policy.needs_approval("Write") is True  # different tool still gates
    assert policy.granted_tools() == frozenset({"Bash"})


def test_grant_session_ignores_empty_or_nonstr_name():
    # Fail-closed hygiene: a junk grant is dropped (it could never match a real
    # request anyway) and does not pollute granted_tools().
    policy = PermissionPolicy()
    policy.grant_session("")
    policy.grant_session(None)  # type: ignore[arg-type]
    assert policy.granted_tools() == frozenset()


# --- PermissionPolicy: /yolo allow-all (D6) ----------------------------------


def test_set_yolo_true_allows_every_tool():
    # D6: yolo on -> NOTHING prompts, including a risky ungranted tool and an
    # unknown one.
    policy = PermissionPolicy()
    policy.set_yolo(True)
    assert policy.yolo is True
    assert policy.needs_approval("Bash") is False
    assert policy.needs_approval("Write") is False
    assert policy.needs_approval("FrobnicateAll") is False
    assert policy.needs_approval("mcp__x__y") is False


def test_set_yolo_false_restores_gating():
    # D6: /unyolo reverts to fail-closed gating for risky tools.
    policy = PermissionPolicy()
    policy.set_yolo(True)
    policy.set_yolo(False)
    assert policy.yolo is False
    assert policy.needs_approval("Bash") is True


# --- PermissionPolicy: clear() drops grants AND yolo (D7) --------------------


def test_clear_drops_all_grants_and_yolo():
    # D7: /reset / new-session / restart clears EVERYTHING — grants gone AND yolo
    # back off, so a risky tool gates again. A restart never resumes allow-all.
    policy = PermissionPolicy()
    policy.grant_session("Bash")
    policy.grant_session("Write")
    policy.set_yolo(True)
    policy.clear()
    assert policy.granted_tools() == frozenset()
    assert policy.yolo is False
    assert policy.needs_approval("Bash") is True  # grant dropped
    assert policy.needs_approval("Write") is True  # grant dropped


def test_granted_tools_returns_immutable_snapshot():
    # granted_tools() is a snapshot: mutating the return (if it were mutable) must
    # not leak back into the policy. frozenset has no mutators, so this also pins
    # the return type contract used by tests/introspection.
    policy = PermissionPolicy()
    policy.grant_session("Bash")
    snap = policy.granted_tools()
    assert isinstance(snap, frozenset)
    policy.grant_session("Write")
    # The earlier snapshot is unchanged by the later grant.
    assert snap == frozenset({"Bash"})
    assert policy.granted_tools() == frozenset({"Bash", "Write"})
