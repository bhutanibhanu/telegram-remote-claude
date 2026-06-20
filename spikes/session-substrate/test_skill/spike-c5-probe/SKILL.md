---
name: spike-c5-probe
description: P0 session-substrate C5 fixture. Invoke this skill when explicitly asked to run the "spike-c5-probe" / C5 probe skill. It deliberately emits a per-tool permission request AND an interactive question so the spike can prove a skill's interactive prompts flow through the SAME channels as C2 (permission) and C3 (AskUserQuestion).
---

# spike-c5-probe — C5 channel-exercise skill (throwaway spike fixture)

This is a **deliberately interactive** test skill for the P0 session-substrate
feasibility spike (criterion **C5**). It exists only to force a skill invocation
to raise the two interactive surfaces the spike must drive from code:

1. a **per-tool permission decision** (the C2 channel), and
2. an **AskUserQuestion** (the C3 channel).

It performs no real work and must stay within the current working directory.

## Steps — perform EXACTLY these, in order, then STOP

1. **Permission channel (C2).** Use the **Write** tool to create a file named
   `c5_skill_sentinel.txt` **in the current working directory** whose contents
   are exactly:

   ```
   C5_SKILL_RAN
   ```

   Do not write anywhere else. This Write is expected to trigger a per-tool
   permission decision that the spike answers from code.

2. **Interactive-question channel (C3).** Use the **AskUserQuestion** tool to ask
   the operator one single-select multiple-choice question:

   - question: `Which greeting should the C5 probe use?`
   - header: `Greeting`
   - options: exactly two — `Alpha` and `Bravo`.

   This question is expected to be answered programmatically (no TTY) by the
   spike harness.

3. **Completion.** After the question is answered, reply with **exactly** one
   line and nothing else:

   ```
   C5_DONE:<chosen option>
   ```

   e.g. `C5_DONE:Alpha` or `C5_DONE:Bravo`, using whichever option the answer
   selected.

## Hard constraints

- Do **not** read, write, or modify any file other than the single
  `c5_skill_sentinel.txt` sentinel above.
- Do **not** run Bash, Edit, or any other tool. Do **not** ask any other
  question. Do **not** call ExitPlanMode.
- Keep the whole interaction to the three steps above. The completion line is the
  signal the spike uses to confirm the skill was driven to completion through the
  permission + question channels.
