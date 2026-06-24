# ADR-007 — Trust layer: durable audit log + Bash command policy

> Derived from the P13 design ([`docs/features/p13-trust/design.md`](../features/p13-trust/design.md))
> and the owner's "trust layer for power" vision (roadmap-v2 phase 5). A **delta** on the
> security-audited P0–P12 tree. Builds on **ADR-003** (the per-tool permission gate this layer hooks
> and extends — `on_tool_request`, the allow-once/allow-session verdicts, `/yolo`, the fail-closed
> SB6 invariant, and the explicit "Bash command-aware classification — deferred" note ADR-003 left
> open), **ADR-001** (the atomic + `0600` persistence discipline the audit log reuses), and the
> **P6/C2** finding ([`docs/features/p6-security-audit/findings.md`](../features/p6-security-audit/findings.md))
> that Bash is the one documented-UNCONFINED tool. Sibling of
> [ADR-001](ADR-001-session-substrate.md) … [ADR-006](ADR-006-known-limitations.md).

- **Status:** **Proposed** — chosen model for P13; owner reviews with the P13 branch.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Related:** ADR-003 (the permission gate + verdicts + `/yolo` + SB6 this layer extends; the
  deferred "Bash command-aware classification" it now delivers); ADR-001 (the C2 per-tool primitive
  + the `JsonSessionStore` atomic/`0600` write discipline); ADR-006 (the deferral pattern this ADR's
  "deferred items" section follows); `docs/features/p13-trust/design.md` (the full design + the
  scope decision + the C2 pin); `docs/features/p6-security-audit/findings.md` (C2 — Bash unconfined);
  `docs/cross-cutting-requirements.md` (SB1/SB3/SB6/RB1).

---

## Context

The owner wants to grant the bot **more autonomy with confidence**. Two gaps stand between today's
posture (the security-audited P0–P12 tree) and that:

1. **No durable audit trail.** Every security-relevant decision — tool approve/deny, plan
   approve/reject, `/yolo`, attach/switch/watch — happens in-memory and is gone the moment it scrolls
   off the chat. Today's logging is `logging`-only (stderr, INFO/DEBUG), body-free (SB3, H1-remediated)
   but **not durable**: there is no reviewable record of "what did Claude actually run, what did I
   approve, and when." If the owner is going to lean on this bot, they need a history.
2. **Bash is the one documented-UNCONFINED surface.** P6/C2 confined the SDK's *file/search* tools
   (`Read`/`Write`/`Edit`/`Glob`/`Grep`/`LS`/`Notebook*`) to `ALLOWED_ROOTS` via the path layer in
   `on_tool_request`. But **`Bash` was deliberately left out** — "an arbitrary shell command has no
   reliable static target, so `Bash` is **not** path-parsed" (findings.md C2). `Bash` gates as RISKY
   by name, but a session-**granted** `Bash` (or one under `/yolo`) is then **unconfined** — it can
   `rm -rf /`, force-push, `curl … | sh`, read `~/.ssh/id_*`, etc. with no further prompt. ADR-003
   explicitly deferred "Bash command-aware classification"; the roadmap names closing this C2 residual
   as P13's job.

This is a **security-core** phase. The bar (from the design): **never weaken the existing gate**, keep
the **safe default = current behavior** (or strictly safer), fail **closed**, stay **body-free (SB3)**,
and never let an audit write break a turn (**RB1**). The scope is deliberately the **two** primitives
the owner's vision names — an audit trail and a Bash policy — with the rest of the roadmap's P13 bullet
explicitly deferred (below).

## Decision drivers

- **ADR-003 (the gate this layer hooks).** `on_tool_request` is the single substrate-neutral chokepoint
  for every tool decision; allow-once/allow-session/deny + `/yolo` are its verdicts; the wrong-way error
  is **fail-closed** (an unknown tool gates, not runs). P13 **extends** this — it does not rebuild it —
  and must preserve every invariant.
- **ADR-001 (the persistence discipline).** `JsonSessionStore._save_raw` writes atomically with `0600`
  perms and fails safe on corrupt/missing reads. The audit log **reuses** that posture (`0600`, parent
  `mkdir -p`, best-effort) rather than inventing a new one.
- **P6/C2 (the honest boundary).** Bash has no reliable static target. Any Bash guardrail is a guardrail
  on the **approval UX**, not OS-level confinement — the C2 boundary stands and must be **restated**, not
  papered over.
- **Cross-cutting:** **SB1** (authn on the new `/audit` surface), **SB3** (no bodies/secrets persisted —
  the gate-blocking bar here), **SB6** (fail closed), **RB1** (an audit write never wedges a turn).

## Decision

**Ship two cohesive, additive primitives hooked at the one approval chokepoint: (1) a durable,
append-only, body-free JSONL audit log, and (2) a conservative Bash command policy that escalates (or,
opt-in, denies) a small denylist of dangerous shapes. Both default on but non-breaking; both fail closed;
neither weakens the existing gate.**

### 1. Audit trail — durable, append-only, **body-free**, `0600`, size-bounded

- **What it is.** A new pure-ish module `claude_tg/audit.py` (no telegram, no SDK — mirrors
  `session_store.py`'s isolation): a frozen `AuditEvent`, an `AuditLog` (append-only JSONL), an
  `AuditSink` protocol, and a `ChatBoundSink` that stamps the chat id + a redacted session tag the
  substrate-neutral engine does not know. JSONL (one object per line) is the right shape — append is
  O(1), tail-read is cheap — versus rewriting a growing JSON document every event.
- **Where it hooks — the gate chokepoint.** The engine gains an **optional** `audit_sink` (default
  `None` → no-op, so every existing `Engine(...)` and the regression test floor are unchanged — the
  same pattern as the optional C2 `cwd`/`allowed_roots`). It records inside `on_tool_request` /
  `_permission_hold`: the **auto-allow** branch (safe tool / live grant / **`/yolo`**), the resolved
  verdict (allow_once / allow_session / deny / backstop_deny / cancel), and the fail-closed no-`tool_use_id`
  deny. **Hooking the engine, not the bot, is load-bearing:** a tool that auto-runs under `/yolo` or an
  existing grant **never reaches the bot**, so only the engine can audit it. Plan / session / policy
  events that the engine does not see (plan approve/reject, `/attach`·`/switch`·`/watch`·`/reset`·`/yolo`)
  are recorded from the bot/session side through the **same** `AuditLog` — two call paths, one writer,
  one schema.
- **SB3 is STRUCTURAL — and the durable log is STRICTER than the prompt.** `AuditEvent` has **no field
  that can carry a raw body** (no file content, command output, prompt/plan text, plan feedback, or raw
  session id) — a leak is impossible *by construction* (mirroring how `ThinkingEvent` has no `signature`
  and `ImageInput.__repr__` elides the base64). The one free-ish field, `summary`, is built **only** by
  `audit_safe_summary` — an **audit-specific renderer that is stricter than the prompt's
  `safe_input_summary`**. The live *prompt* keeps the first ~160 raw chars of an ident field (`command`/
  `path`/`url`) — correct there, because the operator must *see* the command to approve it, and the
  prompt is ephemeral. The **durable on-disk log must not**: persisting 160 raw chars would write a
  secret early in a Bash command to disk. So `audit_safe_summary` **collapses ident fields to a
  length/shape** (`command` → `<rm …40 chars>` — only argv[0] the binary name, then the length; `path`/
  `url` → `<N chars>`) in addition to collapsing free-text body fields. The net invariant: **a secret in
  a Bash command is shown once in the ephemeral prompt but can never reach the durable log.** A unit test
  pins that a `Write` with secret `content` and a `Bash` with a token in the command produce a record
  containing **neither**, and committed fixtures are secret-free so `scripts/secret_scan.py` stays green.
- **Durable / atomic / `0600` / bounded (reused from ADR-001).** The file is created `0600`
  (`os.open(..., O_CREAT|O_WRONLY|O_APPEND, 0o600)`, re-asserted on a pre-existing looser file), parent
  `mkdir -p`'d; each append is a single line under the OS append mode (atomic-enough for one local
  writer — one bot per token, one asyncio loop; **not** designed for concurrent-process appends). A
  **size-bounded 1-keep rotation** (default 5 MB → rotate once to `<file>.1`) caps disk at ~2× the bound.
- **RB1 is total.** Every append/rotate is best-effort: any failure (disk full, perms, bad path) is
  caught, logged once at WARNING (body-free — path + exception class only), and **swallowed**. The audit
  log is an *observer*, never on a turn's critical path — mirroring `JsonSessionStore.add_cost` and
  `StreamingSession._persist` ("never wedge a turn over a write"). A malformed line in `tail` is skipped,
  not fatal.
- **`/audit` command (SB1, body-free).** A read-only `cmd_audit` mirroring the `cmd_macros` template:
  `_ok` allowlist recheck → `chat_id` → `AuditLog.tail(n)` **filtered to the requesting chat** → each
  record rendered as a compact, HTML-escaped, body-free line (default last 20; `/audit <n>` capped at
  100). The render is body-free *by construction* — the record holds only already-safe fields. Empty /
  disabled / one-shot → a clean notice, never an error.
- **Default-on but non-breaking.** When `CLAUDE_STATE_FILE` is set, the audit log defaults to
  `<state_file>.audit.jsonl` (next to the store, inheriting its dir + `0600` posture). With **no** state
  file (a stateless oneshot deploy) it is **off** unless `AUDIT_LOG_FILE` is set explicitly — so an
  existing stateless deploy gains no surprise file. It only *adds* a log; it changes no gate behavior.

### 2. Bash command policy — the ADR-003-C2 follow-up (additive, fail-closed)

ADR-003 deliberately left "Bash command-aware classification" deferred, and P6/C2 documented Bash as the
one tool whose target cannot be statically confined — so a session-**granted** Bash (or one under `/yolo`)
runs unconfined. P13 closes that **residual** with a conservative guardrail on the approval UX (not a
sandbox — the C2 boundary stands; see *Consequences*).

- **What it is.** A pure module `claude_tg/bash_policy.py` (no telegram/SDK/engine — mirrors
  `permissions.py`): `classify_bash(command, *, extra_patterns=()) -> BashPolicyMatch | None`, matching
  the **raw** command against a small, conservative built-in **denylist** of high-signal destructive
  shapes — recursive force-`rm` of `/`·`~`·`$HOME`, `curl|sh`/`wget|sh` pipe-to-shell, `git push --force`
  (and `--force-with-lease`, conservatively), `mkfs`, `dd of=/dev/…`, redirect to a raw `/dev/sd…`, the
  classic fork bomb, recursive `chmod 777`, recursive `chown` on a system root, and reads of obvious
  secret stores (`~/.ssh/id_*`, `.aws/credentials`, `.env`). The match returns a **body-free** label +
  severity, never the command. Every pattern is written **conservatively** — `rm -rf /` matches, `rm -rf
  ./build` does **not**; `curl … | sh` matches, `curl -O url` does **not** — because a denylist that
  fires on benign work trains the operator to disable it (worse for safety than a loud prompt).

- **Decision: FLAG (escalate) by default; AUTO-DENY opt-in.** `BASH_POLICY_MODE = flag | deny | off`,
  **default `flag`**:
  - **`flag` (default, the recommendation).** A matched command **escalates** the existing per-tool
    prompt: a `⚠️` line + the matched pattern label, `[Allow for session]` **dropped** (the only way
    through is a deliberate one-time `[Allow once]`), and — **the one deliberate inversion** — a
    **re-prompt even under an active session-grant or `/yolo`** for the *matched* command. This is the
    concrete closure of the C2 residual: "re-confirm `rm -rf` even when granted." It is strictly safer
    than today (today a granted Bash auto-runs) and never *blocks* a genuine need (a false positive costs
    one extra tap, scoped only to matched dangerous commands).
  - **`deny` (strict opt-in).** A matched command is **auto-denied** outright with the canned
    `DENIED_MESSAGE`, **overriding grant/`/yolo`** — a hard wall for an owner who wants one.
  - **`off`.** The policy is disabled entirely → **byte-for-byte** the pre-P13 gate (Bash gates by name).
  - **Rationale for flag over auto-deny as the default:** shell patterns are heuristic; `rm -rf ./build`,
    a vetted `git push --force-with-lease`, a trusted `curl … | sh` installer are all "dangerous-shaped"
    but sometimes exactly what the operator wants. A default hard-deny would push the operator to disable
    the policy wholesale (or `/yolo`) to get past a false positive — training them toward the bypass. The
    command text is **already shown** in the prompt, so flagging louder is cheap and honest; it keeps the
    human in the loop, which is the whole point of the trust layer.

- **The ADDITIVE invariant (never weakens the gate).** The policy is consulted **inside**
  `on_tool_request`, layered on top of the existing gate, and runs **only** for `Bash` and **only** when
  the mode is not `off` (a non-Bash tool, or any tool with the policy off, is byte-for-byte the pre-P13
  gate). It can only **escalate**: `deny` turns a would-allow/would-prompt into a **deny**; `flag` turns a
  would-**auto-allow** (a prior grant / `/yolo`) into a one-time **prompt** (and a would-prompt stays a
  prompt, just louder + session-button-dropped). It **NEVER** converts a would-prompt/would-deny into an
  auto-allow. A **non-matching** Bash command falls straight through to today's gate (a grant/`/yolo`
  still auto-allows it) — only a **matched** command is escalated. A unit test pins "the policy never
  turns a would-prompt into an auto-allow," and the existing permission/path/`/yolo` matrix stays green.

- **FAIL-CLOSED everywhere (SB6).** `classify_bash` is written never to raise on a `str`, but the engine
  treats **any** exception from it as a **hit** (escalate in flag mode / deny in strict mode) — never a
  silent allow. A flagged command that arrives with no `tool_use_id` (so no resolvable hold can be opened)
  **fails closed to deny** rather than auto-allowing. And a malformed owner regex in
  `BASH_POLICY_EXTRA_PATTERNS` **fails loud at config load** (`validate_extra_patterns` →
  `InvalidBashPattern`) — silently dropping it would be fail-**open** (the owner's safety rule meant to
  catch a dangerous command would be lost, and that command would then auto-allow under a grant/`/yolo`).
  A policy bug always errs toward friction, never exposure (the ADR-003 / SB6 invariant).

- **Configurable but fail-safe.** `BASH_POLICY_EXTRA_PATTERNS` lets the owner **add** denylist patterns
  (additive to the built-ins); the **built-ins cannot be removed via config** — dropping a safety pattern
  must be a code change, not an env var. `BASH_POLICY_MODE=off` is the documented full disable.

## Consequences

**What P13 builds.** `claude_tg/audit.py` (`AuditEvent` / `AuditLog` / `AuditSink` / `ChatBoundSink` +
`audit_safe_summary`); `claude_tg/bash_policy.py` (`classify_bash` + the conservative built-in denylist +
`validate_extra_patterns`); the engine hook (an optional `audit_sink` recorded at the gate + the Bash
policy consulted additively in `on_tool_request`, with an optional `bash_flag`/`bash_flag_label` on
`PermissionEvent` so the render shows `⚠️` and drops the session button); the config knobs
(`AUDIT_LOG_FILE`, `AUDIT_LOG_MAX_BYTES`, `BASH_POLICY_MODE`, `BASH_POLICY_EXTRA_PATTERNS`, all
validated, all defaulting to the non-breaking value); the `/audit` read-only command (mirroring
`cmd_macros`, SB1-gated, body-free, per-chat filtered); and the SB/RB test matrix.

**What P13 reuses (does not rebuild).** ADR-003's gate (`on_tool_request`, the verdicts, `/yolo`, the
fail-closed posture), ADR-001's atomic + `0600` persistence discipline, the `PermissionEvent` /
permission-keyboard render path, and the `cmd_macros` read-only command template. The audit hook is one
optional constructor param; the Bash policy is one additive branch.

**The honest limit — Bash is still not a sandbox (C2 restated).** The Bash policy is **substring/regex
matching on the raw command**, explicitly **not** a shell parser and **not** a defense against a
determined adversarial Claude (it does not attempt to defeat obfuscation — base64-decode-then-eval,
`$IFS` tricks, variable indirection). It raises the floor against *accidental* and *obvious* destruction;
it is a **guardrail on the approval UX, not OS-level confinement.** The P6/C2 boundary — an arbitrary
shell command has no reliable static target, so `Bash` remains not statically confinable — **still stands
and is not weakened.** This ADR (and the module docstrings) state that plainly rather than over-claiming.

**A deliberate, scoped change to `/yolo`/grant semantics.** For a **matched** dangerous command, `flag`
mode re-prompts and `deny` mode denies **even under `/yolo` or an active grant** — the one place a
guardrail beats the bypass, by design (the C2 closure). This is intentional and scoped to matched
commands only; a non-matching command under `/yolo`/grant is unaffected. Flagged for explicit owner ack,
since `/yolo` is otherwise "the operator took the wheel."

**Residual risk.** (a) **Bash-policy false positives** — a heuristic denylist will occasionally flag a
legitimate command; mitigated because the default (`flag`) never *blocks* — it costs one extra tap, not a
wedge. (b) **SB3 surface** — the biggest hazard is a body leaking into the durable log; mitigated
structurally (no body-bearing field), by the stricter `audit_safe_summary`, and by a secret-free fixture
+ an explicit "secret not leaked" test (the gate-blocking bar). (c) **Single-writer audit log** — one
process file holds all chats' events (stamped with chat_id; `/audit` filters per chat), with no
cross-process append locking — consistent with the one-bot-per-token invariant.

**Deferred — not in P13 (revisit triggers, ADR-006 style).** The roadmap's P13 line also lists
**proactive owner-alerts**, a **cost budget + alerts**, **resource limits**, and **state
backup/restore**. P13 ships **only** the audit trail + the Bash policy — the two load-bearing "trust"
primitives the owner's vision names, cohesive because both hook the same decision chokepoint and review
as one security delta. The deferred items are larger and orthogonal (alerts need a wedge/restart-detection
surface; budgets need a cost-cap enforcement loop; backup/restore is its own reliability feature);
bundling them would bloat a security-core phase and dilute review. They are a clean additive task for a
later phase (P13.5 / P14) should the owner want them.
