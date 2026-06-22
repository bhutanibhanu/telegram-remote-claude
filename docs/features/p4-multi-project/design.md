# Design: P4 — Multi-Project Sessions + Restart Recovery

> **Feature slug:** `p4-multi-project` · **Pipeline:** P4 (depends on P1–P3).
> Worktree `feat/p4-multi-project`, branched off `main` (tip `34ead13`) — it carries the full
> P1 streaming engine, P2 permission gate, and P3 interactive prompts. Parents:
> [`docs/interactive-remote-design.md`](../../interactive-remote-design.md) §P4 (roadmap lines 255–263, decision #5, Path policy) ·
> [`docs/cross-cutting-requirements.md`](../../cross-cutting-requirements.md) (**SB2**, **RB3**, **RB6**, SB1, SB4, SB6) ·
> [`docs/adr/ADR-001-session-substrate.md`](../../adr/ADR-001-session-substrate.md) (the `(session_id, cwd)`
> resume coupling; "Normalized-interface gaps → P4/P5 add a session/run correlation envelope"; RB3
> crash-mid-turn / aged / upgrade resume untested; RB6 persistence facts; SB2 containment is
> policy-level, not an OS sandbox).
>
> **The point of P4:** turn the single-session remote into a **multi-project** one that **survives
> restarts**. Add a per-operator registry of named projects — each its own working directory +
> Claude conversation — with `/projects` `/new <name> <path>` `/switch <name>` `/rm <name>`; persist
> the registry and **resume every project across a restart**; extend P1's SB2 path confinement to
> `/new`; and make an interrupted run **fail clean** (RB3). Concurrency stays out — P4 is still
> **one active run at a time** (that's P5).

---

## Scoping decisions (confirmed at G-Scope, 2026-06-22)

Owner-confirmed via grill. These shape the build; the rest of the doc elaborates them.

| # | Decision | Value |
|---|---|---|
| **D1** | **Project scope** | **Per-chat registry.** Each allowlisted `chat_id` owns its own set of named projects (matches today's `chat_id` keying; no deployment-wide lock needed; degenerates to "just my projects" in the common single-chat case). Projects do **not** follow the operator across different allowlisted chats. |
| **D2** | **Single-active-run invariant** | **Refuse `/switch` and `/new` while a turn is in flight.** P4 keeps exactly one active run, one active project (the P1 model). The operator must let the turn finish or `/cancel`. **No** background runs, **no** session/run correlation envelope built here — both are **P5**. |
| **D3** | **Bypass on restart (SB5)** | **Reset to gating-ON.** Durable **identity** (name, cwd, `session_id`) persists; **transient bypass** (`/yolo`, session-allowed-tools) does **not** — every restart comes back fail-closed. A bypass never silently survives a crash. |
| **D4** | **cwd model** | **Fixed at `/new`; `/cd` removed in streaming/multi-project mode.** A project's cwd is set once at creation and bound to its Claude session (the `(session_id, cwd)` coupling makes a mutable cwd unsafe to resume). To work elsewhere, `/new` another project. `/pwd` still shows the active project's dir. **One-shot mode keeps `/cd` unchanged.** |
| **D5** | **Command surface** | **`/projects` · `/new <name> <path>` · `/switch <name>` · `/rm <name>`.** `/new` auto-switches to the created project; `/rm` deletes a project from the registry (leaving its Claude transcript on disk). **`/rename` deferred.** Text commands only — **no new inline keyboards** (no new SB1 callback surface). |
| **D6** | **Upgrade / migration** | **Existing single session → a `default` active project.** On first load, the legacy flat entry is wrapped as an active project named `default`, preserving `session_id` + `cwd`. The operator's in-progress session survives the upgrade seamlessly. |
| **D7** | **Crash recovery (RB3)** | **Abandon the interrupted turn; lazy-resume next message.** A turn in flight at crash is dropped (no auto-replay); the project comes back **idle**. The next message to it resumes `(session_id, cwd)`; if resume fails (torn/aged/upgraded transcript) → fall back to a **fresh** session with a clear notice (reuse the existing `_is_resume_failure` heuristic). Never auto-resume a torn turn; never hang (RB2/RB3). |
| **D8** | **Persistence** | **Single versioned store (v1 flat → v2 registry).** Keep one file (`config.state_file`). One-shot's flat read/write becomes a thin **view over the active project**; streaming uses the full registry. Migrate-on-load (v1→v2). Preserves cross-mode session sharing; the one-shot persistence code path is touched, so its behavior is pinned by regression tests (RB6). |

---

## The basics

**Elevator pitch.** Run **several projects** from your phone — each with its own directory and its own
Claude conversation — and switch between them: `/new work ~/dev/work`, `/new bot ~/dev/claude-telegram-bot`,
`/switch work`, `/projects`. The registry **persists**, so a bot restart brings every project back exactly
where you left it.

**The actual problem.** P1–P3 shipped a real interactive remote, but it is **single-session**: everything
keys off one `chat_id` → one `{session_id, cwd}` (`session_store.py`, `stream_session.py`,
`claude_runner.py`). You can only be "in" one directory/conversation, and `/cd` mutates it in place —
which is hostile to Claude's `(session_id, cwd)` resume coupling (ADR-001). There is no way to keep two
projects going, and while streaming already persists+resumes the *one* session, there is no notion of
"resume **all** my projects" or a defined behavior when a run is interrupted by a crash. P4 adds the
**named-project registry**, **restart recovery**, and the **SB2 path gate on `/new`** the roadmap calls for.

**Who it's for.** The **single allowlisted operator** (the P1 model) — now juggling multiple repos/dirs
from the phone. Still one operator, **still one active run at a time** (concurrency is P5).

**Definition of success (concrete).** Under `ENGINE_MODE=streaming`, from the phone:
- `/new <name> <path>` creates a project **confined to the permitted roots (SB2)**, requires an existing
  directory, rejects a duplicate name, and **switches to it**.
- `/projects` lists every project (name, cwd, the active marker, last-active); `/switch <name>` flips the
  active project; `/rm <name>` deletes one.
- **Two projects keep independent state** — distinct cwd **and** distinct Claude conversation; switching
  between them resumes the right one.
- A **bot restart resumes both** — the registry survives intact; each project lazy-resumes its session on
  the next message to it; **`/yolo` and grants are reset** (D3).
- An **interrupted run fails clean** (RB3, D7) — after a mid-turn crash the project comes back idle, the
  next message resumes-or-falls-back-fresh with a notice, and the session never hangs.
- **`/new` outside the permitted roots is refused** (SB2); a project whose stored cwd is no longer
  permitted is refused on switch/resume (fail-closed).
- **One-shot behavior is unchanged** (flat-view over the active project) and **CI is green**.

**Anti-goals (explicit non-features).**
- **No background concurrency** — a run does **not** continue after you switch away; `/switch` while busy
  is **refused** (D2). Background runs + notifications + routing answers to a non-active project are **P5**.
- **No session/run correlation envelope** (the ADR-001 normalized-interface gap) — unnecessary while there
  is one active run; **P5** adds it.
- **No mutable per-project cwd** — `/cd` is gone in this mode (D4); cwd is fixed at `/new`.
- **No `/rename`**, **no `/projects` switch-buttons** (would add SB1 callback surface), **no per-project
  approval policies** (future), **no multi-operator/shared projects** (D1 is per-chat).
- **Not flipping the `ENGINE_MODE` default** — one-shot stays the safe live default; the flip is the
  owner's call.

**Constraints.** Builds on P1–P3 (streaming engine, `pending.py` hold/backstop/cancel, permission gate,
interactive prompts, SB1 callback path, `paths.py` SB2 resolver, `JsonSessionStore` atomic+0600 writes).
`claude-agent-sdk==0.2.105`, host CLI auth, **no API key**. Single operator, **single active run**. SB/RB
baselines apply; **SB2-on-`/new`, RB3, and RB6 are first fully satisfied here**.

---

## Requirements

### Functional — ranked

1. **Project registry + versioned store (D1/D6/D8, RB6).** Evolve `JsonSessionStore` to a **schema-v2**
   per-chat registry: `{version, chats: {<chat_id>: {active, projects: {<name>: {cwd, session_id,
   created_at, last_active}}}}}`. **Migrate v1→v2 on load** (wrap the legacy flat entry as a `default`
   active project, D6). Expose **two views**: a **flat accessor** (one-shot: read/write the *active*
   project's `session_id`+`cwd`) and a **registry accessor** (streaming). Atomic + `0600` +
   forward-compatible (RB6); a corrupt/unknown-version store fails safe to empty (never crash — today's
   `load()` behavior, kept).
2. **Project lifecycle commands (D5).** `/projects` (list), `/new <name> <path>` (validate name + SB2-resolve
   path + require dir + reject dup + create + auto-switch), `/switch <name>` (flip active), `/rm <name>`
   (delete from registry). All **allowlisted (SB1)** and **busy-guarded (D2)**; **text only** (no new
   callbacks). `/pwd` shows the active project's cwd; **`/cd` is removed in streaming mode** (one-shot
   keeps it, D4). `/reset` clears the **active** project's Claude session (fresh conversation; keeps the
   project; clears that project's transient `/yolo`+grants).
3. **Per-project streaming session (D2/D3/D7).** Key the `StreamingSession` by **(chat_id, active project)**:
   resolve the active project, **lazily** build/resume its engine from its `(session_id, cwd)` on the next
   turn (cwd-scoped — C6), persist `session_id` per project on the `result` event. Enforce the
   **single-active-run** guard across switches (D2). `/yolo` + session-allowed-tools are **per active
   project** and **transient** — never persisted; **reset on restart** (D3).
4. **SB2 path confinement on `/new` (+ re-validation).** Reuse `paths.resolve_within_roots()` **verbatim**
   for `/new` (canonicalize → symlink/`..`-resolve → contain to `ALLOWED_ROOTS`, `ALLOW_ANY_PATH` opt-out,
   fail-closed). **Also re-validate** a project's **stored cwd** on switch/resume (defense in depth: config
   could have narrowed, or a dir could have become an out-of-root symlink) → refuse with a clear message
   if no longer permitted (SB2/SB6).
5. **Restart recovery (RB3/RB6, D7).** On restart, load the registry (metadata only — **lazy** resume, no
   eager reconnect of N sessions). An interrupted turn is **abandoned**; the project is idle; the next
   message resumes-or-falls-back-fresh (reuse `_is_resume_failure`), never hangs. Both projects survive and
   are independently resumable.

### Non-functional

- **SB2 — path confinement** (first fully applied to `/new` here): every operator-supplied path
  (`/new <path>`) is canonicalized + contained before a project is created; the stored cwd is re-checked on
  switch/resume. Reuses the P1 resolver unchanged. **Dedicated tests** (traversal, symlink-escape,
  out-of-root, `ALLOW_ANY_PATH` opt-out, unset-roots fail-closed).
- **RB3 — restart/resume correctness** (first fully satisfied here): interrupted-at-crash turn fails clean;
  resume from the original cwd works; resume-failure (torn/aged/upgrade) falls back fresh with a notice;
  never auto-replays a torn turn; never hangs. **Dedicated test (RB7).**
- **RB6 — persistence integrity** (first fully satisfied here): registry writes are atomic + `0600` +
  versioned; v1→v2 migration is idempotent + one-way; corrupt/unknown-version store → fresh, no crash.
  **Dedicated tests (RB7), incl. the one-shot flat-view regression.**
- **SB1 — authn on inbound**: the four new commands ride the same `allowed` chat filter + handler-level
  `_ok` recheck as every other command. **No new callback surface** (text commands, D5) — the SB1 button
  boundary is unchanged.
- **SB4 — no injection**: project **names** are operator input that appear in messages and as registry keys
  → strict charset `^[A-Za-z0-9_-]{1,32}$`, unique-per-chat (case-insensitive), non-empty; an invalid name
  is refused with a clear message (RB1, never crash). Names never reach a shell (SB4) and never widen the
  on-disk transcript path beyond Claude's own cwd-scoped scheme.
- **SB6 — fail closed**: unknown-version store → empty; out-of-root cwd on `/new`/switch → refused; a bad
  command argument no-ops with a message; resume failure → fresh, not silent corruption.
- **RB1/RB2** preserved (bad input never crashes; engine/substrate errors fail clean).
- **Scale/latency:** single operator, single active run; a **soft cap** on projects-per-chat bounds the
  state file + restart work (number settled in build). No RPS/availability targets.

### Future (does today's design survive P5?)

The registry's per-project records carry **identity** (name, cwd, `session_id`, timestamps); the
single-active-run lock is the only thing P5 relaxes. **P5 (concurrency)** layers over P4 without a rewrite:
it (a) replaces the single lock with a per-project run + a scheduler, (b) adds the **session/run correlation
envelope** (ADR-001 gap — `session_id` + `tool_use_id`/control-request id on every event/decision) so an
inbound answer routes to the **right** pending request in a non-active project, and (c) adds proactive
notifications + per-project status. P4 deliberately **keys state per project now** so P5 is additive. P6
(security audit) consolidates SB2 across `/cd` + `/new`. **No rewrite required.**

---

## Architecture (delta from P1–P3)

**Today (the seam P4 extends).** Everything keys off `chat_id`:
- `bot.py` — commands (`/reset /cancel /yolo /unyolo /pwd /cd`) + the skill-launch passthrough, all keyed by
  `chat_id`; `/cd` calls `paths.resolve_within_roots` (SB2) then `runner.set_cwd`. Turns route
  `on_message → _run_turn → _on_message_streaming → streaming.handle_message(chat_id, …)`. `on_callback` is
  the SB1 boundary.
- `stream_session.py` (`StreamingSession`) — per **chat**: `_resume_ids[chat_id]`; builds the engine for the
  chat's cwd; **resumes the persisted `(session_id, cwd)` on first turn** (cwd-scoped, C6) else starts fresh;
  persists `session_id` on the `result` event; `set_yolo/handle_cancel/reset/resolve_callback(chat_id, …)`;
  enforces one active turn per chat (`StreamingBusy`). Uses `self.store`.
- `session_store.py` (`JsonSessionStore`) — flat `{chat_id: {session_id, cwd}}`, **atomic + 0600**.
- `claude_runner.py` (one-shot) — also uses `self.store`; `get_cwd/set_cwd`, the **`_is_resume_failure`**
  heuristic, a per-chat in-flight lock.
- `paths.py` — `resolve_within_roots()` (SB2, pure, reused as-is).

**P4 delta:**

```
operator                bot.py (commands, SB1)                 registry store (schema v2, RB6)
  /new w ~/dev/w  ──▶  validate name (SB4) + resolve_within_roots (SB2) + is-dir
                       └─ create project, set active ──────────▶ {chats:{<id>:{active:"w",
  /switch w     ──▶  busy-guard (D2) + re-validate stored cwd (SB2) + set active        projects:{...}}}}
  /projects     ──▶  list active+projects from registry          (atomic, 0600, versioned)
  /rm w         ──▶  refuse-if-active + delete from registry ───▶ persist
  message       ──▶  _run_turn ─▶ StreamingSession.handle_message(chat_id)
                                     │ resolve ACTIVE project (name, cwd, session_id)
                                     │ lazily build/resume engine from (session_id, cwd)  ← C6, per project
                                     │ single-active-run guard (D2)
                                     │ per-project transient yolo/grants (reset on restart, D3)
                                     └─ persist session_id per project on `result`
restart       ──▶  load registry (metadata only; LAZY resume) ; interrupted turn abandoned (RB3/D7) ;
                   yolo/grants reset (D3) ; next message resumes-or-falls-back-fresh (_is_resume_failure)
```

- **`claude_tg/session_store.py`** (evolve) — **schema v2 registry** + **v1→v2 migration on load** (D6/D8).
  Keep the atomic + `0600` write. Add: a **flat view** (`load_flat(chat_id) / update(chat_id, session_id,
  cwd)` operating on the *active* project — what `ClaudeRunner` and the one-shot path keep calling,
  behavior-preserved) and a **registry view** (`list/create/switch/remove/get_active/touch` for streaming).
  A corrupt/unknown-version doc → `{}` (today's fail-safe). *This is the single one-shot-facing change —
  pinned by regression tests.*
- **`claude_tg/stream_session.py`** — resolve the **active project** per turn; build/resume its engine from
  **its** `(session_id, cwd)`; persist `session_id` **per project**. Single-active-run guard spans switches
  (D2). `/reset` targets the active project's session. `/yolo`+grants become **per active project**,
  **transient**, reset on restart (D3). On restart: load registry, **lazy** resume, abandon any
  interrupted turn (D7).
- **`claude_tg/bot.py`** — new `cmd_projects / cmd_new / cmd_switch / cmd_rm` (allowlisted, busy-guarded,
  text-only). `/new` = SB4 name check → `paths.resolve_within_roots` (SB2) → is-dir → dup check → create +
  auto-switch. `/switch` = busy-guard → unknown-name error → SB2 re-validate stored cwd → set active. `/rm`
  = refuse-active → unknown-name error → delete. `/pwd` → active project cwd. **`/cd` in streaming mode
  → a message pointing to `/new`** (one-shot `/cd` unchanged).
- **`claude_tg/paths.py`** — **unchanged**; reused for `/new` and the switch/resume re-validation.
- **`claude_tg/config.py`** — schema-version constant; (optional) soft project cap. No new required env.
- **Engine (`engine/`)** — **unchanged in P4.** Single-active-run ⇒ a pending prompt always belongs to the
  active run, so **no correlation envelope is added** (explicitly deferred to P5 — see ADR-001 gaps).

**Data model (schema v2).**

```json
{
  "version": 2,
  "chats": {
    "<chat_id>": {
      "active": "<project-name|null>",
      "projects": {
        "<name>": { "cwd": "/abs/resolved", "session_id": "<id|null>",
                    "created_at": "<iso8601>", "last_active": "<iso8601>" }
      }
    }
  }
}
```

**Persisted = durable identity only.** `/yolo`, session-allowed-tools, and in-flight/status are **transient
in-memory** — never written, reset on restart (D3). **Migration v1→v2**: a doc lacking `version` and shaped
`{<chat_id>: {session_id, cwd}}` is rewritten to the above with each chat's entry as a `default` active
project (`created_at`/`last_active` = now) — idempotent, atomic, one-way.

**Auth model (unchanged).** Secret token + chat-id allowlist (SB1). The four new commands ride the existing
`allowed`-filtered + `_ok`-rechecked handler path; **no new callback surface** (D5) keeps the SB1 button
boundary exactly as P2/P3 left it.

---

## Risks & open questions

**Top risks**
1. *(Operational — the one that can break the live bot)* **Touching the shared persistence store.** D8
   evolves the store the **one-shot live bot** reads. *Mitigation:* the **flat view** preserves one-shot
   semantics exactly (read/write the active project); migration is idempotent + atomic + one-way;
   corrupt/unknown-version → fail-safe empty (today's behavior); a **dedicated one-shot-regression test** +
   migration tests (RB6). The store is the **only** one-shot-facing change; everything else is
   streaming-only and additive.
2. *(Technical)* **Resume of an aged/torn/upgraded transcript** (ADR-001 untested edge). *Mitigation:* reuse
   `_is_resume_failure` → **fail clean to a fresh session + notice** (D7); never auto-resume a torn turn;
   never hang (RB2). The crash-mid-turn path gets its own RB3 test.
3. *(Security)* **Stored-cwd drift.** A project's cwd could fall outside `ALLOWED_ROOTS` if config narrows,
   or a dir could become an out-of-root symlink after `/new`. *Mitigation:* **re-validate the stored cwd**
   via `resolve_within_roots` on switch/resume — not only at `/new` — and fail closed (SB2/SB6). `cwd` is
   **not** an OS sandbox (ADR-001).
4. *(Product)* **Single-active-run friction.** The operator expects to switch mid-run and is refused (D2).
   *Mitigation:* a clear "finish or `/cancel` first" message; concurrency is the **explicit P5 deliverable**;
   the boundary is documented.

**Open questions (resolve during build, not before)**
- **Soft cap** on projects-per-chat (bounds state size + restart work) — pick a number in build (e.g. ~50);
  not a hard requirement.
- **`/rm` of the only project** (can't `/switch` away first) — default: allow it → empty state that prompts
  `/new`; the **active** project is otherwise un-removable (switch first). Settle the edge in build.
- **`last_active` update granularity** (per turn vs per switch) — tune in build.
- Whether `/projects` ever gains **switch-buttons** — deferred (would add SB1 callback surface); text-only
  in P4.

**ADRs to write before code**
- **ADR-004 — Multi-project session model & persistence schema.** The per-chat registry entity; the
  **schema v1→v2 migration** + the **dual flat/registry view** (why one-shot keeps a flat view over the
  active project, D8); the **single-active-run invariant** and **why the session/run correlation envelope is
  deferred to P5** (D2); **crash recovery = abandon + lazy-resume** (RB3, D7); **SB2 on `/new` + cwd
  re-validation**; **RB6** persistence guarantees. Grounded in D1–D8 + ADR-001 (the `(session_id, cwd)`
  coupling, normalized-interface gaps, RB3/RB6 carry-ins).

---

## SDLC plan (delta)

**No SDLC change from P1–P3** — same GitHub Actions CI (tests + ruff + mypy + secret-scan), same lenient
ruff/mypy baseline, same substrate-mocked unit tests + a live-verify probe (not in CI). The **442 P1–P3
tests are the regression floor** (count is **not** an acceptance gate — test policy); P4 adds SB2-on-`/new`,
RB3, RB6 (incl. the one-shot flat-view regression), and registry/command coverage. Branch
`feat/p4-multi-project`; supervised-autonomous per-task build (isolated Implementer + independent reviewer,
auto-commit on green + AGREE), same as P1–P3. `main` stays runnable (one-shot is the safe default;
multi-project is streaming-only).

---

## Roadmap

**In scope (P4 / this pipeline):** ADR-004 → registry data model + **versioned store** (v1→v2 migration,
flat-view for one-shot, atomic+0600) → `StreamingSession` **per-project keying** (active project, lazy
resume, single-active-run guard, per-project transient yolo/grants reset on restart) → **bot commands**
(`/projects /new /switch /rm`; `/cd` removed in streaming; `/pwd` active) → **SB2 on `/new`** + cwd
re-validation on switch/resume → **RB3** crash-fail-clean + **RB6** persistence/migration/one-shot-regression
tests → live verify + owner phone-verify checklist.

**Out of scope (deferred):** background concurrency / notifications / **session-run correlation envelope**
(**P5**); `/rename`; `/projects` switch-buttons; per-project approval policies (future); the
`ENGINE_MODE=streaming` default flip (owner's call); full SB consolidation + threat model (**P6**).

**Expected build order (input to `/plan`):**
1. **ADR-004** — multi-project session model & persistence schema (from D1–D8 + ADR-001).
2. **Registry + versioned store** — schema v2, v1→v2 migration on load, atomic+0600, **flat view**
   (one-shot active-project accessor) + **registry view** (streaming). Unit + migration + corrupt-store +
   **one-shot flat-view regression** tests (RB6).
3. **StreamingSession per-project rework** — resolve active project; lazy build/resume its `(session_id,
   cwd)`; single-active-run guard across switches; per-project **transient** yolo/grants; `/reset` = active
   project's session; restart loads registry (no eager resume), all transient reset (D3). Unit (mock substrate).
4. **Bot command surface** — `/projects`, `/new` (SB4 name + SB2 path + is-dir + dup + auto-switch +
   busy-guard), `/switch` (busy-guard + unknown + cwd re-validate), `/rm` (refuse-active + unknown), `/pwd`
   active, `/cd` removed-in-streaming message. SB1 allowlist on all. Unit.
5. **SB2 on `/new` + cwd re-validation** — reuse `paths.resolve_within_roots`; fail-closed. Unit (traversal,
   symlink-escape, out-of-root, `ALLOW_ANY_PATH`, unset-roots fail-closed).
6. **RB3 crash recovery** — interrupted turn → idle on restart; lazy-resume; resume-failure → fresh + notice
   (reuse `_is_resume_failure`); never hang. Unit + the RB3 scenario test.
7. **SB/RB/regression test matrix (RB7)** — two-projects-independent; restart-resumes-both; RB3 fail-clean;
   SB2-on-`/new` + cwd-revalidate; RB6 atomic/0600/migration/corrupt; **one-shot-unchanged regression**; SB1
   on the new commands; SB4 name validation.
8. **Live verify + owner checklist** — real Claude: `/new` two projects, `/switch`, independent sessions,
   restart, resume both, RB3 interrupted-turn fail-clean; contained + scrubbed; `verify.md` phone-checklist.

---

## Security posture & blast radius (SB2/SB4/SB6)

P4 **widens where** the existing controls apply rather than introducing a new trust model:

- **SB2 path confinement extends to `/new`.** Every operator path (`/new <path>`) is canonicalized
  (symlinks + `..` resolved) and **contained** to `ALLOWED_ROOTS` before a project exists — the **same**
  `resolve_within_roots` resolver P1 uses for `/cd`, reused verbatim. A project's **stored cwd is
  re-validated** on switch/resume, so a cwd that drifts outside the roots (config change, symlink swap) is
  **refused** (fail-closed). `ALLOW_ANY_PATH=true` remains the explicit, documented opt-out; unset roots +
  no opt-out **fail closed** (SB6). `cwd` is **not** an OS sandbox (ADR-001) — confinement is policy-level.
- **SB4 — project names are validated input.** Names are constrained to `^[A-Za-z0-9_-]{1,32}$`, never
  interpolated into a shell, and never widen the on-disk transcript path beyond Claude's own cwd-scoped
  scheme. An invalid name is refused cleanly (RB1).
- **SB1 trust boundary unchanged.** Secret token + chat-id allowlist; the four new commands ride the same
  `allowed`-filtered + `_ok`-rechecked path. **No new callback surface** (text-only, D5) — the P2/P3 button
  boundary is untouched.
- **SB5 carried in — bypass never silently survives a restart.** Durable identity persists; **`/yolo` +
  grants are transient and reset on restart** (D3). A restart never resumes an allow-all posture.
- **Blast radius (SB6).** Single allowlisted operator, **single active run**. Multi-project widens *where*
  Claude can work (more cwds) but not *what* gates it — every risky tool still hits the P2 permission gate
  per project; `/yolo` is still the one loud, per-session, per-project, reset-on-restart bypass. Full SB
  consolidation + threat model (incl. `/cd`+`/new` together) is **P6**.
