# P2 permission-gating — owner phone-verify checklist

_Final human acceptance for P2 (per-tool permission gating), done from a real phone over
Telegram under `ENGINE_MODE=streaming`. The programmatic live probe
(`spikes/p2-permission-verify/verify_permissions.py --live`) proves the gate against real
Claude with code-injected verdicts; **this** checklist proves the same workflow through the
real Telegram UI (the `[Allow once] / [Allow for session] / [Deny]` buttons, `/yolo`,
`/cancel`, `/reset`) end-to-end._

> **Scope.** P2 replaces P1's interim "ordinary tool → auto-allow" posture with a real
> approval gate (ADR-003): risky tools (Write/Edit/Bash/WebFetch/`mcp__*`/unknown) pause for
> **[Allow once] / [Allow for session] / [Deny]**; reads/search run free; `/yolo` is the one
> loud, off-by-default, per-session bypass; the streaming path introduces **no**
> `--dangerously-skip-permissions` (SB5). The legacy one-shot runner is unchanged (D3 — its
> `--dangerously-skip-permissions` is the documented retiring exception; **no `ENGINE_MODE`
> default flip** in P2). **Persisted grants / multi-project / concurrency are NOT in scope.**

---

## 0. Setup

Run on the branch `feat/permission-gating` (never merged to main; the live one-shot bot stays
the owner's default remote access).

**Required environment** (`.env` or exported):

- [ ] `TELEGRAM_BOT_TOKEN` — the bot token (never logged; redacted in any evidence).
- [ ] `TELEGRAM_ALLOWED_CHAT_IDS` — your numeric chat id(s), comma-separated. Get yours from
      `@userinfobot`. **This is the SB1 allowlist — the only chats the bot serves.**
- [ ] **`ENGINE_MODE=streaming`** — selects the engine the gate lives on. **Unset / `oneshot`
      keeps today's one-shot bot unchanged (the safe default); the permission gate, `/yolo`,
      and `/unyolo` apply to streaming mode ONLY.** Flip to `streaming` only for this
      verification.

**Optional environment:**

- [ ] `ANSWER_BACKSTOP_SECONDS` — how long a pending **permission** prompt is held before it
      auto-**denies** + notifies (default `3600` = 60 min). Set a SHORT value (e.g. `120`) if
      you want to observe the backstop firing during this session (check (g·2), optional).
- [ ] `ALLOWED_ROOTS` / `ALLOW_ANY_PATH` — `/cd` confinement (SB2, a P1 control). Not the
      focus of P2; leave at defaults unless you also want to re-confirm `/cd`.

**Confirm `/yolo` is OFF by default (D6):**

- [ ] On a fresh launch you have sent **neither** `/yolo` nor `/unyolo`. The gate is therefore
      active (fail-closed) — risky tools will prompt. (`/yolo` is never silently on; you would
      have seen a loud `⚠️ YOLO MODE ON ⚠️` banner if it were enabled.)

**Launch:**

- [ ] `python main.py` (from the repo root, with the project venv). Confirm the log line
      reports **streaming** mode and **does not print the bot token** (SB3).

---

## 1. Checks

Each check has exact steps and the expected result. Tick the box when the expected result is
observed from your phone. Use a **scratch working dir** (e.g. `/cd` into a throwaway folder)
so the file edits below are harmless.

### (a) A risky action renders the 3-button approval prompt

- [ ] Send a prompt that makes Claude attempt a **risky** tool, e.g.
      `Create a file notes.txt in the current directory containing the text hello.`
      (a Write), or `Run the shell command: echo hi` (a Bash).
- **Expected:** before the action runs, a message appears:
      **`🔐 Permission needed — Claude wants to run Write:`** (or `Bash`), then a **body-free**
      one-line summary (lengths/paths, **not** the file contents — SB3), then
      *"Allow once, allow for this session, or deny?"* — with an inline keyboard:
      **`✅ Allow once`** · **`☑️ Allow for session`** · **`⛔ Deny`**.

### (b) [Allow once] runs it — and the NEXT risky action asks again

- [ ] On the prompt from (a), **tap `✅ Allow once`.**
- **Expected:** a brief toast (*"Allowed once"*); the action **runs** (the file is created /
      the command runs) and Claude continues.
- [ ] Now trigger **another** risky action — a **different** kind is the strongest check, e.g.
      ask Claude to **edit** that file (`Append the line world to notes.txt.`) **or** run a
      command (`Run: ls`).
- **Expected:** it **asks again** — a fresh `🔐 Permission needed …` prompt with the three
      buttons. **Allow once did NOT grant anything beyond that one call** (every risky tool
      gates independently — the ADR-001 caveat).

### (c) [Allow for session] stops re-asking THAT tool

- [ ] Trigger a risky action with a **repeatable** tool — Write is easiest. Ask Claude to
      create a file, and when prompted **tap `☑️ Allow for session`.**
- **Expected:** toast *"Allowed for session"*; the action runs.
- [ ] Ask Claude to do **another Write** (e.g. `Create a second file b.txt containing test.`).
- **Expected:** it runs **with NO new permission prompt** — `Write` is now granted for this
      session.
- [ ] (Caveat check) Trigger a **different** risky tool — e.g. a **Bash** command
      (`Run: echo hi`).
- **Expected:** it **still asks** — the session grant is **per tool name** (Write), so Bash is
      unaffected. Approving one tool does not free the others.

### (d) [Deny] — not run, and Claude adapts to the denial

- [ ] Trigger a risky action and **tap `⛔ Deny`.**
- **Expected:** toast *"Denied"*; the action **does NOT run** (no file created / command not
      executed), and Claude **continues** — it receives the canned denial ("Operator denied
      this tool call") and adapts (acknowledges it couldn't do it / proposes an alternative),
      **without crashing or hanging**.

### (e) A read / search runs with NO prompt

- [ ] Ask Claude to do a **read or search**, e.g. `Read notes.txt and tell me its contents.`
      or `List the files in the current directory.` or `Search the project for the word hello.`
- **Expected:** **no permission prompt at all** — the read/search runs **unprompted** and the
      answer comes back. (Read / Glob / Grep / LS / TodoWrite / **WebSearch** are the safe
      allowlist; everything else gates.)

### (f) /yolo runs everything (loud), /unyolo restores prompting

- [ ] Send **`/yolo`**.
- **Expected:** a **loud** reply — **`⚠️ YOLO MODE ON ⚠️`** — telling you every tool now runs
      with **no approval prompt** until `/unyolo`.
- [ ] Trigger a risky action (a Write or Bash).
- **Expected:** it **runs with NO permission prompt**, **and** the turn carries a persistent
      loud **`⚠️ YOLO`** marker (so an allow-all session is never silent — it shows on the
      turn, not just at the toggle).
- [ ] Send **`/unyolo`**.
- **Expected:** a reply that **gating is restored** (risky tools will ask again; `/yolo` is
      off).
- [ ] Trigger a risky action again.
- **Expected:** the **3-button prompt is back** — the fail-closed gate has returned.

### (g) /cancel aborts a waiting permission prompt (RB4)

- [ ] Trigger a risky action so the bot is **waiting** on your decision (the `🔐 Permission
      needed …` prompt is showing), then — instead of tapping a button — send **`/cancel`**.
- **Expected:** the waiting request **aborts cleanly** (treated as a deny — the action does
      NOT run; no hang, no stuck "working…").
- [ ] Send a fresh trivial message afterward, e.g. `Reply with: OK`.
- **Expected:** it **works** — the session is still usable after the cancel (not wedged).

#### (g·2) Backstop auto-deny (OPTIONAL — only if `ANSWER_BACKSTOP_SECONDS` is short)

- [ ] With `ANSWER_BACKSTOP_SECONDS=120`, trigger a risky action and **do not answer**; wait
      it out.
- **Expected:** after the interval, a **notify** that the request was auto-**denied**; the
      action did NOT run and the session **remains usable** for the next message.

### (h) SB1 — a non-allowlisted chat / forged tap does nothing

- [ ] From a **different Telegram account NOT in `TELEGRAM_ALLOWED_CHAT_IDS`** (or ask a
      friend), send the bot a message; if you can see a permission prompt's buttons (e.g.
      forwarded), tap **`✅ Allow once`**.
- **Expected:** **nothing happens** — the bot does not reply, and the tap **never allows a
      tool**. Inbound messages **and permission-button taps** from a non-allowlisted chat are
      ignored (SB1 is rechecked on every callback, not just on messages). A risky tool can
      **only** be approved by an allowlisted operator.

### (i) /reset clears grants + yolo

- [ ] Establish state first: send **`/yolo`** (so allow-all is on), **or** approve a Write with
      **`☑️ Allow for session`** (so a grant exists). Confirm a risky action currently runs
      without prompting.
- [ ] Send **`/reset`**.
- [ ] Trigger a risky action (a Write or Bash).
- **Expected:** the **3-button permission prompt is back** — `/reset` dropped **all**
      allow-session grants **and** turned `/yolo` off (D7). A new session always starts
      **fail-closed**; a reset / restart never silently resumes an allow-all or granted posture.

---

## 2. Acceptance

P2 is accepted from the phone when **(a)–(i)** all pass (g·2 optional), i.e.:

- [ ] A risky action (edit a file / run a command) renders **[Allow once] / [Allow for session]
      / [Deny]** with a **body-free** summary (SB3).
- [ ] **Allow once** runs it; the **next** risky action asks again (no blanket grant — ADR-001
      caveat).
- [ ] **Allow for session** stops re-asking **that** tool; a **different** risky tool still asks
      (per-tool-name grant).
- [ ] **Deny** does not run the action; Claude adapts to the canned denial without
      crashing/hanging.
- [ ] A **read / search** runs with **no** prompt.
- [ ] **/yolo** runs everything with a loud **⚠️** marker; **/unyolo** restores prompting.
- [ ] **/cancel** aborts a waiting permission prompt cleanly and the session stays usable (RB4).
- [ ] A **non-allowlisted chat / forged tap** can never allow a tool (SB1).
- [ ] **/reset** clears all allow-session grants **and** turns `/yolo` off (a fresh, fail-closed
      session — D7).
- [ ] Throughout, `ENGINE_MODE=oneshot` remained the documented safe default (you flipped to
      `streaming` only for this check); **no** `--dangerously-skip-permissions` was introduced
      on the streaming path (SB5).

If anything fails, capture the message/screenshot and the bot log (token is redacted) and file
it against the permission-gating feature before flipping the live bot to streaming.
