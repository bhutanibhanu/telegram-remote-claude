"""T1 / P1 — async-latency DE-RISK SPIKE (substrate A, live, contained).

GATES the streaming engine. Empirically probes the #1 P1 risk:

  Production must hold a Claude session's ``can_use_tool`` callback (and the
  native interactive-tool answer) OPEN while a human decides asynchronously —
  potentially minutes, with a 60-minute backstop. Does the SDK/CLI tolerate a
  multi-minute hold INSIDE ``can_use_tool`` without timing out the control
  request or wedging the session? Is there a near-term ceiling that would
  invalidate the 60-min-backstop design?

SDK FACTS established by reading claude-agent-sdk==0.2.105 source (recorded into
the evidence transcript at runtime):
  * The INBOUND ``can_use_tool`` control request is dispatched by
    Query._handle_control_request, which does ``await self.can_use_tool(...)``
    with NO ``anyio.fail_after`` / timeout wrapper — the SDK waits indefinitely
    for the callback to return. (query.py:_handle_control_request, ~L408.)
  * Each inbound control request is spawned as its own task
    (_spawn_control_request_handler, ~L239) and is only cancelled if the CLI
    sends a ``control_cancel_request`` for that id (~L279). So any ceiling would
    come from the CLI/model side, not the SDK callback await.
  * The 60s timeout (_send_control_request, ~L502, ``anyio.fail_after(60.0)``)
    governs the OUTBOUND direction (initialize / interrupt / set_permission_mode),
    NOT the held permission callback.
  * receive_response()/receive_messages() impose no client-side timeout; OUR
    harness adds one via asyncio.wait_for, so each send() here is given a
    per-message timeout strictly LARGER than that trial's hold, otherwise the
    harness — not the SDK — would abort the wait. (This is a harness artifact,
    documented so it is not misread as an SDK ceiling.)

TRIALS (run live; total runtime bounded ~<=18 min):
  T1.1 Hold-then-ALLOW (~120s): hold inside can_use_tool, then allow Write of a
       sentinel; assert the tool executes (sentinel present) + clean result.
  T1.2 Hold-then-DENY (~120s): hold, then deny; assert tool did NOT execute
       (sentinel absent) + session ends clean.
  T1.3 Interactive-answer hold (~120s): AskUserQuestion; hold, then answer
       NATIVELY via PermissionResultAllow(updated_input={**input,"answers":{q:label}})
       (the P0-proven C3 path); assert the session continues on the code answer.
  T1.4 Ceiling probe (~300s): ONE 5-min hold on an ALLOW to gather signal on
       whether a near-term ceiling < ~60 min exists. Success -> "no ceiling
       observed <= 5 min"; timeout/error -> record the observed ceiling.
  T1.5 Backstop mechanism (~60s, harness-side): start a hold but have a SHORT
       harness-side timer auto-resolve (DENY + notify log) at 60s; confirm the
       session stays usable after the auto-deny. Proves the engine can implement
       the 60-min backstop as a harness-side timer (no 60-min wait needed).

CONTAINMENT: disposable temp fixtures OUTSIDE the repo; host CLI auth, NO API
key; deny-by-default + path-containment in the callback; descendant-scoped
CLI-process leak check (bot tree out of scope); temp fixtures + CLI project
transcript dirs cleaned. Evidence -> evidence/*.{json,transcript.txt} via the P0
recorder (scrubbed, SB3/X3). One overall verdict file (p1_async_latency) plus a
per-trial file each.

Run (with the spike venv interpreter):
  spikes/p1-async-latency/.venv/bin/python spikes/p1-async-latency/run_trials.py
Quick smoke (tiny holds, proves wiring without the long waits):
  P1_FAST=1 spikes/p1-async-latency/.venv/bin/python spikes/p1-async-latency/run_trials.py
"""

from __future__ import annotations

import asyncio
import importlib.metadata as importlib_metadata
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from _common import (
    EVIDENCE_DIR,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SDKSessionHarness,
    ToolResultBlock,
    ToolUseBlock,
    assistant_text,
    clean_project_transcript_dir,
    descendant_claude_pids,
    git_porcelain,
    record_criterion,
    safe_input_summary,
)

# Hold durations. P1_FAST shrinks them to ~3s/5s so the wiring can be smoke-
# tested without the multi-minute waits; the real verdict uses the full holds.
FAST = os.environ.get("P1_FAST") == "1"
HOLD_SHORT = 3.0 if FAST else 120.0       # trials 1-3
HOLD_CEILING = 5.0 if FAST else 300.0     # trial 4 (~5 min)
BACKSTOP_AT = 2.0 if FAST else 60.0       # trial 5 harness-side auto-resolve
# Per-message harness timeout MUST exceed the hold (else the harness, not the
# SDK, aborts the wait). Generous head-room for the model turn around the hold.
MARGIN = 90.0

SENTINEL = "P1_ASYNC_OK"
ALLOWED_NAME = "ok.txt"


def now() -> float:
    return time.monotonic()


# --- shared callback containment policy (deny-by-default, path-contained) -----

def _resolve_target(fixture: Path, tool_input) -> Path | None:
    target = ""
    if isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return None
    p = Path(target)
    return (p if p.is_absolute() else (fixture / p)).resolve()


def _contained(fixture: Path, resolved: Path | None) -> bool:
    """True iff resolved is the allowed sentinel file inside the fixture."""
    if resolved is None:
        return False
    if resolved != fixture and fixture not in resolved.parents:
        return False
    return resolved.name == ALLOWED_NAME


def _stream_kinds(msg) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, ToolUseBlock):
                out.append(("tool_use", f"{b.name} {safe_input_summary(b.name, b.input)}"))
            elif isinstance(b, ToolResultBlock):
                out.append(("tool_result", f"is_error={getattr(b, 'is_error', None)} {str(b.content)[:160]}"))
    return out


# =============================================================================
# Trial 1 — Hold-then-ALLOW
# =============================================================================
async def trial_allow(hold: float, log) -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="p1_allow_")).resolve()
    target = fixture / ALLOWED_NAME
    cap = {"name": "T1.1 hold-then-ALLOW", "hold_requested": hold, "hold_awaited": None,
           "fired": 0, "decision": None, "control_survived": None, "executed": None,
           "result_is_error": None, "session_continued": None, "sid": None, "error": None}

    async def can_use_tool(tool_name, tool_input, context):
        cap["fired"] += 1
        resolved = _resolve_target(fixture, tool_input)
        t = now()
        await asyncio.sleep(hold)                       # <-- the multi-minute hold
        cap["hold_awaited"] = round(now() - t, 1)
        if _contained(fixture, resolved):
            cap["decision"] = "allow"
            return PermissionResultAllow()
        cap["decision"] = "deny(containment)"
        return PermissionResultDeny(message="containment: only ok.txt inside fixture")

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="default", can_use_tool=can_use_tool)
    try:
        await harness.start()
        final = ""
        async for msg in harness.send(
            f'Use the Write tool to create the file "{target}" with the exact '
            f'content "{SENTINEL}". Do only this one action.',
            timeout=hold + MARGIN,
        ):
            final += assistant_text(msg)
            if isinstance(msg, ResultMessage):
                cap["result_is_error"] = msg.is_error
            cap["sid"] = harness.session_id or cap["sid"]
        # The control request survived iff the callback fired AND we reached a
        # ResultMessage (no control-request-timeout exception was raised).
        cap["control_survived"] = (cap["fired"] >= 1 and cap["result_is_error"] is not None)
        cap["executed"] = target.exists() and (SENTINEL in target.read_text(encoding="utf-8") if target.exists() else False)
        cap["session_continued"] = cap["result_is_error"] is False
        cap["final"] = final.strip()[:160]
    except Exception as exc:  # noqa: BLE001 - record, fail-clean
        cap["error"] = f"{type(exc).__name__}: {exc}"
        cap["control_survived"] = False
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        clean_project_transcript_dir(str(fixture), log)
    return cap


# =============================================================================
# Trial 2 — Hold-then-DENY
# =============================================================================
async def trial_deny(hold: float, log) -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="p1_deny_")).resolve()
    target = fixture / ALLOWED_NAME
    cap = {"name": "T1.2 hold-then-DENY", "hold_requested": hold, "hold_awaited": None,
           "fired": 0, "decision": "deny", "control_survived": None, "executed": None,
           "result_is_error": None, "session_continued": None, "sid": None, "error": None}

    async def can_use_tool(tool_name, tool_input, context):
        cap["fired"] += 1
        t = now()
        await asyncio.sleep(hold)
        cap["hold_awaited"] = round(now() - t, 1)
        return PermissionResultDeny(message="held, then denied by code (T1.2)")

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="default", can_use_tool=can_use_tool)
    try:
        await harness.start()
        final = ""
        async for msg in harness.send(
            f'Use the Write tool to create the file "{target}" with the exact '
            f'content "{SENTINEL}". Do only this one action.',
            timeout=hold + MARGIN,
        ):
            final += assistant_text(msg)
            if isinstance(msg, ResultMessage):
                cap["result_is_error"] = msg.is_error
            cap["sid"] = harness.session_id or cap["sid"]
        cap["control_survived"] = (cap["fired"] >= 1 and cap["result_is_error"] is not None)
        cap["executed"] = target.exists()  # expected False (deny -> not executed)
        # Session "continued/ended clean": we got a ResultMessage at all (deny is a
        # normal outcome; whether is_error is set depends on the model wrap-up).
        cap["session_continued"] = cap["result_is_error"] is not None
        cap["final"] = final.strip()[:160]
    except Exception as exc:  # noqa: BLE001
        cap["error"] = f"{type(exc).__name__}: {exc}"
        cap["control_survived"] = False
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        clean_project_transcript_dir(str(fixture), log)
    return cap


# =============================================================================
# Trial 3 — Interactive-answer hold (AskUserQuestion, native answer)
# =============================================================================
async def trial_interactive(hold: float, log) -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="p1_ask_")).resolve()
    pick_label = "Bravo"  # code's choice; the neutral prompt never names it
    cap = {"name": "T1.3 interactive-answer hold", "hold_requested": hold, "hold_awaited": None,
           "fired": 0, "decision": "allow(answers-map)", "control_survived": None,
           "answered_not_error": None, "session_continued": None, "chosen": pick_label,
           "result_is_error": None, "sid": None, "error": None}
    tool_results: list[tuple] = []

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name == "AskUserQuestion":
            cap["fired"] += 1
            t = now()
            await asyncio.sleep(hold)
            cap["hold_awaited"] = round(now() - t, 1)
            try:
                q = tool_input["questions"][0]["question"]
            except Exception:
                q = None
            # NATIVE C3 path: ALLOW with the documented answers-map (question -> label).
            return PermissionResultAllow(updated_input={**tool_input, "answers": {q: pick_label}})
        return PermissionResultDeny(message="only AskUserQuestion permitted in T1.3")

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="default", can_use_tool=can_use_tool)
    try:
        await harness.start()
        final = ""
        async for msg in harness.send(
            'Use the AskUserQuestion tool to ask me to choose between two options with '
            'labels "Alpha" and "Bravo" (header "Pick", question "Pick one option"). '
            "After you receive my selection, reply with exactly one line: "
            "FINAL_PICK=<the exact label I selected>.",
            timeout=hold + MARGIN,
        ):
            final += assistant_text(msg)
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolResultBlock):
                        tool_results.append((getattr(b, "is_error", None), str(b.content)[:240]))
            if isinstance(msg, ResultMessage):
                cap["result_is_error"] = msg.is_error
            cap["sid"] = harness.session_id or cap["sid"]
        cap["control_survived"] = (cap["fired"] >= 1 and cap["result_is_error"] is not None)
        cap["answered_not_error"] = bool(tool_results) and all(ie in (None, False) for ie, _ in tool_results) \
            and any("have been answered" in txt.lower() for _, txt in tool_results)
        # Session continued on the CODE choice iff the model echoed the code-picked label.
        cap["session_continued"] = pick_label.lower() in final.lower()
        cap["final"] = final.strip()[:160]
        cap["tool_results"] = [(ie, txt[:120]) for ie, txt in tool_results]
    except Exception as exc:  # noqa: BLE001
        cap["error"] = f"{type(exc).__name__}: {exc}"
        cap["control_survived"] = False
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        clean_project_transcript_dir(str(fixture), log)
    return cap


# =============================================================================
# Trial 4 — Ceiling probe (~5 min hold on ALLOW)
# =============================================================================
async def trial_ceiling(hold: float, log) -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="p1_ceil_")).resolve()
    target = fixture / ALLOWED_NAME
    cap = {"name": "T1.4 ceiling probe", "hold_requested": hold, "hold_awaited": None,
           "fired": 0, "decision": None, "control_survived": None, "executed": None,
           "result_is_error": None, "ceiling_observed": None, "sid": None, "error": None}

    async def can_use_tool(tool_name, tool_input, context):
        cap["fired"] += 1
        resolved = _resolve_target(fixture, tool_input)
        t = now()
        await asyncio.sleep(hold)
        cap["hold_awaited"] = round(now() - t, 1)
        if _contained(fixture, resolved):
            cap["decision"] = "allow"
            return PermissionResultAllow()
        cap["decision"] = "deny(containment)"
        return PermissionResultDeny(message="containment")

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="default", can_use_tool=can_use_tool)
    started = now()
    try:
        await harness.start()
        async for msg in harness.send(
            f'Use the Write tool to create the file "{target}" with the exact '
            f'content "{SENTINEL}". Do only this one action.',
            timeout=hold + MARGIN,
        ):
            if isinstance(msg, ResultMessage):
                cap["result_is_error"] = msg.is_error
            cap["sid"] = harness.session_id or cap["sid"]
        cap["control_survived"] = (cap["fired"] >= 1 and cap["result_is_error"] is not None)
        cap["executed"] = target.exists() and (SENTINEL in target.read_text(encoding="utf-8") if target.exists() else False)
        cap["ceiling_observed"] = None  # no ceiling within the awaited hold
    except Exception as exc:  # noqa: BLE001
        # A timeout/error here, with the callback having fired and slept ~hold,
        # is the critical finding: a ceiling at ~ this many seconds.
        elapsed = round(now() - started, 1)
        cap["error"] = f"{type(exc).__name__}: {exc}"
        cap["control_survived"] = False
        cap["ceiling_observed"] = cap.get("hold_awaited") or elapsed
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        clean_project_transcript_dir(str(fixture), log)
    return cap


# =============================================================================
# Trial 5 — Backstop mechanism (harness-side auto-resolve timer)
# =============================================================================
async def trial_backstop(backstop_at: float, log) -> dict:
    """Start a hold, but a harness-side timer auto-resolves (DENY + notify) at a
    SHORT configured interval, proving the engine can back-stop a pending decision
    with a timer instead of waiting the full 60 min. After the auto-deny we send a
    SECOND turn to confirm the session is still usable.
    """
    fixture = Path(tempfile.mkdtemp(prefix="p1_backstop_")).resolve()
    target = fixture / ALLOWED_NAME
    cap = {"name": "T1.5 harness-side backstop", "backstop_at": backstop_at,
           "fired": 0, "auto_resolved_after": None, "notify_logged": False,
           "decision": None, "result1_is_error": None, "executed": None,
           "second_turn_ok": None, "session_usable_after": None, "sid": None, "error": None}
    notify: list[str] = []

    async def can_use_tool(tool_name, tool_input, context):
        cap["fired"] += 1
        # Simulate an indefinite human wait, but race it against a backstop timer.
        # Whichever wins decides. This is the engine's intended pattern: a pending
        # decision Future + a timer that resolves it if the human doesn't.
        t = now()
        human_decides = asyncio.Event()  # never set here -> human "never answers"
        backstop = asyncio.create_task(asyncio.sleep(backstop_at))
        waiter = asyncio.create_task(human_decides.wait())
        done, pending = await asyncio.wait({backstop, waiter}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        cap["auto_resolved_after"] = round(now() - t, 1)
        if backstop in done:
            msg = f"BACKSTOP fired after {backstop_at:.0f}s: no human decision -> auto-DENY"
            notify.append(msg)
            cap["notify_logged"] = True
            cap["decision"] = "auto-deny(backstop)"
            return PermissionResultDeny(message=msg)
        cap["decision"] = "allow(human)"
        return PermissionResultAllow()

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="default", can_use_tool=can_use_tool)
    try:
        await harness.start()
        # Turn 1: triggers the held->auto-denied tool.
        async for msg in harness.send(
            f'Use the Write tool to create the file "{target}" with the exact '
            f'content "{SENTINEL}". Do only this one action.',
            timeout=backstop_at + MARGIN,
        ):
            if isinstance(msg, ResultMessage):
                cap["result1_is_error"] = msg.is_error
            cap["sid"] = harness.session_id or cap["sid"]
        cap["executed"] = target.exists()  # expected False (auto-denied)

        # Turn 2 (SAME session): a plain text turn to prove the session survived
        # the auto-deny and is still usable.
        t2 = ""
        async for msg in harness.send("Reply with exactly: BACKSTOP_SESSION_OK", timeout=MARGIN):
            t2 += assistant_text(msg)
        cap["second_turn_ok"] = "BACKSTOP_SESSION_OK" in t2
        cap["session_usable_after"] = cap["second_turn_ok"]
        cap["final2"] = t2.strip()[:120]
    except Exception as exc:  # noqa: BLE001
        cap["error"] = f"{type(exc).__name__}: {exc}"
        cap["session_usable_after"] = False
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        clean_project_transcript_dir(str(fixture), log)
    cap["notify"] = notify
    return cap


# =============================================================================
# Driver
# =============================================================================
def _record_trial(crit: str, cap: dict, lines: list[str]) -> None:
    """Persist a per-trial evidence file (scrubbed) with a derived verdict."""
    verdict, reason = _trial_verdict(cap)
    body = [f"=== {cap['name']} ({crit}) ==="]
    for k, v in cap.items():
        if k in ("name",):
            continue
        body.append(f"  {k}: {v}")
    body.append(f"  -> trial verdict: {verdict} — {reason}")
    text = "\n".join(body) + "\n"
    extra = [cap["sid"]] if cap.get("sid") else None
    with record_criterion(crit, base_dir=EVIDENCE_DIR, extra_secrets=extra) as rec:
        rec.add_transcript(text)
        rec.set_verdict(verdict, reason)
    lines.append(text)


def _trial_verdict(cap: dict) -> tuple[str, str]:
    name = cap["name"]
    if cap.get("error") and cap.get("ceiling_observed") is None:
        # Hard error not classified as a ceiling -> trial FAIL.
        return "FAIL", f"error during trial: {cap['error']}"
    if name.startswith("T1.1"):
        ok = cap.get("control_survived") and cap.get("executed") and cap.get("session_continued")
        held = (cap.get("hold_awaited") or 0) >= (HOLD_SHORT - 5)
        if ok and held:
            return "PASS", f"held {cap['hold_awaited']}s then ALLOW honored; tool executed; clean result."
        return ("PARTIAL" if (cap.get("control_survived") or cap.get("executed")) else "FAIL"), \
            f"held={cap.get('hold_awaited')} survived={cap.get('control_survived')} executed={cap.get('executed')} continued={cap.get('session_continued')}"
    if name.startswith("T1.2"):
        ok = cap.get("control_survived") and (cap.get("executed") is False) and cap.get("session_continued")
        held = (cap.get("hold_awaited") or 0) >= (HOLD_SHORT - 5)
        if ok and held:
            return "PASS", f"held {cap['hold_awaited']}s then DENY honored; tool did NOT execute; session clean."
        return ("PARTIAL" if cap.get("control_survived") else "FAIL"), \
            f"held={cap.get('hold_awaited')} survived={cap.get('control_survived')} executed={cap.get('executed')} continued={cap.get('session_continued')}"
    if name.startswith("T1.3"):
        ok = cap.get("control_survived") and cap.get("answered_not_error") and cap.get("session_continued")
        held = (cap.get("hold_awaited") or 0) >= (HOLD_SHORT - 5)
        if ok and held:
            return "PASS", f"held {cap['hold_awaited']}s then native answer honored; session continued on code choice {cap.get('chosen')!r}."
        return ("PARTIAL" if cap.get("control_survived") else "FAIL"), \
            f"held={cap.get('hold_awaited')} survived={cap.get('control_survived')} answered={cap.get('answered_not_error')} continued={cap.get('session_continued')}"
    if name.startswith("T1.4"):
        if cap.get("ceiling_observed") is not None:
            return "PARTIAL", f"CEILING observed at ~{cap['ceiling_observed']}s (< target): {cap.get('error')}"
        ok = cap.get("control_survived") and cap.get("executed")
        held = (cap.get("hold_awaited") or 0) >= (HOLD_CEILING - 5)
        if ok and held:
            return "PASS", f"no ceiling observed <= {cap['hold_awaited']}s (~{(cap['hold_awaited'] or 0)/60:.1f} min); allow honored after the long hold."
        return ("PARTIAL" if cap.get("control_survived") else "FAIL"), \
            f"held={cap.get('hold_awaited')} survived={cap.get('control_survived')} executed={cap.get('executed')}"
    if name.startswith("T1.5"):
        ok = cap.get("notify_logged") and (cap.get("executed") is False) and cap.get("session_usable_after")
        fired_at_backstop = abs((cap.get("auto_resolved_after") or 0) - cap["backstop_at"]) <= 5
        if ok and fired_at_backstop:
            return "PASS", f"backstop auto-resolved at ~{cap['auto_resolved_after']}s (DENY+notify); tool not executed; session usable after (2nd turn OK)."
        return ("PARTIAL" if cap.get("notify_logged") else "FAIL"), \
            f"auto_resolved_after={cap.get('auto_resolved_after')} notify={cap.get('notify_logged')} executed={cap.get('executed')} usable_after={cap.get('session_usable_after')}"
    return "FAIL", "unknown trial"


async def main() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T1 / P1 — async-latency DE-RISK SPIKE (substrate A, live, contained) ===")
    log(f"sdk: claude-agent-sdk=={importlib_metadata.version('claude-agent-sdk')} "
        f"(ADR-001 environment of record)")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (MUST be False — host CLI auth only)")
    log(f"P1_FAST (smoke) mode: {FAST}  | holds: short={HOLD_SHORT}s ceiling={HOLD_CEILING}s backstop_at={BACKSTOP_AT}s margin={MARGIN}s")
    log("")
    log("SDK FACTS (from reading claude-agent-sdk==0.2.105 source):")
    log("  * Query._handle_control_request does `await self.can_use_tool(...)` with NO")
    log("    fail_after/timeout — the SDK waits indefinitely for the callback (query.py ~L408).")
    log("  * Each inbound control request is its own spawned task; only a CLI-sent")
    log("    control_cancel_request cancels it (query.py ~L239/~L279). Any ceiling is CLI/model-side.")
    log("  * The 60s timeout (_send_control_request, ~L502) is OUTBOUND only (initialize/interrupt/")
    log("    set_permission_mode) — it does NOT bound the held permission callback.")
    log("  * receive_response() has no client-side timeout; OUR harness adds asyncio.wait_for, so each")
    log("    send() below is given timeout=hold+margin so the harness never aborts the wait first.")
    log("")

    pids_before = descendant_claude_pids()
    git_before = git_porcelain()
    log(f"descendant claude pids before: {pids_before}")
    log(f"git porcelain (set) before run: {sorted(git_before) if git_before else 'clean'}")

    started = now()
    trials: list[tuple[str, dict]] = []

    # Run trials sequentially (one session at a time; bounded runtime).
    log("\n>>> running T1.1 hold-then-ALLOW ...")
    c = await trial_allow(HOLD_SHORT, log); trials.append(("t1_1_allow", c)); _record_trial("t1_1_allow", c, report)

    log("\n>>> running T1.2 hold-then-DENY ...")
    c = await trial_deny(HOLD_SHORT, log); trials.append(("t1_2_deny", c)); _record_trial("t1_2_deny", c, report)

    log("\n>>> running T1.3 interactive-answer hold ...")
    c = await trial_interactive(HOLD_SHORT, log); trials.append(("t1_3_interactive", c)); _record_trial("t1_3_interactive", c, report)

    log("\n>>> running T1.4 ceiling probe (~5 min) ...")
    c = await trial_ceiling(HOLD_CEILING, log); trials.append(("t1_4_ceiling", c)); _record_trial("t1_4_ceiling", c, report)

    log("\n>>> running T1.5 harness-side backstop ...")
    c = await trial_backstop(BACKSTOP_AT, log); trials.append(("t1_5_backstop", c)); _record_trial("t1_5_backstop", c, report)

    elapsed = round(now() - started, 1)
    pids_after = descendant_claude_pids()
    git_after = git_porcelain()
    leaked = sorted(set(pids_after) - set(pids_before))
    git_new = sorted(git_after - git_before)

    # --- per-trial verdicts ---
    verdicts = {crit: _trial_verdict(cap)[0] for crit, cap in trials}
    log("\n=== per-trial verdicts ===")
    for crit, cap in trials:
        v, r = _trial_verdict(cap)
        log(f"  {crit:18s} {v:8s}  {r}")

    # --- overall verdict logic ---
    def vd(crit: str) -> str:
        return verdicts.get(crit, "FAIL")

    core = ["t1_1_allow", "t1_2_deny", "t1_3_interactive"]   # the >=120s holds
    core_pass = all(vd(c) == "PASS" for c in core)
    backstop_pass = vd("t1_5_backstop") == "PASS"
    ceiling = trials[3][1]  # t1_4 cap
    ceiling_observed = ceiling.get("ceiling_observed")
    ceiling_pass = vd("t1_4_ceiling") == "PASS"

    log("\n=== OVERALL VERDICT REASONING ===")
    log(f"  core 120s holds (allow/deny/interactive) all PASS: {core_pass}")
    log(f"  harness-side backstop PASS: {backstop_pass}")
    log(f"  ceiling probe: {'no ceiling <= ~5 min' if ceiling_pass else f'ceiling ~{ceiling_observed}s'}")

    if core_pass and backstop_pass and ceiling_pass:
        overall, reason = "PASS", (
            "Multi-minute holds (>=120s) are tolerated on ALLOW, DENY, and the native "
            "interactive answer; the session continues on the resolved decision in every "
            "case; the harness-side backstop works (auto-resolves a pending decision with a "
            "timer + notify, session usable after); and no near-term ceiling was observed up "
            "to ~5 min. The SDK does not bound the can_use_tool callback (no fail_after on the "
            "inbound control request), so the 60-min backstop can be implemented engine-side as "
            "a timer over a pending-decision Future. Engine build (T4+) may proceed."
        )
    elif core_pass and backstop_pass and ceiling_observed is not None:
        overall, reason = "PARTIAL", (
            f"Multi-minute holds (>=120s) and the harness-side backstop work, BUT a ceiling was "
            f"observed at ~{ceiling_observed}s in the 5-min probe. The async-approve design works "
            f"only up to that bound; the engine must keep-alive the session or use a backstop "
            f"SHORTER than the ceiling (or a different pending pattern). Record the exact bound."
        )
    elif core_pass:
        overall, reason = "PARTIAL", (
            f"The core 120s holds (allow/deny/interactive) PASS, but a supporting trial did not: "
            f"backstop={vd('t1_5_backstop')}, ceiling={vd('t1_4_ceiling')} "
            f"(ceiling_observed={ceiling_observed}). The async-hold primitive works at 120s; the "
            f"flagged trial needs a design accommodation or a re-run."
        )
    else:
        failed = [c for c in core if vd(c) != "PASS"]
        overall, reason = "FAIL", (
            f"At least one CORE multi-minute hold did not hold cleanly: {failed} "
            f"(verdicts: {{{', '.join(f'{c}={vd(c)}' for c in core)}}}). The async-approve-from-"
            f"phone design as conceived does NOT work as-is and must be redesigned (e.g. shorter "
            f"holds + keep-alive, or a different pending pattern). This GATES the engine."
        )

    log("\n=== CONTAINMENT / CLEANUP ===")
    log(f"  total runtime: {elapsed}s (~{elapsed/60:.1f} min)")
    log(f"  descendant claude pids after: {pids_after}  leaked: {leaked} (expected: [])")
    log(f"  git porcelain NEW during run: {git_new if git_new else 'NONE'} (temp fixtures live OUTSIDE the repo)")
    log(f"  ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (expected False)")

    # If anything leaked, surface it and try a scoped cleanup (descendant-only).
    if leaked:
        log(f"  WARNING: leaked CLI pids {leaked}; attempting scoped termination ...")
        import signal
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    log("")
    log(f"OVERALL VERDICT: {overall}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    extra = [cap["sid"] for _, cap in trials if cap.get("sid")]
    with record_criterion("p1_async_latency", base_dir=EVIDENCE_DIR, extra_secrets=extra or None) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(overall, reason)

    return 0 if overall in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
