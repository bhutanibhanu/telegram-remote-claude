"""T12 — C6 session resume by id ACROSS PROCESS RUNS (substrate A: claude-agent-sdk).

C6 (design 6): "A session is resumed BY ID ACROSS SEPARATE PROCESS RUNS with
history intact. Proven: run A starts a session and records its id; run B (fresh
process) resumes it and demonstrates retained context."

T5 left cross-process resume as the SEAM only (the harness `resume()` passes
`resume=session_id` to `ClaudeAgentOptions`). This check PROVES it genuinely by
running phase B in a BRAND-NEW python interpreter, not just a new harness object
in the same process.

=============================================================================
HOW THIS PROVES GENUINE CROSS-PROCESS RESUME (causal, not coincidental)
=============================================================================
The proof rests on a CODE-GENERATED, UNGUESSABLE codeword (`C6_<8 hex>`) that:
  * is planted into the session ONLY in phase A's prompt, and
  * NEVER appears in phase B's prompt (phase B asks "what codeword did I ask
    you to remember earlier?" with no hint).
So a correct answer in phase B can ONLY come from resumed session history --
not from the prompt, and (because it is random) not from the model guessing.

TWO NEGATIVE CONTROLS pin down the attribution:
  * control_same_cwd : fresh start() (NO resume) in the SAME cwd as phase A.
    If THIS knew the word, retention would be cwd-scoped leakage, not resume.
    Its blindness is what earns the "rules out shared-cwd leakage" claim.
  * control_diff_cwd : fresh start() (NO resume) in a SEPARATE fresh cwd.
    Belt-and-braces: a session that shares neither id nor cwd must be blank.

ONE CWD-COUPLING PROBE characterises the resume handle's scope:
  * resume_diff_cwd : resume phase A's id from a DIFFERENT cwd. On
    claude-agent-sdk 0.2.105 / CLI 2.1.185 this is EXPECTED to FAIL with
    "No conversation found with session ID" because the CLI stores transcripts
    at ~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl -- the id alone is
    NOT a global handle. The failure is recorded as POSITIVE evidence of
    cwd-coupling; if it unexpectedly succeeds and returns the word, that is
    reported honestly (resume would then NOT be cwd-scoped).

Phases, each a SEPARATE OS process (separate python interpreter):
  --phase a            --cwd <dir>           : fresh session; plant codeword;
                                               print C6_SID + C6_WORD; stop.
  --phase b            --cwd <dir> --sid <id>: FRESH process; RESUME the id; ask
                                               for the codeword (NOT in prompt);
                                               print C6_ANSWER + C6_SID2.
  --phase control_same --cwd <dir>           : fresh start() (NO resume), SAME
                                               cwd as A; ask the same question.
  --phase control_diff --cwd <dir>           : fresh start() (NO resume), a
                                               DIFFERENT cwd; ask the same Q.
  --phase resume_diff  --cwd <dir> --sid <id>: resume the id from a DIFFERENT
                                               cwd -- expected to FAIL.

The default (orchestrator) mode runs A, B, both controls and the cwd-coupling
probe as separate subprocesses and decides the verdict.

VERDICT (honest; PASS / PARTIAL / FAIL all valid -- do NOT manufacture a PASS):
  PASS    : phase B (separate process) resumed the SAME id AND returned the
            exact code-planted codeword (context retained), AND BOTH negative
            controls (same-cwd no-resume, diff-cwd no-resume) were blind.
  PARTIAL : resume connected but context retention is partial/unreliable, OR a
            negative control is inconclusive (e.g. it somehow knew the word), OR
            the id changed on resume -- records exactly what held.
  FAIL    : resume errored, or the resumed session did not retain the codeword.

Containment: cwd is a disposable temp dir; text-only (Bash/Write/Edit denied);
~/.claude/plans is snapshotted before/after and any new scratch is cleaned; the
~/.claude/projects/<sanitized-cwd>/ transcript dirs THIS run creates are removed
(conservatively, by exact SDK-derived name + unique temp token). No risky tools
needed. Host CLI auth only -- NO API key. The C6_<hex> codeword is a harmless
random token (NOT a secret); it is fine for it to appear in evidence.

Run (orchestrator, the normal way):
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c6_resume.py
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as md
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# harness_sdk + evidence_recorder live one level up (spikes/session-substrate/).
_SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SPIKE_ROOT))
from evidence_recorder import record_criterion  # noqa: E402
from harness_sdk import SDKSessionHarness, assistant_text  # noqa: E402

# The SDK's OWN cwd->project-dir-name sanitization (realpath + NFC + djb2-hashed
# truncation), so we can compute the EXACT ~/.claude/projects/<name>/ dir the CLI
# writes for a given cwd and clean up only what this run created. Imported from a
# private module -- best-effort; cleanup degrades to a unique-token match if the
# private API ever moves (see _projects_cleanup).
try:
    from claude_agent_sdk._internal.sessions import (  # noqa: E402
        _get_projects_dir as _sdk_projects_dir,
        project_key_for_directory as _sdk_project_key,
    )
except Exception:  # noqa: BLE001 - degrade gracefully if the private API moves
    _sdk_projects_dir = None  # type: ignore[assignment]
    _sdk_project_key = None  # type: ignore[assignment]

# Machine-parseable markers the orchestrator greps out of each subprocess stdout.
RE_SID = re.compile(r"^C6_SID=(.+)$", re.MULTILINE)
RE_WORD = re.compile(r"^C6_WORD=(.+)$", re.MULTILINE)
RE_SID2 = re.compile(r"^C6_SID2=(.+)$", re.MULTILINE)
RE_ANSWER = re.compile(r"^C6_ANSWER=(.*)$", re.MULTILINE)
RE_RESUME_FAILED = re.compile(r"^C6_RESUME_FAILED=(.+)$", re.MULTILINE)
RE_RESUME_ERR = re.compile(r"^C6_RESUME_ERR=(.*)$", re.MULTILINE)

# Text-only probe: deny everything risky. No tool is needed to remember a word.
_DENY_TOOLS = ["Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch"]

# Phase B / control ask the codeword question with NO hint of the word itself.
_RECALL_PROMPT = (
    "What exact codeword did I ask you to remember earlier? "
    "Reply with only the codeword, nothing else."
)


# ---------------------------------------------------------------------------
# Phase implementations (each runs in its OWN process).
# ---------------------------------------------------------------------------

async def _phase_a(cwd: str) -> int:
    """Start a fresh session, plant a code-generated codeword, print SID + WORD."""
    codeword = "C6_" + secrets.token_hex(4)  # e.g. C6_1a2b3c4d -- unguessable
    harness = SDKSessionHarness(
        cwd=cwd, permission_mode="default", disallowed_tools=_DENY_TOOLS
    )
    answer = ""
    try:
        await harness.start()
        async for msg in harness.send(
            f"Remember this codeword exactly: {codeword}. Reply with only the "
            f"single word: STORED",
            timeout=120,
        ):
            answer += assistant_text(msg)
        sid = harness.session_id
    finally:
        await harness.stop()

    # Machine-parseable lines for the orchestrator.
    print(f"C6_SID={sid}")
    print(f"C6_WORD={codeword}")
    print(f"C6_PLANT_REPLY={answer.strip()[:40]!r}", file=sys.stderr)
    return 0 if sid else 1


async def _phase_b(cwd: str, sid: str) -> int:
    """FRESH process: resume `sid`, ask for the codeword (NOT in the prompt)."""
    harness = SDKSessionHarness(
        cwd=cwd, permission_mode="default", disallowed_tools=_DENY_TOOLS
    )
    answer = ""
    seen_sid = None
    try:
        await harness.resume(sid)
        async for msg in harness.send(_RECALL_PROMPT, timeout=120):
            answer += assistant_text(msg)
        seen_sid = harness.session_id
    finally:
        await harness.stop()

    print(f"C6_SID2={seen_sid}")
    print(f"C6_ANSWER={answer.strip()}")
    return 0


async def _phase_control(cwd: str) -> int:
    """NEGATIVE CONTROL: fresh start() (NO resume); ask the same question.

    Used for BOTH controls (same-cwd and diff-cwd); the only difference is the
    cwd the orchestrator passes. A blind answer here means the model is not
    leaking the word via the prompt, the cwd, or guessing.
    """
    harness = SDKSessionHarness(
        cwd=cwd, permission_mode="default", disallowed_tools=_DENY_TOOLS
    )
    answer = ""
    seen_sid = None
    try:
        await harness.start()
        async for msg in harness.send(_RECALL_PROMPT, timeout=120):
            answer += assistant_text(msg)
        seen_sid = harness.session_id
    finally:
        await harness.stop()

    print(f"C6_SID2={seen_sid}")
    print(f"C6_ANSWER={answer.strip()}")
    return 0


async def _phase_resume_diff(cwd: str, sid: str) -> int:
    """CWD-COUPLING PROBE: resume `sid` from a DIFFERENT cwd than phase A.

    Expected to FAIL ("No conversation found with session ID") because resume is
    cwd/project-scoped. Catches the expected failure and records it as POSITIVE
    evidence of cwd-coupling -- it must NOT crash the orchestrator. If resume
    unexpectedly succeeds and returns text, that is reported honestly too (it
    would mean resume is NOT cwd-scoped on this build).
    """
    harness = SDKSessionHarness(
        cwd=cwd, permission_mode="default", disallowed_tools=_DENY_TOOLS
    )
    answer = ""
    seen_sid = None
    failed = False
    err = ""
    try:
        await harness.resume(sid)
        async for msg in harness.send(_RECALL_PROMPT, timeout=120):
            answer += assistant_text(msg)
        seen_sid = harness.session_id
    except Exception as exc:  # noqa: BLE001 - this failure is the EXPECTED result
        failed = True
        # Record the error class + a short, token-free snippet of the message.
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        try:
            await harness.stop()
        except Exception:  # noqa: BLE001
            pass

    print(f"C6_RESUME_FAILED={failed}")
    print(f"C6_RESUME_ERR={err}")
    print(f"C6_SID2={seen_sid}")
    print(f"C6_ANSWER={answer.strip()}")
    return 0


# ---------------------------------------------------------------------------
# Orchestrator: runs each phase as a SEPARATE subprocess and decides verdict.
# ---------------------------------------------------------------------------

def _plans_dir() -> Path:
    return Path.home() / ".claude" / "plans"


def _snapshot_plans() -> set[str]:
    d = _plans_dir()
    try:
        return {p.name for p in d.iterdir()}
    except FileNotFoundError:
        return set()


def _projects_dir() -> Path:
    """~/.claude/projects, via the SDK's resolver when available."""
    if _sdk_projects_dir is not None:
        try:
            return _sdk_projects_dir()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".claude" / "projects"


def _snapshot_projects() -> set[str]:
    try:
        return {p.name for p in _projects_dir().iterdir()}
    except FileNotFoundError:
        return set()


def _project_dir_name(cwd: str) -> str | None:
    """Exact ~/.claude/projects/<name> the CLI uses for `cwd`, via the SDK."""
    if _sdk_project_key is None:
        return None
    try:
        return _sdk_project_key(cwd)
    except Exception:  # noqa: BLE001
        return None


def _unique_token(cwd: str) -> str:
    """The mkdtemp suffix of `cwd`, sanitized (slashes/underscores -> dashes).

    mkdtemp("t12_c6_ab_") yields ".../t12_c6_ab_<rand>"; the SDK sanitizes that
    leaf to "t12-c6-ab-<rand>" inside the project dir name. We match on that
    unique token as a conservative fallback when the exact SDK name is absent.
    """
    leaf = Path(cwd).name  # e.g. t12_c6_ab_46mnozx3
    return leaf.replace("_", "-")


def _run_phase(phase: str, cwd: str, log, *, sid: str | None = None) -> str:
    """Run one phase in a FRESH python interpreter (separate OS process).

    Returns the subprocess stdout. Raises on non-zero exit / timeout so the
    orchestrator fails clean.
    """
    cmd = [sys.executable, str(Path(__file__).resolve()), "--phase", phase, "--cwd", cwd]
    if sid is not None:
        cmd += ["--sid", sid]
    # Child inherits env (host CLI auth); ANTHROPIC_API_KEY is unset and stays so.
    child_env = dict(os.environ)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=240, env=child_env
    )
    log(f"  [phase {phase}] pid-exit={proc.returncode} "
        f"(separate interpreter: {Path(sys.executable).name})")
    if proc.stderr.strip():
        # stderr carries only structural notes (plant reply preview), never tokens.
        for line in proc.stderr.strip().splitlines():
            log(f"  [phase {phase} stderr] {line}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"phase {phase} exited {proc.returncode}; stdout tail: "
            f"{proc.stdout.strip()[-200:]!r}"
        )
    return proc.stdout


def _first(rx: re.Pattern[str], text: str) -> str | None:
    m = rx.search(text)
    return m.group(1).strip() if m else None


def _projects_cleanup(
    cwds: list[str], before: set[str], log
) -> list[str]:
    """Remove ONLY ~/.claude/projects/<name> dirs THIS run created for `cwds`.

    EXTREMELY CONSERVATIVE. A project dir is removed only if it is NEW (absent
    from the pre-run snapshot) AND it matches one of this run's temp cwds by
    either: (a) the exact SDK-derived dir name for that cwd, or (b) containing
    that cwd's unique mkdtemp token. Pre-existing dirs and any dir not matching
    this run's temp cwds are NEVER touched.
    """
    pdir = _projects_dir()
    after = _snapshot_projects()
    new = after - before
    log(f"~/.claude/projects entries before/after run: {len(before)} -> {len(after)} "
        f"(new this run: {sorted(new)})")
    if not new:
        log("project-transcript dirs created by run: none")
        return []

    # Build the conservative match set for this run's cwds.
    exact_names = {n for n in (_project_dir_name(c) for c in cwds) if n}
    tokens = [_unique_token(c) for c in cwds]
    log(f"  cleanup match set -> exact SDK names: {sorted(exact_names)}")
    log(f"  cleanup match set -> unique cwd tokens: {tokens}")

    removed: list[str] = []
    for name in sorted(new):
        exact_hit = name in exact_names
        token_hit = any(tok in name for tok in tokens)
        if not (exact_hit or token_hit):
            log(f"  KEEP (new but not this run's): {name}")
            continue
        target = pdir / name
        try:
            shutil.rmtree(target)
            removed.append(name)
            log(f"  REMOVED project-transcript dir "
                f"(exact={exact_hit}, token={token_hit}): {name}")
        except Exception as exc:  # noqa: BLE001
            log(f"  (could not remove project dir {name}: {exc})")
    if not removed:
        log("project-transcript dirs removed: none (no conservative match)")
    return removed


async def _orchestrate() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    log("=== T12 / C6 — session resume by id ACROSS PROCESS RUNS (substrate A) ===")
    log(f"sdk: claude-agent-sdk=={md.version('claude-agent-sdk')}")
    try:
        cli_ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        cli_ver = f"<unavailable: {exc}>"
    log(f"claude CLI: {cli_ver}")
    log(f"orchestrator pid={os.getpid()} python={Path(sys.executable).name} "
        f"(phases run as SEPARATE child processes)")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} "
        f"(must be False -- host CLI auth only)")
    log(f"fork_session: NOT set (default False) -> resume continues SAME id, "
        f"does not fork to a new id")
    log(f"SDK project-key fn available for exact cleanup: "
        f"{_sdk_project_key is not None}")

    # Disposable temp cwds:
    #   cwd_ab  : phase A, phase B AND control_same all use this so the SDK can
    #             locate the session transcript by project/cwd on resume, and so
    #             control_same exercises the SAME project scope WITHOUT resuming.
    #   cwd_ctl : control_diff -- a separate fresh cwd that shares neither id nor
    #             project scope, so it must start blank.
    #   cwd_xdir: the cwd-coupling probe resumes phase A's id from HERE (a
    #             different cwd) and is expected to FAIL.
    cwd_ab = tempfile.mkdtemp(prefix="t12_c6_ab_")
    cwd_ctl = tempfile.mkdtemp(prefix="t12_c6_ctl_")
    cwd_xdir = tempfile.mkdtemp(prefix="t12_c6_xdir_")
    log(f"cwd (phase A + B + control_same, shared, temp): {cwd_ab}")
    log(f"cwd (control_diff, separate, temp): {cwd_ctl}")
    log(f"cwd (cwd-coupling probe, different, temp): {cwd_xdir}")

    plans_before = _snapshot_plans()
    projects_before = _snapshot_projects()
    log(f"~/.claude/plans snapshot before: {sorted(plans_before)}")
    log(f"~/.claude/projects entry count before: {len(projects_before)}")

    verdict, reason = "FAIL", "check did not complete"
    try:
        # -- Phase A: plant codeword in a fresh process ----------------------
        log("\n--- PHASE A (process 1): start fresh session, plant codeword ---")
        out_a = _run_phase("a", cwd_ab, log)
        sid_a = _first(RE_SID, out_a)
        word = _first(RE_WORD, out_a)
        log(f"  phase A session_id (recorded): {sid_a}")
        log(f"  phase A planted codeword: {word}  (random token; not a secret)")
        if not sid_a or not word:
            raise RuntimeError(
                f"phase A did not yield both SID and WORD (sid={sid_a!r}, word={word!r})"
            )

        # -- Phase B: resume that id in a SEPARATE fresh process -------------
        log("\n--- PHASE B (process 2, SEPARATE): resume by id, ask for codeword ---")
        log(f"  phase B prompt does NOT contain the codeword "
            f"(asks 'what codeword did I ask you to remember?')")
        out_b = _run_phase("b", cwd_ab, log, sid=sid_a)
        sid_b = _first(RE_SID2, out_b)
        answer_b = _first(RE_ANSWER, out_b) or ""
        log(f"  phase B resumed/observed session_id: {sid_b}")
        log(f"  phase B answer: {answer_b!r}")

        # -- Control 1: same cwd, NO resume (rules out shared-cwd leakage) ----
        log("\n--- NEGATIVE CONTROL (same cwd, NO resume): fresh start() in cwd_ab ---")
        log(f"  proves retention is NOT cwd-scoped leakage: same project scope as")
        log(f"  phase A/B but a brand-new session that never resumed.")
        out_cs = _run_phase("control_same", cwd_ab, log)
        sid_cs = _first(RE_SID2, out_cs)
        answer_cs = _first(RE_ANSWER, out_cs) or ""
        log(f"  control_same session_id: {sid_cs}")
        log(f"  control_same answer: {answer_cs!r}")

        # -- Control 2: different cwd, NO resume (belt-and-braces) -----------
        log("\n--- NEGATIVE CONTROL (different cwd, NO resume): fresh start() ---")
        out_cd = _run_phase("control_diff", cwd_ctl, log)
        sid_cd = _first(RE_SID2, out_cd)
        answer_cd = _first(RE_ANSWER, out_cd) or ""
        log(f"  control_diff session_id: {sid_cd}")
        log(f"  control_diff answer: {answer_cd!r}")

        # -- CWD-coupling probe: resume phase A's id from a DIFFERENT cwd -----
        log("\n--- CWD-COUPLING PROBE: resume phase A's id from a DIFFERENT cwd ---")
        log(f"  expected to FAIL ('No conversation found') if resume is cwd-scoped.")
        out_x = _run_phase("resume_diff", cwd_xdir, log, sid=sid_a)
        resume_diff_failed = (_first(RE_RESUME_FAILED, out_x) == "True")
        resume_diff_err = _first(RE_RESUME_ERR, out_x) or ""
        answer_x = _first(RE_ANSWER, out_x) or ""
        sid_x = _first(RE_SID2, out_x)
        log(f"  resume_diff_cwd FAILED (expected True): {resume_diff_failed}")
        log(f"  resume_diff_cwd error: {resume_diff_err!r}")
        log(f"  resume_diff_cwd answer (should be empty): {answer_x!r}")
        log(f"  resume_diff_cwd observed session_id: {sid_x}")

        # -- Evaluation ------------------------------------------------------
        # Compare case-insensitively; the codeword is C6_<hex> (no case ambiguity
        # in the hex, but be liberal in case the model upper/lower-cases C6_).
        wl = word.lower()
        retained = wl in answer_b.lower()
        # phase B used the SAME id (default fork_session=False keeps the id).
        same_id = bool(sid_b) and (sid_b == sid_a)
        forked = bool(sid_b) and bool(sid_a) and (sid_b != sid_a)
        control_same_knows = wl in answer_cs.lower()
        control_diff_knows = wl in answer_cd.lower()
        controls_blind = (not control_same_knows) and (not control_diff_knows)
        # cwd-coupling observation: did resume from a different cwd return the word?
        resume_diff_leaked_word = wl in answer_x.lower()

        log("\n=== C6 evaluation ===")
        log(f"phase B resumed SAME id (sid_b==sid_a): {same_id}  "
            f"(sid_a={sid_a}, sid_b={sid_b})")
        log(f"resume forked to a NEW id: {forked}  "
            f"(would indicate fork_session semantics; should be False)")
        log(f"codeword retained in phase B answer: {retained}  "
            f"(word={word})")
        log(f"control_same (same cwd, no resume) knew codeword: {control_same_knows}  "
            f"(MUST be False -> rules out shared-cwd leakage)")
        log(f"control_diff (diff cwd, no resume) knew codeword: {control_diff_knows}  "
            f"(MUST be False -> rules out guessing)")
        log(f"BOTH negative controls blind: {controls_blind}")
        log(f"OBSERVATION cwd-coupling: resume from a DIFFERENT cwd failed: "
            f"{resume_diff_failed} (returned-word={resume_diff_leaked_word})")
        if resume_diff_failed:
            log(f"  -> resume is CWD/PROJECT-SCOPED: the id alone is not a global "
                f"handle (err: {resume_diff_err!r}).")
        elif resume_diff_leaked_word:
            log(f"  -> SURPRISE: resume from a different cwd SUCCEEDED and returned "
                f"the word -> resume is NOT cwd-scoped on this build. Reported.")
        else:
            log(f"  -> resume from a different cwd neither failed nor returned the "
                f"word (inconclusive coupling signal); reported as-is.")

        if retained and controls_blind:
            if same_id:
                verdict = "PASS"
                reason = (
                    f"Cross-process resume PROVEN: phase A (process 1) planted "
                    f"code-generated codeword {word} in session {sid_a}; phase B "
                    f"(SEPARATE process, fresh interpreter) resumed BY ID "
                    f"(same id {sid_b}, fork_session unset) and returned the exact "
                    f"codeword though its prompt never contained it -> session "
                    f"history was retained across process runs. BOTH negative "
                    f"controls were blind: the same-cwd no-resume control did NOT "
                    f"know the codeword (rules out SHARED-CWD LEAKAGE -- retention "
                    f"comes from resuming the id, not from sharing the project "
                    f"directory), and the diff-cwd no-resume control also did not "
                    f"(rules out guessing). OBSERVED: resuming the same id from a "
                    f"DIFFERENT cwd failed ({resume_diff_err!r}) -> resume is "
                    f"cwd/project-scoped (the id is not a global handle); persist "
                    f"(session_id, cwd) together and resume from the original cwd."
                )
            else:
                # Context retained but the visible id changed (e.g. SDK reports a
                # forked id on resume even with fork_session unset). Still strong
                # evidence of cross-process resume, but the id-identity bullet is
                # not clean -> PARTIAL, recorded honestly.
                verdict = "PARTIAL"
                reason = (
                    f"Context RETAINED across processes (phase B returned the exact "
                    f"code-planted codeword {word}; BOTH controls blind), BUT the "
                    f"resumed session id differed from phase A's "
                    f"(sid_a={sid_a}, sid_b={sid_b}; forked={forked}). Resume "
                    f"carried history but did not present the identical id -- "
                    f"records the observed id behavior rather than overclaiming "
                    f"'same id'."
                )
        elif retained and not controls_blind:
            verdict = "PARTIAL"
            reason = (
                f"Phase B returned the codeword {word}, BUT a negative control ALSO "
                f"produced it (control_same_knows={control_same_knows} "
                f"answer={answer_cs!r}; control_diff_knows={control_diff_knows} "
                f"answer={answer_cd!r}) -> retention is not cleanly attributable to "
                f"resume (possible guessing / shared-cwd leakage). Inconclusive "
                f"control; recorded honestly."
            )
        else:
            verdict = "FAIL"
            reason = (
                f"Cross-process resume did NOT demonstrate retained context: "
                f"codeword_retained={retained} (phase B answer={answer_b!r}, "
                f"expected {word}), same_id={same_id}, "
                f"controls_blind={controls_blind} "
                f"(control_same={answer_cs!r}, control_diff={answer_cd!r})."
            )
    except Exception as exc:  # noqa: BLE001 - fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
    finally:
        # -- Containment cleanup --------------------------------------------
        all_cwds = [cwd_ab, cwd_ctl, cwd_xdir]
        for d in all_cwds:
            shutil.rmtree(d, ignore_errors=True)
        # Plans scratch.
        plans_after = _snapshot_plans()
        new_plans = sorted(plans_after - plans_before)
        log(f"\n~/.claude/plans snapshot after: {sorted(plans_after)}")
        if new_plans:
            log(f"plan-scratch created by run -> removing: {new_plans}")
            for name in new_plans:
                target = _plans_dir() / name
                try:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    log(f"  (could not remove plan-scratch {name}: {exc})")
            log(f"plan-scratch after cleanup: {sorted(_snapshot_plans())}")
        else:
            log("plan-scratch created by run: none")
        # Project transcript dirs (~/.claude/projects/<sanitized-cwd>/) this run made.
        removed_projects = _projects_cleanup(all_cwds, projects_before, log)
        log(f"~/.claude/projects entry count after cleanup: "
            f"{len(_snapshot_projects())} (removed {len(removed_projects)} this run)")
        log(f"temp cwds removed: {', '.join(all_cwds)}")

    log("")
    log("Limitations / scope:")
    log("- Proves C6 (cross-process resume with retained context) ONLY; no C1-C5 claim.")
    log("- The codeword is code-generated & absent from phase B's prompt, so a correct")
    log("  answer can only come from resumed history; BOTH negative controls (same-cwd")
    log("  no-resume, diff-cwd no-resume) being blind rule out guessing AND shared-cwd")
    log("  leakage -- retention is attributable to resuming the id.")
    log("- Distinguishes SDK resume mechanism from model cooperation: SDK loads history;")
    log("  the model merely reads it back. fork_session is left unset (default False).")
    log("- RESUME IS CWD/PROJECT-SCOPED: resuming the same session id from a DIFFERENT")
    log("  cwd fails ('No conversation found') -- the id alone is not a global handle.")
    log("  The CLI persists transcripts at ~/.claude/projects/<sanitized-cwd>/")
    log("  <session_id>.jsonl. OPERATIONAL RULE for P1: persist (session_id, cwd)")
    log("  together and resume only from the original project cwd.")
    log("- NOT TESTED HERE (deferred to P1 RB3): resume after a CRASH mid-turn (torn")
    log("  transcript / fail-clean), concurrent or double resume of the same id")
    log("  (substrate does not prevent double-attach -- the engine must), resume of aged")
    log("  sessions or across a CLI/SDK upgrade. C6 proves the clean-restart happy path")
    log("  only.")
    log("- HOUSEKEEPING (RB6): ~/.claude/projects/<sanitized-cwd>/ transcript dirs")
    log("  survive deletion of the working dir; deleting a project workspace does not")
    log("  reclaim its session transcripts. (This check cleans up only the dirs it")
    log("  creates, conservatively, by exact SDK-derived name + unique temp token.)")
    log("- Single host/run; session ids are not reproducible across runs (verdict is).")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    # The codeword (C6_<hex>) is a harmless random token, not a secret; recorded as-is.
    with record_criterion("c6") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=["a", "b", "control_same", "control_diff", "resume_diff"],
        default=None,
        help="run a single phase in THIS process (used by the orchestrator's "
             "subprocesses); omit for orchestrator mode.",
    )
    parser.add_argument("--cwd", default=None, help="working dir for the phase session")
    parser.add_argument("--sid", default=None, help="session id to resume (phase b)")
    args = parser.parse_args()

    if args.phase is None:
        return asyncio.run(_orchestrate())

    if not args.cwd:
        parser.error("--cwd is required when --phase is given")
    if args.phase == "a":
        return asyncio.run(_phase_a(args.cwd))
    if args.phase == "b":
        if not args.sid:
            parser.error("--sid is required for --phase b")
        return asyncio.run(_phase_b(args.cwd, args.sid))
    if args.phase in ("control_same", "control_diff"):
        return asyncio.run(_phase_control(args.cwd))
    if args.phase == "resume_diff":
        if not args.sid:
            parser.error("--sid is required for --phase resume_diff")
        return asyncio.run(_phase_resume_diff(args.cwd, args.sid))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
