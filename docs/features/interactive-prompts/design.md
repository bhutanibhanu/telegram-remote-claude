# Design: P3 — Interactive Prompts (run skills from the phone)

> **Feature slug:** `interactive-prompts` · **Pipeline:** P3 (the HEADLINE — "run `/grill` & `/pipeline`
> from Telegram"). Worktree `feat/interactive-prompts`, branched off P2's `feat/permission-gating`
> (tip `dd9375d`) — it carries the **full P1 + P2** code. Parents:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) §P3 ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md) (SB1) ·
> ADR-001 (C5: a slash-command skill is invocable in-session) · ADR-002 (answer-hold) · ADR-003 (gating).
>
> **The point of P3 is small on purpose.** P1 already shipped the interactive-tool **relay**
> (AskUserQuestion → buttons + "Other"; ExitPlanMode → Approve/Reject+feedback; free-text reply; the
> async answer-hold + backstop + `/cancel`; live coalesced rendering) and verified it live (T9). P2 added
> per-tool permission gating. **The only thing missing to "run `/grill` from the phone" is a way to LAUNCH
> a skill in the session** — today an unregistered slash-command is silently dropped. P3 adds that one path
> and **verifies the full loop composes** end-to-end. It reuses P1 + P2 wholesale; it does not rebuild them.

---

## Scoping decisions (confirmed at G-Scope, 2026-06-21)

| # | Decision | Value |
|---|---|---|
| **D1** | **Skill-launch mechanism** | **Passthrough.** Any slash-command that is NOT one of the bot's own commands (`/start /help /reset /cancel /pwd /cd /yolo /unyolo`) is forwarded **verbatim** (command + args) to the Claude session — so `/grill`, `/pipeline`, `/scaffold`, … "just work". The bot's own commands always take precedence. |
| **D2** | **Unknown / typo'd command** | **Forward, let Claude handle.** An unrecognized slash-command is sent to the session like any other; if it isn't a real skill, Claude just replies (and any risky tool it tries is still gated by P2). Low risk — single allowlisted operator. No allowlist to maintain. |
| **D3** | **Full-loop verify scope** | **`/grill` only.** Verify a complete `/grill` loop end-to-end from a real chat (multi-question → a real design doc); `/pipeline` is covered-by-mechanism (it reuses the identical relay + launch path). |

---

## The basics

**Elevator pitch.** Type `/grill` (or `/pipeline`, `/scaffold`, …) in Telegram and the bot runs that skill
**in the live Claude session** — its questions become buttons, its plans become Approve/Reject, its risky
tools pause for approval — all already built; P3 just lets you **start** it.

**The actual problem.** The headline workflow is "run `/grill` and `/pipeline` from the phone." P1 made the
bot **answer** a skill's interactive prompts, but there is **no way to start a skill**: `claude_tg/bot.py`
registers specific bot commands and its `MessageHandler` excludes `~filters.COMMAND`, so a slash-command
like `/grill` matches no handler and is **silently dropped**. The operator can chat with Claude but cannot
invoke a slash-command skill in the session.

**Who it's for.** The single allowlisted repo owner, driving the bot as their remote Claude Code.

**Definition of success (concrete).** From a real chat under `ENGINE_MODE=streaming`:
- Sending **`/grill`** (or any non-bot slash-command) **starts that skill in the Claude session**; its first
  AskUserQuestion renders as option buttons (P1), you answer by tapping, and the skill **proceeds**.
- A **complete `/grill` loop** runs to a **written design doc**, driven entirely from the phone (questions
  via buttons / "Other", any file writes gated by P2).
- The bot's **own commands still work and take precedence** (`/reset`, `/cd`, `/cancel`, `/yolo`, … are
  never shadowed by a skill of the same name).
- A non-allowlisted chat **cannot** launch a skill (SB1 holds on the new command surface).
- **One-shot remains the safe default**; existing behavior unchanged.

**Anti-goals (explicit non-features).**
- **NOT rebuilding** the relay / answer-hold / rendering (P1) or gating (P2) — P3 reuses them verbatim.
- **No skill *menu* / discovery UI**, no per-skill bot commands, no argument validation — passthrough is
  dumb-and-verbatim (D1/D2).
- **No multi-project / named sessions** (P4), **no concurrency** (P5), **no crash-recovery** (P4).
- **No new permission posture** — a launched skill's tools are gated exactly as P2 already does.

**Constraints.** Builds on P1 + P2 (this worktree has both). `claude-agent-sdk==0.2.105`, host CLI auth,
no API key. Single operator, single active session. SB1 applies to the new command surface.

---

## Requirements

### Functional — ranked
1. **Skill-launch passthrough.** A slash-command that is not a registered bot command is forwarded verbatim
   to the active session (streaming → `engine.send`; one-shot → `runner.run`), exactly like a normal
   message turn, so the skill is invoked in-session (ADR-001 C5). Bot commands take precedence (D1/D2).
2. **Full loop composes.** A launched `/grill` drives P1's relay (questions → buttons, "Other" → free-text)
   and P2's gating (risky tools → approval) with no new wiring — confirmed by the live verify (D3).
3. **SB1 on the new surface.** The passthrough is allowlist-guarded (same `_ok`/`allowed` filter as every
   other handler); a non-allowlisted chat cannot launch a skill.
4. **Discoverability.** `/help` notes that other slash-commands run as skills in the Claude session; an
   unknown command is forwarded (D2) rather than erroring.

### Non-functional
- **SB1** (the only cross-cutting item P3 newly touches): the launch path is a new inbound surface and must
  be allowlist-checked. **RB1** preserved (a malformed/empty command never crashes). Everything else
  (SB2–SB6, RB2/RB4/RB5) is inherited unchanged from P1/P2 — P3 adds no new risky surface beyond the launch.
- **Scale/latency:** single operator/session; a long multi-question `/grill` stresses rendering, but P1's
  coalescer + chunking already handle it (re-confirmed by the verify).

### Future
The passthrough is session-agnostic; P4 (multi-project) routes the same launch to the active project's
session — no rewrite. Nothing here blocks P4/P5.

---

## Architecture (delta from P1+P2)

**Current:** `bot.py` registers `CommandHandler`s for `/start /help /reset /cancel /pwd /cd /yolo /unyolo`
and a `MessageHandler(allowed & TEXT & ~COMMAND, on_message)`. A non-bot slash-command matches nothing →
dropped.

**P3 delta — one new handler in `bot.py`:**

```
Telegram ── /grill … ──▶ bot.py
   │   CommandHandler(/reset,/cd,/yolo,…)   ← bot commands (registered first → take precedence)
   │   MessageHandler(allowed & COMMAND)    ← NEW passthrough (registered AFTER the bot commands):
   │        on_skill_command → forward update.message.text VERBATIM to the same turn path as
   │        on_message (streaming → StreamingSession.handle_message → engine.send; oneshot → runner.run)
   ▼
 (then P1 relays the skill's ask/plan/text; P2 gates its risky tools — all unchanged)
```

- **`claude_tg/bot.py`** (delta) — a single `on_skill_command` handler registered for
  `allowed & filters.COMMAND` **after** the specific `CommandHandler`s (PTB first-match-wins, so a real bot
  command is handled by its own handler and never reaches the passthrough; only *unregistered* commands fall
  through). It reuses the existing turn path (the same code `on_message` calls) so the command text is sent
  to the session verbatim. SB1: same `_ok` recheck + `allowed` filter. `HELP_TEXT` gains a line. **No other
  production file changes** — the relay (`render.py`/`stream_session.py`/`engine`), gating (`permissions.py`),
  and one-shot runner are untouched.
- **No ADR.** Unlike ADR-002 (answer-hold) / ADR-003 (gating), the launch mechanism is a straightforward,
  fully-reversible wiring choice captured by D1–D3 here; a standalone ADR would be ceremony. (Add one only
  if the owner wants the trust-surface decision recorded separately.)

**Auth model (unchanged).** Secret token + chat-id allowlist; the new command surface is allowlist-guarded
exactly like messages and bot commands (SB1).

---

## Risks & open questions

**Top risks**
1. *(Security)* **The passthrough forwards an arbitrary command into the live session.** *Mitigation:* it is
   **allowlist-guarded** (SB1 — only the operator can reach it), and any tool the launched skill attempts is
   **gated by P2**; an unknown command is just a prompt Claude answers. The new surface adds no capability
   the operator didn't already have by typing a message. Document the **reserved** bot-command names.
2. *(Product)* **A skill name could collide with a future bot command.** *Mitigation:* bot commands are
   registered first and take precedence (D1); the reserved set is documented; adding a bot command later
   simply shadows a same-named skill (acceptable, documented).
3. *(Technical)* **A long multi-question `/grill` could stress rendering** (many ask round-trips, big plans).
   *Mitigation:* P1's coalescer + chunking already handle bursts/long output; the live verify (D3) exercises
   a real multi-question loop to confirm.

**Open questions (resolve in build)**
- PTB handler ordering: confirm a `MessageHandler(filters.COMMAND)` registered after the `CommandHandler`s
  only fires for *unregistered* commands (first-match-wins within the group) — settle with a test.
- Whether to strip the leading `/` or forward it verbatim — forward **verbatim** (the session interprets the
  slash-command); confirm in the live verify.

**ADRs to write before code:** none (see Architecture).

---

## SDLC plan (delta)
None — same GitHub Actions CI (tests + lint + type-check + secret-scan), substrate-mocked unit tests, a live
verify probe (not in CI), supervised-autonomous Implementer+reviewer build. The **342** P1+P2 tests are the
regression floor.

---

## Roadmap

**In scope (P3):** the skill-launch passthrough (`bot.py`) + SB1/RB1 tests → a live `/grill` end-to-end
verify + owner phone-verify checklist.

**Out of scope (deferred):** skill discovery/menu UI, per-skill commands, argument validation; multi-project
(P4); concurrency (P5); crash-recovery (P4); anything already delivered by P1 (relay) / P2 (gating).

**Expected build order (input to `/plan`):**
1. **Skill-launch passthrough** — `on_skill_command` in `bot.py` (forward unregistered slash-commands verbatim
   to the active turn path; bot commands take precedence; SB1-guarded; `/help` updated). Unit tests:
   passthrough forwards to the session; a bot command is NOT intercepted by the passthrough; a non-allowlisted
   command is ignored (SB1); empty/garbage command never crashes (RB1); one-shot + streaming both route.
2. **Live `/grill` end-to-end verify + owner checklist** — a programmatic launch-smoke (the passthrough
   reaches `engine.send`) plus a real `/grill` loop run from a chat to a design doc; `verify.md` phone
   checklist (launch a skill, answer its questions via buttons, see it finish; confirm bot commands still
   win; confirm a non-allowlisted chat can't launch). Contained + scrubbed; no API key.
