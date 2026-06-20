"""T11 — C5 skill invocation through the same channels (substrate A; sandboxed, X1).

C5 (design): a custom slash-command skill is invoked in-session and its
interactive prompts flow through the SAME channels proven in C2-C4. Proven here:
the throwaway `spike-c5-probe` skill (T10) is invoked in a live session and driven
to completion, with its per-tool permission request answered over the C2 code path
and its AskUserQuestion answered over the C3 code path -- the very same can_use_tool
callback used by checks/c2_permission.py and checks/c3_ask.py.

How the skill is loaded (claude-agent-sdk==0.2.105 mechanism, see the SDK's
`_apply_skills_defaults`): a *project* skill is discovered from
`<cwd>/.claude/skills/<name>/SKILL.md`; setting `skills=["spike-c5-probe"]`
injects the `Skill(spike-c5-probe)` tool and (because it is added to
allowed_tools) auto-approves THAT tool. We pass `setting_sources=["project"]`
explicitly so only the disposable temp cwd's `.claude/` is consulted (never a
repo-root or user `.claude/`). The committed fixture
`test_skill/spike-c5-probe/SKILL.md` is copied into `<tempcwd>/.claude/skills/`
per trial; the temp cwd is rmtree'd in `finally`.

What the skill emits (T10 fixture), in order:
  1. a Write of `c5_skill_sentinel.txt` (contents `C5_SKILL_RAN`)   -> C2 channel
  2. an AskUserQuestion (single-select, options Alpha / Bravo)      -> C3 channel
  3. a final line `C5_DONE:<chosen option>`                         -> completion

The can_use_tool callback (the SAME levers as C2/C3 -- allow / deny+message):
  * Skill / Skill(spike-c5-probe): ALLOW (let the skill run). (It is also in
    allowed_tools, so the CLI may auto-approve it before the callback; we record
    whichever way it arrives and detect the Skill tool_use from the stream.)
  * Write: resolve the target; ALLOW iff it is `c5_skill_sentinel.txt` INSIDE the
    fixture (the C2 channel -- we must allow it so the skill can proceed to the
    question); DENY anything outside-fixture / traversal (containment X1).
  * AskUserQuestion: answer via the C3 deny-with-answer-message WORKAROUND,
    conveying the trial's CODE-SELECTED option. (C3 is PARTIAL: native allow /
    updated_input does NOT answer on this SDK version; the only working path is
    PermissionResultDeny(message=<the selected option>) which the model interprets.
    C5 inherits that caveat.)
  * Bash, Edit, ExitPlanMode, everything else: DENY (containment X1).

Two trials select DIFFERENT options (trial 1 -> Alpha, trial 2 -> Bravo) under a
neutral invocation prompt, to prove the chosen option is CODE-DRIVEN (the final
`C5_DONE:<option>` must echo the code-selected option in each trial), not a model
guess / prompt-following.

CONTAINMENT (X1): repo `git status --porcelain` is snapshotted before/after; the
only filesystem effect is the in-fixture sentinel (the fixture, including its
`.claude/` tree, is OUTSIDE the repo and rmtree'd in `finally`). Containment is
policy-level (the callback resolves + denies out-of-fixture targets), not an OS
sandbox. No API key (host CLI auth). No bypass / dangerously-skip.

Honest verdict policy (PASS/PARTIAL/FAIL all valid; not manufactured):
  PASS    : the skill is invoked in-session; its Write flows through the SAME
            can_use_tool path and is honored (sentinel present); its
            AskUserQuestion is answered via the SAME C3 code path; the skill is
            driven to completion (C5_DONE seen); and BOTH trials' option is
            code-driven (C5_DONE echoes the code-selected option, Alpha vs Bravo).
            DISCLOSE that the AskUserQuestion answer rides C3's PARTIAL
            deny-with-answer-message workaround (non-native), inherited here.
  PARTIAL : channels are exercised but e.g. completion/option-echo is unreliable,
            or the skill loads but native answering is still required, etc. --
            record exactly what held and what did not.
  FAIL    : the skill does not load/invoke, or prompts do not flow through the
            code paths.

Evidence -> evidence/c5.* via the T3 recorder (scrubbed, SB3/X3).

Run:
  spikes/session-substrate/.venv/bin/python spikes/session-substrate/checks/c5_skill.py
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evidence_recorder import record_criterion  # noqa: E402
from harness_sdk import SDKSessionHarness, assistant_text  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    PermissionResultAllow,
    PermissionResultDeny,
    ToolResultBlock,
    ToolUseBlock,
)

WORKTREE = Path(__file__).resolve().parents[2]  # repo worktree root
SKILL_NAME = "spike-c5-probe"
SKILL_SRC = (
    Path(__file__).resolve().parent.parent / "test_skill" / SKILL_NAME / "SKILL.md"
)
SENTINEL_NAME = "c5_skill_sentinel.txt"
SENTINEL_CONTENT_MARK = "C5_SKILL_RAN"
DONE_PREFIX = "C5_DONE:"
PLANS_DIR = (Path.home() / ".claude" / "plans").resolve()

# Neutral invocation prompt: it tells the model to run the skill and follow ITS
# steps, but never says which option (Alpha/Bravo) to pick -- the code picks.
INVOKE_PROMPT = (
    f"Invoke the {SKILL_NAME} skill now. Follow its steps exactly."
)


def safe_input_summary(tool_name: str, tool_input) -> dict:
    """Summarize tool input WITHOUT dumping raw bodies (lengths, not content)."""
    if not isinstance(tool_input, dict):
        return {"_repr": str(tool_input)[:80]}
    out: dict = {}
    for k, v in tool_input.items():
        if k in ("content", "new_string", "old_string"):
            out[k] = f"<{len(str(v))} chars>"
        elif k in ("file_path", "path", "command", "pattern", "url"):
            out[k] = str(v)[:160]
        elif k == "questions":
            out[k] = options_of(tool_input)
        else:
            out[k] = str(v)[:60]
    return out


def options_of(ask_input) -> list[str]:
    try:
        return [o["label"] for o in ask_input["questions"][0]["options"]]
    except Exception:
        return []


def write_target(fixture: Path, tool_input) -> Path | None:
    target = ""
    if isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return None
    p = Path(target)
    return (p if p.is_absolute() else (fixture / p)).resolve()


def git_status_lines() -> set[str]:
    """Set of `git status --porcelain` lines (compare before/after the run)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKTREE), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return {"<git status unavailable>"}


async def run_trial(pick_index: int, log) -> dict:
    """One live session: copy the skill into a temp cwd, invoke it, answer its
    prompts from code (Write -> allow in fixture; AskUserQuestion -> deny-with-
    answer-message picking options[pick_index]); capture everything.
    """
    fixture = Path(tempfile.mkdtemp(prefix="t11_c5_")).resolve()
    skill_dir = fixture / ".claude" / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_SRC, skill_dir / "SKILL.md")

    cap: dict = {
        "fixture": str(fixture),
        "skill_tool_fired": 0,           # Skill / Skill(name) tool_use seen in stream
        "skill_callback_seen": 0,        # callback was asked to decide on Skill
        "write_callback_fired": 0,
        "write_allowed": False,
        "write_denied_outside": [],      # any out-of-fixture / traversal Write denials
        "ask_fired": 0,
        "ask_options": None,
        "chosen_label": None,
        "tool_results": [],
        "other_denied": [],              # Bash/Edit/ExitPlanMode/etc denied
        "final": "",
        "sid": None,
    }

    async def can_use_tool(tool_name, tool_input, context):
        name = tool_name or ""
        # --- Skill invocation: ALLOW (let the skill run). ---
        if name == "Skill" or name.startswith("Skill("):
            cap["skill_callback_seen"] += 1
            return PermissionResultAllow()

        # --- Write: C2 channel -- allow ONLY the in-fixture sentinel. ---
        if name == "Write":
            cap["write_callback_fired"] += 1
            resolved = write_target(fixture, tool_input)
            in_fixture = resolved is not None and (
                resolved == fixture or fixture in resolved.parents
            )
            is_sentinel = resolved is not None and resolved.name == SENTINEL_NAME
            if in_fixture and is_sentinel:
                cap["write_allowed"] = True
                return PermissionResultAllow()
            # Outside fixture / traversal / wrong name -> deny (containment).
            cap["write_denied_outside"].append(str(resolved))
            return PermissionResultDeny(
                message="contained: only c5_skill_sentinel.txt inside the fixture is permitted"
            )

        # --- AskUserQuestion: C3 channel -- deny-with-answer-message workaround. ---
        if name == "AskUserQuestion":
            cap["ask_fired"] += 1
            cap["ask_options"] = options_of(tool_input)
            labels = cap["ask_options"]
            label = (
                labels[pick_index]
                if labels and 0 <= pick_index < len(labels)
                else "<no-option>"
            )
            cap["chosen_label"] = label
            # Same code path proven in C3/T8: native allow does NOT answer on this
            # SDK; deny-with-answer-message conveys the code-selected option.
            return PermissionResultDeny(message=f"The user selected: {label}")

        # --- Everything else (Bash, Edit, ExitPlanMode, ...) -> DENY. ---
        cap["other_denied"].append(name)
        return PermissionResultDeny(message=f"contained: {name} not permitted in C5 check")

    harness = SDKSessionHarness(
        cwd=str(fixture),
        permission_mode="default",
        can_use_tool=can_use_tool,
        skills=[SKILL_NAME],
        setting_sources=["project"],
    )
    try:
        await harness.start()
        async for msg in harness.send(INVOKE_PROMPT, timeout=180):
            # Separate each message's text with a newline so distinct text blocks
            # (e.g. step narration vs. the final C5_DONE line) never merge into one
            # run-on line -- the SDK yields them as separate TextBlocks with no
            # intrinsic separator.
            chunk = assistant_text(msg)
            if chunk:
                cap["final"] += chunk + "\n"
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolUseBlock):
                        bn = b.name or ""
                        if bn == "Skill" or bn.startswith("Skill("):
                            cap["skill_tool_fired"] += 1
                    elif isinstance(b, ToolResultBlock):
                        cap["tool_results"].append(
                            (getattr(b, "is_error", None), str(b.content)[:160])
                        )
            cap["sid"] = harness.session_id or cap["sid"]
    except Exception as exc:  # noqa: BLE001 -- fail-clean
        cap["final"] += f" [EXC {type(exc).__name__}: {exc}]"
    finally:
        # Capture the sentinel proof BEFORE removing the fixture.
        sentinel_path = fixture / SENTINEL_NAME
        cap["sentinel_present"] = sentinel_path.exists()
        cap["sentinel_content_ok"] = (
            cap["sentinel_present"]
            and SENTINEL_CONTENT_MARK in sentinel_path.read_text(encoding="utf-8", errors="replace")
        )
        try:
            await harness.stop()
        except Exception:
            pass
        shutil.rmtree(fixture, ignore_errors=True)

    cap["final"] = cap["final"].strip()
    # Skill is "invoked" if EITHER the Skill tool_use streamed OR the callback was
    # asked to decide on it (one or the other depending on allowed_tools auto-approve).
    cap["skill_invoked"] = (cap["skill_tool_fired"] >= 1) or (cap["skill_callback_seen"] >= 1)
    # Did the model emit the completion line, and with which option? Scan the
    # whole final text for `C5_DONE:<token>` as a substring (the SDK concatenates
    # text blocks with no guaranteed line break, so a strict line-start match is
    # too brittle); capture the first option token after the prefix.
    done_label = None
    m = re.search(re.escape(DONE_PREFIX) + r"\s*\*?\s*([A-Za-z0-9_-]+)", cap["final"])
    if m:
        done_label = m.group(1).strip()
    cap["done_seen"] = done_label is not None
    cap["done_label"] = done_label
    cap["done_matches_code"] = (
        done_label is not None
        and cap["chosen_label"] is not None
        and done_label.lower() == cap["chosen_label"].lower()
    )
    return cap


def log_trial(log, name, cap):
    log(f"\n--- {name} ---")
    log(f"  fixture (temp, outside repo): {cap['fixture']}")
    log(f"  session_id: {cap.get('sid')}")
    log(f"  Skill invoked? {cap['skill_invoked']} "
        f"(tool_use_streamed={cap['skill_tool_fired']}, callback_seen={cap['skill_callback_seen']})")
    log(f"  Write callback fired: {cap['write_callback_fired']}  allowed_in_fixture={cap['write_allowed']}")
    log(f"  out-of-fixture Write denials: {cap['write_denied_outside'] or 'NONE'}")
    log(f"  sentinel present in fixture: {cap['sentinel_present']}  content_ok={cap['sentinel_content_ok']}")
    log(f"  AskUserQuestion fired: {cap['ask_fired']}  options_presented={cap['ask_options']}")
    log(f"  code-chosen label (via C3 deny-message path): {cap['chosen_label']!r}")
    log(f"  other tools denied (containment): {cap['other_denied'] or 'NONE'}")
    for ie, txt in cap["tool_results"]:
        log(f"  tool_result is_error={ie}: {txt!r}")
    log(f"  completion line seen: {cap['done_seen']}  C5_DONE label={cap['done_label']!r}  "
        f"matches code choice={cap['done_matches_code']}")
    log(f"  model final (first 200): {cap['final'][:200]!r}")


async def _run() -> int:
    report: list[str] = []

    def log(text: str = "") -> None:
        report.append(text)

    import importlib.metadata as md
    log("=== T11 / C5 — skill invocation through the same channels (substrate A, sandboxed) ===")
    log(f"sdk: claude-agent-sdk=={md.version('claude-agent-sdk')}")
    try:
        cli_ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        cli_ver = f"<unavailable: {type(exc).__name__}>"
    log(f"claude CLI: {cli_ver}")
    log(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))} (must be False)")
    log(f"skill source: {SKILL_SRC} (exists={SKILL_SRC.exists()})")
    log("Mechanism: skills=['spike-c5-probe'] injects Skill(spike-c5-probe) into allowed_tools "
        "and (with setting_sources=['project']) loads the cwd .claude/skills tree. The skill's "
        "Write (C2 channel) + AskUserQuestion (C3 channel) flow through the SAME can_use_tool "
        "callback as C2/C3. AskUserQuestion is answered via C3's PARTIAL deny-with-answer-message "
        "workaround (no native answer API on this SDK). No bypass; permission_mode=default.")
    log("Two trials pick DIFFERENT options (trial1 Alpha, trial2 Bravo) to prove the C5_DONE "
        "option is CODE-DRIVEN, not a model guess.")

    plans_before = set(PLANS_DIR.glob("*.md")) if PLANS_DIR.exists() else set()
    repo_before = git_status_lines()

    verdict, reason = "FAIL", "check did not complete"
    try:
        if not SKILL_SRC.exists():
            raise FileNotFoundError(f"committed skill fixture missing: {SKILL_SRC}")

        # Trial 1: code picks option index 0 (expected Alpha).
        t_alpha = await run_trial(0, log)
        log_trial(log, "TRIAL 1 — code picks option[0] (expect Alpha)", t_alpha)

        # Trial 2: code picks option index 1 (expected Bravo) -- DIFFERENT pick.
        t_bravo = await run_trial(1, log)
        log_trial(log, "TRIAL 2 — code picks option[1] (expect Bravo)", t_bravo)

        repo_after = git_status_lines()
        repo_new_changes = sorted(repo_after - repo_before)

        # --- per-channel evaluation across both trials ---
        skill_invoked = t_alpha["skill_invoked"] and t_bravo["skill_invoked"]
        permission_channel = (
            t_alpha["write_callback_fired"] >= 1 and t_alpha["write_allowed"] and t_alpha["sentinel_content_ok"]
            and t_bravo["write_callback_fired"] >= 1 and t_bravo["write_allowed"] and t_bravo["sentinel_content_ok"]
        )
        question_channel = (
            t_alpha["ask_fired"] >= 1 and t_alpha["chosen_label"] is not None
            and t_bravo["ask_fired"] >= 1 and t_bravo["chosen_label"] is not None
        )
        completion = t_alpha["done_seen"] and t_bravo["done_seen"]
        # Code-driven: each trial's C5_DONE echoes ITS code-chosen option, and the
        # two trials chose DIFFERENT options (so it is not a constant/guess).
        code_driven = (
            t_alpha["done_matches_code"] and t_bravo["done_matches_code"]
            and t_alpha["chosen_label"] is not None and t_bravo["chosen_label"] is not None
            and t_alpha["chosen_label"].lower() != t_bravo["chosen_label"].lower()
        )
        # Containment (X1): the skill only writes the in-fixture sentinel; any
        # out-of-fixture attempt is recorded + denied (logged below). The
        # authoritative check is the repo delta: no NEW git changes during the run.
        repo_untouched = (len(repo_new_changes) == 0)

        log("\n=== C5 evaluation (per channel, across both trials) ===")
        log(f"skill_invoked (Skill tool fired / callback saw it, both trials): {skill_invoked}")
        log(f"permission_channel (in-fixture sentinel Write allowed + present, both trials): {permission_channel}")
        log(f"question_channel (AskUserQuestion fired + answered via C3 path, both trials): {question_channel}")
        log(f"completion (C5_DONE line seen, both trials): {completion}")
        log(f"  trial1 C5_DONE={t_alpha['done_label']!r} (code chose {t_alpha['chosen_label']!r}) matches={t_alpha['done_matches_code']}")
        log(f"  trial2 C5_DONE={t_bravo['done_label']!r} (code chose {t_bravo['chosen_label']!r}) matches={t_bravo['done_matches_code']}")
        log(f"code_driven (both echo their code pick AND picks differ -> not model guess): {code_driven}")
        log(f"out-of-fixture Write attempts denied: t1={t_alpha['write_denied_outside'] or 'NONE'} "
            f"t2={t_bravo['write_denied_outside'] or 'NONE'}")
        log(f"repo NEW changes during run: {repo_new_changes if repo_new_changes else 'NONE'} (expected NONE -> X1)")
        log(f"  (pre-existing uncommitted entries ignored: {sorted(repo_before) if repo_before else 'none'})")

        if skill_invoked and permission_channel and question_channel and completion and code_driven and repo_untouched:
            verdict = "PASS"
            reason = (
                "C5 honored: the spike-c5-probe SKILL was invoked in-session (Skill tool fired) and its "
                "interactive prompts flowed through the SAME can_use_tool channels as C2/C3. Its per-tool "
                "permission request (Write c5_skill_sentinel.txt) was answered over the C2 code path and "
                "honored (sentinel present with C5_SKILL_RAN in both trials); its AskUserQuestion was "
                "answered over the C3 code path; the skill was driven to its C5_DONE completion in both "
                "trials; and the completion option was CODE-DRIVEN (trial1 C5_DONE=Alpha when code picked "
                "Alpha, trial2 C5_DONE=Bravo when code picked Bravo -- differing picks, each echoed -> not a "
                "model guess). Containment (X1): repo untouched (no new git changes); the only filesystem "
                "effect was the in-fixture sentinel; out-of-fixture/traversal Writes and Bash/Edit/ExitPlanMode "
                "denied; fixtures (incl. their .claude tree) rmtree'd. INHERITED CAVEAT: the AskUserQuestion "
                "answer rides C3's PARTIAL deny-with-answer-message workaround (PermissionResultDeny "
                "message=<selected option>, tool_result is_error=True) -- NOT a native structured answer API "
                "(none exists on claude-agent-sdk==0.2.105) -- and depends on the model interpreting the "
                "message. Containment is policy-level (callback resolves/denies targets), not an OS sandbox. "
                "C5 only; no claim about C1/C2/C3/C4/C6 beyond reusing their proven code paths here. Host CLI "
                f"auth, no API key. claude CLI {cli_ver}; skill loaded/invoked on this CLI version."
            )
        elif skill_invoked and (permission_channel or question_channel):
            verdict = "PARTIAL"
            reason = (
                "C5 partially demonstrated: skill_invoked={si}, permission_channel={pc}, question_channel={qc}, "
                "completion={comp}, code_driven={cd}, repo_untouched={ru}. The skill loaded/invoked and at least "
                "one interactive prompt flowed through the C2/C3 code path, but not every C5 condition held "
                "(see per-channel lines above). The AskUserQuestion answer, where exercised, rides C3's PARTIAL "
                "deny-with-answer-message workaround (non-native), inherited here.".format(
                    si=skill_invoked, pc=permission_channel, qc=question_channel,
                    comp=completion, cd=code_driven, ru=repo_untouched,
                )
            )
        else:
            verdict = "FAIL"
            reason = (
                "C5 not demonstrated: skill_invoked={si} (trial1 invoked={t1}, trial2 invoked={t2}); "
                "permission_channel={pc}; question_channel={qc}; completion={comp}; code_driven={cd}; "
                "repo_untouched={ru}. Either the skill did not load/invoke on this CLI version, or its "
                "prompts did not flow through the can_use_tool code paths.".format(
                    si=skill_invoked, t1=t_alpha["skill_invoked"], t2=t_bravo["skill_invoked"],
                    pc=permission_channel, qc=question_channel, comp=completion,
                    cd=code_driven, ru=repo_untouched,
                )
            )
    except Exception as exc:  # noqa: BLE001 -- fail-clean
        verdict, reason = "FAIL", f"exception during check: {type(exc).__name__}: {exc}"
        log(reason)
    finally:
        # Clean up any plan-scratch the run might have produced (defensive; the
        # skill is forbidden from ExitPlanMode, but be safe).
        removed = 0
        if PLANS_DIR.exists():
            for f in set(PLANS_DIR.glob("*.md")) - plans_before:
                try:
                    f.unlink(); removed += 1
                except Exception:
                    pass
        log(f"\ncleanup: removed {removed} plan-scratch file(s) created in {PLANS_DIR} (expected 0)")

    log("")
    log("Distinguishers / limitations (explicit disclosures):")
    log("- The skill is loaded as a PROJECT skill from the disposable temp cwd's .claude/skills tree")
    log("  (setting_sources=['project']); the repo-root .claude/ and user .claude/ are NOT used.")
    log("- skills=['spike-c5-probe'] injects Skill(spike-c5-probe) into allowed_tools, so the CLI may")
    log("  auto-approve the Skill tool itself; we detect invocation from the Skill tool_use in the")
    log("  stream AND record if the callback was consulted. The skill's Write + AskUserQuestion are NOT")
    log("  in allowed_tools, so they route through the SAME can_use_tool callback as C2/C3.")
    log("- Permission (C2 channel): the in-fixture sentinel Write is ALLOWED (so the skill proceeds);")
    log("  out-of-fixture/traversal Writes are DENIED (containment). Same callback as c2_permission.py.")
    log("- Question (C3 channel): answered via C3's PARTIAL deny-with-answer-message workaround")
    log("  (PermissionResultDeny message=<selected option>); native allow/updated_input does NOT answer")
    log("  on claude-agent-sdk==0.2.105. This PARTIAL caveat is INHERITED by C5's completion path.")
    log("- Code-driven proof: the neutral invocation prompt never names Alpha/Bravo; the CODE picks per")
    log("  trial. Two trials with DIFFERENT picks each echoed their pick in C5_DONE -> code-driven.")
    log("- Containment (X1): repo git status snapshotted before/after (only in-fixture sentinel changes);")
    log("  fixtures rmtree'd; containment is policy-level (callback denies out-of-fixture), not an OS sandbox.")
    log("- C5 only; no claim about C1/C2/C3/C4/C6 beyond reusing their code paths. Substrate B is out of scope here.")
    log(f"VERDICT: {verdict}")
    log(f"REASON: {reason}")

    out = "\n".join(report)
    print(out)
    with record_criterion("c5") as rec:
        rec.add_transcript(out + "\n")
        rec.set_verdict(verdict, reason)

    return 0 if verdict in ("PASS", "PARTIAL", "FAIL") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
