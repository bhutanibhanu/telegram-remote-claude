"""T4 preflight — record Python + `claude` CLI versions and empirically probe
whether the Claude Agent SDK package exists and installs.

This is the spike's FIRST evidence artifact. A prior doc-only research pass
wrongly concluded the Agent SDK does not exist; T4 settles that empirically by
actually resolving/installing the package and recording the exact name+version
(or its documented absence).

Run with the spike venv interpreter (isolated, git-ignored deps, X2):

    spikes/session-substrate/.venv/bin/python spikes/session-substrate/preflight.py

Output: evidence/preflight.json + evidence/preflight.transcript.txt, written via
the T3 evidence recorder so EVERY byte is routed through the T2 scrubber (SB3/X3).

Verdict (all three are valid outcomes — this is an empirical probe, NOT a gate to
be forced to PASS):
  PASS    - Python + claude CLI versions captured AND the Agent SDK package
            exists, installs into the spike venv, and imports with a version.
  PARTIAL - some facts captured but the SDK probe is incomplete (e.g. installs
            but exposes no version, import fails, or CLI version unreadable).
  FAIL    - the Agent SDK package cannot be found or installed at all (substrate
            A viability for later checks is unconfirmed).

Scope limit: T4 only probes existence/version + import. Whether the SDK can
actually answer permissions / AskUserQuestion / ExitPlanMode programmatically is
C1-C6 work (T5+); no such claim is made here.

No API key is used — the spike relies on the host's existing CLI auth. Any pip
install targets ONLY this interpreter's venv (X2); production requirements*.txt
are never touched.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# The T3 recorder (which imports the T2 scrubber) lives beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_recorder import record_criterion  # noqa: E402

# Candidate Agent SDK distributions, primary first. ``claude-agent-sdk`` is the
# current PyPI name (the package was formerly ``claude-code-sdk``); we record
# whichever resolves so the evidence is robust to the rename.
SDK_CANDIDATES = [
    ("claude-agent-sdk", "claude_agent_sdk"),
    ("claude-code-sdk", "claude_code_sdk"),
]

PIP_TIMEOUT_S = 300


def run(cmd: list[str]) -> tuple[int, str]:
    """Run a command; return (returncode, combined stdout+stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - record any failure as evidence
        return -1, f"{type(exc).__name__}: {exc}"


def dist_version(dist_name: str) -> str | None:
    """Installed version of a distribution, or None if not installed."""
    try:
        return importlib_metadata.version(dist_name)
    except Exception:
        return None


def try_import(module_name: str) -> tuple[bool, str | None, list[str], str | None]:
    """Import a module; return (ok, version, public_symbols, error)."""
    try:
        mod = importlib.import_module(module_name)
        version = getattr(mod, "__version__", None)
        symbols = sorted(n for n in dir(mod) if not n.startswith("_"))
        return True, version, symbols, None
    except Exception as exc:  # noqa: BLE001 - import failure is valid evidence
        return False, None, [], f"{type(exc).__name__}: {exc}"


def main() -> int:
    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(text)

    log("=== T4 PREFLIGHT — session-substrate feasibility spike ===")
    log("Purpose: record Python + claude CLI versions; probe Agent SDK "
        "existence/version (first evidence artifact).")
    log("")

    # 1) Python / platform -----------------------------------------------------
    log("## Python / platform")
    log(f"python_version: {platform.python_version()}")
    log(f"sys.version: {sys.version.splitlines()[0]}")
    log(f"sys.executable: {sys.executable}")
    log(f"platform: {platform.platform()}")
    log("")

    # 2) claude CLI version ----------------------------------------------------
    log("## claude CLI")
    claude_path = shutil.which("claude")
    log(f"claude_on_PATH: {claude_path or 'NOT FOUND'}")
    cli_ok = False
    cli_version: str | None = None
    if claude_path:
        rc, out = run(["claude", "--version"])
        log(f"$ claude --version   (rc={rc})")
        log(out.strip())
        if rc == 0 and out.strip():
            cli_ok = True
            cli_version = out.strip().splitlines()[0]
    log("")

    # 3) Agent SDK probe (the load-bearing unknown) ----------------------------
    log("## Agent SDK probe (existence + install + import)")
    sdk_found = False
    sdk_dist: str | None = None
    sdk_version: str | None = None
    sdk_import_name: str | None = None
    sdk_symbols: list[str] = []
    import_ok = False

    # 3a) already present in the venv?
    for dist, modname in SDK_CANDIDATES:
        version = dist_version(dist)
        if version:
            log(f"already_installed: {dist}=={version}")
            sdk_found, sdk_dist, sdk_version, sdk_import_name = (
                True, dist, version, modname,
            )
            break
        log(f"not_yet_installed: {dist}")

    # 3b) attempt to install the primary candidate if none present
    if not sdk_found:
        dist, modname = SDK_CANDIDATES[0]
        log(f"$ {Path(sys.executable).name} -m pip install {dist}   "
            f"(into spike venv: {sys.executable})")
        rc, out = run([sys.executable, "-m", "pip", "install", dist])
        log(f"(rc={rc})")
        log(out.strip()[-2000:])  # tail of pip output
        if rc == 0:
            version = dist_version(dist)
            if version:
                sdk_found, sdk_dist, sdk_version, sdk_import_name = (
                    True, dist, version, modname,
                )
                log(f"installed: {dist}=={version}")
            else:
                log(f"install reported success but {dist} metadata not found")
        else:
            log(f"install_failed: {dist}")

    # 3c) import probe
    if sdk_found and sdk_import_name:
        import_ok, imported_version, sdk_symbols, import_err = try_import(
            sdk_import_name
        )
        log(f"import {sdk_import_name}: {'OK' if import_ok else 'FAILED'}")
        if imported_version:
            log(f"{sdk_import_name}.__version__: {imported_version}")
            sdk_version = sdk_version or imported_version
        if sdk_symbols:
            shown = ", ".join(sdk_symbols[:40])
            more = "" if len(sdk_symbols) <= 40 else f" (+{len(sdk_symbols) - 40} more)"
            log(f"top_level_symbols ({len(sdk_symbols)}): {shown}{more}")
        if not import_ok:
            log(f"import_error: {import_err}")
    log("")

    # 4) anthropic package for contrast ---------------------------------------
    log("## anthropic package (context — the one-shot Messages SDK, NOT the agent SDK)")
    anthropic_version = dist_version("anthropic")
    log(f"anthropic_installed: {anthropic_version or 'no'}")
    log("")

    # 5) verdict ---------------------------------------------------------------
    versions_ok = cli_ok  # Python version is always available in-process
    if versions_ok and sdk_found and import_ok and sdk_version:
        verdict = "PASS"
        reason = (
            f"Python {platform.python_version()} + claude CLI {cli_version} "
            f"captured; Agent SDK {sdk_dist}=={sdk_version} exists, installs into "
            f"the spike venv, and imports."
        )
    elif sdk_found or cli_ok:
        verdict = "PARTIAL"
        reason = (
            f"Partial: claude CLI={cli_version}; Agent SDK found={sdk_found} "
            f"(dist={sdk_dist}, version={sdk_version}, import_ok={import_ok}). "
            f"Not all preflight facts captured for a full PASS."
        )
    else:
        verdict = "FAIL"
        reason = (
            "Could not read the claude CLI version and could not find or install "
            "the Agent SDK package; substrate A viability is unconfirmed."
        )

    # 6) assumptions & limitations --------------------------------------------
    log("## Assumptions & limitations")
    log("- T4 probes existence/version/import ONLY; it does NOT test C1-C6 "
        "interactive capabilities (permissions / AskUserQuestion / ExitPlanMode). "
        "Those are T5+ and no claim about them is made here.")
    log(f"- 'installs' was verified on THIS host only (macOS, Python "
        f"{platform.python_version()}); other OS/Python combos are untested.")
    log("- No API key used; the spike relies on the host's existing claude CLI auth.")
    log("- The install probe needs PyPI network access; an offline host yields "
        "FAIL/PARTIAL, which is a valid empirical outcome.")
    log("- Exact pinned versions for reproduction are recorded in "
        "spikes/session-substrate/requirements.lock (regenerated after this run).")
    log("")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    report = "\n".join(lines)
    print(report)

    # Persist via the T3 recorder -> evidence/preflight.{json,transcript.txt}.
    # Every byte is scrubbed (SB3/X3) inside the recorder before it touches disk.
    with record_criterion("preflight") as rec:
        rec.add_transcript(report + "\n")
        rec.set_verdict(verdict, reason)

    # The probe ran and recorded a verdict; PASS/PARTIAL/FAIL are all valid, so
    # exit 0. A non-zero exit is reserved for the harness failing to run at all.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
