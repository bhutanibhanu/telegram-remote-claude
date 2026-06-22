# ADR-004 — Multi-project session model & persistence schema

> Derived from the P4 design ([`docs/features/p4-multi-project/design.md`](../features/p4-multi-project/design.md))
> and the G-Scope owner decisions **D1–D8**. Builds on **ADR-001** (the `(session_id, cwd)` resume
> coupling; the normalized-interface "P4/P5 add a session/run correlation envelope" gap; the RB3
> crash-mid-turn / aged / upgrade resume edges left untested; the RB6 persistence facts) and reuses the
> P1 `paths.resolve_within_roots` SB2 resolver unchanged. Sibling of
> [ADR-001](ADR-001-session-substrate.md) / [ADR-002](ADR-002-async-answer-hold.md) /
> [ADR-003](ADR-003-permission-gating.md).

- **Status:** **Proposed** — chosen model for P4; owner reviews with the P4 branch.
- **Date:** 2026-06-22
- **Deciders:** repo owner
- **Related:** ADR-001 (substrate + `(session_id, cwd)` coupling + normalized-interface gaps);
  `docs/features/p4-multi-project/design.md` (D1–D8); `docs/cross-cutting-requirements.md`
  (SB2/SB4/RB3/RB6/SB1/SB5/SB6); `docs/interactive-remote-design.md` §P4.

---

## Context

P1–P3 shipped a real interactive remote, but it is **single-session**: everything keys off one
`chat_id` → one `{session_id, cwd}` (`session_store.py`, `stream_session.py`, `claude_runner.py`),
persisted in a flat JSON store shared by **both** the one-shot runner and the streaming session.
`/cd` mutates that single cwd in place — which fights Claude's **`(session_id, cwd)` resume coupling**
(ADR-001: a session id is *not* a global handle; resume fails from a different cwd).

P4 ("Multi-project sessions + restart recovery", roadmap §P4) must let one operator drive **several
named projects** — each its own cwd + Claude conversation — that **survive a restart**, while keeping
the live one-shot bot unaffected (`main` stays shippable; one-shot is the default, multi-project is
streaming-only). This ADR records the data model, how it coexists with one-shot, the run model, and the
crash/restart semantics — decisions that are hard to reverse once state is on disk.

**Constraints inherited from ADR-001 that bind this decision:**
- Resume is **cwd/project-scoped** — persist `(session_id, cwd)` together; resume only from the original
  cwd; the substrate does **not** guard double-attach.
- The normalized engine interface is **single-session / single-active-run**; a **session/run correlation
  envelope** (to route an answer to the right pending request across sessions) is a documented gap for
  "P4/P5".
- **RB3 edges** (crash-mid-turn / torn transcript, aged sessions, CLI/SDK-upgrade resume) are **untested
  by P0** — P4 must define fail-clean behavior.
- Containment is **policy-level, not an OS sandbox** — SB2 path confinement is the control.

---

## Decision drivers

- **Don't break the live bot.** The one-shot path reads the shared store; its behavior must be preserved.
- **Survive restarts** with the per-project `(session_id, cwd)` intact (RB6) and **fail clean** on a torn
  resume (RB3) — never auto-replay a half-run turn, never hang.
- **Stay small.** P4 is multi-project, **not** concurrency. One active run at a time; defer the
  correlation envelope, background runs, and notifications to P5.
- **Reuse, don't rebuild** the SB2 resolver, the atomic+`0600` write, and the `_is_resume_failure`
  fallback.
- **Fail closed** (SB6): a bypass must never silently survive a restart; an out-of-root cwd must be
  refused; a corrupt store must not crash.

---

## Decision

Adopt the **per-chat named-project registry** with a **single versioned store**, **single-active-run**,
and **abandon-and-lazy-resume** crash recovery. The eight load-bearing choices (design D1–D8):

1. **D1 — Per-chat registry.** Each allowlisted `chat_id` owns its own project set (keeps today's
   `chat_id` keying; no deployment-wide lock; degenerates to "just my projects" for a single chat).
   Projects do **not** cross chats. *(Alternative — a deployment-global shared set — rejected: it forces
   a global in-flight lock + double-attach guard, brushing P5, for little gain in the one-operator model.)*
2. **D2 — Single-active-run invariant.** Exactly one active run, one active project. `/switch` and
   `/new` are **refused while a turn is in flight** ("finish or `/cancel` first"). **No** background runs
   and **no session/run correlation envelope** are built here — both are **P5**. *(Rationale: with one
   active run, a pending permission/ask/plan request unambiguously belongs to that run, so the ADR-001
   correlation gap need not be closed in P4. Closing it early would pull P5 complexity forward.)*
3. **D3 — Bypass never survives a restart.** Durable **identity** (name, cwd, `session_id`, timestamps)
   persists; **transient bypass** (`/yolo`, session-allowed-tools) is in-memory only and **reset to
   gating-ON** on every restart (SB5). Per-project, never persisted.
4. **D4 — cwd fixed at `/new`; `/cd` removed in streaming mode.** A project's cwd is bound to its session
   for the life of that session (the ADR-001 coupling), so it is immutable; to work elsewhere, `/new`
   another project. `/pwd` still reports the active cwd. **One-shot `/cd` is unchanged.** *(Alternatives —
   `/cd` retargets+resets the session, or `/cd` becomes a path-based switch — rejected: both silently
   surprise the operator about session continuity.)*
5. **D5 — Command surface `/projects /new /switch /rm`, text-only.** `/new` auto-switches; `/rm` drops a
   project from the registry (leaving its Claude transcript on disk). `/rename` deferred. **No new inline
   keyboards** — the SB1 callback boundary is untouched.
6. **D6 — Migrate the existing session to a `default` project.** On first load, the legacy flat entry is
   wrapped as an **active** project named `default`, preserving `session_id`+`cwd` — the in-progress
   session survives the upgrade.
7. **D7 — Crash recovery = abandon + lazy-resume (RB3).** A turn in flight at crash is **dropped** (no
   auto-replay); the project comes back **idle**. The next message resumes `(session_id, cwd)`; on resume
   failure (torn/aged/upgraded transcript — `_is_resume_failure`) it falls back to a **fresh** session
   with a clear notice. Never auto-resume a torn turn; never hang (RB2). Resume is **lazy** (on the next
   message), not eager on boot. *(Alternative — auto-resume the interrupted turn — rejected: fragile
   against a torn transcript and risks re-running partially-executed side-effecting tools.)*
8. **D8 — Single versioned store (v1 flat → v2 registry).** Keep **one** file (`config.state_file`).
   One-shot's flat read/write becomes a thin **view over the active project**; streaming uses the full
   registry. Migrate-on-load (v1→v2). *(Alternative — a separate streaming-only registry store, leaving
   the flat store byte-for-byte untouched — was the lower-risk default but **not** chosen: the owner
   preferred preserving cross-mode session sharing in one file, accepting that the one-shot code path is
   touched and pinning it with a regression test.)*

### Schema v2

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

**Persisted = durable identity only.** `/yolo`, session-allowed-tools, and in-flight/status are
**transient** (D3). **Migration v1→v2**: a doc lacking `version`, shaped `{<chat_id>: {session_id, cwd}}`,
is rewritten with each chat's entry as a `default` active project (`created_at`/`last_active` = now) —
**idempotent, atomic, one-way**. A corrupt or **unknown-version** doc loads as empty (today's fail-safe;
SB6) — never a crash.

### Two views over one store (D8)

- **Flat view (one-shot):** `update(chat_id, session_id, cwd)` writes the **active** project's fields
  (creating a `default` active project if none exists); the flat read returns the active project's
  `{session_id, cwd}`. This preserves the **exact** pre-P4 one-shot contract — the single behavioral
  change to the live path, guarded by a regression test (RB6).
- **Registry view (streaming):** `list / create / switch / remove / get_active / touch`, with **SB4**
  name validation (`^[A-Za-z0-9_-]{1,32}$`, unique-per-chat case-insensitive).

### Path confinement (SB2) — `/new` and resume

`/new <path>` is confined by the **existing** `paths.resolve_within_roots` (canonicalize → resolve
symlinks + `..` → contain to `ALLOWED_ROOTS`; `ALLOW_ANY_PATH` opt-out; unset-roots fail-closed). The
**authoritative** re-check lives on the **resume path**: before a turn builds/resumes a project's engine,
the **stored cwd is re-validated** — so a cwd that drifts outside the roots (config narrowed, dir became
an out-of-root symlink) is **refused at use** (SB2/SB6), not just at creation.

---

## Consequences

**What P5 inherits.** The per-project records carry identity; the single-active-run lock is the only thing
P5 relaxes. P5 adds: a per-project run + scheduler, the **session/run correlation envelope** (`session_id`
+ `tool_use_id`/control-request id on every event/decision) so an inbound answer routes to the right
pending request in a **non-active** project, proactive notifications, and per-project status. P4 keys state
per project **now** so P5 is additive — **no rewrite**.

**RB6 (persistence integrity).** Registry writes stay **atomic (temp+replace)** and **`0600`**; the schema
carries a `version`; migration is idempotent + one-way; corrupt/unknown-version → empty. The one-shot
flat-view is pinned by a regression test.

**RB3 (restart/resume correctness).** Interrupted-at-crash turn fails clean (idle, no replay); resume from
the original cwd works; resume-failure → fresh + notice; **never hangs**. Resume is lazy.

**SB carry-ins.** SB1 trust boundary unchanged (text commands, no new callback surface); SB2 widened to
`/new` + re-validated on resume; SB4 names validated; SB5 bypass never survives restart (D3); SB6
fail-closed throughout. `cwd` is **not** an OS sandbox (ADR-001) — confinement is policy-level.

**What is explicitly NOT decided here (deferred).** Background concurrency / notifications / the
correlation envelope (**P5**); `/rename`; `/projects` switch-buttons; per-project approval policies; the
`ENGINE_MODE=streaming` default flip (owner's call); the full SB consolidation + threat model (**P6**).

**Risks.** (1) The shared-store evolution (D8) is the one change that can hurt the live one-shot bot —
mitigated by the flat-view contract + a dedicated regression test, idempotent/atomic migration, and
fail-safe corrupt-load. (2) Aged/torn/upgrade resume is an ADR-001-untested edge — mitigated by the
`_is_resume_failure` fail-clean fallback + an RB3 test. (3) Stored-cwd drift — mitigated by SB2
re-validation on the resume path, fail-closed.
