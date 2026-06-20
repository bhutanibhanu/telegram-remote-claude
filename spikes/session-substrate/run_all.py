#!/usr/bin/env python3
"""run_all.py -- aggregate the C1-C6 x {A,B} feasibility matrix (T17).

WHAT THIS IS
============
The single runnable entry point that builds the C1-C6 x {substrate A, substrate
B} PASS/FAIL/PARTIAL/N-A matrix from the per-criterion evidence the earlier tasks
(T6-T16) already recorded, PRINTS a readable table, and PERSISTS it to
``evidence/matrix.json`` (structured) + ``evidence/matrix.md`` (human table).

This is DETERMINISTIC AGGREGATION of already-reviewed evidence, not a live probe.
The canonical verdicts live in the per-criterion ``evidence/*.json`` files (each
written by the T3 evidence recorder: ``{criterion, verdict, observed_reason,
timestamp, transcript_path, recorder}``). This script READS those, applies the
design S1 B-contingency rule, and renders the matrix. It does NOT recompute,
re-judge, or auto-flip any verdict -- it surfaces exactly what the evidence says,
including any PARTIAL or model-variance footnote, rather than hiding it.

Re-running on the same evidence yields the same matrix (stable format): the only
non-deterministic field is the generation timestamp, which is segregated into a
``generated_at`` header field so the matrix CELLS are byte-stable across runs.

SUBSTRATES (design S1)
======================
  A = claude-agent-sdk (claude-agent-sdk==0.2.105) -- the in-process Agent SDK.
  B = raw `claude` CLI driven over stream-json (no SDK import) -- the fallback.

B-CONTINGENCY RULE (design S1), applied + documented in the output
==================================================================
  * C2 / C3 / C4 -> B is tested UNCONDITIONALLY (C3/C4 are make-or-break; C2 is
    the safety backbone). The B cell carries the verdict from the *_cli.json.
  * C1 / C5 / C6 -> these are non-make-or-break. The rule is: B is exercised
    only where A is FAIL/PARTIAL. Substrate A is PASS on C1/C5/C6, so per S1 B
    is intentionally NOT run -> the B cell is N-A WITH AN EXPLICIT REASON.
    The rule is ENCODED and ASSERTED below: if any of C1/C5/C6 had an A verdict
    of FAIL/PARTIAL, the matrix would be flagged as REQUIRING a B run that is
    missing (a contingency violation), instead of silently showing N-A.

ON-DISK OUTPUT FORMAT (stable, documented)
==========================================
  evidence/matrix.json  -- structured. Top-level keys:
      generated_at   : ISO-8601 UTC (the ONLY per-run-varying field)
      recorder       : "run_all_matrix/1" (format version tag)
      substrates     : {A: <desc>, B: <desc>}
      sdk_version / cli_versions / gate1 / asymmetry_note / b_contingency_rule
      criteria       : ordered list, each:
          { id, make_or_break, A:{...cell}, B:{...cell} }
        where each cell is:
          { verdict, evidence, reason, footnote }
            verdict  : "PASS"/"FAIL"/"PARTIAL"/"N-A"/"MISSING"
            evidence : basename of the source evidence JSON, or null for N-A
            reason   : short reason / the source observed_reason (truncated)
            footnote : variability / contingency note, or null

  evidence/matrix.md    -- human-readable table mirroring the JSON.

Both writes are routed through ``scrub()`` (the T2 secret chokepoint, X3) before
hitting disk -- the source observed_reason strings already came from scrubbed
evidence, but the matrix re-scrubs defensively so no write path bypasses X3.

ROBUSTNESS (fail-clean)
=======================
If an expected evidence JSON is missing or unreadable, the corresponding cell
shows "MISSING" with the reason, instead of crashing. The matrix is always
produced.

USAGE
=====
  spikes/session-substrate/.venv/bin/python run_all.py
        DEFAULT: aggregate the existing evidence -> print + persist the matrix.
        This is the reproducible artifact (no live model calls, no variability).

  spikes/session-substrate/.venv/bin/python run_all.py --rerun
        OPTIONAL, NOT the default. Re-executes each criterion check as a
        subprocess to REGENERATE the per-criterion evidence/*.json BEFORE
        aggregating. This makes live model calls -> it is SLOW and reintroduces
        model/substrate variability (and overwrites committed evidence), which
        is exactly why aggregation is the default. Do NOT run --rerun to produce
        the committed matrix; use it only to deliberately refresh evidence.

Pure standard library only. No third-party deps. No API key (host CLI auth).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Spike root on sys.path so we can import the sibling scrub chokepoint (X3).
_SPIKE_ROOT = Path(__file__).resolve().parent
if str(_SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SPIKE_ROOT))
from scrub import scrub  # noqa: E402

EVIDENCE_DIR = _SPIKE_ROOT / "evidence"
MATRIX_TAG = "run_all_matrix/1"

# Truncate long observed_reason strings for the compact cell. The FULL reason
# lives in the per-criterion evidence JSON the cell points at; we never hide it.
_REASON_MAX = 200

# Verdicts we treat as "A failed/partial -> S1 requires a B run".
_A_NEEDS_B = ("FAIL", "PARTIAL", "MISSING")

# ---------------------------------------------------------------------------
# Substrate / header constants (observed, from the recorded evidence + T13).
# ---------------------------------------------------------------------------
SUBSTRATE_A_DESC = "claude-agent-sdk (in-process Agent SDK)"
SUBSTRATE_B_DESC = "raw `claude` CLI over stream-json (no SDK import)"
SDK_VERSION = "claude-agent-sdk==0.2.105"
# Preflight recorded CLI 2.1.183; the later C2-C6/CLI checks ran on 2.1.185.
# Both are surfaced honestly rather than collapsed to one.
CLI_VERSIONS = "claude CLI 2.1.183 (preflight) / 2.1.185 (C-checks, observed)"
SUBSTRATE_NEUTRAL_NOTE = (
    "C2/C3/C4 are substrate-NEUTRAL: each PASSes on BOTH A and B via the same "
    "permission/answer/plan mechanism, so they do not by themselves decide A vs B."
)
GATE1_NOTE = (
    "GATE-1 GREEN: both make-or-break criteria (C3, C4) PASS on BOTH substrates "
    "(A and B). The design's constrained-mode / re-plan risk is retired."
)
ASYMMETRY_NOTE = (
    "A-vs-B asymmetry: B couples directly to the UNDOCUMENTED "
    "`--permission-prompt-tool stdio` stdio flag plus hand-rolled NDJSON/threads/"
    "timeouts (the `updatedInput`-on-allow workaround), whereas A uses the SDK's "
    "in-process callback. Per design S1, B coverage is limited to the "
    "make-or-break/backbone C2/C3/C4."
)
B_CONTINGENCY_RULE = (
    "design S1: A is exercised on all of C1-C6; B is exercised on C2/C3/C4 "
    "unconditionally and on the remaining criteria (C1/C5/C6) ONLY where A is "
    "FAIL/PARTIAL. Where A PASSes a non-make-or-break criterion, B is N-A."
)

# Reason used for an intentionally-skipped B cell (S1 contingency).
_B_NA_REASON = (
    "substrate A PASS on a non-make-or-break criterion; per design S1, B is "
    "exercised only where A is FAIL/PARTIAL -- B beyond C2/C3/C4 intentionally "
    "not run"
)

# ---------------------------------------------------------------------------
# Criterion layout. For each criterion: its A evidence file, its B evidence file
# (or None if B is contingent/skipped), whether it's make-or-break, and the
# check module to re-run under --rerun (A check; B re-run handled separately).
# ---------------------------------------------------------------------------
# id -> dict
_CRITERIA = [
    {
        "id": "C1",
        "title": "bidirectional streaming",
        "make_or_break": False,
        "a_evidence": "c1.json",
        "b_evidence": None,            # contingent (A PASS -> N-A)
        "a_check": "checks/c1_streaming.py",
        "b_check": None,
    },
    {
        "id": "C2",
        "title": "per-tool permission decision",
        "make_or_break": False,        # safety backbone; B tested unconditionally
        "a_evidence": "c2.json",
        "b_evidence": "c2_cli.json",
        "a_check": "checks/c2_permission.py",
        "b_check": "checks/c2_permission_cli.py",
    },
    {
        "id": "C3",
        "title": "AskUserQuestion answered programmatically",
        "make_or_break": True,
        "a_evidence": "c3.json",
        "b_evidence": "c3_cli.json",
        "a_check": "checks/c3_ask.py",
        "b_check": "checks/c3_ask_cli.py",
    },
    {
        "id": "C4",
        "title": "ExitPlanMode approve/reject + feedback",
        "make_or_break": True,
        "a_evidence": "c4.json",
        "b_evidence": "c4_cli.json",
        "a_check": "checks/c4_plan.py",
        "b_check": "checks/c4_plan_cli.py",
    },
    {
        "id": "C5",
        "title": "skill invocation through the same channels",
        "make_or_break": False,
        "a_evidence": "c5.json",
        "b_evidence": None,            # contingent (A PASS -> N-A)
        "a_check": "checks/c5_skill.py",
        "b_check": None,
    },
    {
        "id": "C6",
        "title": "session resume by id across process runs",
        "make_or_break": False,
        "a_evidence": "c6.json",
        "b_evidence": None,            # contingent (A PASS -> N-A)
        "a_check": "checks/c6_resume.py",
        "b_check": None,
    },
]

# Markers in an observed_reason that indicate model/substrate variability we must
# surface in the cell footnote (per the T17 "preserve variability honestly" rule).
_VARIABILITY_HINTS = ("model-variant", "model-variance", "variability", "model guess")


def _short(text: str, limit: int = _REASON_MAX) -> str:
    """Collapse whitespace and truncate a reason for the compact cell."""
    one_line = " ".join(str(text).split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "…"  # ellipsis


def _load_evidence(name: str) -> dict:
    """Read one evidence JSON; fail-clean to a MISSING marker dict on any error."""
    path = EVIDENCE_DIR / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"_missing": True, "_why": f"evidence file not found: {name}"}
    except Exception as exc:  # noqa: BLE001 -- fail-clean, never crash the matrix
        return {"_missing": True, "_why": f"could not read {name}: {type(exc).__name__}: {exc}"}
    if "verdict" not in data:
        return {"_missing": True, "_why": f"{name} has no 'verdict' field"}
    return data


def _variability_footnote(reason: str) -> Optional[str]:
    """Surface a model-variability note if the source reason mentions one."""
    low = reason.lower()
    for hint in _VARIABILITY_HINTS:
        if hint in low:
            return "source evidence notes model/substrate variability (preserved, not hidden)"
    return None


def _cell_from_evidence(name: str) -> dict:
    """Build a matrix cell from an evidence JSON basename."""
    data = _load_evidence(name)
    if data.get("_missing"):
        return {
            "verdict": "MISSING",
            "evidence": name,
            "reason": data.get("_why", "evidence missing"),
            "footnote": "MISSING evidence -- cell could not be populated (fail-clean)",
        }
    verdict = str(data.get("verdict", "")).strip().upper()
    reason = str(data.get("observed_reason", ""))
    footnote = _variability_footnote(reason)
    if verdict == "PARTIAL":
        # Never hide a PARTIAL: stamp it explicitly in the footnote too.
        note = "PARTIAL verdict preserved verbatim from source evidence"
        footnote = note if not footnote else f"{note}; {footnote}"
    return {
        "verdict": verdict,
        "evidence": name,
        "reason": _short(reason),
        "footnote": footnote,
    }


def _na_cell(reason: str) -> dict:
    return {"verdict": "N-A", "evidence": None, "reason": reason, "footnote": None}


def build_matrix() -> tuple[dict, list[str]]:
    """Build the structured matrix from the recorded evidence.

    Returns (matrix_dict, contingency_violations). The violations list is empty
    when the S1 B-contingency rule holds; any entry means an A cell is
    FAIL/PARTIAL/MISSING on a criterion whose B was skipped -> S1 requires B.
    """
    violations: list[str] = []
    criteria_out: list[dict] = []

    for crit in _CRITERIA:
        a_cell = _cell_from_evidence(crit["a_evidence"])

        if crit["b_evidence"] is not None:
            # B tested unconditionally (C2/C3/C4).
            b_cell = _cell_from_evidence(crit["b_evidence"])
        else:
            # B is contingent. Encode + ASSERT the S1 rule: if A did NOT pass,
            # the rule REQUIRES a B run that we don't have -> flag a violation
            # rather than silently showing a benign N-A.
            if a_cell["verdict"] in _A_NEEDS_B:
                violations.append(
                    f"{crit['id']}: substrate A is {a_cell['verdict']} (non-make-or-break) "
                    f"-> design S1 REQUIRES the B check to be exercised, but no B "
                    f"evidence is present"
                )
                b_cell = {
                    "verdict": "MISSING",
                    "evidence": None,
                    "reason": (
                        f"S1 CONTINGENCY VIOLATION: A is {a_cell['verdict']}; B "
                        f"should have been exercised but was not"
                    ),
                    "footnote": "contingency rule violated -- B run required by S1 is absent",
                }
            else:
                b_cell = _na_cell(_B_NA_REASON)

        criteria_out.append(
            {
                "id": crit["id"],
                "title": crit["title"],
                "make_or_break": crit["make_or_break"],
                "A": a_cell,
                "B": b_cell,
            }
        )

    matrix = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recorder": MATRIX_TAG,
        "substrates": {"A": SUBSTRATE_A_DESC, "B": SUBSTRATE_B_DESC},
        "sdk_version": SDK_VERSION,
        "cli_versions": CLI_VERSIONS,
        "gate1": GATE1_NOTE,
        "substrate_neutral_note": SUBSTRATE_NEUTRAL_NOTE,
        "asymmetry_note": ASYMMETRY_NOTE,
        "b_contingency_rule": B_CONTINGENCY_RULE,
        "criteria": criteria_out,
        "contingency_violations": violations,
    }
    return matrix, violations


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _footnote_index(matrix: dict) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Collect distinct cell footnotes into a numbered list for table markers.

    Returns (ordered_footnotes, {(crit_id, side): footnote_number}).
    """
    notes: list[str] = []
    mapping: dict[tuple[str, str], int] = {}
    for crit in matrix["criteria"]:
        for side in ("A", "B"):
            fn = crit[side].get("footnote")
            if not fn:
                continue
            if fn not in notes:
                notes.append(fn)
            mapping[(crit["id"], side)] = notes.index(fn) + 1
    return notes, mapping


def render_text(matrix: dict) -> str:
    """Render the human-readable matrix as plain text (for stdout)."""
    notes, fnmap = _footnote_index(matrix)
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("SESSION-SUBSTRATE FEASIBILITY -- C1-C6 x {A,B} VERDICT MATRIX (T17)")
    lines.append("=" * 78)
    lines.append(f"SDK : {matrix['sdk_version']}")
    lines.append(f"CLI : {matrix['cli_versions']}")
    lines.append(f"A   : {matrix['substrates']['A']}")
    lines.append(f"B   : {matrix['substrates']['B']}")
    lines.append("")
    lines.append(f"GATE-1: {matrix['gate1']}")
    lines.append("")
    lines.append("[substrate-neutral] " + matrix["substrate_neutral_note"])
    lines.append("[asymmetry] " + matrix["asymmetry_note"])
    lines.append("")

    # Table.
    header = f"{'Crit':<5} {'Make/Break':<11} {'A':<10} {'B':<10}  Criterion"
    lines.append(header)
    lines.append("-" * len(header))
    for crit in matrix["criteria"]:
        a_mark = f"[{fnmap[(crit['id'], 'A')]}]" if (crit["id"], "A") in fnmap else ""
        b_mark = f"[{fnmap[(crit['id'], 'B')]}]" if (crit["id"], "B") in fnmap else ""
        a_disp = f"{crit['A']['verdict']}{a_mark}"
        b_disp = f"{crit['B']['verdict']}{b_mark}"
        mob = "yes" if crit["make_or_break"] else "no"
        lines.append(
            f"{crit['id']:<5} {mob:<11} {a_disp:<10} {b_disp:<10}  {crit['title']}"
        )
    lines.append("-" * len(header))
    lines.append("")

    if notes:
        lines.append("Footnotes:")
        for i, fn in enumerate(notes, start=1):
            lines.append(f"  [{i}] {fn}")
        lines.append("")

    lines.append("B-contingency (design S1): " + matrix["b_contingency_rule"])
    lines.append("")
    lines.append("Per-cell reasons (evidence source -> short reason):")
    for crit in matrix["criteria"]:
        for side in ("A", "B"):
            cell = crit[side]
            src = cell["evidence"] if cell["evidence"] else "(no evidence file)"
            lines.append(f"  {crit['id']}({side}) = {cell['verdict']:<8} [{src}]")
            lines.append(f"        {cell['reason']}")
    lines.append("")

    if matrix["contingency_violations"]:
        lines.append("!!! S1 B-CONTINGENCY VIOLATIONS (B run required but absent):")
        for v in matrix["contingency_violations"]:
            lines.append(f"  - {v}")
    else:
        lines.append(
            "S1 B-contingency check: OK -- A PASSes every contingent criterion "
            "(C1/C5/C6), so B N-A on those is correct (no B run required)."
        )
    lines.append("=" * 78)
    return "\n".join(lines) + "\n"


def render_markdown(matrix: dict) -> str:
    """Render the matrix as a Markdown document (for evidence/matrix.md)."""
    notes, fnmap = _footnote_index(matrix)
    out: list[str] = []
    out.append("# Session-substrate feasibility -- C1-C6 x {A,B} verdict matrix (T17)")
    out.append("")
    out.append(f"- **SDK:** {matrix['sdk_version']}")
    out.append(f"- **CLI:** {matrix['cli_versions']}")
    out.append(f"- **Substrate A:** {matrix['substrates']['A']}")
    out.append(f"- **Substrate B:** {matrix['substrates']['B']}")
    out.append(f"- **Generated at:** {matrix['generated_at']}")
    out.append(f"- **Format:** `{matrix['recorder']}`")
    out.append("")
    out.append(f"> **GATE-1:** {matrix['gate1']}")
    out.append("")
    out.append(f"- _Substrate-neutral:_ {matrix['substrate_neutral_note']}")
    out.append(f"- _Asymmetry:_ {matrix['asymmetry_note']}")
    out.append("")
    out.append("## Matrix")
    out.append("")
    out.append("| Criterion | Make-or-break | A | B | Description |")
    out.append("|-----------|---------------|---|---|-------------|")
    for crit in matrix["criteria"]:
        a_mark = f" [^{fnmap[(crit['id'], 'A')]}]" if (crit["id"], "A") in fnmap else ""
        b_mark = f" [^{fnmap[(crit['id'], 'B')]}]" if (crit["id"], "B") in fnmap else ""
        mob = "yes" if crit["make_or_break"] else "no"
        out.append(
            f"| {crit['id']} | {mob} | "
            f"`{crit['A']['verdict']}`{a_mark} | `{crit['B']['verdict']}`{b_mark} | "
            f"{crit['title']} |"
        )
    out.append("")
    if notes:
        out.append("### Footnotes")
        out.append("")
        for i, fn in enumerate(notes, start=1):
            out.append(f"[^{i}]: {fn}")
        out.append("")
    out.append("## B-contingency rule (design S1)")
    out.append("")
    out.append(matrix["b_contingency_rule"])
    out.append("")
    if matrix["contingency_violations"]:
        out.append("> **S1 B-CONTINGENCY VIOLATIONS (B run required but absent):**")
        out.append("")
        for v in matrix["contingency_violations"]:
            out.append(f"> - {v}")
    else:
        out.append(
            "> **S1 B-contingency check: OK** -- substrate A PASSes every "
            "contingent criterion (C1/C5/C6), so showing B as N-A on those is "
            "correct (no B run required)."
        )
    out.append("")
    out.append("## Per-cell evidence")
    out.append("")
    out.append("| Cell | Verdict | Evidence | Reason |")
    out.append("|------|---------|----------|--------|")
    for crit in matrix["criteria"]:
        for side in ("A", "B"):
            cell = crit[side]
            src = f"`{cell['evidence']}`" if cell["evidence"] else "_(no evidence file)_"
            # Escape pipes so the markdown table stays well-formed.
            reason = cell["reason"].replace("|", "\\|")
            out.append(
                f"| {crit['id']}({side}) | `{cell['verdict']}` | {src} | {reason} |"
            )
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Persistence (routed through scrub -- X3)
# ---------------------------------------------------------------------------
def persist(matrix: dict) -> tuple[Path, Path]:
    """Write evidence/matrix.json + evidence/matrix.md, both scrubbed (X3)."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    json_text = json.dumps(matrix, indent=2, ensure_ascii=False) + "\n"
    md_text = render_markdown(matrix)

    # X3: every write passes through the T2 secret chokepoint before disk.
    json_path = EVIDENCE_DIR / "matrix.json"
    md_path = EVIDENCE_DIR / "matrix.md"
    json_path.write_text(scrub(json_text), encoding="utf-8")
    md_path.write_text(scrub(md_text), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# --rerun (optional, never the default; not run for the committed matrix)
# ---------------------------------------------------------------------------
def rerun_checks() -> None:
    """Re-execute each criterion check as a subprocess to regenerate evidence.

    SLOW + reintroduces model variability + OVERWRITES committed evidence -- this
    is why it is gated behind an explicit flag and is NEVER the default. The
    aggregation default is what produces the committed matrix.
    """
    py = sys.executable
    for crit in _CRITERIA:
        for side, rel in (("A", crit["a_check"]), ("B", crit["b_check"])):
            if not rel:
                continue
            check_path = _SPIKE_ROOT / rel
            print(f"[--rerun] {crit['id']}({side}) -> {rel}", flush=True)
            # Run from the spike root so the check's relative imports resolve.
            subprocess.run([py, str(check_path)], cwd=str(_SPIKE_ROOT), check=False)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate the C1-C6 x {A,B} feasibility matrix (T17).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Re-execute each criterion check (SLOW, live model calls, "
        "OVERWRITES evidence, reintroduces variability) before aggregating. "
        "NOT the default; do not use for the committed matrix.",
    )
    args = parser.parse_args(argv)

    if args.rerun:
        print(
            "[--rerun] regenerating per-criterion evidence via live checks "
            "(this is slow and overwrites evidence) ...",
            flush=True,
        )
        rerun_checks()

    matrix, violations = build_matrix()
    text = render_text(matrix)
    print(text)

    json_path, md_path = persist(matrix)
    print(f"persisted: {json_path}")
    print(f"persisted: {md_path}")

    # A contingency violation is a real harness signal (S1 not satisfied), so it
    # gets a non-zero exit -- but we STILL print + persist the matrix first
    # (fail-clean: the artifact is always produced).
    if violations:
        print(
            f"\nWARNING: {len(violations)} S1 B-contingency violation(s) "
            f"-- see the matrix.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
