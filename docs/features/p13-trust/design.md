# P13 — Trust Layer for Power (design)

_Roadmap-v2 phase 5 ("Trust layer for power"). A **delta** on the shipped, security-audited
P0–P12 tree (`main` @ `f7c1cda`); worktree `feat/p13-trust`. **Built** — T-AUDIT (`0640130`) +
T-BASH (`7fb92cd`); in cross-model QA (Codex round-1 NO_SHIP → 2 blockers fixed: the durable
audit log now uses a STRICTER body-free summary that collapses idents too, and a malformed
`BASH_POLICY_EXTRA_PATTERNS` regex now fails loud at config load). Pins **ADR-003** (permission
gating) and the **P6/C2** "Bash documented-unconfined" finding ([`docs/features/p6-security-audit/findings.md`](../p6-security-audit/findings.md))._

---

## Problem

The owner wants to grant the bot **more autonomy with confidence**. Two gaps stand between today's
posture and that:

1. **No durable audit trail.** Today every security-relevant decision (tool approve/deny, plan
   approve/reject, `/yolo`, attach/watch) happens in-memory and is gone the moment it scrolls off
   the chat. Logging is `logging`-only (INFO/DEBUG to stderr), body-free (SB3, H1-remediated), and
   **not durable** — there is no append-only record of "what did Claude actually run, and what did
   I approve, and when." If the owner is going to lean on this bot, they need a reviewable history.

2. **Bash is the one documented-UNCONFINED surface.** P6/C2 confined the SDK's *file/search* tools
   (`Read`/`Write`/`Edit`/`Glob`/`Grep`/`LS`/`Notebook*`) to `ALLOWED_ROOTS` via
   `permissions.path_needs_approval`. But **`Bash` was explicitly left out** — "an arbitrary shell
   command has no reliable static target, so `Bash` is deliberately NOT path-parsed" (findings.md
   C2). `Bash` gates as RISKY by name (prompts unless granted/`/yolo`), but a session-**granted**
   `Bash` is then **unconfined** — it can `rm -rf`, force-push, `curl … | sh`, read `~/.ssh`, etc.
   The roadmap calls this out as P13's job: *"Bash destructive-command policy — re-confirm `rm -rf`
   / force-push / `curl|sh` even when granted (closes the documented C2 residual)."*

This is a **security-core** phase (roadmap risk note). The bar: **never weaken the existing gate**,
keep the **safe default = current behavior**, fail **closed**, stay **body-free (SB3)**.

## Success

- A durable, append-only, body-free **audit log** of security-relevant events, written atomically
  with `0600` perms (the session-store discipline), size-bounded, with a phone-reviewable `/audit`
  command (SB1, body-free render). An audit-write failure **never** breaks a turn (RB1, best-effort).
- A configurable **Bash command policy** layered on the existing approval gate: a denylist of
  dangerous patterns that, when matched, **escalates** the existing per-tool prompt (a `⚠️`-flagged
  prompt that can never auto-allow — even a session grant or `/yolo` is overridden to a one-time
  re-confirm for a matched command), with an **optional** strict auto-deny mode. **Fail-closed:** a
  policy error or an ambiguous match escalates/denies, never silent-allows.
- The safe **DEFAULT is unchanged behavior**: audit defaults **on** but non-breaking (it only
  *adds* a log file); the Bash policy ships with a **sensible built-in denylist in FLAG mode** that
  only ever *adds friction* to commands that are already RISKY and already prompt — it can never
  make something auto-run that doesn't today. Both are env-tunable; both can be fully disabled.

## Anti-goals

- **NOT** a sandbox / OS-level confinement for Bash. We cannot reliably statically parse an
  arbitrary shell command's filesystem reach (the honest C2 boundary stands). The Bash policy is a
  **pattern guardrail on the approval UX**, not a containment mechanism. We state this limit plainly.
- **NOT** the other P13 roadmap bullets. P13's roadmap line also lists *proactive owner-alerts*,
  *cost budget + alerts*, *resource limits*, *state backup/restore*. **Scope decision (below): this
  phase ships ONLY the audit trail + the Bash policy** — the two the owner's vision statement names
  ("(1) an AUDIT TRAIL … and (2) a BASH POLICY"). The rest are deferred to a P13.5 / P14.
- **NOT** free-text denial reasons, per-resource grant granularity, or any change to the ask/plan
  answer-hold. Those remain as ADR-003 left them.
- **NOT** a change to the C1 default-gate posture or `ENGINE_MODE` default (oneshot stays oneshot).

---

## Scope decision (the one judgment call)

The roadmap's P13 bullet lists four things; the owner's vision statement names **two**. I am
scoping P13 to **exactly the two named** — audit trail + Bash policy — and explicitly **deferring**
proactive owner-alerts, cost budget/limits, and state backup/restore to a follow-up.

**Rationale:** (a) the two named items are the load-bearing "trust" primitives — a record of what
happened + a guardrail on the most dangerous surface; (b) they are cohesive (both hook the same
approval gate / decision chokepoint) and reviewable as one security delta; (c) the deferred items
are larger and orthogonal (alerts need a wedge/restart-detection surface; budgets need a cost-cap
enforcement loop; backup/restore is its own reliability feature) — bundling them would bloat a
security-core phase and dilute review. This keeps P13 **shippable and grounded**. Flagged here for
orchestrator review — if the owner wants alerts folded in, that is a clean additive task on top.

---

## Current-state findings (what exists today)

### The approval gate (where decisions happen) — the audit hook point
`Engine.on_tool_request` ([`claude_tg/engine/engine.py:146`](../../../claude_tg/engine/engine.py))
is the single chokepoint for every tool/interactive request. Its branches:
- **ask/plan** (`AskUserQuestion`/`ExitPlanMode`) → `_answer_hold` (answered, not gated).
- **policy allows** (safe tool / live grant / `/yolo`) → `decision_to_substrate(allow)` with **no
  prompt** (line 199-203).
- **out-of-root path** (C2/SB2, not `/yolo`) → hold for approval (line 190-198).
- **risky + not granted** → `_permission_hold` (line 221): injects a `PermissionEvent` (body-free
  `safe_input_summary`, SB3) and awaits the operator's `PermissionDecision`.
- **risky + no `tool_use_id`** → fail-closed **deny** (line 210-219).

The **verdict is finalized** in `_permission_hold` / `_verdict_for`
([engine.py:263-340](../../../claude_tg/engine/engine.py)): `allow_session` records the grant then
allows; `allow_once` allows; `deny`/backstop/cancel → deny. **This method is the natural
engine-side audit hook** — it sees the tool name, the body-free summary, the resolved verdict, and
the session id, all in one place, for *every* gated tool.

The **decision arrives** from the bot via `engine.resolve(tool_use_id, decision)`
([engine.py:344](../../../claude_tg/engine/engine.py)) → `PendingRegistry.resolve`
([pending.py:146](../../../claude_tg/engine/pending.py)). The bot-side chokepoint is
`StreamingSession.resolve_callback` → `_resolve_permission`
([`claude_tg/stream_session.py:~3875`](../../../claude_tg/stream_session.py)), which calls
`engine.resolve(...)` for permission taps (and the sibling paths for ask/plan/free-text).

**Decision: hook the audit in the engine (`_permission_hold` + the auto-allow branch of
`on_tool_request`), not the bot.** The engine is the single substrate-neutral chokepoint that sees
*every* outcome — auto-allows (safe/grant/yolo, which never reach the bot), out-of-root holds, and
resolved verdicts — whereas the bot only sees the taps it routes. Hooking the engine means a tool
that auto-runs under `/yolo` is still audited; hooking the bot would miss it. The engine already
holds `safe_input_summary` and the session id, so the hook needs no new data plumbing.

### How a Bash command is seen + summarized
- The adapter's `can_use_tool` ([adapter_sdk.py:448](../../../claude_tg/engine/adapter_sdk.py))
  bridges the SDK to `Engine.on_tool_request`, passing `tool_name`, the raw `tool_input` dict
  (`{"command": "...", "description": "..."}` for Bash), and `tool_use_id`.
- `safe_input_summary` ([types.py:218](../../../claude_tg/engine/types.py)) renders the prompt
  summary. **Verified probe:** for `Bash`, the `command` field is in `_IDENT_FIELDS` → **truncated
  at 160 chars, NOT collapsed to a length**. So the permission prompt **already shows the command
  text** (up to 160 chars): `Bash(command=rm -rf /tmp/foo)`. This is load-bearing for the policy
  design — the operator already sees the command, so "flag louder" is cheap and honest.
  - Caveat: at 160 chars a long command is truncated; the policy must scan the **raw**
    `tool_input["command"]`, not the truncated summary.
- The permission prompt render
  (`render._render_permission_body` / `permission_keyboard`,
  [`claude_tg/render.py:~1888`](../../../claude_tg/render.py)) shows
  `🔐 Permission needed — Claude wants to run {tool_name}:\n{tool_input_summary}` + an
  `[Allow once] / [Allow for session] / [Deny]` keyboard. **The Bash policy adds a `⚠️` warning
  line + (in flag mode) drops `[Allow for session]` for a flagged command** so the only allow is a
  deliberate one-time tap.

### Existing logging / audit — what's logged today
`grep` across `claude_tg/` confirms **no durable audit exists**. Logging is `logging.getLogger`
to stderr, configured in `app.py` (`basicConfig(level=INFO)`, httpx/httpcore quieted). It is
**body-free + secret-free** (SB3, H1-remediated 8428ce1):
- session ids go through `_redact_sid` (engine.py:374, 396) — never raw in logs.
- tool inputs are summarized via `safe_input_summary` (lengths/truncated idents, never bodies).
- images: `log.info("… received an image (%d KB)")` — size only (types.py `ImageInput.__repr__`
  elides the base64).
- raw external error bodies render body-free; secret_scan gates committed files.

So today's "audit" is **ephemeral stderr**. P13 adds the **durable, structured, reviewable** layer
— reusing the exact body-free discipline already proven.

### The persistence pattern to reuse (atomic + 0600)
`JsonSessionStore._save_raw` ([session_store.py:140-155](../../../claude_tg/session_store.py)):
`mkdir -p` parent → write `<name>.tmp` → best-effort `chmod 0o600` → `tmp.replace(target)` (atomic
on same fs). Corrupt/missing/unknown-version reads fail safe to empty, never raise (SB6/RB1/RB6).
The store path is `config.state_file` (`CLAUDE_STATE_FILE`); the audit log lives **next to it**
(e.g. `<state_file>.audit.jsonl`, or `AUDIT_LOG_FILE` override) so it inherits the same dir + perms
posture. **An append-only JSONL log is a better fit than the JSON-document store** (audit is
append-heavy, tail-read; rewriting a growing JSON doc every event is wasteful) — but it uses the
**same atomic + 0600 discipline** (append via a 0600-opened handle; rotate by size).

### Config knobs pattern
`config.py` `Config` dataclass + `from_env` + per-knob `parse_*` validators (fail loud on a bad
value, default on unset). P13 adds knobs the same way (below), all defaulting to the
**non-breaking** value.

### Command + SB1 pattern (for `/audit`)
- `_authorized`/`_ok` ([bot.py:297-306](../../../claude_tg/bot.py)) gate every handler; commands are
  also registered with `filters.Chat(chat_id=list(allowed))`; callbacks re-check `_authorized`
  inside `on_callback` (defense-in-depth).
- `COMMAND_MENU` ([bot.py:113](../../../claude_tg/bot.py)) + `HELP_TEXT`
  ([bot.py:55](../../../claude_tg/bot.py)) + `set_my_commands` registration — must stay in
  lock-step. `/audit` adds one row to each.
- Read-only template: `cmd_macros` ([bot.py:1411](../../../claude_tg/bot.py)) — `_ok` guard →
  `chat_id = update.effective_chat.id` → read store → HTML-escape all content → reply. `/audit`
  follows this verbatim.

### Inherited gates / constraints
- Gates (worktree `.venv`): `pytest`, `ruff check .`, `mypy claude_tg`, `python
  scripts/secret_scan.py`. **Regression floor: 1288 tests** (collected on `f7c1cda`).
- `secret_scan` blocks committed credential-shaped strings — **any committed audit-log sample /
  test fixture must be body-free + secret-free** or the gate trips.
- SDK pinned (`claude-agent-sdk==0.2.105`); one bot per token (live-verify is single-instance).
- Clean single-line commit subjects, **NO `Co-Authored-By`** trailer.

---

## Design

### Part 1 — Audit trail

#### 1.1 What is audited (security-relevant events only — body-free)
A fixed, small set of event kinds. Each record carries **only** non-sensitive fields:

| event | fields (all body-free) |
|---|---|
| `tool_decision` | `tool_name`, `summary` (= `safe_input_summary`, already body-free), `verdict` (`allow_once`/`allow_session`/`auto_allow`/`deny`/`backstop_deny`/`cancel`), `bash_flagged` (bool), `chat_id`, `session_tag` (redacted), `ts` |
| `plan_decision` | `verdict` (`approve`/`reject`), `chat_id`, `session_tag`, `ts` (NO plan text, NO feedback text) |
| `session_event` | `action` (`attach`/`watch`/`unwatch`/`reset`/`switch`), `chat_id`, `session_tag`, `ts` |
| `policy_event` | `action` (`yolo_on`/`yolo_off`/`bash_policy_block`/`bash_policy_flag`), `chat_id`, `session_tag`, `ts` |

**SB3 is structural, not just a convention:** the record is built **only** from values that are
*already* body-free — `safe_input_summary` (lengths/truncated idents, the same string the prompt
shows) and the redacted session tag (`_redact_sid`). The raw `tool_input`, the plan text, plan
feedback, command output, file contents, and the raw session id are **never** passed to the audit
writer. The writer's input type should be a small frozen dataclass (`AuditEvent`) whose fields are
all already-safe strings — so a raw body cannot be logged "by mistake" (mirrors how `ThinkingEvent`
has no `signature` field and `ImageInput.__repr__` elides base64). A unit test asserts a `Write`
with secret `content` and a `Bash` with a token in the command produce a record that contains
**neither** the content nor the token (and the committed fixture is secret-free so `secret_scan`
stays green).

#### 1.2 Where it's written (durable, append-only, atomic, 0600, bounded)
- A new module `claude_tg/audit.py` — a pure-ish `AuditLog` class (no telegram, no SDK), mirroring
  `session_store.py`'s isolation. Holds the path; exposes `append(event: AuditEvent)` and
  `tail(n: int) -> list[AuditEvent]`.
- **Format: append-only JSONL** (`one JSON object per line`). Append is O(1) and tail-read is cheap
  — the right shape for a log (vs. the rewrite-the-whole-doc JSON store).
- **Perms:** the file is created `0600` (open with `os.open(..., O_CREAT|O_WRONLY|O_APPEND, 0o600)`
  or create-then-`chmod 0o600` before first write), parent `mkdir -p`. Inherits the state-file dir.
- **Atomicity:** each `append` writes one line and `flush()`+`fsync`-best-effort. A single
  `write()` of one line under the OS append mode is atomic enough for a local single-writer log
  (one bot per token; single asyncio loop). We are **not** doing cross-process concurrent appends.
- **Size bound / rotation:** before append, if the file exceeds `AUDIT_LOG_MAX_BYTES` (default e.g.
  5 MB), rotate once to `<file>.1` (replace any prior `.1`) and start fresh — a simple 1-keep
  rotation, bounded disk. No unbounded growth.
- **Path:** default `= <state_file>.audit.jsonl` when `CLAUDE_STATE_FILE` is set; else (no state
  file — e.g. a stateless oneshot deploy) audit is **off** unless `AUDIT_LOG_FILE` is set explicitly.
  Overridable via `AUDIT_LOG_FILE`.

#### 1.3 RB1 — a write failure NEVER breaks a turn
Every `append` is **best-effort**: wrapped so that *any* exception (disk full, perms, bad path) is
caught, logged once at WARNING (body-free), and swallowed. The turn continues. This mirrors
`_persist` (stream_session.py) and `add_cost` (session_store.py) — "never wedge a turn over a
write." The engine hook calls `audit.append(...)` and ignores failures; the audit log is an
observer, never on the critical path.

#### 1.4 The engine hook
- The `Engine` gains an **optional** `audit_sink: AuditSink | None = None` constructor param
  (default `None` → a no-op, so every existing `Engine(...)` construction and all 1288 tests are
  unchanged — same pattern as the optional `cwd`/`allowed_roots` C2 params).
- `AuditSink` is a tiny protocol: `record(event: AuditEvent) -> None` (best-effort). Production
  passes an adapter over `AuditLog`; tests pass a fake list-sink or `None`.
- **Hook sites** (all inside `on_tool_request` / `_permission_hold`, the one chokepoint):
  - auto-allow branch (safe/grant/`/yolo`) → `record(tool_decision, verdict="auto_allow")`.
  - resolved verdict in `_permission_hold` → `record(tool_decision, verdict=<the resolved verdict>)`
    (allow_once/allow_session/deny/backstop_deny/cancel).
  - fail-closed no-`tool_use_id` deny → `record(tool_decision, verdict="deny")`.
- **chat_id** is not known to the substrate-neutral engine. Thread it in the same way `cwd` is — the
  `_default_engine_factory` (stream_session.py:210) already receives per-project context; add an
  `audit_sink` kwarg bound (like `allowed_roots`) that **closes over the chat_id** (the factory is
  built per chat/project). So the engine calls `record(...)` with no chat_id and the bound sink
  stamps the chat_id + session_tag. Keeps the engine substrate-neutral.
- **plan / session / policy events** are recorded from the **bot/session** side (they are not all
  visible to the engine): `resolve_callback`/`_resolve_permission` for plan verdicts; the
  `/attach`/`/watch`/`/reset`/`/switch` and `/yolo` handlers for session/policy events. These call
  the same `AuditLog.append` directly (the bot already has the chat_id + store dir). Two call paths,
  **one** writer + one record schema.

#### 1.5 `/audit` command (SB1, body-free render)
- New `cmd_audit` on `TelegramBot`, mirroring `cmd_macros`: `_ok` guard → `chat_id` →
  `AuditLog.tail(n)` (default last ~20, optional `/audit <n>` capped at e.g. 100) → render each
  record as a compact body-free line, HTML-escaped, `parse_mode="HTML"`, paths wrapped via the
  existing `code_path` helper.
- Render is **body-free by construction** (it only has the already-safe record fields). Example
  line: `12:03 ✅ allow_once Bash(command=npm test) ⚠️flagged`.
- **Filtered to the requesting chat_id** (the record carries `chat_id`; `/audit` shows only this
  chat's events) — consistent with per-chat isolation.
- One row in `COMMAND_MENU` + `HELP_TEXT` + `set_my_commands` (lock-step). Marked read-only.
- Empty log → a clean "no audit events yet" reply (never an error).

### Part 2 — Bash command policy

#### 2.1 Recommendation: FLAG (escalate the prompt), default-on; AUTO-DENY optional
**Recommend: a built-in denylist in FLAG mode by default; AUTO-DENY behind an opt-in strict mode.**

Rationale (the owner's question — auto-deny vs flag):
- **Auto-deny risks false positives that block real work.** Shell patterns are heuristic; `rm -rf
  ./build`, a legitimate `git push --force-with-lease` to a feature branch, or a vetted
  `curl … | sh` installer are all "dangerous-shaped" but sometimes exactly what the operator wants.
  A hard auto-deny would force the operator to disable the policy wholesale (or `/yolo`) to get past
  a false positive — which is **worse** for safety than a loud prompt, because it trains the
  operator to reach for the bypass.
- **Flagging keeps the human in the loop on the existing per-tool gate** — which is the whole point
  of the trust layer. The command text is **already shown** in the prompt (verified: `command`
  truncates at 160, not collapsed), so flagging is cheap and honest: add a `⚠️` + the matched
  pattern name, and **remove `[Allow for session]`** for a flagged command so the only way through
  is a **deliberate one-time `[Allow once]`**. This is strictly safer than today (today a granted
  `Bash` auto-runs; flagged-mode forces a fresh confirm) and never blocks a genuine need.
- **The escalation also overrides a live `Bash` session-grant AND `/yolo`** for a *matched*
  command: if `Bash` is session-granted (or yolo is on) and Claude runs `rm -rf /`, the policy
  **re-prompts** (flag mode) or **denies** (strict mode) instead of silently auto-allowing. This is
  the concrete closure of the C2 residual ("re-confirm `rm -rf` even when granted").
- **AUTO-DENY is available** for owners who want a hard wall (`BASH_POLICY_MODE=deny`): a matched
  command is denied outright with the canned `DENIED_MESSAGE` (the model adapts), and a
  `policy_event(bash_policy_block)` is audited. Off by default.

So the modes (`BASH_POLICY_MODE`, default `flag`):
- `flag` (default): matched command → **escalated prompt** (`⚠️`, no allow-session, one-time only),
  overriding any grant/yolo for that command. Audited `bash_policy_flag`.
- `deny` (strict opt-in): matched command → **auto-deny**, overriding grant/yolo. Audited
  `bash_policy_block`.
- `off`: policy disabled entirely → **exactly today's behavior** (Bash gates by name only).

**Every mode FAILS CLOSED:** if the matcher raises, or the command is unparseable / ambiguous, the
result is **escalate** (flag mode) or **deny** (strict mode) — **never** silent-allow. A policy bug
errs toward friction, never toward exposure (the ADR-003 / SB6 invariant).

#### 2.2 The denylist (heuristic, configurable, conservative)
- A pure module `claude_tg/bash_policy.py` (no telegram/SDK/engine) — mirrors `permissions.py`'s
  isolation. Exposes `classify_bash(command: str) -> BashPolicyHit | None` (a hit = the matched
  pattern's name + severity), pure + unit-testable.
- A **small, conservative built-in denylist** of high-signal destructive shapes — examples (final
  list pinned in `/plan`):
  - `rm -rf` / `rm -fr` targeting `/`, `~`, `$HOME`, or with `--no-preserve-root`.
  - `git push --force` / `-f` (note: `--force-with-lease` MAY be treated as lower severity / not
    flagged — decide in build; conservative default = flag both).
  - pipe-to-shell installers: `curl … | sh`/`| bash`, `wget … | sh` (arbitrary remote code exec).
  - disk/format: `mkfs`, `dd of=/dev/…`, `> /dev/sd…`.
  - fork bombs / `:(){ :|:& };:`.
  - `chmod -R 777` / `chmod 777 /`, `chown -R` on system roots.
  - reads of obvious secret stores: `cat ~/.ssh/id_*`, `.aws/credentials`, `.env` exfil shapes
    (lower severity — flag, since reading is less destructive than `rm`).
- **Matching is on the raw `tool_input["command"]` string** (not the truncated summary). It is
  **substring/regex pattern matching**, explicitly NOT a shell parser — we do not attempt to defeat
  obfuscation (base64-decode-then-eval, `$IFS` tricks, etc.). **Stated limit:** this is a guardrail
  against *accidental* and *obvious* destructive commands, **not** a defense against a determined
  adversarial Claude (which can obfuscate). It raises the floor; it is not a sandbox. The honest C2
  boundary (Bash is not statically confinable) **still stands** and is restated.
- The built-in list is **the default**; `BASH_POLICY_EXTRA_PATTERNS` (env) lets the owner add
  patterns; the built-ins cannot be silently removed (removing a safety pattern should require a
  code change, not an env var — fail-safe). `BASH_POLICY_MODE=off` is the documented full disable.

#### 2.3 Where the policy is wired (into the existing gate — never weakening it)
- The Bash policy is consulted **inside `Engine.on_tool_request`**, layered **on top of** the
  existing gate — it can only **add** a prompt/deny, never remove one. The ordering (extending the
  current `on_tool_request` ordering, which is `/yolo` → C2 path → name-only):
  1. Compute `bash_hit = bash_policy.classify(tool_input["command"])` **iff** `tool_name == "Bash"`
     and the policy is enabled. (A non-Bash tool is untouched — zero behavior change.)
  2. **If `bash_hit` and mode == `deny`** → **deny now** (override grant/yolo), audit
     `bash_policy_block`. (This is checked BEFORE the `/yolo` short-circuit so strict mode genuinely
     overrides yolo — the one place policy beats yolo, by design, because the owner opted into a hard
     wall.)
  3. **If `bash_hit` and mode == `flag`** → force a **hold for approval** (a `PermissionEvent` with
     `bash_flag=True`), overriding the auto-allow that a grant/yolo would otherwise give for *this
     command*; the prompt renders the `⚠️` + matched-pattern line and **omits `[Allow for session]`**;
     audit `bash_policy_flag`. The verdict still flows through the normal hold → `engine.resolve`
     path (allow_once/deny), so the operator stays in control.
  4. **No `bash_hit`** (or non-Bash, or policy off) → **today's exact behavior** (yolo → C2 → name).
- This is **additive**: with the policy `off`, step 1 is skipped and the function is byte-for-byte
  the current logic. With it on, the only change is *more* friction on *matched* commands — the gate
  is never loosened. A unit test pins "policy never turns a would-prompt into an auto-allow."
- **`PermissionEvent` gains an optional `bash_flag: bool = False`** (+ optional matched-pattern
  name) so the renderer can show the warning and drop the session button. Default `False` keeps
  every existing event + render unchanged (SB3 still holds — the pattern *name* is body-free, e.g.
  `"force-push"`, never the command body beyond the existing 160-char summary).

#### 2.4 Config knobs (all default to non-breaking)
| env | default | meaning |
|---|---|---|
| `AUDIT_LOG_FILE` | `<CLAUDE_STATE_FILE>.audit.jsonl` (or unset→off if no state file) | audit log path; `""`/`off` → disable audit |
| `AUDIT_LOG_MAX_BYTES` | ~5 MB | rotate-once size bound (positive int; fail loud on bad value) |
| `BASH_POLICY_MODE` | `flag` | `flag` \| `deny` \| `off` (validated; bad value → fail loud at startup) |
| `BASH_POLICY_EXTRA_PATTERNS` | `""` | extra denylist patterns (newline/`;`-separated); additive to built-ins |

**On the "default" question:** audit-on and bash-policy-flag-on are **non-breaking** — audit only
*adds* a file; flag mode only *adds friction to already-RISKY commands that already prompt*. Neither
can make anything auto-run that doesn't today, and neither weakens the gate, so defaulting them
**on** is consistent with "safe default = current behavior" while delivering the trust value
out-of-the-box. Both are one env var away from fully off. (If the orchestrator/owner prefers the
absolute-minimal-surprise reading — "default = literally identical behavior" — flip `BASH_POLICY_MODE`
default to `off`; I recommend `flag` because it is strictly-safer-and-non-blocking, but this is a
one-line owner call and I flag it explicitly.)

---

## Safety / robustness rules (stated, each gets a test — RB7)

- **SB1** — `/audit` and any new command gate on `_ok`/`_authorized`; no new callback surface is
  added (the Bash-flag prompt reuses the existing permission keyboard, already SB1-checked in
  `on_callback`). Test: `/audit` from a non-allowlisted chat is ignored.
- **SB3 (body-free, the load-bearing one)** — the audit record is built **only** from already-safe
  fields (`safe_input_summary`, redacted session tag); raw tool input / plan text / feedback /
  output / file contents / raw session id are **never** written. The `AuditEvent` type has no field
  that can carry a body. Tests: (a) a `Write` with secret `content` → record has no content; (b) a
  `Bash` with a token in the command → the record's `summary` is the same 160-char-truncated,
  body-free string the prompt shows and contains no extra body; (c) **any committed sample/fixture
  is secret-free so `scripts/secret_scan.py` stays green.**
- **Audit file is 0600 + atomic-append** — created `0600`, parent `mkdir -p`, single-line appends.
  Test: the created file's mode is `0600`; a write produces a parseable JSONL line.
- **Bash policy FAILS CLOSED** — a matcher exception, or an unparseable/ambiguous command, →
  escalate (flag) or deny (strict), never auto-allow. Tests: a classifier that raises → the gate
  holds/denies; a flagged command under a live `Bash` session-grant still re-prompts (flag) or
  denies (strict); a flagged command under `/yolo` still re-prompts (flag) or denies (strict).
- **RB1** — an audit-write failure (disk full / perms / bad path) is caught + swallowed; the turn
  completes normally. Test: an `AuditLog` whose path is unwritable → `append` does not raise and the
  turn's events still flow.
- **Safe DEFAULT = current behavior** — with `BASH_POLICY_MODE=off` the gate is byte-for-byte today;
  with the policy on it can only *add* friction, never auto-allow. Test: policy never converts a
  would-prompt into an auto-allow; a non-Bash tool is unaffected by the policy.
- **Never weaken the gate** — the policy is consulted *additively* in `on_tool_request`; the C2 path
  layer, the name-only classifier, and the fail-closed no-`tool_use_id` deny are all unchanged. Test:
  the existing permission/path/yolo test matrix stays green (regression floor 1288).
- **C2 pin** — the design **restates** that Bash is not statically confinable (the honest C2
  boundary); the policy is a UX guardrail against obvious/accidental destruction, not a sandbox, and
  does not claim otherwise. Cites `findings.md` C2 + ADR-003.

---

## Task breakdown (build order — foundational/cheap first; each: acceptance + what to test)

> Inherited constraints on every task: gates `pytest` / `ruff check .` / `mypy claude_tg` /
> `python scripts/secret_scan.py` green; regression floor **1288** tests; SDK pinned; one bot per
> token; clean single-line commit subjects, **NO `Co-Authored-By`**. RB7: each SB/RB rule above is
> pinned by a dedicated test in the task that introduces it.

**T1 — Audit core (`claude_tg/audit.py`): `AuditEvent` + `AuditLog` + `AuditSink`.** _(foundational,
pure, no wiring)_
- The frozen `AuditEvent` dataclass (body-free fields only); `AuditLog` (path, `append`, `tail`,
  0600 create, size-bounded 1-keep rotation, JSONL); `AuditSink` protocol + a `ChatBoundSink`
  adapter that stamps chat_id + redacted session_tag.
- **Acceptance:** `append` creates a `0600` JSONL file (parent `mkdir -p`), one parseable line per
  event; `tail(n)` returns the last n parsed; a write failure is swallowed (no raise); rotation
  fires at the size bound keeping exactly one `.1`. `AuditEvent` has no body-bearing field.
- **Test:** unit — 0600 mode; round-trip append/tail; **secret-not-leaked** (a record built from a
  `Write`-with-secret summary contains no content); unwritable-path `append` does not raise (RB1);
  rotation bound. Committed fixtures secret-free (secret_scan green).

**T2 — Config knobs (`config.py`): `AUDIT_LOG_FILE` / `AUDIT_LOG_MAX_BYTES` / `BASH_POLICY_MODE` /
`BASH_POLICY_EXTRA_PATTERNS`.** _(cheap, isolated; defaults non-breaking)_
- `parse_*` validators (fail loud on bad value, default on unset); `Config` fields + `from_env`
  wiring; default audit path derived from `state_file`.
- **Acceptance:** unset → non-breaking defaults (audit path from state_file or off; mode `flag`); a
  bad `BASH_POLICY_MODE` / non-positive `AUDIT_LOG_MAX_BYTES` raises at startup; `off`/`""` disable.
- **Test:** parser unit tests (mirror existing `parse_*` tests) — valid/empty/invalid for each knob.

**T3 — Engine audit hook (`engine.py`): optional `audit_sink`, recorded at the gate.** _(wires T1
into the chokepoint)_
- Add `audit_sink: AuditSink | None = None` to `Engine.__init__` (default None → no-op); call
  `record(...)` in the auto-allow branch, the resolved-verdict path (`_permission_hold`), and the
  fail-closed deny. No chat_id in the engine — the bound sink stamps it.
- **Acceptance:** every gate outcome (auto_allow / allow_once / allow_session / deny / backstop_deny
  / cancel) produces exactly one `tool_decision` record via the sink; `audit_sink=None` → no calls,
  behavior identical (1288 floor holds).
- **Test:** unit with a fake list-sink — drive each verdict through `on_tool_request`/`resolve`,
  assert one record with the right verdict + body-free summary; assert `None` sink is a no-op.

**T4 — Wire the sink in production + `/attach`/`/watch`/`/reset`/`/yolo` + plan events.**
_(stream_session + bot; the bot-side records)_
- `_default_engine_factory` / `_bound_factory` (stream_session.py) build a `ChatBoundSink` over the
  process `AuditLog` (one log instance, dir from `config.state_file`) and pass it to `Engine(...)`;
  `App`/`StreamingSession` construct the single `AuditLog` from config. Record `plan_decision` in
  `resolve_callback`/`_resolve_permission`/free-text-reject; `session_event`/`policy_event` in the
  `/attach`·`/watch`·`/unwatch`·`/reset`·`/switch`·`/yolo`·`/unyolo` handlers.
- **Acceptance:** a live-ish turn (mocked substrate) that approves a tool, approves/rejects a plan,
  and toggles `/yolo` produces the matching records (with chat_id + redacted session_tag) in the log
  file; audit-disabled config → no file, no errors.
- **Test:** integration with the mocked substrate + a temp audit path — assert records appear with
  correct chat_id/verdict; assert a write failure does not break the turn (RB1).

**T5 — `/audit` command (bot.py): body-free tail view.** _(read-only surface)_
- `cmd_audit` (mirror `cmd_macros`): `_ok` → `chat_id` → `AuditLog.tail(n)` filtered to this chat →
  compact body-free HTML lines; `/audit <n>` capped; empty → clean message. `COMMAND_MENU` +
  `HELP_TEXT` + `set_my_commands` rows (lock-step).
- **Acceptance:** `/audit` shows the recent tail for the chat, body-free, HTML-safe; non-allowlisted
  chat ignored (SB1); empty log → clean reply; `/audit 50` bounded.
- **Test:** handler unit (mirror macros tests) — SB1 reject; render of a few records is body-free +
  HTML-escaped; bound on n; empty case.

**T6 — Bash policy core (`claude_tg/bash_policy.py`): `classify_bash` + the built-in denylist.**
_(pure, no wiring — like T1)_
- Pure `classify_bash(command, extra_patterns=()) -> BashPolicyHit | None`; the conservative
  built-in pattern set; matches the **raw** command; fail-closed contract documented (the caller
  treats a raise as a hit).
- **Acceptance:** each built-in dangerous shape matches (`rm -rf /`, `curl|sh`, force-push, `mkfs`,
  fork bomb, `chmod -R 777 /`, secret-store reads); benign commands (`ls`, `npm test`, `git status`)
  do **not** match; extra patterns are additive; pure + import-clean (no telegram/SDK).
- **Test:** table-driven match/no-match unit tests incl. tricky-but-benign (`rm -rf ./build` policy
  decision pinned), false-positive guards, extra-pattern additivity.

**T7 — Wire the Bash policy into the gate (engine.py + render.py + PermissionEvent).** _(the gate
delta — additive, fail-closed)_
- `on_tool_request`: iff `tool_name=="Bash"` and policy enabled, `classify` the raw command (a raise
  → treat as hit); `deny` mode → deny+audit before the yolo short-circuit; `flag` mode → force a
  hold with `PermissionEvent(bash_flag=True, …)` overriding grant/yolo for that command + audit.
  Add optional `bash_flag` (+ matched-name) to `PermissionEvent` (default False → unchanged);
  `render` shows a `⚠️` line and **omits `[Allow for session]`** when `bash_flag`.
- **Acceptance:** flag mode — a matched command holds for approval even under a live `Bash` grant /
  `/yolo`, prompt shows `⚠️` + pattern name and no session button, resolves via the normal path;
  deny mode — matched command auto-denies (overrides grant/yolo) with `DENIED_MESSAGE`; `off` →
  today's behavior; **policy never converts a would-prompt into an auto-allow**; non-Bash unaffected;
  fail-closed on a matcher raise.
- **Test:** unit — grant+flagged→re-prompt; yolo+flagged→re-prompt; deny-mode→deny; matcher-raises→
  hold/deny (fail-closed); off→byte-for-byte gate; non-Bash untouched; render shows warning + drops
  session button; SB3 (pattern name body-free).

**T8 — Docs + verify (ADR + README + live phone-verify).** _(close-out)_
- Short **ADR** ("ADR-007 — Trust layer: audit log + Bash policy") recording the flag-vs-deny
  decision (rationale above), the SB3-body-free + 0600-append posture, the fail-closed contract, and
  the restated C2 limit (cite ADR-003 + findings.md C2). README: `/audit` + the new env knobs + the
  honest "guardrail not sandbox" statement.
- **Live phone-verify** (browser-drive Telegram, per the live-verify memory): (a) approve a tool →
  `/audit` shows the `allow_once` record; (b) ask Claude to run `rm -rf /tmp/p13probe` (or a benign
  flagged shape) → the prompt shows `⚠️` + no session button (flag mode); (c) with `Bash`
  session-granted, a flagged command **still re-prompts**; (d) audit log on disk is `0600` and
  body-free (grep it for any secret/UUID before finishing — evidence-scrub memory). Cross-model
  Codex QA pass (it has historically caught state/lifecycle bugs same-model review missed).
- **Acceptance:** Codex SHIP + live-verify PASS; full gates green (≥1288 + new); merge to `main`
  (per-phase checkpoint loop).

---

## Risks / things to flag (not guess)

- **Bash-policy false positives.** A heuristic denylist *will* occasionally flag a legitimate
  command. **Mitigation:** flag mode (default) never *blocks* — it re-prompts, so a false positive
  costs one extra tap, not a wedge. The `--force-with-lease` vs `--force` severity split is a build-
  time decision (conservative default: flag both); pinned in `/plan`.
- **SB3 surface area.** The biggest risk is a body leaking into the audit log. **Mitigation:** the
  `AuditEvent` type structurally cannot carry a body (no such field), the writer only receives
  already-safe strings, and a `secret_scan`-green committed fixture + an explicit "secret not
  leaked" test guard it. This is the gate-blocking invariant — call it out in `/plan` as the #1
  acceptance bar.
- **Policy-beats-yolo (strict mode) is a deliberate inversion.** In `deny` mode the Bash policy
  overrides `/yolo` — the one place a guardrail beats the bypass. This is intentional (the owner
  opted into a hard wall) but worth an explicit owner ack, since `/yolo` is otherwise "the operator
  took the wheel." Flag mode does **not** silently allow under yolo either (it re-prompts) — also a
  small, deliberate change to yolo semantics for *matched* commands only. **Flagged for review.**
- **Default-on vs default-identical.** I recommend `BASH_POLICY_MODE=flag` default (strictly safer,
  non-blocking) over `off` (literally-identical behavior). One-line owner call; flagged above.
- **Audit log is per-chat-filtered in `/audit` but one file.** A single process audit file holds all
  chats' events (stamped with chat_id); `/audit` filters to the requesting chat. For a single-owner
  bot this is fine; noted for completeness.
- **No cross-process append locking.** The append-only log assumes one writer (one bot per token,
  single asyncio loop) — consistent with the project's one-instance invariant. Not designed for
  concurrent processes appending to the same file.
