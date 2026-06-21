# P3 interactive-prompts — owner phone-verify checklist

_Final human acceptance for P3 (run skills from the phone — the HEADLINE workflow), done from a
real phone over Telegram under `ENGINE_MODE=streaming`. The programmatic live probe
(`spikes/p3-skill-launch-verify/verify_skill_launch.py --live`) proves a `/grill` loop reaches
real Claude through the launch passthrough and drives the relay + gating to a written doc;
**this** checklist proves the same workflow through the real Telegram UI (typing `/grill`,
answering its questions via the inline buttons, seeing it finish to a doc) end-to-end._

> **Scope.** P3 adds **one** thing on top of P1 (the interactive relay: AskUserQuestion →
> buttons + "Other"; ExitPlanMode → Approve/Reject; the async answer-hold + `/cancel`; coalesced
> rendering) and P2 (per-tool permission gating): a **skill-launch passthrough** (D1). Any
> slash-command that is **not** one of the bot's own commands (`/start /help /reset /cancel /pwd
> /cd /yolo /unyolo`) is forwarded **verbatim** (command + args, leading `/` intact) to the active
> Claude session — so `/grill`, `/pipeline`, `/scaffold`, … "just work" as skills in-session
> (ADR-001 C5). The bot's own commands always take precedence. **NOT in scope:** any skill
> menu/discovery UI, per-skill bot commands, argument validation; multi-project (P4); concurrency
> (P5). **No new permission posture** — a launched skill's tools are gated exactly as P2 already
> does. **No `ENGINE_MODE` default flip** — one-shot remains the documented safe default.

---

## 0. Setup

Run on the branch `feat/interactive-prompts` (never merged to main; the live one-shot bot stays
the owner's default remote access).

**Required environment** (`.env` or exported):

- [ ] `TELEGRAM_BOT_TOKEN` — the bot token (never logged; redacted in any evidence).
- [ ] `TELEGRAM_ALLOWED_CHAT_IDS` — your numeric chat id(s), comma-separated. Get yours from
      `@userinfobot`. **This is the SB1 allowlist — the only chats the bot serves.**
- [ ] **`ENGINE_MODE=streaming`** — the interactive relay (and therefore launching a skill that
      uses it) lives on the streaming engine. **Unset / `oneshot` keeps today's one-shot bot
      unchanged (the safe default); in one-shot mode an unregistered slash-command is still
      forwarded to `claude -p` but there is no live interactive relay (no buttons, no answer-hold)
      — so launch a skill from `streaming` for this verification.** Flip to `streaming` only for
      this check.

**Optional environment:**

- [ ] `ANSWER_BACKSTOP_SECONDS` — how long a pending interactive prompt (an AskUserQuestion or a
      permission prompt) is held before it auto-**denies** + notifies (default `3600` = 60 min).
      Leave at the default unless you specifically want to observe the backstop.
- [ ] `ALLOWED_ROOTS` / `ALLOW_ANY_PATH` — `/cd` confinement (SB2, a P1 control). Not the focus of
      P3; leave at defaults. **Tip:** `/cd` into a throwaway scratch dir before launching `/grill`
      so the design doc it writes lands somewhere harmless.

**Launch:**

- [ ] `python main.py` (from the repo root, with the project venv). Confirm the log line reports
      **streaming** mode and **does not print the bot token** (SB3).

---

## 1. Checks

Each check has exact steps and the expected result. Tick the box when the expected result is
observed from your phone. Use a **scratch working dir** (`/cd` into a throwaway folder) so the
doc `/grill` writes is harmless.

### (a) Launching `/grill` starts the skill in the session — and its first question renders as buttons

- [ ] Send **`/grill a tiny CLI todo app in Python`** (a non-bot slash-command, with a short
      concrete idea in the same message so grill has something to interrogate).
- **Expected:** the `/grill` **skill launches in the live Claude session** (it is NOT treated as an
      unknown bot command and is NOT silently dropped). Within a few seconds its **first
      `AskUserQuestion` renders as an inline keyboard** — the question text followed by one button
      per option (and an **"Other"** button), exactly like any P1 interactive prompt.
- [ ] **Answer by tapping an option button.** (To free-type instead, tap **"Other"** and send your
      answer as the next message — the free-text capture routes it back as the answer.)
- **Expected:** a brief toast confirming your answer; the skill **proceeds** to its next question
      (more buttons) or to writing the brief.

### (b) The full loop runs to a written design doc

- [ ] Continue answering each question grill asks (buttons / "Other") until it stops asking.
- **Expected:** the loop runs through grill's questions and reaches the point where it **writes a
      design doc / project brief**.
- [ ] When grill attempts the **file write**, a P2 **permission prompt** appears
      (**`🔐 Permission needed — Claude wants to run Write:`** with a **body-free** one-line
      summary and the **`✅ Allow once`** · **`☑️ Allow for session`** · **`⛔ Deny`** buttons).
      **Tap `✅ Allow once`.**
- **Expected:** the action runs and the **brief file is written** (in your scratch dir); grill
      finishes and reports the doc. The **complete `/grill` loop ran from your phone** — questions
      via buttons, the write gated by P2 — to a written doc. _(P1's relay + P2's gating compose
      under the P3 launch; nothing new was wired for them.)_

### (c) A bot command still WINS (is not forwarded as a skill)

- [ ] Send **`/reset`** (one of the bot's own commands).
- **Expected:** the bot replies **`🔄 Fresh Claude session started.`** — `/reset` is handled by its
      **own command** (it resets the session) and is **NOT** forwarded to Claude as a skill. The
      bot's commands always take precedence over the passthrough (D1). _(Same for `/cd`, `/cancel`,
      `/yolo`, `/unyolo`, `/pwd`, `/help` — a skill of the same name never shadows them.)_
- [ ] (Optional) Send **`/pwd`** — it shows the working directory (its own command), it is not sent
      to the session.

### (d) A non-allowlisted chat cannot launch a skill (SB1)

- [ ] From a **different Telegram account NOT in `TELEGRAM_ALLOWED_CHAT_IDS`** (or ask a friend),
      send the bot **`/grill take over the world`** (or any slash-command).
- **Expected:** **nothing happens** — the bot does **not** reply and **no skill launches**. The new
      command surface carries the SAME allowlist guard as every other handler (the `allowed` chat
      filter on the registration **and** the `_ok` recheck inside `on_skill_command`) — a
      non-allowlisted chat reaches neither the runner nor the streaming session. **Only an
      allowlisted operator can launch a skill.**

### (e) `/help` mentions that other slash-commands run as skills (discoverability)

- [ ] Send **`/help`**.
- **Expected:** the help text includes a line noting that **any *other* slash-command (e.g.
      `/grill`, `/pipeline`, `/scaffold`) is forwarded verbatim and runs as a skill in the Claude
      session.** (Discoverability — D2; there is no skill menu, by design.)

### (f) (Optional) `/cancel` aborts a launched skill mid-question (RB4)

- [ ] Launch `/grill` again; when it is **waiting on a question or a permission prompt**, instead of
      answering send **`/cancel`**.
- **Expected:** the waiting request **aborts cleanly** (no hang, no stuck "working…"); a fresh
      trivial message afterward (e.g. `Reply with: OK`) still works — the session stays usable.
      _(This is P1/P2 cancel behavior, re-confirmed under a launched skill.)_

---

## 2. Acceptance

P3 is accepted from the phone when **(a)–(e)** all pass (f optional), i.e.:

- [ ] Sending **`/grill <idea>`** (or any non-bot slash-command) **starts that skill in the Claude
      session**; its first `AskUserQuestion` renders as option buttons and answering (buttons /
      "Other") makes the skill **proceed**.
- [ ] A **complete `/grill` loop** runs to a **written design doc**, driven entirely from the phone
      (questions via buttons / "Other"; the file write gated by P2's `Allow once`).
- [ ] The bot's **own commands still work and take precedence** (`/reset` resets — it is NOT
      forwarded as a skill; same for `/cd`, `/cancel`, `/yolo`, … — never shadowed by a same-named
      skill).
- [ ] A **non-allowlisted chat cannot launch a skill** (SB1 holds on the new command surface — no
      reply, no session call).
- [ ] **`/help`** notes that other slash-commands run as skills in the Claude session.
- [ ] Throughout, `ENGINE_MODE=oneshot` remained the documented safe default (you flipped to
      `streaming` only for this check); **no** `--dangerously-skip-permissions` was introduced on
      the streaming path (SB5, inherited from P2), and no new permission posture was added — the
      launched skill's tools were gated exactly as P2 already does.

This is the gate **before any cutover.** P3 stays branch-only (`feat/interactive-prompts`, never
merged to main); the live one-shot bot keeps running unchanged until the owner decides to flip the
default. If anything fails, capture the message/screenshot and the bot log (token is redacted) and
file it against the interactive-prompts feature before considering a cutover.
