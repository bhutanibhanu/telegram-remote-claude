# test_skill — C5 fixture (T10)

A minimal, **throwaway** custom skill for the session-substrate spike's **C5**
criterion (skill invocation). It deliberately emits the two interactive surfaces
the spike must drive from code, so C5 can be proven through the **same** channels
as C2 and C3:

- a **per-tool permission request** (a single `Write` of a sentinel file) — the
  **C2** channel; and
- an **AskUserQuestion** (a 2-option single-select) — the **C3** channel.

## Layout

```
test_skill/
  README.md                  # this file
  spike-c5-probe/
    SKILL.md                 # the skill definition (name: spike-c5-probe)
```

The skill is stored **plainly** here (not under a `.claude/` tree) so the
committed fixture is explicit and never mistaken for live project config.

## How it is loaded (T11)

`claude-agent-sdk==0.2.105` discovers a *project* skill from
`<cwd>/.claude/skills/<name>/SKILL.md` and enables it when
`ClaudeAgentOptions.skills=["spike-c5-probe"]` is set (the SDK then injects the
`Skill(spike-c5-probe)` tool and, when `setting_sources` is unset, defaults it to
`["user", "project"]` — see the SDK's `_apply_skills_defaults`). T11 passes
`setting_sources=["project"]` explicitly, which still includes the `"project"`
source that loads the cwd `.claude/` tree.

So the **C5 check (T11)** will, at runtime and inside a disposable temp fixture:

1. create `<tempcwd>/.claude/skills/spike-c5-probe/` and copy this `SKILL.md`
   into it (keeping the spike contained — the repo-root `.claude/` is never
   touched);
2. start the harness with `cwd=<tempcwd>`, `skills=["spike-c5-probe"]`,
   `setting_sources=["project"]`, and a `can_use_tool` callback;
3. invoke the skill in-session, answer its permission request and its
   AskUserQuestion **from code** (the C2/C3 channels), and confirm the skill
   reaches its `C5_DONE:<option>` completion line.

This file is a **fixture only**; the C5 verdict + transcript are produced by
`checks/c5_skill.py` (T11), not here.
