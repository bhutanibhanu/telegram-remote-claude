"""T5 — Substrate A (claude-agent-sdk) persistent session lifecycle harness.

Stands up the Agent SDK persistent client and the start / resume / send / stop
lifecycle that the C1-C6 checks (T6-T12) drive against. Substrate A was confirmed
to exist/install/import in T4 (claude-agent-sdk==0.2.105).

Lifecycle seams (the "event stream out" + the session controls the checks need):

    h = SDKSessionHarness(cwd=..., permission_mode="default", can_use_tool=cb, ...)
    await h.start()                      # fresh persistent session (host CLI auth, no API key)
    async for msg in h.send(prompt):     # one turn; yields the SDK messages streamed back
        ...                              # h.session_id is populated during the first turn
    await h.send(prompt2)                # multi-turn over the SAME session
    await h.stop()                       # disconnect; terminate the CLI subprocess cleanly

    # cross-process re-attach (fully exercised by C6/T12):
    h2 = SDKSessionHarness(...); await h2.resume(session_id); async for m in h2.send(...): ...

Design intent (kept deliberately thin, per the spike's throwaway-quality rule):
  * This harness owns ONLY connection lifecycle + session-id capture, and passes
    SDK messages straight through. It makes NO claim about C1-C6 behaviour.
  * Normalization into the "events out / decisions in" contract is drafted later
    (T18). Per-criterion behaviour (streaming C1, permission C2, AskUserQuestion
    C3, plan C4, skill C5, resume C6) is exercised by the T6-T12 checks, which
    import this harness — not by T5.
  * Auth: the SDK spawns the host `claude` CLI, which uses the existing logged-in
    session. No API key is read or set by this harness; run it in an environment
    with ANTHROPIC_API_KEY unset to guarantee the no-API-key constraint.

Run the built-in lifecycle self-check (bounded, two short text turns) with the
spike venv interpreter:

    spikes/session-substrate/.venv/bin/python spikes/session-substrate/harness_sdk.py

It writes evidence/t5_lifecycle_smoke.{json,transcript.txt} via the T3 recorder
(scrubbed, SB3/X3).
"""

from __future__ import annotations

import asyncio
import importlib.metadata as importlib_metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

# The T3 recorder (which imports the T2 scrubber) lives beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_recorder import record_criterion  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

# Permission callback shape the SDK expects (used by C2/T7, not by T5 itself).
PermissionCallback = Callable[[str, dict, Any], Awaitable[Any]]


def assistant_text(msg: Any) -> str:
    """Concatenate the plain-text blocks of an AssistantMessage (else '')."""
    if isinstance(msg, AssistantMessage):
        return "".join(
            block.text for block in msg.content if isinstance(block, TextBlock)
        )
    return ""


class SDKSessionHarness:
    """A persistent, multi-turn Claude session over substrate A.

    Not thread-safe; drive it from a single asyncio task. One harness instance
    owns one session (one CLI subprocess) between start()/resume() and stop().
    """

    def __init__(
        self,
        *,
        cwd: Optional[str | os.PathLike] = None,
        permission_mode: str = "default",
        can_use_tool: Optional[PermissionCallback] = None,
        include_partial_messages: bool = False,
        allowed_tools: Optional[list[str]] = None,
        disallowed_tools: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
        setting_sources: Optional[list[str]] = None,
        skills: Optional[list[str] | str] = None,
    ) -> None:
        self._cwd = str(cwd) if cwd is not None else None
        self._permission_mode = permission_mode
        self._can_use_tool = can_use_tool
        self._include_partial = include_partial_messages
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._system_prompt = system_prompt
        self._setting_sources = setting_sources
        self._skills = skills

        self._client: Optional[ClaudeSDKClient] = None
        #: Claude session id, captured from the first turn's init/result message.
        self.session_id: Optional[str] = None
        #: The most recent ResultMessage (carries cost/turns/session_id/errors).
        self.last_result: Optional[ResultMessage] = None

    # -- options ---------------------------------------------------------------

    def _build_options(self, resume: Optional[str] = None) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "permission_mode": self._permission_mode,
            "include_partial_messages": self._include_partial,
        }
        if self._cwd is not None:
            kwargs["cwd"] = self._cwd
        if self._can_use_tool is not None:
            kwargs["can_use_tool"] = self._can_use_tool
        if self._allowed_tools is not None:
            kwargs["allowed_tools"] = self._allowed_tools
        if self._disallowed_tools is not None:
            kwargs["disallowed_tools"] = self._disallowed_tools
        if self._system_prompt is not None:
            kwargs["system_prompt"] = self._system_prompt
        if self._setting_sources is not None:
            kwargs["setting_sources"] = self._setting_sources
        if self._skills is not None:
            kwargs["skills"] = self._skills
        if resume:
            kwargs["resume"] = resume
        return ClaudeAgentOptions(**kwargs)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> "SDKSessionHarness":
        """Establish a fresh persistent session (host CLI auth; no API key)."""
        if self._client is not None:
            raise RuntimeError("session already started; call stop() first")
        self._client = ClaudeSDKClient(options=self._build_options())
        await self._client.connect()
        return self

    async def resume(self, session_id: str) -> "SDKSessionHarness":
        """Re-attach to an existing session by id.

        Cross-process resume with retained history is the C6 criterion (T12);
        this method is the seam that test exercises.
        """
        if not session_id:
            raise ValueError("resume() requires a non-empty session_id")
        if self._client is not None:
            raise RuntimeError("session already started; call stop() first")
        self._client = ClaudeSDKClient(options=self._build_options(resume=session_id))
        await self._client.connect()
        self.session_id = session_id
        return self

    async def send(self, prompt: str, *, timeout: float = 120.0) -> AsyncIterator[Any]:
        """Send one operator turn; async-yield the SDK messages streamed back.

        This is the "message in / event stream out" seam. Iteration ends after
        the turn's ResultMessage. Each awaited message is bounded by ``timeout``
        seconds so the harness fails clean instead of hanging (RB2/fail-clean).
        """
        if self._client is None:
            raise RuntimeError("session not started; call start()/resume() first")
        await self._client.query(prompt)
        iterator = self._client.receive_response().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                break
            self._capture_session_id(msg)
            if isinstance(msg, ResultMessage):
                self.last_result = msg
            yield msg

    def _capture_session_id(self, msg: Any) -> None:
        sid: Optional[str] = None
        if isinstance(msg, SystemMessage):
            data = getattr(msg, "data", None) or {}
            sid = data.get("session_id") if isinstance(data, dict) else None
        elif isinstance(msg, ResultMessage):
            sid = getattr(msg, "session_id", None)
        if sid and not self.session_id:
            self.session_id = sid

    async def stop(self) -> None:
        """Disconnect and terminate the CLI subprocess. Idempotent."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None


# ---------------------------------------------------------------------------
# Built-in lifecycle self-check (T5 probe). Two short text-only turns: proves
# start -> multi-turn send (event stream out) -> stable session id -> clean stop
# with no leaked CLI process. No risky tools are used (text replies only), so X1
# is not engaged here; C1-C6 behaviour is NOT asserted (that is T6-T12).
# ---------------------------------------------------------------------------


def _descendant_pids(root: Optional[int] = None) -> set[int]:
    """All descendant pids of ``root`` (default: this process), via pgrep -P."""
    root = root if root is not None else os.getpid()
    seen: set[int] = set()
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(parent)],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            out = ""
        for token in out.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid not in seen:
                seen.add(pid)
                frontier.append(pid)
    return seen


def _child_claude_pids() -> list[int]:
    """Descendant pids of THIS process whose command mentions 'claude'.

    Scoped to descendants so it can never catch the running Telegram bot's own
    claude subprocesses (a different process tree) -- only the CLI the SDK spawns
    under this self-check is in scope.
    """
    result: list[int] = []
    for pid in _descendant_pids():
        try:
            cmd = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            cmd = ""
        if "claude" in cmd.lower():
            result.append(pid)
    return sorted(result)


async def _selfcheck() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T5 SELF-CHECK — substrate A persistent session lifecycle ===")
    log(f"sdk: claude-agent-sdk=={importlib_metadata.version('claude-agent-sdk')}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} "
        f"(must be False — no API key)")
    workdir = tempfile.mkdtemp(prefix="t5_sdk_")
    log(f"cwd (temp, isolated from repo): {workdir}")

    harness = SDKSessionHarness(
        cwd=workdir,
        permission_mode="default",
        disallowed_tools=["Bash", "Write", "Edit"],  # text-only smoke; no tools
    )

    verdict, reason = "FAIL", "self-check did not complete"
    try:
        before = _child_claude_pids()
        await harness.start()
        log(f"start(): connected; child claude pids now: {_child_claude_pids()}")

        # Turn 1
        t1, n1 = "", 0
        async for msg in harness.send("Reply with exactly: TURN1_OK", timeout=120):
            n1 += 1
            t1 += assistant_text(msg)
        sid1 = harness.session_id
        log(f"turn1: msgs={n1} session_id={sid1} text~={t1.strip()[:60]!r}")

        # Turn 2 (multi-turn over the SAME session)
        t2, n2 = "", 0
        async for msg in harness.send("Reply with exactly: TURN2_OK", timeout=120):
            n2 += 1
            t2 += assistant_text(msg)
        sid2 = harness.session_id
        log(f"turn2: msgs={n2} session_id={sid2} text~={t2.strip()[:60]!r}")

        await harness.stop()
        log("stop(): disconnected")

        after = _child_claude_pids()
        leaked = sorted(set(after) - set(before))
        log(f"leak-check: before={before} after={after} leaked={leaked}")

        ok_turns = ("TURN1_OK" in t1) and ("TURN2_OK" in t2)
        ok_session = bool(sid1) and (sid1 == sid2)
        ok_stream = (n1 >= 1) and (n2 >= 1)
        ok_noleak = (leaked == [])
        log(f"checks: turns_ok={ok_turns} session_stable={ok_session} "
            f"streamed={ok_stream} no_leak={ok_noleak}")

        if ok_turns and ok_session and ok_stream and ok_noleak:
            verdict = "PASS"
            reason = (
                f"start / send x2 (multi-turn) / stop OK; stable session_id={sid1}; "
                f"events streamed both turns; clean disconnect, no leaked CLI process."
            )
        else:
            verdict = "PARTIAL"
            reason = (
                f"lifecycle ran but not all bullets green: turns_ok={ok_turns}, "
                f"session_stable={ok_session}, streamed={ok_stream}, "
                f"no_leak={ok_noleak} (leaked={leaked})."
            )
    except Exception as exc:  # noqa: BLE001 - record any failure, fail-clean
        verdict, reason = "FAIL", f"exception: {type(exc).__name__}: {exc}"
        log(reason)
        try:
            await harness.stop()
        except Exception:
            pass

    log("")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    with record_criterion("t5_lifecycle_smoke") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    # PASS/PARTIAL -> 0 (harness ran); FAIL -> 1 (lifecycle did not work).
    return 0 if verdict in ("PASS", "PARTIAL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_selfcheck()))
