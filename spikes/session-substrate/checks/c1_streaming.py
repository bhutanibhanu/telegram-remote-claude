"""T6 — C1 bidirectional streaming check (substrate A: claude-agent-sdk==0.2.105).

C1 (design): stand up a persistent, multi-turn session; send a NEW operator
message in mid-session and observe a continuous event stream OUT. Proven: >=2
turns over one live session with streamed events captured.

What this probe does (and only this):
  * Enables partial/streaming messages through the existing T5 harness
    (include_partial_messages=True) so the SDK surfaces incremental StreamEvents.
  * Runs TWO turns over ONE persistent session (the 2nd query is the "new
    operator message mid-session" = the bidirectional 'in').
  * Captures the exact ordered event sequence with relative timing, the event
    types, the session id, and the terminal ResultMessage per turn.
  * Classifies every event as:
      - incremental : StreamEvent content_block_delta / message_delta (true token-
                      level streaming OUT, arriving before the turn completes)
      - completed   : AssistantMessage (the fully assembled message)
      - control     : SystemMessage (init/...) + StreamEvent framing
                      (message_start, content_block_start/stop, message_stop) + UserMessage
      - terminal    : ResultMessage
    and detects "buffered-at-completion" (no incremental deltas; full text only
    appears at the AssistantMessage).

Verdict (honest; PASS/PARTIAL/FAIL all valid — do NOT manufacture a PASS):
  PASS    : >=2 turns on one stable session_id, each turn emitted a terminal
            result, AND genuinely incremental events streamed OUT before
            completion in BOTH turns.
  PARTIAL : >=2 turns on one stable session_id with events captured, but the
            stream was buffered-at-completion (no token-incremental deltas).
  FAIL    : could not run 2 turns on one session / errored / no events.

No risky tools are used (text-only prompts; X1 not engaged). No API key; host CLI
auth only. Evidence is written via the T3 recorder -> evidence/c1.* (scrubbed,
SB3/X3). Only structural event types + timing + a short answer preview are
persisted — no per-token output content.

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c1_streaming.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# harness_sdk + evidence_recorder live one level up (spikes/session-substrate/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evidence_recorder import record_criterion  # noqa: E402
from harness_sdk import SDKSessionHarness, assistant_text  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
)

# StreamEvent.event["type"] values that represent genuine incremental output.
INCREMENTAL_EVENT_TYPES = {"content_block_delta", "message_delta"}


def classify(msg) -> tuple[str, str]:
    """Return (kind, structural-detail). Detail carries NO model output tokens."""
    if isinstance(msg, StreamEvent):
        event = msg.event or {}
        etype = event.get("type", "?")
        if etype in INCREMENTAL_EVENT_TYPES:
            delta_type = (event.get("delta") or {}).get("type", "")
            return "incremental", f"StreamEvent/{etype}" + (f":{delta_type}" if delta_type else "")
        return "control", f"StreamEvent/{etype}"
    if isinstance(msg, SystemMessage):
        return "control", f"System/{msg.subtype}"
    if isinstance(msg, UserMessage):
        return "control", "UserMessage"
    if isinstance(msg, AssistantMessage):
        return "completed", "AssistantMessage"
    if isinstance(msg, ResultMessage):
        return "terminal", f"Result/{msg.subtype}(is_error={msg.is_error})"
    return "other", type(msg).__name__


async def run_turn(harness: SDKSessionHarness, prompt: str, turn_no: int, log) -> dict:
    t0 = time.monotonic()
    counts = {"incremental": 0, "completed": 0, "control": 0, "terminal": 0, "other": 0}
    seq: list[tuple[float, str, str]] = []
    first_incr_ms = None
    completed_ms = None
    result_ms = None
    result_is_error = None
    answer = ""

    log(f"\n--- turn {turn_no}: operator message IN -> event stream OUT ---")
    async for msg in harness.send(prompt, timeout=120):
        t_rel = round((time.monotonic() - t0) * 1000.0, 1)
        kind, detail = classify(msg)
        counts[kind] += 1
        seq.append((t_rel, kind, detail))
        if kind == "incremental" and first_incr_ms is None:
            first_incr_ms = t_rel
        if kind == "completed":
            completed_ms = t_rel
            answer += assistant_text(msg)
        if kind == "terminal":
            result_ms = t_rel
            result_is_error = getattr(msg, "is_error", None)

    # Genuinely incremental iff we saw >=2 incremental deltas AND the first one
    # arrived before the completed AssistantMessage (i.e. streamed, not buffered).
    incr = counts["incremental"]
    genuinely_incremental = (
        incr >= 2
        and first_incr_ms is not None
        and (completed_ms is None or first_incr_ms <= completed_ms)
    )
    buffered_at_completion = (incr == 0) and bool(answer)

    # Ordered event sequence (structural only; no token content).
    log(f"session_id: {harness.session_id}")
    log(f"event sequence ({len(seq)} events) [ms, kind, type]:")
    for t_rel, kind, detail in seq:
        log(f"  {t_rel:8.1f}  {kind:11s}  {detail}")
    log(f"counts: {counts}")
    log(f"timing: first_incremental={first_incr_ms}ms  completed={completed_ms}ms  result={result_ms}ms")
    log(f"terminal result is_error={result_is_error}")
    log(f"answer preview ({len(answer)} chars): {answer.strip()[:60]!r}")
    log(f"genuinely_incremental={genuinely_incremental}  buffered_at_completion={buffered_at_completion}")

    return {
        "session_id": harness.session_id,
        "counts": counts,
        "first_incr_ms": first_incr_ms,
        "completed_ms": completed_ms,
        "result_ms": result_ms,
        "result_is_error": result_is_error,
        "answer_len": len(answer),
        "genuinely_incremental": genuinely_incremental,
        "buffered_at_completion": buffered_at_completion,
        "n_events": len(seq),
    }


async def _run() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T6 / C1 — bidirectional streaming check (substrate A) ===")
    import importlib.metadata as md
    log(f"sdk: claude-agent-sdk=={md.version('claude-agent-sdk')}")
    import os
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")

    import tempfile
    workdir = tempfile.mkdtemp(prefix="t6_c1_")
    log(f"cwd (temp, isolated): {workdir}")

    harness = SDKSessionHarness(
        cwd=workdir,
        permission_mode="default",
        include_partial_messages=True,      # the C1-relevant streaming option
        disallowed_tools=["Bash", "Write", "Edit"],  # text-only; no risky tools
    )

    verdict, reason = "FAIL", "check did not complete"
    try:
        await harness.start()
        log("start(): persistent session connected")

        turn1 = await run_turn(
            harness, "Count from 1 to 10, one number per line, nothing else.", 1, log
        )
        turn2 = await run_turn(
            harness, "Now list five fruits, one per line, nothing else.", 2, log
        )

        await harness.stop()
        log("\nstop(): disconnected")

        sid1, sid2 = turn1["session_id"], turn2["session_id"]
        session_stable = bool(sid1) and sid1 == sid2
        both_terminal = (turn1["result_is_error"] is False) and (turn2["result_is_error"] is False)
        events_captured = turn1["n_events"] > 0 and turn2["n_events"] > 0
        incremental_both = turn1["genuinely_incremental"] and turn2["genuinely_incremental"]
        buffered = turn1["buffered_at_completion"] or turn2["buffered_at_completion"]

        log("\n=== C1 evaluation ===")
        log(f"session_stable={session_stable} (sid={sid1})")
        log(f"both_turns_terminal_ok={both_terminal}")
        log(f"events_captured_both={events_captured}")
        log(f"genuinely_incremental_both={incremental_both}  any_buffered_at_completion={buffered}")

        if session_stable and both_terminal and events_captured and incremental_both:
            verdict = "PASS"
            reason = (
                f"2 turns over one stable session_id={sid1}; each emitted a clean terminal "
                f"result; genuinely incremental StreamEvent deltas streamed OUT before "
                f"completion in BOTH turns (turn1 incr={turn1['counts']['incremental']}, "
                f"turn2 incr={turn2['counts']['incremental']}). Bidirectional 'message in "
                f"mid-session -> event stream out' demonstrated."
            )
        elif session_stable and both_terminal and events_captured:
            verdict = "PARTIAL"
            reason = (
                f"2 turns over one stable session_id={sid1} with events captured, but stream "
                f"was buffered-at-completion (no token-incremental deltas: turn1 incr="
                f"{turn1['counts']['incremental']}, turn2 incr={turn2['counts']['incremental']}). "
                f"Message-granularity streaming only."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"C1 not met: session_stable={session_stable}, both_terminal={both_terminal}, "
                f"events_captured={events_captured}."
            )
    except Exception as exc:  # noqa: BLE001 - fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
        try:
            await harness.stop()
        except Exception:
            pass

    log("")
    log("Limitations / scope:")
    log("- Proves C1 (bidirectional streaming) ONLY; makes no claim about C2-C6.")
    log("- Single host/run; text-only prompts; verdict is reproducible, the session_id is not.")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    with record_criterion("c1") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
