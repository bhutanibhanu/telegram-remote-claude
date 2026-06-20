# Autonomous build loop — P0 session-substrate-feasibility

How to drive the P0 spike from the Claude app with minimal hand-holding, while
keeping the evidence trustworthy.

## How far it can run unattended

| Tasks | Nature | Unattended? |
|---|---|---|
| **T1–T3** (scaffold · secret scrubber + unit test · evidence recorder) | Deterministic code, objective acceptance criteria, **no external environment needed** | ✅ Yes |
| **T4+** (preflight probe · harnesses · C1–C6 checks) | Need an authenticated `claude`/Agent SDK and produce **empirical verdicts** | ⛔ No — human in the loop |

**Run T1 → T2 → T3 in one autonomous batch, then STOP at T4.** T4 is the first
task that depends on the uncertain environment and resolves the make-or-break
unknown (does the Agent SDK exist/install?) — you want eyes on that result before
the harness tasks fan out. (T10, the skill fixture, is also environment-free and
only depends on T1, so it can optionally be added to the batch.)

## "Perfection" — scope it correctly

- **T1–T3:** objective, testable → iterate implement→verify until they fully pass.
- **T4+ (evidence):** a `FAIL`/`PARTIAL` verdict is a **legitimate, valuable
  result**. Never "iterate until pass" on an evidence check — that corrupts the
  evidence. Record honestly.

## The prompt

Open the app on branch `feat/session-substrate-feasibility` and paste:

```
You are running an autonomous, self-verifying build loop for the P0 feasibility
spike in this repo. Work on branch feat/session-substrate-feasibility.

Read first and treat as authoritative:
- docs/features/session-substrate-feasibility/design.md
- docs/features/session-substrate-feasibility/progress.md   (task list + acceptance criteria; single source of task truth)
- docs/adr/ADR-001-session-substrate.md
- docs/cross-cutting-requirements.md

SCOPE: do tasks T1, T2, T3 in order WITHOUT pausing for my approval. Then STOP.
Do NOT start T4 (it needs an authenticated claude/Agent SDK environment and a
human decision on the Agent-SDK probe result). Do NOT run any Codex/external QA.

For EACH task, run this loop:
1. IMPLEMENT — spawn a subagent to implement the task exactly per its acceptance
   criteria + the cross-cutting rules X1/X2/X3 + the "Rules" section. Hard limits:
   create/modify ONLY files under spikes/session-substrate/ (plus ticking
   progress.md). Never touch any production file (bot.py, claude_tg/,
   claude_runner.py, requirements.txt, requirements-dev.txt, main.py, tests/).
   Never touch the running bot.
2. VERIFY — spawn 2–3 INDEPENDENT reviewer subagents (fresh context, not the
   implementer). Each checks the implementation against EVERY acceptance bullet
   and reports PASS/FAIL per bullet WITH evidence. They must actually verify:
   - run git status / git diff to prove no production file changed and the venv
     is git-ignored (T1),
   - actually run pytest for T2 (python -m pytest spikes/session-substrate/test_scrub.py)
     and paste results,
   - confirm requirements.lock has pinned versions (T1),
   - confirm every transcript write routes through scrub() (T3).
3. ITERATE TO GREEN — if any reviewer reports a real failure, hand the specifics
   back to the implementer and repeat 1–2. Continue until ALL reviewers sign off
   on ALL bullets. Cap at 4 rounds per task; if still not green, STOP and report
   what's blocking — never weaken the criteria to force a pass.
4. COMMIT — once green, commit only that task's files with message
   "spike(session-substrate): <Tn short title>", then tick [x] for that task in
   progress.md (with short sha) and commit that. Move to the next task.

"Perfection" applies ONLY to T1–T3 (objective, testable code). Do NOT carry an
"iterate until pass" mindset into T4+: there a FAIL/PARTIAL verdict is a valid,
valuable result and must be recorded honestly — never grind a check to make it
"pass".

When you stop (after T3, or if blocked), post a summary: per task — what was
built, reviewer verdicts, commit sha; the current progress.md checkbox state;
and exactly what T4 needs from me (environment/auth) to continue.
```

## After the batch

Review the three commits + the reviewer summaries, then decide how to tackle T4
(confirm the app environment has an authed `claude`/Agent SDK, or run the
evidence checks locally). The remaining tasks (T4–T19) stay human-in-the-loop.
