"""Permission gating — the fail-closed risk classifier + per-session policy state.

This is the **pure** core of P2 per-tool permission gating (ADR-003): no telegram,
no engine, no SDK imports, no I/O, no async, no logging of tool inputs. It imports
cleanly with neither the SDK nor python-telegram-bot present, so it is trivially
unit-testable and reusable. The engine (T3) and bot/session (T5) *consult* it; they
do not reach into it.

Two things live here:

* :func:`is_risky` — the **fail-closed safe-allowlist** classifier (ADR-003 §1, D1/D2,
  SB6). A tool is SAFE *only* if its name is one of the six in :data:`SAFE_TOOLS`
  (local reads + search); **everything else is RISKY** — Write/Edit/Bash, ``WebFetch``
  (arbitrary-URL egress), every ``mcp__*`` tool, and any unknown/new/empty/non-string
  name. The wrong-way error is deliberately toward *gating* (friction), never toward
  *running* (exposure).

* :class:`PermissionPolicy` — the per-session state the engine asks "does this tool
  need approval?". It holds the in-memory **allow-session grants** (keyed by tool
  NAME only, D4) and the off-by-default ``/yolo`` allow-all flag (D6). All of it is
  in-memory and dropped by :meth:`~PermissionPolicy.clear` on ``/reset`` / a fresh
  session / a restart (D7) — a restart never silently resumes an allow-all posture.

**Out of scope here (by design).** Mapping a verdict onto the substrate primitive
(``decision_to_substrate`` in ``engine/types.py``), holding the request open for the
operator (``engine/pending.py``), rendering the prompt (``render.py``), and routing
the taps (``bot.py``) are T3/T4/T5 — this module is only the classifier + the state.
``AskUserQuestion`` / ``ExitPlanMode`` are answered on the engine's async answer-hold
path (``engine.ASK_TOOL`` / ``PLAN_TOOL``) and **never reach this classifier**; if one
ever did it would be treated as RISKY (it is not in :data:`SAFE_TOOLS`) — harmless,
fail-closed.
"""

from __future__ import annotations

#: The ONLY tools that auto-run without an approval prompt (ADR-003 §1, D1/D2).
#: Local reads + search — low blast radius, so prompts stay rare and meaningful.
#: EVERYTHING not in this set gates (fail-closed, SB6). Note ``WebSearch`` IS here
#: but ``WebFetch`` is deliberately NOT (arbitrary-URL egress is risky — D2).
#: Guarded by a test so an accidental future widening fails CI.
SAFE_TOOLS: frozenset[str] = frozenset(
    {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch"}
)


def is_risky(tool_name: str, tool_input: dict | None = None) -> bool:
    """Return ``True`` iff the tool must be approved (fail-closed); ``False`` only for safe tools.

    The classifier is a pure function of the tool **name** (D4 — grants and the risk
    verdict are name-only in P2; ``tool_input`` is accepted for a stable signature and
    forward-compatibility but is intentionally **not** inspected — no Bash
    command-aware classification, no per-resource scoping; both are explicit P2
    anti-goals).

    RISKY (returns ``True``):
        Write, Edit, MultiEdit, NotebookEdit, Bash, ``WebFetch`` (arbitrary-URL
        egress), ANY name starting ``mcp__`` (MCP tools), and ANY unknown / new /
        empty / ``None`` / non-``str`` name. Unknown gates by default — this is the
        load-bearing SB6 fail-closed behavior: a new tool the classifier has never
        seen is treated as dangerous, not waved through.

    SAFE (returns ``False``):
        exactly the names in :data:`SAFE_TOOLS`, and nothing else.

    ``AskUserQuestion`` / ``ExitPlanMode`` are handled on the engine's answer-hold
    path and never reach here; if classified anyway they are RISKY (not in
    :data:`SAFE_TOOLS`) — harmless.
    """
    # Fail closed on anything that is not a real, non-empty tool name: None, "",
    # or a non-str (e.g. a malformed substrate payload) is risky, never safe (SB6).
    if not isinstance(tool_name, str) or not tool_name:
        return True
    # The ONLY way to be safe is to be an explicit member of the allowlist. This
    # ordering also means ``mcp__*`` / Write / unknown all fall through to RISKY
    # without needing a deny-list to enumerate them (a deny-list would itself be a
    # fail-OPEN hazard — a new risky tool not on it would slip through).
    return tool_name not in SAFE_TOOLS


class PermissionPolicy:
    """Per-session permission state the engine consults (ADR-003 §2/§3/§5, D4/D6/D7).

    In-memory only — one instance per session. The engine asks
    :meth:`needs_approval` before running an ordinary tool; the bot records an
    [Allow for session] tap via :meth:`grant_session`, flips ``/yolo`` via
    :meth:`set_yolo`, and wipes everything via :meth:`clear` on ``/reset`` / a new
    session (D7). Nothing here persists across a restart — that is the point.

    :meth:`needs_approval` returns ``False`` (run free, no prompt) when **any** of:
      * ``/yolo`` is on (allow-all, D6), OR
      * the tool is in :data:`SAFE_TOOLS` (a safe read/search, D1/D2), OR
      * the tool name has a live allow-session grant this session (D4);
    otherwise ``True`` (pause for an approval prompt). This is the inverse of "is it
    allowed" — the engine pauses unless one of the three allow-conditions holds, so a
    bug that fails to record a grant errs toward *prompting*, not toward running.
    """

    def __init__(self) -> None:
        #: ``/yolo`` allow-all flag. OFF by default (D6) — a fresh policy gates every
        #: risky tool; allow-all is never the silent default.
        self.yolo: bool = False
        #: Allow-session grants, keyed by tool NAME only (D4). A set, not a map: P2
        #: has no per-input / per-resource granularity (explicit anti-goal).
        self._granted: set[str] = set()

    def needs_approval(self, tool_name: str, tool_input: dict | None = None) -> bool:
        """Return ``True`` if this tool call must pause for operator approval.

        ``False`` iff ``/yolo`` is on, OR the tool is safe (:func:`is_risky` ->
        ``False``), OR its name has a live allow-session grant. The ``yolo`` short-
        circuit comes first so an allow-all session never even classifies. ``tool_input``
        is forwarded to :func:`is_risky` for signature symmetry only (unused, D4).
        """
        if self.yolo:
            return False
        if not is_risky(tool_name, tool_input):
            return False
        # Risky + not yolo: only a live per-name grant suppresses the prompt (D4).
        return not self.is_granted(tool_name)

    def grant_session(self, tool_name: str) -> None:
        """Record an [Allow for session] grant for ``tool_name`` (D4 — by NAME only).

        After this, :meth:`needs_approval` is ``False`` for that exact tool name for
        the rest of the session; a *different* risky tool is unaffected (the ADR-001
        caveat — one grant greenlights nothing else). A non-``str`` / empty name is
        ignored (it could never match a real request anyway — fail-closed).
        """
        if isinstance(tool_name, str) and tool_name:
            self._granted.add(tool_name)

    def is_granted(self, tool_name: str) -> bool:
        """Return ``True`` iff ``tool_name`` has a live allow-session grant (D4)."""
        return tool_name in self._granted

    def set_yolo(self, on: bool) -> None:
        """Set the ``/yolo`` allow-all flag (``/yolo`` -> ``True``, ``/unyolo`` -> ``False``, D6).

        When ``True``, :meth:`needs_approval` returns ``False`` for every tool. The
        loud banner / on-indicator that makes this never-silent is the renderer's job
        (T4) — this only holds the bit.
        """
        self.yolo = bool(on)

    def clear(self) -> None:
        """Drop ALL allow-session grants AND reset ``/yolo`` to off (D7).

        Called by the bot/engine on ``/reset``, a fresh session, or a restart so a
        new session always starts fail-closed (no inherited grants, yolo off) — a
        restart can never silently resume an allow-all / yolo posture.
        """
        self._granted.clear()
        self.yolo = False

    def granted_tools(self) -> frozenset[str]:
        """Return the current allow-session grants (immutable snapshot, for tests/introspection)."""
        return frozenset(self._granted)


__all__ = ["SAFE_TOOLS", "is_risky", "PermissionPolicy"]
