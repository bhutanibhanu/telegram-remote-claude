"""The ``Engine`` — drives a :class:`~claude_tg.engine.substrate.Substrate`.

This is the production object behind the normalized interface. In T4 its job is:

* **lifecycle passthrough** — ``start`` / ``resume`` / ``send`` / ``stop`` over the
  injected substrate, carrying the session id;
* **events out** — expose the substrate's normalized event stream (each event already
  carries a ``session_id`` from the adapter);
* **decisions in (the SEAM)** — wire the substrate's permission/decision callback to
  an engine-side decision provider. T4 ships a **simple synchronous default** so the
  lifecycle is fully testable without a human in the loop; **T5** replaces the
  provider body with the async answer-hold (a ``PendingDecision`` Future + 60-min
  backstop + ``/cancel``, per ADR-002) **without changing this seam's shape**.

The engine owns the ``(session_id, cwd)`` coupling at the call site (it is handed the
cwd and persists the pair via the existing session store — wired in T7); the substrate
does not enforce the cwd-scoped-resume / double-attach rules (ADR-001 / C6), the engine
does.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from .substrate import Substrate
from .types import (
    Decision,
    Event,
    PermissionVerdict,
    SubstrateDecision,
    decision_to_substrate,
)

log = logging.getLogger(__name__)

#: An engine-side decision provider: given a tool request, return the decision to
#: apply. T4's default returns synchronously; T5 swaps in the async answer-hold
#: (awaiting an operator's Telegram tap, racing a backstop timer). The shape is the
#: same either way — that is why the seam is defined now.
DecisionProvider = Callable[
    [str, dict[str, Any], Optional[str]],
    Awaitable[Decision],
]


async def _default_decision_provider(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: Optional[str],
) -> Decision:
    """T4 default: allow the request unchanged, synchronously.

    A deliberately trivial provider so the lifecycle is unit-testable end-to-end
    without a human or a pending-decision machine. It echoes the original tool input
    on the allow (so the B ``updatedInput``-record gotcha is satisfied downstream).

    This is NOT the production answer flow: T5 replaces this with the async
    answer-hold (hold the request open for an operator decision, with a backstop).
    Interim P1 posture (design S3): risky tools run in the substrate's default
    permission mode inside the single allowlisted chat — no new bypass is introduced
    here; per-tool gating (deny-by-default) hardens in P2.
    """
    return PermissionVerdict(behavior="allow", updated_input=dict(tool_input))


class Engine:
    """Drives a single :class:`Substrate` session behind the normalized interface."""

    def __init__(
        self,
        substrate: Substrate,
        *,
        decision_provider: Optional[DecisionProvider] = None,
        send_timeout: float = 120.0,
    ) -> None:
        self._substrate = substrate
        self._decision_provider = decision_provider or _default_decision_provider
        self._send_timeout = send_timeout

    # -- session id ----------------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        """The current Claude session id (None before the substrate reports one)."""
        return self._substrate.session_id

    # -- the decision seam ---------------------------------------------------

    async def on_tool_request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: Optional[str],
    ) -> SubstrateDecision:
        """Resolve one substrate tool/interactive request to a substrate decision.

        This is the callback the engine hands the substrate (see
        :class:`~claude_tg.engine.substrate.DecisionCallback`). It asks the engine's
        decision provider for a :class:`~claude_tg.engine.types.Decision`, then maps
        it through the **single** load-bearing mapper
        (:func:`~claude_tg.engine.types.decision_to_substrate`) so every [FLAG]
        (native answers-map, plan-reject-rides-deny, allow-carries-updated_input) is
        honored in exactly one place. The original ``tool_input`` is passed as the
        allow base so the ``updatedInput`` record gotcha is always satisfied.
        """
        decision = await self._decision_provider(tool_name, tool_input, tool_use_id)
        return decision_to_substrate(decision, tool_input=tool_input)

    # -- lifecycle passthrough ----------------------------------------------

    async def start(self) -> None:
        """Establish a fresh session (host CLI auth; no API key)."""
        await self._substrate.start()
        log.debug("engine started; session_id=%s", self.session_id)

    async def resume(self, session_id: str) -> None:
        """Re-attach to an existing session by id (cwd-scoped — engine-owned, C6)."""
        await self._substrate.resume(session_id)
        log.debug("engine resumed session_id=%s", self.session_id)

    async def send(self, prompt: str, *, timeout: Optional[float] = None) -> AsyncIterator[Event]:
        """Send one operator turn; async-yield normalized events out.

        Pure passthrough of the substrate's bounded, fail-clean stream (RB2 lives in
        the adapter — a timeout/driver error surfaces as a ``driver_error`` event, not
        an exception, so this never hangs).
        """
        async for event in self._substrate.send(prompt, timeout=timeout or self._send_timeout):
            yield event

    async def stop(self) -> None:
        """Tear down the session. Idempotent."""
        await self._substrate.stop()
        log.debug("engine stopped")


__all__ = ["Engine", "DecisionProvider"]
