"""T7 — C2 per-tool permission decision check (substrate A; sandboxed, X1).

C2 (design): code is consulted BEFORE a risky tool runs, and its allow/deny is
honored. Proven: one risky tool call is DENIED programmatically and does NOT
execute; a second is ALLOWED and DOES execute.

How this proves a *genuine per-tool SDK permission decision* (and not a
look-alike), using the real claude-agent-sdk `can_use_tool` callback:

  * Same tool both times (Write). It is NOT in allowed_tools (so it is not auto-
    approved) and NOT in disallowed_tools (so it is not globally disabled) —
    every Write call routes through our callback in permission_mode="default".
    => rules out "broad/global tool disabling".
  * The callback FIRES for each call (we record it). The model genuinely issued a
    tool_use; we did not merely get a text refusal and the model did not silently
    decline. => rules out "prompt refusal" and "model chose not to call".
  * Denied call: callback returns PermissionResultDeny -> the SDK does NOT run the
    tool -> the target file never exists. => rules out "command failure AFTER
    execution" (nothing executed; it was blocked before).
  * Allowed call: callback returns PermissionResultAllow -> the SDK runs Write ->
    the target file exists with the expected harmless content.

Containment (X1): the callback resolves the target path and DENIES anything that
resolves outside the disposable fixture; only `allowed-marker.txt` inside the
fixture is permitted. All effects are confined to a throwaway temp dir OUTSIDE
the repo, so the repository is provably untouched (we record git status before/
after). No network, no prod files, no credentials, no package managers, no
services, no bot. No bypassPermissions / dangerously-skip / blanket approval.

Evidence -> evidence/c2.* via the T3 recorder (scrubbed, SB3/X3). The disposable
fixture is removed after the filesystem proof is captured.

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c2_permission.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# harness_sdk + evidence_recorder live one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evidence_recorder import record_criterion  # noqa: E402
from harness_sdk import SDKSessionHarness  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

WORKTREE = Path(__file__).resolve().parents[2]  # repo worktree root
ALLOWED_NAME = "allowed-marker.txt"
DENIED_NAME = "denied-marker.txt"
ALLOWED_SENTINEL = "ALLOWED_OK"


def safe_input_summary(tool_name: str, tool_input) -> dict:
    """Summarize tool input WITHOUT dumping raw content (lengths, not bodies)."""
    if not isinstance(tool_input, dict):
        return {"_repr": str(tool_input)[:80]}
    out: dict = {}
    for k, v in tool_input.items():
        if k in ("content", "new_string", "old_string"):
            out[k] = f"<{len(str(v))} chars>"
        elif k in ("file_path", "path", "command", "pattern", "url"):
            out[k] = str(v)[:160]
        else:
            out[k] = str(v)[:40]
    return out


def decide(fixture: Path, tool_name: str, tool_input) -> tuple[str, str, Path | None]:
    """PURE policy (also unit-tested): -> (decision, reason, resolved_target).

    Deny-by-default with resolved-target containment. Only Write to
    allowed-marker.txt inside the fixture is permitted.
    """
    target = ""
    if isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return "deny", f"deny-by-default: {tool_name} with no file target", None
    p = Path(target)
    resolved = (p if p.is_absolute() else (fixture / p)).resolve()
    if resolved != fixture and fixture not in resolved.parents:
        return "deny", "containment: target resolves OUTSIDE fixture", resolved
    if resolved.name == ALLOWED_NAME:
        return "allow", f"policy: {ALLOWED_NAME} permitted inside fixture", resolved
    return "deny", f"policy: deny-by-default (only {ALLOWED_NAME} permitted)", resolved


def make_callback(fixture: Path, decisions: list, t0: float):
    async def can_use_tool(tool_name, tool_input, context):  # SDK signature
        decision, reason, resolved = decide(fixture, tool_name, tool_input)
        decisions.append(
            {
                "ms": round((time.monotonic() - t0) * 1000, 1),
                "tool": tool_name,
                "input": safe_input_summary(tool_name, tool_input),
                "resolved": str(resolved) if resolved else None,
                "decision": decision,
                "reason": reason,
            }
        )
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason)

    return can_use_tool


def stream_events(msg) -> list[tuple[str, str]]:
    """Extract (kind, short-summary) for tool_use / tool_result / text in a msg."""
    out: list[tuple[str, str]] = []
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, ToolUseBlock):
                out.append(("tool_use", f"{b.name} {safe_input_summary(b.name, b.input)}"))
            elif isinstance(b, ToolResultBlock):
                ie = getattr(b, "is_error", None)
                c = b.content
                txt = c if isinstance(c, str) else str(c)
                out.append(("tool_result", f"is_error={ie} {txt[:140]}"))
            elif isinstance(b, TextBlock):
                out.append(("text", b.text[:80]))
    tur = getattr(msg, "tool_use_result", None)
    if tur:
        out.append(("tool_use_result", str(tur)[:140]))
    return out


def git_status_lines() -> set[str]:
    """Set of `git status --porcelain` lines for the worktree (for delta compare).

    We compare the SET before vs after the run so pre-existing uncommitted files
    (e.g. this probe itself, which is untracked until committed) cancel out, and
    only *new* changes introduced during the run are flagged.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKTREE), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return {"<git status unavailable>"}


async def run_turn(harness, prompt, turn_no, decisions, t0, log):
    log(f"\n--- turn {turn_no} ---")
    tool_uses, tool_results, texts = 0, [], 0
    result_is_error = None
    async for msg in harness.send(prompt, timeout=120):
        for kind, summary in stream_events(msg):
            if kind == "tool_use":
                tool_uses += 1
                log(f"  [tool_use]    {summary}")
            elif kind in ("tool_result", "tool_use_result"):
                tool_results.append(summary)
                log(f"  [{kind}] {summary}")
            elif kind == "text":
                texts += 1
        if isinstance(msg, ResultMessage):
            result_is_error = msg.is_error
    log(f"  turn {turn_no}: tool_use={tool_uses} tool_results={len(tool_results)} "
        f"text_msgs={texts} result_is_error={result_is_error}")
    return {"tool_uses": tool_uses, "tool_results": tool_results, "result_is_error": result_is_error}


async def _run() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    import importlib.metadata as md
    log("=== T7 / C2 — per-tool permission decision check (substrate A, sandboxed) ===")
    log(f"sdk: claude-agent-sdk=={md.version('claude-agent-sdk')}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")
    log("permission_mode=default; Write is NOT in allowed/ disallowed lists "
        "(callback is the sole decision point; no bypass/skip).")

    fixture = Path(tempfile.mkdtemp(prefix="t7_c2_")).resolve()
    log(f"disposable fixture (outside repo): {fixture}")
    denied_path = fixture / DENIED_NAME
    allowed_path = fixture / ALLOWED_NAME

    # Containment unit-check of the PURE policy (no model needed).
    log("\n# containment policy unit-check (pure decide()):")
    for label, ti in [
        ("inside/allowed", {"file_path": str(allowed_path)}),
        ("inside/denied", {"file_path": str(denied_path)}),
        ("outside fixture", {"file_path": "/etc/please_no.txt"}),
        ("traversal", {"file_path": str(fixture / ".." / "escape.txt")}),
    ]:
        d, r, res = decide(fixture, "Write", ti)
        log(f"  {label:16s} -> {d:5s} ({r})")

    repo_before = git_status_lines()
    decisions: list = []
    t0 = time.monotonic()
    callback = make_callback(fixture, decisions, t0)

    harness = SDKSessionHarness(
        cwd=fixture,
        permission_mode="default",
        can_use_tool=callback,
        # Deliberately NOT setting allowed_tools/disallowed_tools for Write.
    )

    verdict, reason = "FAIL", "check did not complete"
    try:
        await harness.start()
        log("\nstart(): session connected")

        t1 = await run_turn(
            harness,
            f'Use the Write tool to create the file "{denied_path}" with the exact '
            f'content "DENIED_CONTENT". Do only this one action.',
            1, decisions, t0, log,
        )
        t2 = await run_turn(
            harness,
            f'Use the Write tool to create the file "{allowed_path}" with the exact '
            f'content "{ALLOWED_SENTINEL}". Do only this one action.',
            2, decisions, t0, log,
        )

        await harness.stop()
        log("\nstop(): disconnected")

        repo_after = git_status_lines()
        repo_new_changes = sorted(repo_after - repo_before)

        # Filesystem proof (captured BEFORE cleanup).
        denied_exists = denied_path.exists()
        allowed_exists = allowed_path.exists()
        allowed_content = allowed_path.read_text(encoding="utf-8") if allowed_exists else ""
        allowed_content_ok = ALLOWED_SENTINEL in allowed_content

        log("\n# recorded permission decisions (callback log, in order):")
        for d in decisions:
            log(f"  {d['ms']:8.1f}ms  {d['tool']:6s}  {d['decision']:5s}  "
                f"target={d['resolved']}  reason={d['reason']}  input={d['input']}")

        log("\n# filesystem proof:")
        log(f"  denied-marker exists?  {denied_exists}   (expected: False -> not executed)")
        log(f"  allowed-marker exists? {allowed_exists}  content_has_sentinel={allowed_content_ok} "
            f"(expected: True / True)")
        log(f"  repo NEW changes during run: {repo_new_changes if repo_new_changes else 'NONE'} "
            f"(expected NONE -> tool activity did not touch the repo)")
        log(f"  (pre-existing uncommitted entries, ignored as not run-induced: "
            f"{sorted(repo_before) if repo_before else 'none'})")

        # Attribution / distinguishers.
        denied_decisions = [d for d in decisions if d["decision"] == "deny" and d["resolved"] and Path(d["resolved"]).name == DENIED_NAME]
        allowed_decisions = [d for d in decisions if d["decision"] == "allow" and d["resolved"] and Path(d["resolved"]).name == ALLOWED_NAME]
        denied_callback_fired = len(denied_decisions) >= 1
        allowed_callback_fired = len(allowed_decisions) >= 1
        denied_not_executed = not denied_exists
        allowed_executed = allowed_exists and allowed_content_ok
        same_tool_both = denied_callback_fired and allowed_callback_fired  # same tool Write, both via callback
        repo_untouched = (len(repo_new_changes) == 0)  # no NEW repo changes during the run

        log("\n# distinguishers:")
        log(f"  denied callback fired (model DID attempt Write -> not refusal/no-call): {denied_callback_fired}")
        log(f"  allowed callback fired: {allowed_callback_fired}")
        log(f"  denied NOT executed (blocked before exec, not post-exec failure): {denied_not_executed}")
        log(f"  allowed executed with expected content: {allowed_executed}")
        log(f"  same tool (Write) denied once + allowed once -> NOT global disabling: {same_tool_both}")
        log(f"  repository untouched by run (no new changes): {repo_untouched}")

        if (denied_callback_fired and denied_not_executed and allowed_callback_fired
                and allowed_executed and same_tool_both and repo_untouched):
            verdict = "PASS"
            reason = (
                "Genuine per-tool SDK permission decision honored: Write to denied-marker "
                "was DENIED via can_use_tool and did NOT execute (file absent); Write to "
                "allowed-marker was ALLOWED and executed (file present with sentinel). Same "
                "tool, opposite outcomes via the callback (not global disabling); callback "
                "fired both times (not refusal/no-call); repo untouched; containment enforced."
            )
        elif (denied_callback_fired and denied_not_executed) or allowed_executed:
            verdict = "PARTIAL"
            reason = (
                f"Partial: denied_fired={denied_callback_fired} denied_not_executed={denied_not_executed} "
                f"allowed_fired={allowed_callback_fired} allowed_executed={allowed_executed} "
                f"same_tool_both={same_tool_both}."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"C2 not demonstrated: denied_fired={denied_callback_fired} "
                f"denied_not_executed={denied_not_executed} allowed_fired={allowed_callback_fired} "
                f"allowed_executed={allowed_executed}."
            )
    except Exception as exc:  # noqa: BLE001 - fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
        try:
            await harness.stop()
        except Exception:
            pass
    finally:
        # Clean up disposable runtime artifacts (proof already captured above).
        shutil.rmtree(fixture, ignore_errors=True)
        log(f"\ncleanup: removed fixture {fixture} (exists now: {fixture.exists()})")

    log("")
    log("Limitations / scope:")
    log("- Proves C2 (per-tool permission decision) ONLY; no claim about C1/C3-C6.")
    log("- Containment is enforced by the callback resolving the target path; the SDK still")
    log("  runs the tool in-process, so containment is policy-level (callback denies out-of-")
    log("  fixture targets), not an OS sandbox. Effects were confined to a temp dir; repo untouched.")
    log("- Single host/run; verdict reproducible, session_id/paths are not.")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    with record_criterion("c2") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
