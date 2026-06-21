# P1 streaming-engine — owner phone-verify checklist

_Final human acceptance for P1 (interactive streaming session engine), done from a real
phone over Telegram under `ENGINE_MODE=streaming`. The programmatic live probe
(`spikes/p1-live-verify/verify_streaming.py --live`) proves the engine path against real
Claude with code-injected decisions; **this** checklist proves the same workflow through
the real Telegram UI (buttons, taps, typed feedback) end-to-end._

> **Scope.** P1 is the streaming session engine + the answer-hold (AskUserQuestion /
> ExitPlanMode answered from the phone) + live coalesced rendering + `/cancel`. **Per-tool
> permission gating (allow/deny buttons before risky tools, bypass removal, the full 60-min
> backstop UX) is P2 — NOT in scope here.** In P1, ordinary tools run in the substrate's
> default permission mode inside the single allowlisted chat (no new bypass introduced).

---

## 0. Setup

Run on the branch `feat/streaming-engine` (never merged to main; the live one-shot bot
stays the owner's default remote access).

**Required environment** (`.env` or exported):

- [ ] `TELEGRAM_BOT_TOKEN` — the bot token (never logged; redacted in any evidence).
- [ ] `TELEGRAM_ALLOWED_CHAT_IDS` — your numeric chat id(s), comma-separated. Get yours
      from `@userinfobot`. **This is the SB1 allowlist — the only chats the bot serves.**
- [ ] **`ENGINE_MODE=streaming`** — selects the new engine. **Unset / `oneshot` keeps
      today's one-shot bot unchanged (the safe default).** Flip to `streaming` only for
      this verification.

**Optional environment:**

- [ ] `ALLOWED_ROOTS` — colon/comma-separated dirs that `/cd` may enter (SB2 confinement).
      **Default when unset = the workdir only** (`CLAUDE_WORKDIR`, itself defaulting to
      `$HOME`) — confinement is **ON by default**.
- [ ] `ALLOW_ANY_PATH=true` — explicit opt-out that disables `/cd` confinement entirely
      (you take the wheel). **Leave unset for this check** so SB2 is exercised.
- [ ] `ANSWER_BACKSTOP_SECONDS` — how long a pending question/plan is held before it
      auto-denies + notifies (default `3600` = 60 min). Set a SHORT value (e.g. `120`) if
      you want to observe the backstop firing during this session (check 4b, optional).

**Launch:**

- [ ] `python main.py` (from the repo root, with the project venv). Confirm the log line
      reports streaming mode and **does not print the bot token** (SB3).
- [ ] Sanity: oneshot is still the default — re-launching with `ENGINE_MODE` unset gives
      the unchanged one-shot bot. (Optional; only if you want to confirm the flag default.)

---

## 1. Checks

Each check has exact steps and the expected result. Tick the box when the expected result
is observed from your phone.

### (a) Live coalesced streaming (not a flood)

- [ ] Send a normal message that makes Claude work for a few seconds, e.g.
      `List three short ideas for a weekend project, one line each.`
- **Expected:** you see a **status line that edits in place** as work progresses (a single
  message being updated), then the final answer — **not** a rapid burst of many separate
  messages. Edits are throttled (~1 update/sec; RB5). The final prose arrives as its own
  message.

### (b) AskUserQuestion → option buttons → answer → continuation (THE headline)

- [ ] Trigger an AskUserQuestion-emitting flow. Either run **`/grill`** (it interrogates
      you and asks multiple-choice questions), or send a direct prompt such as:
      `Use the AskUserQuestion tool to ask me whether to optimize for speed or simplicity (two options), then proceed based on my choice.`
- **Expected:** the question renders as a message with **one inline button per option**
  plus an **"Other (free text)"** button.
- [ ] **Tap one option button.**
- **Expected:** a brief toast (e.g. "Answered: …"); the **session continues on that exact
  answer** (Claude's next output reflects the option you tapped). The button you tapped
  determined the answer — not anything you typed.
- [ ] Trigger another question, then **tap "Other (free text)"**.
- **Expected:** a prompt to type your answer; your **next message is captured as the
  answer** (it does NOT start a new turn) and the session continues on it.

### (c) Plan flow → Approve / Reject + feedback

- [ ] Trigger a plan-producing flow (run **`/pipeline`** or a planning prompt such as:
      `Make a short plan for renaming a variable across the project, then call ExitPlanMode and wait for my approval.`).
- **Expected:** the **plan text** renders (chunked if long) with **`[Approve]`** and
      **`[Reject + feedback]`** buttons.
- [ ] **Tap `[Approve]`.**
- **Expected:** toast "Plan approved"; Claude **proceeds** with the plan.
- [ ] Trigger another plan, then **tap `[Reject + feedback]`** and **type a specific
      change** (e.g. `Use snake_case, not camelCase`).
- **Expected:** a prompt to type feedback; your typed feedback is delivered to Claude, and
  Claude **revises the plan incorporating your feedback** (and re-presents it / proceeds
  accordingly).

### (d) `/cancel` aborts a waiting run cleanly (RB4)

- [ ] Trigger a question or plan (so the bot is **waiting** on your decision), then — instead
      of tapping — send **`/cancel`**.
- **Expected:** the waiting run **aborts cleanly** (no hang, no stuck "working…"). 
- [ ] Send a fresh trivial message afterward, e.g. `Reply with: OK`.
- **Expected:** it **works** — the session is still usable after the cancel (not wedged).

#### (4b) Backstop auto-deny (OPTIONAL — only if `ANSWER_BACKSTOP_SECONDS` is short)

- [ ] With `ANSWER_BACKSTOP_SECONDS=120`, trigger a question and **do not answer**; wait it
      out.
- **Expected:** after the interval, a **notify** that the request was auto-denied; the
  session **remains usable** for the next message. (Full backstop UX is P2; this just
  confirms it fails clean.)

### (e) SB1 spot-check — a non-allowlisted chat / forged tap does nothing

- [ ] From a **different Telegram account NOT in `TELEGRAM_ALLOWED_CHAT_IDS`** (or ask a
      friend), send the bot a message and, if you can see a button, tap it.
- **Expected:** **nothing happens** — the bot does not reply, does not answer a question,
  does not approve a plan. Inbound messages **and button-callback taps** from a
  non-allowlisted chat are ignored (SB1 is rechecked on every callback, not just messages).

### (f) `/cd` confinement — out-of-root path refused (SB2)

- [ ] Send `/cd /etc` (or any path outside your `ALLOWED_ROOTS` / workdir).
- **Expected:** **refused** with a clear message; the working dir does **not** change.
- [ ] Send `/cd` to a path **inside** an allowed root (e.g. a subdir of your workdir).
- **Expected:** **accepted**; subsequent work happens there.
- [ ] (Note) This confinement only relaxes if you set `ALLOW_ANY_PATH=true` — which you
      should leave **unset** for normal operation.

---

## 2. Acceptance

P1 is accepted from the phone when **(a)–(f)** all pass (4b optional), i.e.:

- [ ] Live output streams coalesced (edited-in-place), not flooded.
- [ ] A question renders option buttons; tapping one (or "Other" + typed text) continues
      the session on that answer.
- [ ] A plan renders Approve / Reject+feedback; approve proceeds, reject + typed feedback
      revises.
- [ ] `/cancel` aborts a waiting run cleanly and the session stays usable.
- [ ] A non-allowlisted chat / forged tap does nothing (SB1).
- [ ] An out-of-root `/cd` is refused; an in-root `/cd` is accepted (SB2).
- [ ] Throughout, `ENGINE_MODE=oneshot` remained the documented safe default (you flipped
      to `streaming` only for this check).

If anything fails, capture the message/screenshot and the bot log (token is redacted) and
file it against the streaming-engine feature before flipping the live bot to streaming.
