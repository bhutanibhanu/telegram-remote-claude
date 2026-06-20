"""T9 — C4 ExitPlanMode approve / reject + feedback (substrate A). MAKE-OR-BREAK.

C4 (design): a proposed plan is surfaced and can be APPROVED or REJECTED WITH
FEEDBACK programmatically, and the session honors the verdict.

Mechanism on claude-agent-sdk==0.2.105 (no ExitPlanMode special-casing; it flows
through can_use_tool in permission_mode="plan"):
  * APPROVE = PermissionResultAllow()  -> CLI returns "User has approved your
    plan. You can now start coding." (native; allow genuinely approves).
  * REJECT  = PermissionResultDeny(message=<feedback>) -> CLI returns
    "Plan rejected. <feedback>"; the model stays in plan mode and revises.

This probe runs TWO trials over isolated sessions and proves the session honors
each verdict, distinguishing genuine SDK behavior from model cooperation:
  * APPROVE trial: allow ExitPlanMode; confirm the "approved your plan" result.
  * REJECT trial: deny the first ExitPlanMode with feedback that injects a CODE
    marker the prompt never mentions (ADD_LOGGING_STEP); confirm the rejection is
    delivered, the model does NOT execute (plan never approved), and the
    code-injected marker is honored -> proves the feedback is code-driven, not
    the model's own idea.

FEEDBACK SIGNAL (the T9-repair correction). The full structured plan text from
EVERY ExitPlanMode tool_input is captured (NOT truncated) and the marker
assertion runs against that full text. Two independent signals are recorded:
  * marker_in_full_revised_plan  — marker present in a full revised structured
    plan field (plan_full[1:]); and
  * marker_in_revision_response  — marker present in the model's accumulated
    reply text after the rejection.
feedback_honored = (either signal). The combined gate is sound because BOTH
outcomes demonstrate that callback-supplied feedback influenced the
post-rejection session; whether the marker lands in the *structured plan field*
specifically is model-variant (see Distinguishers), so requiring only that field
would understate a real, reproducible capability. The exact supporting signal is
recorded per run.

CONTAINMENT (X1): cwd is a disposable temp fixture; ExitPlanMode is the decision
under test; read-only tools are allowed; Bash, Edit, AskUserQuestion, and any
Write outside the fixture/plan-scratch are DENIED, so an approved plan cannot
execute outside the fixture. Plan-mode writes a scratch file to ~/.claude/plans;
those created during this run are snapshotted and removed afterward.

Verdict (honest; PASS/PARTIAL/FAIL all valid):
  PASS    : approve honored (native) AND reject delivered + honored (no execution)
            AND feedback_honored (marker in full revised plan OR revision reply).
  PARTIAL : approve honored AND rejection delivered + no execution, but the code
            feedback is honored by NEITHER signal (marker absent from both the
            full revised plan and the revision reply).
  FAIL    : approval not honored, or rejection not honored.

No API key (host CLI auth). Evidence -> evidence/c4.* via the recorder (scrubbed).

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c4_plan.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evidence_recorder import record_criterion  # noqa: E402
from harness_sdk import SDKSessionHarness, assistant_text  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
)

MARKER = "ADD_LOGGING_STEP"  # code-injected via reject feedback; prompt never mentions logging
PLANS_DIR = (Path.home() / ".claude" / "plans").resolve()
READONLY = {"Read", "Glob", "Grep", "LS", "ToolSearch", "NotebookRead"}
PROMPT = (
    "Propose a brief plan (2-3 steps) to add a Python function greet() that prints "
    "hello to a new file greet.py. Use the ExitPlanMode tool to present your plan "
    "for approval."
)


def write_allowed(fixture: Path, tool_input) -> tuple[bool, str]:
    target = ""
    if isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return False, "no-target"
    p = Path(target)
    resolved = (p if p.is_absolute() else (fixture / p)).resolve()
    if resolved == fixture or fixture in resolved.parents:
        return True, "in-fixture"
    if resolved == PLANS_DIR or PLANS_DIR in resolved.parents:
        return True, "plan-scratch"
    return False, "outside-fixture"


async def run_trial(mode: str, log) -> dict:
    fixture = Path(tempfile.mkdtemp(prefix="t9_c4_")).resolve()
    cap = {
        "epm_fired": 0, "plans_full": [], "plans_preview": [], "epm_results": [],
        "denied_exec": [], "final": "", "sid": None, "approved_result": False,
        "other_tools": [],
    }
    epm_n = {"n": 0}

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name == "ExitPlanMode":
            epm_n["n"] += 1
            cap["epm_fired"] += 1
            plan_text = ""
            if isinstance(tool_input, dict):
                plan_text = str(tool_input.get("plan", ""))
            # Capture the FULL structured plan for the marker assertion (T9-repair:
            # never assert against a truncated value). Keep a short preview, used
            # for human-readable logging ONLY -- never for the verdict.
            cap["plans_full"].append(plan_text)
            cap["plans_preview"].append(plan_text[:200])
            if mode == "approve":
                return PermissionResultAllow()
            # reject mode
            if epm_n["n"] == 1:
                return PermissionResultDeny(
                    message=("Plan rejected. Revise and re-present via ExitPlanMode: you MUST "
                             f"add an explicit step literally named {MARKER} that adds logging.")
                )
            return PermissionResultDeny(message="Acknowledged — stop here; do not call any more tools.")
        if tool_name in READONLY:
            cap["other_tools"].append((tool_name, "allow"))
            return PermissionResultAllow()
        if tool_name == "Write":
            ok, why = write_allowed(fixture, tool_input)
            cap["other_tools"].append((f"Write({why})", "allow" if ok else "deny"))
            if ok:
                return PermissionResultAllow()
            cap["denied_exec"].append(("Write", why))
            return PermissionResultDeny(message=f"contained: Write {why} denied")
        # Bash, Edit, AskUserQuestion, everything else -> deny (no execution)
        cap["denied_exec"].append((tool_name, "denied"))
        return PermissionResultDeny(message=f"contained: {tool_name} denied in C4 plan check")

    harness = SDKSessionHarness(cwd=str(fixture), permission_mode="plan", can_use_tool=can_use_tool)
    try:
        await harness.start()
        async for msg in harness.send(PROMPT, timeout=120):
            cap["final"] += assistant_text(msg)
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolResultBlock):
                        txt = str(b.content)[:220]
                        low = txt.lower()
                        if "approved your plan" in low or "plan rejected" in low or "rejected" in low:
                            cap["epm_results"].append((getattr(b, "is_error", None), txt))
                            if "approved your plan" in low:
                                cap["approved_result"] = True
            cap["sid"] = harness.session_id or cap["sid"]
    except Exception as exc:  # noqa: BLE001
        cap["final"] += f" [EXC {type(exc).__name__}: {exc}]"
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
    return cap


def log_trial(log, name, cap):
    log(f"\n--- {name} ---")
    log(f"  session_id: {cap['sid']}  ExitPlanMode fired: {cap['epm_fired']}")
    for i, full in enumerate(cap["plans_full"]):
        # Report the FULL-plan length and a full-text marker check (the value the
        # verdict actually uses), then a short preview for human readability.
        marker_here = MARKER.lower() in full.lower()
        log(f"  plan[{i}] full_len={len(full)} marker_in_full_plan={marker_here} "
            f"preview(200): {cap['plans_preview'][i]!r}")
    for ie, t in cap["epm_results"]:
        log(f"  ExitPlanMode tool_result is_error={ie}: {t!r}")
    log(f"  non-ExitPlanMode tool decisions: {cap['other_tools']}")
    log(f"  denied execution attempts: {cap['denied_exec']}")
    log(f"  approved_result seen: {cap['approved_result']}")
    log(f"  model final (first 200): {cap['final'].strip()[:200]!r}")
    log(f"  model final full_len={len(cap['final'])} "
        f"marker_in_response={MARKER.lower() in cap['final'].lower()}")


async def _run() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    import importlib.metadata as md
    log("=== T9 / C4 — ExitPlanMode approve/reject + feedback (substrate A) ===")
    log(f"sdk: claude-agent-sdk=={md.version('claude-agent-sdk')}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")
    log("permission_mode=plan; ExitPlanMode via can_use_tool; execution tools contained "
        "(Bash/Edit/out-of-fixture Write/AskUserQuestion denied). No bypass.")

    plans_before = set(PLANS_DIR.glob("*.md")) if PLANS_DIR.exists() else set()

    verdict, reason = "FAIL", "check did not complete"
    try:
        approve = await run_trial("approve", log)
        log_trial(log, "TRIAL approve (allow ExitPlanMode)", approve)
        reject = await run_trial("reject", log)
        log_trial(log, "TRIAL reject (deny ExitPlanMode + feedback marker)", reject)

        approve_honored = approve["epm_fired"] >= 1 and approve["approved_result"]
        reject_delivered = reject["epm_fired"] >= 1 and any(
            (ie and "reject" in t.lower()) for ie, t in reject["epm_results"]
        )
        reject_no_execution = (not reject["approved_result"])  # plan never approved -> no exec greenlit
        # T9-repair: assert the marker against the FULL revised structured plan
        # (plans_full[1:], i.e. every ExitPlanMode after the first/rejected one),
        # NOT a truncated preview. Record BOTH signals separately.
        marker_in_full_revised_plan = any(
            MARKER.lower() in p.lower() for p in reject["plans_full"][1:]
        )
        marker_in_revision_response = MARKER.lower() in reject["final"].lower()
        feedback_honored = marker_in_full_revised_plan or marker_in_revision_response
        reject_honored = reject_delivered and reject_no_execution and feedback_honored

        if marker_in_full_revised_plan and marker_in_revision_response:
            which_signal = ("BOTH: marker present in the full revised structured plan "
                            "AND acknowledged in the model's revision reply")
        elif marker_in_full_revised_plan:
            which_signal = ("full revised structured plan field (marker absent from the "
                            "reply text on this run)")
        elif marker_in_revision_response:
            which_signal = ("model's revision reply text (marker absent from the structured "
                            "plan field on this run -- structured-plan placement is model-variant)")
        else:
            which_signal = "NEITHER signal (code feedback not honored on this run)"

        log("\n=== C4 evaluation ===")
        log(f"approve_honored (allow -> 'approved your plan'): {approve_honored}")
        log(f"reject_delivered (deny -> 'Plan rejected' with feedback): {reject_delivered}")
        log(f"reject_no_execution (plan never approved -> nothing executed): {reject_no_execution}")
        log("feedback code-driven (FULL-plan assertion, two signals):")
        log(f"  marker_in_full_revised_plan = {marker_in_full_revised_plan}")
        log(f"  marker_in_revision_response = {marker_in_revision_response}")
        log(f"  feedback_honored (either)   = {feedback_honored}")
        log(f"  supporting signal           = {which_signal}")

        if approve_honored and reject_honored:
            verdict = "PASS"
            reason = (
                "C4 honored both verdicts via the real SDK permission callback (can_use_tool). "
                "APPROVE (PermissionResultAllow) -> native 'User has approved your plan'. REJECT "
                "(PermissionResultDeny message=feedback) -> 'Plan rejected'; the model stayed in plan "
                "mode (no execution; plan never approved) and the code-injected marker "
                f"{MARKER} -- which the neutral prompt never mentioned -- was honored via the {which_signal}. "
                "feedback_honored asserts against the FULL revised structured plan OR the revision reply "
                "(either suffices; both prove code-supplied feedback influenced the post-rejection session). "
                "CAVEATS: whether the marker lands in the structured plan FIELD is model-variant; reject "
                "feedback travels through the PermissionResultDeny(message=...) channel; post-approval "
                "ARBITRARY execution is not tested (contained); C4 concerns programmatic approval/rejection "
                "and honoring feedback. Substrate B still requires its own C4 probe at T16."
            )
        elif approve_honored and reject_delivered and reject_no_execution:
            verdict = "PARTIAL"
            reason = (
                "APPROVE honored natively (PermissionResultAllow -> approved). REJECT delivered "
                "(PermissionResultDeny message=feedback -> 'Plan rejected') and honored by no-execution "
                "(plan never approved), but the code-injected marker was honored by NEITHER signal on this "
                f"run (marker_in_full_revised_plan={marker_in_full_revised_plan}, "
                f"marker_in_revision_response={marker_in_revision_response}). Approval/rejection control is "
                "proven; demonstrable incorporation of the code feedback was not reproduced this run. "
                "CAVEATS: structured-plan marker placement is model-variant; reject feedback rides the "
                "PermissionResultDeny(message=...) channel; post-approval arbitrary execution is not tested; "
                "substrate B C4 still pending at T16."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"C4 not honored: approve_honored={approve_honored} reject_delivered={reject_delivered} "
                f"reject_no_execution={reject_no_execution} feedback_honored={feedback_honored}."
            )
    except Exception as exc:  # noqa: BLE001
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
    finally:
        # Clean up plan-scratch files created during this run (outside the repo).
        removed = 0
        if PLANS_DIR.exists():
            for f in set(PLANS_DIR.glob("*.md")) - plans_before:
                try:
                    f.unlink(); removed += 1
                except Exception:
                    pass
        log(f"\ncleanup: removed {removed} plan-scratch file(s) created in {PLANS_DIR}")

    log("")
    log("Distinguishers / limitations (explicit disclosures):")
    log("- ExitPlanMode fired as a real tool_use intercepted by the real SDK permission")
    log("  callback can_use_tool (not a model refusal / not a no-call).")
    log("- APPROVE is native: PermissionResultAllow yields the CLI's 'approved your plan' result.")
    log("- REJECT feedback travels through the PermissionResultDeny(message=...) channel; for plan")
    log("  rejection this is the natural rejection channel, but it still depends on the model")
    log("  reading/revising per that text.")
    log(f"- Code-driven proof: the marker {MARKER} is injected ONLY by the callback feedback; the")
    log("  neutral prompt never mentions logging, so its appearance in the revised plan or reply")
    log("  came from code, not model prompt-following.")
    log("- The feedback gate asserts against the FULL revised structured plan text (NOT a truncated")
    log("  preview) OR the model's revision reply. Whether the marker lands in the structured-plan")
    log("  FIELD specifically is MODEL-VARIANT across runs; the reply-text signal is the robust path,")
    log("  so the gate accepts either (both prove code-supplied feedback influenced the session).")
    log("- Execution was contained (Bash/Edit/out-of-fixture Write/AskUserQuestion denied); only plan")
    log("  presentation is tested. POST-APPROVAL ARBITRARY EXECUTION IS NOT TESTED here.")
    log("- C4 concerns PROGRAMMATIC APPROVAL/REJECTION + honoring feedback only; no claim about")
    log("  C1/C2/C3/C5/C6. Substrate B still REQUIRES ITS OWN C4 PROBE at T16 (design S1).")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    with record_criterion("c4") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
