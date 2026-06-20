"""T14 — C2 per-tool permission decision OVER THE WIRE (substrate B; sandboxed, X1).

This is the RIGOROUS C2 proof on SUBSTRATE B: the raw ``claude`` CLI driven over
the bidirectional ``stream-json`` control protocol via the T13 ``CLISessionHarness``
(stdlib subprocess + NDJSON; NO claude-agent-sdk import). It is held to the SAME
standard as substrate A's C2 (T7 / ``checks/c2_permission.py``) and mirrors its
attribution logic.

C2 (design): code is consulted BEFORE a risky tool runs, and its allow/deny is
honored over the wire. Proven here exactly as on substrate A:

  * Same tool both times (Write). Write is NOT in ``allowed_tools`` (so it is not
    auto-approved by the CLI) and NOT in ``disallowed_tools`` (so it is not globally
    disabled) -- every Write routes through our ``can_use_tool`` control-protocol
    callback in ``--permission-mode default`` with ``--permission-prompt-tool stdio``.
    => rules out "broad/global tool disabling".
  * The callback FIRES for each call (we record every ``can_use_tool``
    control_request the CLI sent us, via ``harness.permission_requests``). The model
    genuinely issued a tool_use; we did not merely get a text refusal and the model
    did not silently decline. => rules out "prompt refusal" and "model chose not to
    call".
  * Denied call: the callback returns ``deny_tool(...)`` -> the driver sends a
    ``{behavior:"deny"}`` control_response -> the CLI does NOT run Write -> the
    target file never exists (and the tool_result rides ``is_error=True``).
    => rules out "command failure AFTER execution" (nothing executed; blocked first).
  * Allowed call: the callback returns ``allow_tool()`` -> the driver sends a
    ``{behavior:"allow", updatedInput:<input>}`` control_response -> the CLI runs
    Write -> the target file exists with the expected harmless sentinel content.

GENUINE PER-REQUEST GATE (not global tool config): the SAME tool (Write) receives
OPPOSITE per-request decisions -- the FIRST denied-sentinel Write is DENIED, the
SECOND allowed-sentinel Write is ALLOWED -- driven purely by the resolved target
path our callback inspects. Identical tool, opposite outcomes => the decision is
per-request, made by our code over the wire, not a static ``--allowedTools`` config.

Containment (X1): the callback resolves the target path against the CLI's cwd (the
fixture) and DENIES anything that resolves outside the disposable fixture or that is
not the single allowed in-fixture sentinel; traversal (``../``) and absolute
out-of-fixture targets are denied. This pure policy is unit-checked below with no
model in the loop. All effects are confined to a throwaway temp dir OUTSIDE the repo;
we record ``git status`` before/after and assert only the allowed in-fixture sentinel
changed (the repo is provably untouched). No network, no prod files, no credentials,
no package managers, no services, no bot. ``--permission-mode default`` (NO bypass /
NO ``--dangerously-skip-permissions``). NOTE: like T7, containment is POLICY-LEVEL
(the callback resolves/denies targets), NOT OS/kernel sandboxing -- the CLI still
runs allowed tools in-process against the real filesystem.

Evidence -> evidence/c2_cli.* via the T3 recorder (scrubbed, SB3/X3). The disposable
fixture and any ``~/.claude/projects/<sanitized-cwd>`` transcript dir the CLI creates
are removed after the filesystem proof is captured.

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c2_permission_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
ALLOWED_NAME = "allowed-marker.txt"
DENIED_NAME = "denied-marker.txt"
ALLOWED_SENTINEL = "ALLOWED_OK"
DENIED_CONTENT = "DENIED_CONTENT"


def safe_input_summary(tool_name: str, tool_input: Any) -> dict:
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


def decide(
    fixture: Path, tool_name: str, tool_input: Any
) -> Tuple[str, str, Optional[Path]]:
    """PURE policy (also unit-checked below): -> (decision, reason, resolved_target).

    Deny-by-default with resolved-target containment. Only a Write to
    allowed-marker.txt INSIDE the fixture is permitted; everything else (the
    denied sentinel, anything outside the fixture, traversal) is denied. Relative
    paths are resolved against the fixture (the CLI's cwd), NOT the driver's cwd.
    Identical containment policy to substrate A's T7 decide().
    """
    target = ""
    if isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return "deny", f"deny-by-default: {tool_name} with no file target", None
    p = Path(str(target))
    resolved = (p if p.is_absolute() else (fixture / p)).resolve()
    if resolved != fixture and fixture not in resolved.parents:
        return "deny", "containment: target resolves OUTSIDE fixture", resolved
    if resolved.name == ALLOWED_NAME:
        return "allow", f"policy: {ALLOWED_NAME} permitted inside fixture", resolved
    return "deny", f"policy: deny-by-default (only {ALLOWED_NAME} permitted)", resolved


def make_callback(fixture: Path, decisions: list, t0: float):
    """Build the synchronous can_use_tool callback for CLISessionHarness.

    Signature is (tool_name, tool_input, meta) -> Decision dict (the substrate-B
    wire contract), unlike substrate A's async PermissionResult* return.
    """

    def can_use_tool(tool_name: str, tool_input: Dict[str, Any], meta: Dict[str, Any]):
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
            # allow_tool() defaults updatedInput to the original input in the
            # harness (the CLI control schema requires it; omitting -> ZodError).
            return allow_tool()
        return deny_tool(reason)

    return can_use_tool


def summarize_block(block: Dict[str, Any]) -> Tuple[str, str]:
    """-> (kind, short-summary) for an assistant/user content block (NDJSON dict)."""
    btype = block.get("type")
    if btype == "tool_use":
        return (
            "tool_use",
            f"{block.get('name')} {safe_input_summary(block.get('name', ''), block.get('input') or {})}",
        )
    if btype == "tool_result":
        c = block.get("content")
        txt = c if isinstance(c, str) else json.dumps(c)
        return ("tool_result", f"is_error={block.get('is_error')} {txt[:140]}")
    if btype == "text":
        return ("text", str(block.get("text", ""))[:80])
    if btype == "thinking":
        return ("thinking", f"<thinking len={len(str(block.get('thinking', '')))}>")
    return (str(btype), "")


def git_status_lines() -> set[str]:
    """Set of ``git status --porcelain`` lines for the worktree (for delta compare).

    We compare the SET before vs after the run so pre-existing uncommitted files
    (this probe + its evidence, untracked until committed) cancel out, and only
    *new* changes introduced during the run are flagged.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKTREE), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return {"<git status unavailable>"}


def run_turn(harness: CLISessionHarness, prompt: str, turn_no: int, log) -> dict:
    """Drive one operator turn; parse the NDJSON event stream; summarize it."""
    log(f"\n--- turn {turn_no} ---")
    tool_uses = 0
    tool_results: List[str] = []
    texts = 0
    result_subtype: Optional[str] = None
    result_is_error: Optional[bool] = None
    try:
        for ev in harness.send(prompt, turn_timeout=180, idle_timeout=120):
            mtype = ev.get("type")
            if mtype == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    kind, summary = summarize_block(b)
                    if kind == "tool_use":
                        tool_uses += 1
                        log(f"  [tool_use]    {summary}")
                    elif kind == "text":
                        texts += 1
            elif mtype == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        kind, summary = summarize_block(b)
                        tool_results.append(summary)
                        log(f"  [tool_result] {summary}")
            elif mtype == "result":
                result_subtype = ev.get("subtype")
                result_is_error = ev.get("is_error")
    except CLIDriverError as exc:
        log(f"  turn {turn_no}: driver error (fail-clean): {exc}")
    log(
        f"  turn {turn_no}: tool_use={tool_uses} tool_results={len(tool_results)} "
        f"text_msgs={texts} result_subtype={result_subtype!r} result_is_error={result_is_error}"
    )
    return {
        "tool_uses": tool_uses,
        "tool_results": tool_results,
        "result_subtype": result_subtype,
        "result_is_error": result_is_error,
    }


def _clean_project_transcript_dir(workdir: str, log) -> None:
    """Remove the ~/.claude/projects/<sanitized-cwd> dir the CLI created, if any.

    The CLI persists transcripts under a sanitized form of the cwd (separators,
    dots AND underscores collapse to '-'). We match ONLY the sanitized basename
    TOKEN (which embeds the unique mkdtemp suffix) so we can never touch a real
    project's transcripts. Same approach as T13's harness cleanup.
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


def _run() -> int:
    report: List[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T14 / C2 — per-tool permission decision OVER THE WIRE (substrate B, sandboxed) ===")
    log("substrate B: raw `claude` CLI over stream-json control protocol (NO claude-agent-sdk).")
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=20, cwd="/tmp"
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        ver = f"<version probe failed: {exc}>"
    log(f"claude --version: {ver}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")
    log(
        "permission_mode=default; --permission-prompt-tool stdio (set automatically by the "
        "harness when a can_use_tool callback is provided); NO bypass / NO --dangerously-skip. "
        "Write is NOT in allowed/disallowed lists -> the control-protocol callback is the sole "
        "decision point for Write."
    )

    fixture = Path(tempfile.mkdtemp(prefix="t14_c2_cli_")).resolve()
    log(f"disposable fixture (outside repo): {fixture}")
    denied_path = fixture / DENIED_NAME
    allowed_path = fixture / ALLOWED_NAME

    # --- Containment unit-check of the PURE policy (no model needed) ---
    log("\n# containment policy unit-check (pure decide()):")
    unit_cases = [
        ("inside/allowed", {"file_path": str(allowed_path)}, "allow"),
        ("inside/denied", {"file_path": str(denied_path)}, "deny"),
        ("outside fixture", {"file_path": "/etc/please_no.txt"}, "deny"),
        ("traversal abs", {"file_path": str(fixture / ".." / "escape.txt")}, "deny"),
        ("traversal rel", {"file_path": "../escape.txt"}, "deny"),
        ("no target", {}, "deny"),
    ]
    unit_ok = True
    for label, ti, expected in unit_cases:
        d, r, res = decide(fixture, "Write", ti)
        ok = d == expected
        unit_ok = unit_ok and ok
        log(f"  {label:16s} -> {d:5s} (expected {expected:5s} {'OK' if ok else 'MISMATCH!'}) [{r}]")
    log(f"  containment unit-check all-pass: {unit_ok}")

    repo_before = git_status_lines()
    decisions: List[dict] = []
    t0 = time.monotonic()
    callback = make_callback(fixture, decisions, t0)

    harness = CLISessionHarness(
        cwd=fixture,
        permission_mode="default",
        # Read-only orientation tools auto-approved so the model can act without
        # extra prompts; the RISKY tool (Write) is deliberately NOT in either list
        # -> it routes through the callback. Bash/Edit/etc disallowed for safety.
        allowed_tools=["Read", "LS", "Glob", "Grep"],
        disallowed_tools=["Bash", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        can_use_tool=callback,
        # permission_prompt_tool defaults to "stdio" because can_use_tool is set.
    )
    log(f"spawn command: {' '.join(harness._build_command())}")

    verdict, reason = "FAIL", "check did not complete"
    t1: dict = {}
    t2: dict = {}
    sid: Optional[str] = None
    try:
        harness.start()
        log("\nstart(): subprocess spawned; reader thread running")
        init_resp = harness.initialize(timeout=60)
        log(
            "initialize(): control handshake done; reply keys="
            f"{sorted(init_resp.keys()) if isinstance(init_resp, dict) else type(init_resp)}"
        )

        # TRIAL 1 — DENY: induce a Write of the denied sentinel; callback denies it.
        t1 = run_turn(
            harness,
            f'Use the Write tool to create the file "{denied_path}" with the exact '
            f'content "{DENIED_CONTENT}". Do only this one action.',
            1,
            log,
        )
        # TRIAL 2 — ALLOW: induce a Write of the allowed sentinel; callback allows it.
        t2 = run_turn(
            harness,
            f'Use the Write tool to create the file "{allowed_path}" with the exact '
            f'content "{ALLOWED_SENTINEL}". Do only this one action.',
            2,
            log,
        )

        sid = harness.session_id
        harness.stop()
        log("\nstop(): stdin closed, subprocess terminated, threads joined")
        if harness.stderr_text:
            log(f"stderr tail: {harness.stderr_text[-300:]}")

        repo_after = git_status_lines()
        repo_new_changes = sorted(repo_after - repo_before)

        # --- Filesystem proof (captured BEFORE cleanup) ---
        denied_exists = denied_path.exists()
        allowed_exists = allowed_path.exists()
        allowed_content = allowed_path.read_text(encoding="utf-8") if allowed_exists else ""
        allowed_content_ok = ALLOWED_SENTINEL in allowed_content

        log("\n# recorded permission decisions (callback log, in order):")
        for d in decisions:
            log(
                f"  {d['ms']:8.1f}ms  {d['tool']:6s}  {d['decision']:5s}  "
                f"target={d['resolved']}  reason={d['reason']}  input={d['input']}"
            )

        # The CLI's own can_use_tool control_requests (the over-the-wire proof the
        # callback actually fired on the wire, independent of our decisions log).
        wire_requests = [
            {
                "tool_name": (m.get("request") or {}).get("tool_name"),
                "file_path": str(((m.get("request") or {}).get("input") or {}).get("file_path", "")),
            }
            for m in harness.permission_requests
        ]
        log("\n# can_use_tool control_requests received over the wire (CLI -> driver):")
        for w in wire_requests:
            log(f"  wire can_use_tool: tool={w['tool_name']!r} file_path={w['file_path']!r}")

        log("\n# filesystem proof:")
        log(f"  denied-marker exists?  {denied_exists}   (expected: False -> not executed)")
        log(
            f"  allowed-marker exists? {allowed_exists}  content_has_sentinel={allowed_content_ok} "
            f"(expected: True / True)"
        )
        log(
            f"  repo NEW changes during run: {repo_new_changes if repo_new_changes else 'NONE'} "
            f"(expected NONE -> tool activity did not touch the repo)"
        )
        log(
            "  (pre-existing uncommitted entries, ignored as not run-induced: "
            f"{sorted(repo_before) if repo_before else 'none'})"
        )

        # --- Attribution / distinguishers (mirror T7) ---
        denied_decisions = [
            d
            for d in decisions
            if d["decision"] == "deny" and d["resolved"] and Path(d["resolved"]).name == DENIED_NAME
        ]
        allowed_decisions = [
            d
            for d in decisions
            if d["decision"] == "allow" and d["resolved"] and Path(d["resolved"]).name == ALLOWED_NAME
        ]
        # Over-the-wire confirmation: the CLI actually SENT a can_use_tool for each.
        denied_wire = any(Path(w["file_path"]).name == DENIED_NAME for w in wire_requests if w["file_path"])
        allowed_wire = any(Path(w["file_path"]).name == ALLOWED_NAME for w in wire_requests if w["file_path"])

        denied_callback_fired = len(denied_decisions) >= 1 and denied_wire
        allowed_callback_fired = len(allowed_decisions) >= 1 and allowed_wire
        denied_not_executed = not denied_exists
        allowed_executed = allowed_exists and allowed_content_ok
        same_tool_both = denied_callback_fired and allowed_callback_fired  # same tool Write, opposite decisions over the wire
        repo_untouched = len(repo_new_changes) == 0
        containment_ok = unit_ok

        log("\n# distinguishers:")
        log(f"  denied callback fired over the wire (model DID attempt Write -> not refusal/no-call): {denied_callback_fired}")
        log(f"  allowed callback fired over the wire: {allowed_callback_fired}")
        log(f"  denied NOT executed (blocked before exec, not post-exec failure): {denied_not_executed}")
        log(f"  allowed executed with expected content: {allowed_executed}")
        log(f"  SAME tool (Write) denied once + allowed once over the wire -> per-request gate, NOT global config: {same_tool_both}")
        log(f"  repository untouched by run (no new changes): {repo_untouched}")
        log(f"  containment policy unit-check passed (deny outside-fixture + traversal): {containment_ok}")

        if (
            denied_callback_fired
            and denied_not_executed
            and allowed_callback_fired
            and allowed_executed
            and same_tool_both
            and repo_untouched
            and containment_ok
        ):
            verdict = "PASS"
            reason = (
                "Genuine per-tool permission decision honored OVER THE WIRE (substrate B, raw CLI "
                "stream-json control protocol): a can_use_tool control_request for Write to "
                "denied-marker arrived over the wire, our deny control_response was honored and the "
                "tool did NOT execute (file absent); a can_use_tool for Write to allowed-marker was "
                "answered allow and executed (file present with sentinel). SAME tool (Write), opposite "
                "per-request decisions over the wire (not global --allowedTools config); the callback "
                "fired over the wire both times (not refusal/no-call); denied not executed (not post-exec "
                "failure); repo untouched; resolved-target containment enforced (policy-level)."
            )
        elif (denied_callback_fired and denied_not_executed) or allowed_executed:
            verdict = "PARTIAL"
            reason = (
                f"Partial: denied_fired={denied_callback_fired} denied_not_executed={denied_not_executed} "
                f"allowed_fired={allowed_callback_fired} allowed_executed={allowed_executed} "
                f"same_tool_both={same_tool_both} repo_untouched={repo_untouched} containment_ok={containment_ok}."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"C2-over-the-wire not demonstrated: denied_fired={denied_callback_fired} "
                f"denied_not_executed={denied_not_executed} allowed_fired={allowed_callback_fired} "
                f"allowed_executed={allowed_executed}."
            )
    except Exception as exc:  # noqa: BLE001 - fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
        try:
            harness.stop()
        except Exception:
            pass
    finally:
        # Clean up disposable runtime artifacts (proof already captured above):
        # the fixture AND any ~/.claude/projects/<sanitized-cwd> dir the CLI made.
        shutil.rmtree(fixture, ignore_errors=True)
        _clean_project_transcript_dir(str(fixture), log)
        log(f"\ncleanup: removed fixture {fixture} (exists now: {fixture.exists()})")

    log("")
    log("Limitations / scope:")
    log("- Proves C2 (per-tool permission decision) on SUBSTRATE B ONLY; no claim about C1/C3-C6.")
    log("- Mechanism = stream-json control protocol activated by the UNDOCUMENTED")
    log("  `--permission-prompt-tool stdio` spawn flag (T13 finding); B couples directly to this")
    log("  host CLI flag -> pin/monitor the CLI version, treat a missing/changed flag as fail-clean.")
    log("- Containment is enforced by the callback resolving the target path; the CLI still runs the")
    log("  tool in-process, so containment is POLICY-LEVEL (callback denies out-of-fixture targets),")
    log("  NOT an OS/kernel sandbox. Effects were confined to a temp dir; repo untouched.")
    log("- Single host/run; verdict reproducible, session_id/paths are not.")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)

    # session_id is not a secret, but pass it to the scrubber out of caution.
    extra = [sid] if sid else None
    with record_criterion("c2_cli", extra_secrets=extra) as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(_run())
