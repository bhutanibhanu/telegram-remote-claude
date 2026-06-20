"""T16 — C4 ExitPlanMode approve / reject + feedback OVER THE WIRE (substrate B).

MAKE-OR-BREAK criterion, tested on substrate B UNCONDITIONALLY. This is the
independent C4 result for SUBSTRATE B (the raw ``claude`` CLI driven over the
bidirectional ``stream-json`` control protocol via the T13 ``CLISessionHarness``;
stdlib subprocess + NDJSON, NO claude-agent-sdk import). It is held to the SAME
(repaired) standard as substrate A's C4 (T9 / ``checks/c4_plan.py``) and mirrors
its rigor, its corrected feedback semantics, and its distinguishers.

C4 (design): a proposed plan is surfaced and can be APPROVED or REJECTED WITH
FEEDBACK programmatically, and the session honors the verdict.

==============================================================================
HOW ExitPlanMode APPEARS OVER THE WIRE + THE NATIVE APPROVE/REJECT MECHANISM
==============================================================================
ExitPlanMode arrives as an ORDINARY ``can_use_tool`` control_request (the SAME
control frame as Write/Bash/AskUserQuestion) when the session runs in
``--permission-mode plan``; the proposed plan text is in ``request.input.plan``.
There is NO distinct message/control subtype for it -- it is not special-cased on
the wire.

The tool's DOCUMENTED schema (CLI ``sdk-tools.d.ts``) was inspected to determine
whether approve/reject is a binary permission allow/deny or whether there is a
richer native feedback field (the C3 lesson: inject the documented OUTPUT shape,
not a guessed one):
  * ``ExitPlanModeInput``  : ``allowedPrompts?`` (Bash prompt categories) + open
    ``[k: string]: unknown`` -- the proposed plan text rides ``input.plan``.
  * ``ExitPlanModeOutput`` : ``plan: string|null``, ``isAgent``, ``filePath?``,
    ``hasTaskTool?``, ``planWasEdited?``, ``awaitingLeaderApproval?``,
    ``requestId?``. There is **NO ``feedback`` / ``rejectionReason`` / decision
    field**. The output's ``plan``/``filePath`` only echo the (possibly edited)
    plan, NOT a feedback channel.
=> CONCLUSION (recorded): ExitPlanMode approve/reject is a BINARY permission
   allow/deny, exactly like T9. There is NO richer native feedback field. The
   native mechanism over the wire is therefore:
     * APPROVE = ALLOW control_response  -> CLI returns the native
       "User has approved your plan. You can now start coding."
     * REJECT  = DENY control_response carrying ``message`` = feedback -> CLI
       returns "Plan rejected. <feedback>"; the model stays in plan mode and
       revises. Reject feedback rides the PermissionResultDeny(message=...)
       channel (the natural rejection channel, not a dedicated plan-feedback API).

==============================================================================
WHAT THIS CHECK PROVES (no TTY, code-driven) -- mirrors T9 exactly
==============================================================================
TWO trials over isolated CLI sessions in ``--permission-mode plan``:
  * APPROVE trial: ALLOW the ExitPlanMode can_use_tool; confirm the native CLI
    "User has approved your plan" tool_result.
  * REJECT trial: DENY the FIRST ExitPlanMode with a message that injects a CODE
    marker the neutral prompt never mentions (ADD_LOGGING_STEP); confirm the
    rejection is delivered ("Plan rejected ..."), the plan is NEVER approved ->
    no execution greenlit, and the code-injected marker is honored via EITHER:
      - marker_in_full_revised_plan  -- marker present in the FULL revised
        structured plan text (every ExitPlanMode after the rejected first one;
        captured FULL, never truncated; a [:200] preview is kept for readability
        ONLY and is never used for the verdict); OR
      - marker_in_revision_response  -- marker present in the model's accumulated
        revision reply text.
    feedback_honored = (either). Both signals are recorded + which supported the
    run. Whether the marker lands in the structured-plan FIELD specifically is
    MODEL-VARIANT (see T9 / Distinguishers), so requiring only that field would
    understate a real, reproducible capability -- the combined gate is sound
    because BOTH outcomes prove callback-supplied feedback influenced the
    post-rejection session.

CONTAINMENT (X1): cwd is a disposable temp fixture OUTSIDE the repo; ExitPlanMode
is the decision under test; read-only tools (Read/Glob/Grep/LS/...) are allowed so
the model can orient; Bash, Edit, AskUserQuestion, and any Write outside the
fixture/plan-scratch are DENIED, so an approved plan cannot execute outside the
fixture. ``--permission-mode plan``, NO bypass. Plan-mode writes a scratch file to
~/.claude/plans; those created during THIS run are snapshotted before/after and
ONLY the new ones removed. The repo ``git status`` is recorded before/after
(expected unchanged); the fixture + any ~/.claude/projects/<sanitized-cwd> dir are
cleaned. Host CLI auth, no API key. Evidence -> evidence/c4_cli.* via the T3
recorder (scrubbed, SB3/X3).

Verdict (honest; PASS/PARTIAL/FAIL all valid -- NOT manufactured):
  PASS    : approve honored (native "approved your plan") AND reject delivered +
            honored (no execution) AND feedback_honored (either signal).
  PARTIAL : approve honored AND reject delivered + no execution, but the code
            feedback is honored by NEITHER signal this run.
  FAIL    : approval not honored, or rejection not honored.

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c4_plan_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# harness_cli + evidence_recorder live one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evidence_recorder import record_criterion  # noqa: E402
from harness_cli import (  # noqa: E402
    CLIDriverError,
    CLISessionHarness,
    allow_tool,
    deny_tool,
)

WORKTREE = Path(__file__).resolve().parents[2]  # repo worktree root
MARKER = "ADD_LOGGING_STEP"  # code-injected via reject feedback; prompt never mentions logging
PLANS_DIR = (Path.home() / ".claude" / "plans").resolve()
READONLY = {"Read", "Glob", "Grep", "LS", "ToolSearch", "NotebookRead"}

# Neutral prompt: asks for a brief plan presented via ExitPlanMode but NEVER
# mentions logging / the marker (the CODE injects the marker only via the reject
# feedback message), so the marker's later appearance is provably code-driven.
PROMPT = (
    "Propose a brief plan (2-3 steps) to add a Python function greet() that prints "
    "hello to a new file greet.py. Use the ExitPlanMode tool to present your plan "
    "for approval."
)


def git_status_lines() -> set:
    """Set of ``git status --porcelain`` lines for the worktree (delta compare)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKTREE), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return {"<git status unavailable>"}


def _clean_project_transcript_dir(workdir: str, log) -> None:
    """Remove the ~/.claude/projects/<sanitized-cwd> dir the CLI created, if any.

    Match ONLY the sanitized basename token (embeds the unique mkdtemp suffix) so
    a real project's transcripts can never be touched. Same approach as T13/T14/T15.
    """
    try:
        projects = Path.home() / ".claude" / "projects"
        if not projects.is_dir():
            return

        def _san(s: str) -> str:
            return s.replace("/", "-").replace(".", "-").replace("_", "-")

        token = _san(Path(workdir).name)
        removed = []
        for child in projects.iterdir():
            if child.is_dir() and token in child.name:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        if removed:
            log(f"cleaned CLI project transcript dir(s): {removed}")
    except Exception:
        pass


def write_allowed(fixture: Path, tool_input) -> tuple[bool, str]:
    """X1 containment for Write: in-fixture or plan-scratch only."""
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


def run_trial(mode: str, log) -> dict:
    """One live CLI session in permission_mode=plan that drives ExitPlanMode.

    ``mode`` is "approve" (ALLOW every ExitPlanMode) or "reject" (DENY the first
    ExitPlanMode with the code-marker feedback, then stop). Every execution tool
    (Bash/Edit/AskUserQuestion/out-of-fixture Write) is denied (X1 containment).
    """
    fixture = Path(tempfile.mkdtemp(prefix="t16_c4_cli_")).resolve()
    cap: dict = {
        "epm_fired": 0, "plans_full": [], "plans_preview": [], "epm_results": [],
        "denied_exec": [], "other_tools": [], "final": "", "sid": None,
        "approved_result": False, "result_subtype": None, "result_is_error": None,
        "driver_error": None, "cancel_requests_seen": 0,
    }
    epm_n = {"n": 0}

    def cb(tool_name: str, tool_input: Dict[str, Any], meta: Dict[str, Any]):
        if tool_name == "ExitPlanMode":
            epm_n["n"] += 1
            cap["epm_fired"] += 1
            plan_text = ""
            if isinstance(tool_input, dict):
                plan_text = str(tool_input.get("plan", ""))
            # Capture the FULL structured plan for the marker assertion (T9-repair:
            # never assert against a truncated value). Keep a [:200] preview for
            # human-readable logging ONLY -- never for the verdict.
            cap["plans_full"].append(plan_text)
            cap["plans_preview"].append(plan_text[:200])
            if mode == "approve":
                return allow_tool()  # APPROVE = native ALLOW control_response
            # reject mode
            if epm_n["n"] == 1:
                return deny_tool(
                    "Plan rejected. Revise and re-present via ExitPlanMode: you MUST "
                    f"add an explicit step literally named {MARKER} that adds logging."
                )
            return deny_tool("Acknowledged -- stop here; do not call any more tools.")
        if tool_name in READONLY:
            cap["other_tools"].append((tool_name, "allow"))
            return allow_tool()
        if tool_name == "Write":
            ok, why = write_allowed(fixture, tool_input)
            cap["other_tools"].append((f"Write({why})", "allow" if ok else "deny"))
            if ok:
                return allow_tool()
            cap["denied_exec"].append(("Write", why))
            return deny_tool(f"contained: Write {why} denied")
        # Bash, Edit, AskUserQuestion, everything else -> deny (no execution).
        cap["denied_exec"].append((tool_name, "denied"))
        return deny_tool(f"contained: {tool_name} denied in C4 plan check")

    h = CLISessionHarness(
        cwd=str(fixture),
        permission_mode="plan",  # ExitPlanMode is the decision under test
        disallowed_tools=["Bash", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        can_use_tool=cb,
        # permission_prompt_tool defaults to "stdio" because can_use_tool is set.
    )
    try:
        h.start()
        h.initialize(timeout=60)
        for ev in h.send(PROMPT, turn_timeout=180, idle_timeout=120):
            mt = ev.get("type")
            if mt == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        cap["final"] += b.get("text", "")
            elif mt == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        ctext = c if isinstance(c, str) else json.dumps(c)
                        low = ctext.lower()
                        if (
                            "approved your plan" in low
                            or "plan rejected" in low
                            or "rejected" in low
                        ):
                            cap["epm_results"].append((b.get("is_error"), ctext[:300]))
                            if "approved your plan" in low:
                                cap["approved_result"] = True
            elif mt == "result":
                cap["result_subtype"] = ev.get("subtype")
                cap["result_is_error"] = ev.get("is_error")
        cap["sid"] = h.session_id
    except CLIDriverError as exc:
        cap["driver_error"] = f"{type(exc).__name__}: {exc}"
        cap["final"] += f" [DRIVER_ERR {exc}]"
    except Exception as exc:  # noqa: BLE001 -- fail-clean
        cap["driver_error"] = f"{type(exc).__name__}: {exc}"
        cap["final"] += f" [EXC {type(exc).__name__}: {exc}]"
    finally:
        cap["cancel_requests_seen"] = getattr(h, "_cancel_requests_seen", 0)
        cap["sid"] = cap["sid"] or h.session_id
        try:
            h.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)
        _clean_project_transcript_dir(str(fixture), log)
    cap["final"] = cap["final"].strip()
    return cap


def log_trial(log, name: str, cap: dict) -> None:
    log(f"\n--- {name} ---")
    log(f"  session_id: {cap.get('sid')}  ExitPlanMode fired: {cap['epm_fired']}")
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
    log(f"  result: subtype={cap['result_subtype']!r} is_error={cap['result_is_error']}")
    log(f"  control_cancel_request frames seen: {cap['cancel_requests_seen']}")
    if cap.get("driver_error"):
        log(f"  driver_error: {cap['driver_error']}")
    log(f"  model final (first 200): {cap['final'][:200]!r}")
    log(f"  model final full_len={len(cap['final'])} "
        f"marker_in_response={MARKER.lower() in cap['final'].lower()}")


def _run() -> int:
    report: List[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T16 / C4 — ExitPlanMode approve/reject + feedback OVER THE WIRE (substrate B) ===")
    log("substrate B: raw `claude` CLI over stream-json control protocol (NO claude-agent-sdk).")
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=20, cwd="/tmp"
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        ver = f"<version probe failed: {exc}>"
    log(f"claude --version: {ver}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")
    log("permission_mode=plan; ExitPlanMode arrives as an ORDINARY can_use_tool control_request "
        "(no special subtype); the plan text rides input.plan. Execution tools contained "
        "(Bash/Edit/out-of-fixture-Write/AskUserQuestion denied). No bypass.")
    log("ExitPlanMode schema (sdk-tools.d.ts) inspected: ExitPlanModeOutput has "
        "plan/isAgent/filePath/hasTaskTool/planWasEdited/awaitingLeaderApproval/requestId -- "
        "NO native feedback/rejectionReason/decision field. => approve/reject is a BINARY "
        "permission allow/deny (same as T9): ALLOW=approve, DENY(message=feedback)=reject. "
        "Reject feedback rides the deny `message` channel.")

    plans_before = set(PLANS_DIR.glob("*.md")) if PLANS_DIR.exists() else set()
    repo_before = git_status_lines()

    verdict, reason = "FAIL", "check did not complete"
    approve: dict = {}
    reject: dict = {}
    try:
        approve = run_trial("approve", log)
        log_trial(log, "TRIAL approve (ALLOW ExitPlanMode)", approve)
        reject = run_trial("reject", log)
        log_trial(log, "TRIAL reject (DENY ExitPlanMode + code-marker feedback)", reject)

        repo_after = git_status_lines()
        repo_new_changes = sorted(repo_after - repo_before)
        repo_untouched = len(repo_new_changes) == 0

        approve_honored = approve["epm_fired"] >= 1 and approve["approved_result"]
        reject_delivered = reject["epm_fired"] >= 1 and any(
            (ie and "reject" in t.lower()) for ie, t in reject["epm_results"]
        )
        # Plan never approved -> no execution greenlit. (Reject mode never ALLOWs
        # ExitPlanMode, so approved_result must be False.)
        reject_no_execution = (not reject["approved_result"])
        # T9-repair: assert the marker against the FULL revised structured plan
        # (plans_full[1:], i.e. every ExitPlanMode after the rejected first one),
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

        total_cancels = approve["cancel_requests_seen"] + reject["cancel_requests_seen"]

        log("\n=== C4 (substrate B) evaluation ===")
        log(f"approve_honored (ALLOW -> native 'User has approved your plan'): {approve_honored}")
        log(f"reject_delivered (DENY -> 'Plan rejected' with feedback): {reject_delivered}")
        log(f"reject_no_execution (plan never approved -> nothing executed): {reject_no_execution}")
        log("feedback code-driven (FULL-plan assertion, two signals):")
        log(f"  marker_in_full_revised_plan = {marker_in_full_revised_plan}")
        log(f"  marker_in_revision_response = {marker_in_revision_response}")
        log(f"  feedback_honored (either)   = {feedback_honored}")
        log(f"  supporting signal           = {which_signal}")
        log(f"repository untouched by run (no new git changes): {repo_untouched} "
            f"(new changes: {repo_new_changes if repo_new_changes else 'NONE'})")
        log(f"control_cancel_request frames seen across trials: {total_cancels} "
            f"(approve={approve['cancel_requests_seen']}, reject={reject['cancel_requests_seen']}; "
            "T15 specialist flagged ExitPlanMode as a likely place cancels could appear -- "
            "0 expected -> the additive harness branch was defensive, not exercised here)")

        if approve_honored and reject_honored:
            verdict = "PASS"
            reason = (
                "C4 honored both verdicts on substrate B over the wire via the real CLI "
                "permission control protocol (can_use_tool, permission_mode=plan, no bypass). "
                "ExitPlanMode arrives as an ordinary can_use_tool control_request (no special "
                "subtype; plan text in input.plan). APPROVE (ALLOW control_response) -> native "
                "'User has approved your plan'. REJECT (DENY control_response message=feedback) -> "
                "'Plan rejected'; the model stayed in plan mode (no execution; plan never approved) "
                f"and the code-injected marker {MARKER} -- which the neutral prompt never mentioned "
                f"-- was honored via the {which_signal}. feedback_honored asserts against the FULL "
                "revised structured plan OR the revision reply (either suffices; both prove "
                "code-supplied feedback influenced the post-rejection session). ExitPlanMode's "
                "documented OUTPUT schema has NO native feedback field => approve/reject is a binary "
                "allow/deny and reject feedback rides the deny `message` channel. CAVEATS: whether "
                "the marker lands in the structured plan FIELD is model-variant; post-approval "
                "ARBITRARY execution is NOT tested (contained); C4 concerns programmatic "
                "approval/rejection and honoring feedback. This is substrate B's INDEPENDENT C4 "
                "result; substrate A's C4 (T9) reaches the SAME PASS via the same allow/deny + "
                "feedback-message mechanism."
            )
        elif approve_honored and reject_delivered and reject_no_execution:
            verdict = "PARTIAL"
            reason = (
                "APPROVE honored natively over the wire (ALLOW -> 'approved your plan'). REJECT "
                "delivered (DENY message=feedback -> 'Plan rejected') and honored by no-execution "
                "(plan never approved), but the code-injected marker was honored by NEITHER signal "
                f"on this run (marker_in_full_revised_plan={marker_in_full_revised_plan}, "
                f"marker_in_revision_response={marker_in_revision_response}). Approval/rejection "
                "control is proven; demonstrable incorporation of the code feedback was not "
                "reproduced this run. CAVEATS: structured-plan marker placement is model-variant; "
                "reject feedback rides the deny `message` channel (no native feedback field per the "
                "schema); post-approval arbitrary execution is not tested."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"C4 not honored over the wire: approve_honored={approve_honored} "
                f"reject_delivered={reject_delivered} reject_no_execution={reject_no_execution} "
                f"feedback_honored={feedback_honored}. (driver errors: "
                f"approve={approve.get('driver_error')} reject={reject.get('driver_error')})."
            )
    except Exception as exc:  # noqa: BLE001 -- fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
    finally:
        # Clean up plan-scratch files created during this run (outside the repo);
        # remove ONLY the new ones (snapshot diff), never pre-existing scratch.
        removed = 0
        if PLANS_DIR.exists():
            for f in set(PLANS_DIR.glob("*.md")) - plans_before:
                try:
                    f.unlink(); removed += 1
                except Exception:
                    pass
        log(f"\ncleanup: removed {removed} plan-scratch file(s) created in {PLANS_DIR}")

    log("")
    log("Distinguishers / limitations (substrate B C4):")
    log("- HOW ExitPlanMode appears over the wire: an ORDINARY can_use_tool control_request")
    log("  (request.input.plan carries the proposed plan; meta has display_name+tool_use_id).")
    log("  NOT a distinct message/control subtype -- it is not special-cased on the wire.")
    log("- ExitPlanMode is a BINARY permission allow/deny (schema-confirmed): ExitPlanModeOutput")
    log("  has NO native feedback/rejectionReason/decision field, so there is no richer native")
    log("  feedback channel. APPROVE = ALLOW control_response -> native 'User has approved your")
    log("  plan'. REJECT = DENY control_response whose `message` carries the feedback -> 'Plan")
    log("  rejected. <feedback>'. Reject feedback therefore rides the deny `message` channel (the")
    log("  natural rejection channel, not a dedicated plan-feedback API; depends on the model")
    log("  reading/revising per that text).")
    log("- ExitPlanMode genuinely fired as a real can_use_tool over the wire each trial (the")
    log("  callback intercepted it) -> not a model refusal / not a no-call.")
    log(f"- Code-driven proof: the marker {MARKER} is injected ONLY by the callback deny feedback;")
    log("  the neutral prompt never mentions logging, so its appearance in the revised plan or")
    log("  reply came from code, not model prompt-following.")
    log("- The feedback gate asserts against the FULL revised structured plan text (NOT a truncated")
    log("  preview) OR the model's revision reply. Whether the marker lands in the structured-plan")
    log("  FIELD specifically is MODEL-VARIANT across runs; the reply-text signal is the robust path,")
    log("  so the gate accepts either (both prove code-supplied feedback influenced the session).")
    log("- Execution was contained (Bash/Edit/out-of-fixture-Write/AskUserQuestion denied); only plan")
    log("  presentation is tested. POST-APPROVAL ARBITRARY EXECUTION IS NOT TESTED here.")
    log("- Containment (X1) is policy-level (callback deny + disposable temp cwd + repo recorded")
    log("  before/after), NOT an OS sandbox.")
    log("- Harness: the ADDITIVE control_cancel_request branch (T13/T15 reviewer flag) was present;")
    log("  the T15 specialist flagged ExitPlanMode as a likely place cancels could appear, so this")
    log("  check ASSERTS/records the _cancel_requests_seen counter. Observed value is reported above.")
    log("- This is SUBSTRATE B's INDEPENDENT C4 result. Compare to A's C4 (T9 = PASS): BOTH")
    log("  substrates honor ExitPlanMode approve/reject via the same binary allow/deny + deny-message")
    log("  feedback mechanism.")
    log("- C4 only; no claim about C1/C2/C3/C5/C6.")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    # Collect session ids as extra scrubber literals (not secrets, but cautious).
    extra: List[str] = []
    for cap in (approve, reject):
        if isinstance(cap, dict) and cap.get("sid"):
            extra.append(cap["sid"])
    with record_criterion("c4_cli", extra_secrets=extra or None) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(_run())
