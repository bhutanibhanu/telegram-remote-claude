# Progress: p4-multi-project

_Plan generated 2026-06-22 from design.md · 9 tasks · supervised build (autonomous: isolated Implementer + independent reviewer, auto-commit on green + AGREE)_

> **Context for the builder.** Delta on a shipped codebase (P1–P3 on `main` @ `34ead13`).
> **Every task keeps `main` runnable** — one-shot is the safe default; multi-project is
> **streaming-only** (`ENGINE_MODE=streaming`). Locked decisions **D1–D8** live in
> [`design.md`](design.md); the schema/migration/crash-recovery rationale is **ADR-004** (T1).
> Reuse, don't rebuild: `paths.resolve_within_roots` (SB2), `JsonSessionStore`'s atomic+0600
> write, `_is_resume_failure` (resume fallback). **Gates** (from the worktree `.venv`):
> `pytest`, `ruff check .`, `mypy`, `python scripts/secret_scan.py`. Regression floor = **442
> tests** (count is **not** an acceptance gate — test policy). Commits: clean single-line,
> **no Co-Authored-By trailer**.

## Task list
- [x] T1 — ADR-004: multi-project session model & persistence schema (1ab4560)
- [x] T2 — Versioned store + v1→v2 migration + flat (one-shot) view (0d7d4fe)
- [x] T3 — Registry accessors + SB4 name validation (1de9998)
- [x] T4 — StreamingSession per-project rework (637eaef)
- [x] T5 — Bot navigation commands (/projects, /switch, /rm, /pwd, /cd-removed) (0502c7c)
- [x] T6 — Bot /new command (SB2 path-input) (c0bc0fa)
- [x] T7 — Resume hardening: cwd re-validation (SB2) + RB3 crash recovery (e068a37)
- [x] T8 — Integration / SB·RB acceptance matrix (0d721cd)
- [x] T9 — Live verify + verify.md phone-checklist (verify.md written; live run = owner's phone-verify at the Verify+QA gate)
- [x] QF1 — fix B1: `/reset` cwd corruption (gate `runner.reset()` to one-shot) — Codex NO_SHIP blocker (613ba17)
- [x] QF2 — fix B2: `/switch` re-validates stored cwd before activating (SB2 conformance) — Codex blocker (613ba17)
- [x] QF3 — fix B3: streaming resume-failure-on-result fallback (port `_is_resume_failure`) — Codex NO_SHIP blocker (d6ba25a)

- [x] QF4 — fix B3′ (Codex re-QA NO_SHIP): resume()-raises fallback wedge — discard the partial engine + clear the dead id (before fresh start) + start a FRESH instance; never re-resume the dead id (507e6f6)

### QA-fix follow-ups (from the QF3 review)
- ✅ CLOSED by QF4: `_recover_failed_resume` now best-effort `stop()`s the connected-but-dead engine before dropping it (no orphaned SDK client); same stop() added to the resume-raises path.
- Resume-failure detection is a text heuristic (shared verbatim with one-shot `_is_resume_failure`); a torn-transcript error lacking session-gone phrasing would be missed → stuck re-resume. Confirm the real SDK exception text against a live SDK (ties to the owner phone-verify / a P5 hardening).
- `_recover_failed_resume`/result-persist clear the store via the *active* project, not the pinned `turn_name` (harmless under the busy-guard; the deferred persist-to-captured-name defense-in-depth).

Legend: `[ ]` todo · `[>]` in progress · `[x]` done (short sha) · `[!]` blocked

## Tasks

### T1 — ADR-004: multi-project session model & persistence schema
- **Goal:** Record the P4 architecture decisions (D1–D8) as `docs/adr/ADR-004-multi-project-sessions.md`, Status **Proposed**, before any code.
- **Depends on:** none
- **Files (expected):** `docs/adr/ADR-004-multi-project-sessions.md`
- **Acceptance:**
  - WHEN ADR-004 is written, it SHALL record: the per-chat registry entity + **schema v2** shape; the **v1→v2 migration**; the **dual flat/registry view** rationale (why one-shot keeps a flat view over the active project, D8); the **single-active-run invariant** and **why the session/run correlation envelope is deferred to P5** (D2); **crash recovery = abandon + lazy-resume** (RB3, D7); **SB2 on `/new` + cwd re-validation**; **RB6** guarantees.
  - It SHALL cite ADR-001 (the `(session_id, cwd)` coupling, normalized-interface gaps, RB3/RB6 carry-ins) and SHALL NOT contradict ADR-001/002/003.
- **Tests:** none — decision record only.
- **Status:** done (1ab4560)

### T2 — Versioned store + v1→v2 migration + flat (one-shot) view
- **Goal:** Evolve `JsonSessionStore` to schema v2 with migrate-on-load and a flat accessor that preserves one-shot behavior. **The one-shot-risk task (D8).**
- **Depends on:** T1
- **Files (expected):** `claude_tg/session_store.py`, `tests/test_session_store.py`
- **Acceptance:**
  - WHEN a v1 doc (`{<chat_id>:{session_id,cwd}}`, no `version`) is loaded, the system SHALL migrate it to v2 (`{version:2, chats:{<id>:{active:"default", projects:{"default":{cwd,session_id,created_at,last_active}}}}}`) preserving `session_id`+`cwd`; the migration SHALL be **idempotent** (loading a v2 doc is a no-op).
  - WHEN the one-shot flat API `update(chat_id, session_id, cwd)` runs on a v2 doc, the system SHALL write the **active** project's fields (creating a `default` active project if none exists); `load`/flat-read SHALL return the active project's `{session_id,cwd}` — **byte-for-byte the pre-P4 one-shot contract**.
  - WHEN the file is corrupt or an unknown `version`, load SHALL return empty and SHALL NOT raise.
  - Writes SHALL remain **atomic (temp+replace)** and **`0600`** (RB6).
- **Tests:** v1→v2 migration (idempotent, preserves data); **flat-view round-trip == pre-P4 one-shot behavior** (the regression guard); corrupt + unknown-version → `{}`, no crash; atomic+0600 preserved.
- **Status:** done (0d7d4fe) — reviewer AGREE; OLD-vs-NEW flat-view differential byte-identical across 14 caller-realistic sequences. Two reviewer nice-to-haves deferred to T8.

### T3 — Registry accessors + SB4 name validation
- **Goal:** Add the streaming-only registry CRUD on top of T2, with strict project-name rules. Purely additive.
- **Depends on:** T2
- **Files (expected):** `claude_tg/session_store.py`, `tests/test_session_store.py`
- **Acceptance:**
  - WHEN `create(chat_id, name, cwd)` is called with a valid, unique name, the system SHALL add the project (and the caller MAY set it active); a **duplicate** name (case-insensitive) SHALL be rejected with a clear signal, no write.
  - WHEN a name fails `^[A-Za-z0-9_-]{1,32}$` (empty / >32 / bad chars / path-ish `../x`), `create` SHALL reject it (**SB4**) without writing.
  - `switch(chat_id, name)` SHALL set active iff the project exists else signal not-found; `remove(chat_id, name)` SHALL delete the project; `get_active` / `list` / `touch(last_active)` behave as specified.
- **Tests:** create/list/switch/remove/get_active happy + error paths; **SB4 name matrix** (valid, empty, 33-char, spaces, unicode, `../x`, dup case-insensitive) — incl. `fullmatch` anti-regression (newline/null/CRLF/tab/leading-trailing-whitespace rejected).
- **Status:** done (1de9998) — reviewer AGREE; 50-trial OLD-vs-NEW flat-view differential clean; SB4 anti-regression cases added per review. Malformed-doc never-crash + dangling-active tests deferred to T8.

### T4 — StreamingSession per-project rework
- **Goal:** Make the streaming session project-aware: resolve the active project and operate on its `(session_id, cwd)`; transient bypass reset on restart (D3).
- **Depends on:** T3
- **Files (expected):** `claude_tg/stream_session.py`, `tests/test_stream_session.py`
- **Acceptance:**
  - WHEN a turn runs, the system SHALL resolve the chat's **active** project and build/resume the engine from **that** project's `(session_id, cwd)`; WHEN no active project exists it SHALL prompt the operator to `/new` (no crash, RB1).
  - WHEN a `result` event carries a `session_id`, the system SHALL persist it to the **active** project (per-project), not a chat-global slot.
  - WHEN the bot restarts, in-memory `/yolo` + session-allowed-tools SHALL be **reset (gating ON)** for all projects (D3/SB5); identity (name/cwd/session_id) SHALL reload from the registry.
  - `/reset` SHALL clear the **active** project's `session_id` (fresh conversation), keep the project, and clear that project's transient grants/yolo.
  - The session SHALL expose **is-a-turn-in-flight** for a chat (for the D2 busy-guard) and SHALL keep the one-active-turn-per-chat invariant (`StreamingBusy`).
- **Tests:** active-project resolution + per-project resume (mock substrate); per-project `session_id` persist; no-active-project → prompt; restart resets yolo/grants; `/reset` targets active; is-busy exposure.
- **Status:** done (637eaef) — reviewer AGREE (live held-turn+concurrent-resolve probe confirmed the relay unblocks against the active-project engine); 507 tests green. **As-built refinement:** no-active-project AUTO-CREATES a `default` project at `config.workdir` (backward-compat with P1–P3 UX, symmetric with migration) rather than prompting `/new` — flagged for owner review. **⚠️ LOAD-BEARING INVARIANT surfaced:** `_active_engine` returns the held turn's engine *only because* the turn lock is held throughout an answer-hold (so `is_busy` is true) and the active project therefore cannot change mid-hold. A mid-hold `store.switch()` **deadlocks** the parked turn (reviewer Scenario-B). ⇒ T5/T6's busy-guard is mandatory for *relay correctness*, not just UX (see T5/T6/T8).

### T5 — Bot navigation commands (/projects, /switch, /rm, /pwd, /cd-removed)
- **Goal:** The registry-navigation command surface (no path input). SB1-gated, busy-guarded, text-only (no new callbacks).
- **Depends on:** T3, T4
- **Files (expected):** `claude_tg/bot.py`, `tests/test_bot.py` (and/or `test_bot_streaming.py`)
- **Acceptance:**
  - WHEN `/projects` is sent, the system SHALL list each project (name, cwd, active marker, last_active), or prompt `/new` if none.
  - WHEN `/switch <name>` is sent AND `streaming.is_busy(chat_id)` is true, the system SHALL **refuse** with a "finish or `/cancel` first" message (D2) and NOT change the active project; WHEN idle and the name exists → set active; WHEN unknown → error **listing available names**. **⚠️ This busy-guard is load-bearing for RELAY CORRECTNESS, not just UX** (T4 reviewer): an answer-hold parks the turn with the lock held (`is_busy` true), so changing `store.active` mid-hold would make the held turn's callback resolve the *wrong/absent* engine → **deadlock**. The guard MUST gate `store.switch`.
  - WHEN `/rm <name>` targets the **active** project → refuse (switch away first); unknown → error; else delete (registry only; transcript left on disk).
  - WHEN `/pwd` is sent → show the active project's cwd (or no-active-project message).
  - WHEN `/cd` is sent in **streaming** mode → reply that cwd is fixed per project (use `/new`); **one-shot `/cd` unchanged**.
  - All commands SHALL be allowlist-gated (**SB1**) and SHALL never crash on bad/missing args (RB1).
- **Tests:** each command happy + error path; `/switch`-while-busy refusal; `/rm`-active refusal; SB1 (non-allowlisted ignored); `/cd`-in-streaming message; no-arg usage.
- **Status:** done (0502c7c) — reviewer found + I fixed an **RB1 blocker** (`cmd_switch`/`cmd_rm` crashed on streaming+no-STATE_FILE `store=None`); now guarded + pinned (store-None graceful, `/rm` non-active case-insensitive, `/projects` sparse/dangling-active). Busy-guard false-pass-checked. 534 tests green.

### T6 — Bot /new command (SB2 path-input)
- **Goal:** The one path-input command — create a project confined to the permitted roots. **The SB2-on-`/new` task.**
- **Depends on:** T3, T4
- **Files (expected):** `claude_tg/bot.py`, `tests/test_bot.py`
- **Acceptance:**
  - WHEN `/new <name> <path>` is sent with a valid name and an **in-roots existing directory**, the system SHALL create the project with the **resolved** cwd, set it active, and confirm; WHEN `streaming.is_busy(chat_id)` is true → refuse (D2). **⚠️ `/new` also flips `store.active` (it auto-switches), so the same load-bearing busy-guard as `/switch` applies — a mid-hold `/new` would deadlock the parked turn.**
  - WHEN the path resolves **outside `ALLOWED_ROOTS`** (and `ALLOW_ANY_PATH` unset), the system SHALL refuse via `PathNotAllowed` (**SB2**) and NOT create the project.
  - WHEN the path is **not an existing directory** → refuse (not-a-directory), no create.
  - WHEN the name is **invalid (SB4)** or **duplicate** → refuse with a clear message.
  - `/new` SHALL be allowlist-gated (**SB1**) and SHALL never crash on missing args (RB1).
- **Tests:** happy create+autoswitch; SB2 out-of-root refusal; traversal/symlink-escape refusal; not-a-dir; invalid/dup name; busy refusal; missing-args usage; `ALLOW_ANY_PATH` opt-out.
- **Status:** done (c0bc0fa) — reviewer AGREE; 3 mutation-probes confirmed SB2 + busy-guard tests have teeth (resolved-contained cwd stored, not raw arg). Relative-path/`~` `/new` test deferred to T8 (resolver itself is unit-tested; confinement holds regardless of base).

### T7 — Resume hardening: cwd re-validation (SB2) + RB3 crash recovery
- **Goal:** Put the authoritative SB2 gate on the resume path and make an interrupted run fail clean (the genuine new reliability behavior).
- **Depends on:** T4
- **Files (expected):** `claude_tg/stream_session.py`, `tests/test_security_reliability.py` (+ `test_stream_session.py`)
- **Acceptance:**
  - WHEN a turn starts/resumes a project, the system SHALL **re-validate the stored cwd** via `resolve_within_roots` and, if no longer permitted, **refuse the turn** with a clear message (SB2 fail-closed) WITHOUT starting the engine.
  - WHEN a resume fails (torn/aged/upgraded transcript — `_is_resume_failure`), the system SHALL fall back to a **fresh** session for that project, **notify** the operator, and complete **without hanging** (RB2/RB3).
  - WHEN the bot restarts after an interrupted (in-flight-at-crash) turn, the affected project SHALL come back **idle** (no auto-replay of the torn turn); the next message SHALL resume-or-fresh per above (D7/RB3).
  - **(Defense-in-depth, from T4 review)** `_drive_turn`'s result-persist SHALL write the `session_id` to the project **captured at turn start** (not "whatever is active now"). Harmless today (busy-guard keeps active stable), but explicit capture removes the reliance. Consider also cancelling pending holds inside `reset()` so a `/reset` during a held turn doesn't strand the parked turn (a **pre-existing** HEAD bug, not a T4 regression; `/cancel` is today's escape hatch — fix here only if cheap).
- **Tests:** cwd-no-longer-permitted → refused (SB2); resume-failure → fresh+notice, no hang; interrupted-turn → idle on restart, next msg recovers.
- **Status:** done (e068a37) — reviewer AGREE; mutation-probe confirmed the SB2 gate has teeth; `allow_any_path=True` turn-fixture flip verified NOT to weaken `/new` SB2 coverage (those use the bot's own config). **Flagged for owner:** auto-created `default` (cwd=`config.workdir`) is refused on first turn if `workdir`∉`ALLOWED_ROOTS` — arguably SB2-correct; `from_env` defaults `workdir` into roots so the common case is fine. **Deferred to T8:** (a) persist-to-turn-captured-project-name (defense-in-depth; busy-guard makes it harmless today); (b) tighten the interrupted-turn test comment (FakeEngine.send ignores prompt — property proven structurally); (c) a `send`-raises-on-refusal no-wedge test.

### T8 — Integration / SB·RB acceptance matrix
- **Goal:** Prove the design's cross-task acceptance criteria end-to-end (no single task owns these).
- **Depends on:** T2, T3, T4, T5, T6, T7
- **Files (expected):** `tests/test_multi_project.py` (new) and/or extensions; **no production code**
- **Acceptance:**
  - WHEN two projects with different cwds are created and used, each SHALL keep an **independent** `(session_id, cwd)` — a turn in A SHALL NOT affect B's session.
  - WHEN the bot restarts, **both** projects SHALL be present with correct cwd/session_id and each SHALL resume independently on its next message (**restart-resumes-both**).
  - The store SHALL survive a simulated **crash-during-write** without corruption (atomic replace leaves the prior good file) (**RB6**).
  - One-shot mode SHALL behave **exactly as pre-P4** against a v2 store (regression assertion; complements T2).
  - **⭐ Busy-guard invariant (highest-value test, from T4 review):** WHEN a turn is parked awaiting an answer (`is_busy` true), `/switch` and `/new` SHALL be refused — verified end-to-end so that if T5/T6 ever drop the guard, this **fails loudly** (a mid-hold active-project change deadlocks the relay). Also cover `_stop_other_started`'s stop-failure path (old engine `stop()` raises → new turn still runs) and `_resume_id` defensive branches (non-str/empty `session_id` → fresh start). Plus deferred-from-T6: a **relative-path `/new`** resolves against the active project's cwd and stays SB2-confined (in-roots relative → created; `..`-escape relative → refused).
- **Tests:** the above scenarios + full lifecycle `/new→/switch→/rm` via the session/registry layer. Plus two deferred-from-T2 store contracts: (a) `update()` after an **unknown/future-version** load starts a clean v2 (does not preserve the future doc — locks the SB6 fail-safe-clobber contract); (b) an empty-string `session_id`/`cwd` on disk normalizes to **absent** in the flat view (documents the truthy-omit). Plus deferred-from-T3 **SB6 never-crash** tests: registry accessors against a hand-edited/malformed doc (non-dict `chats`/`projects`, non-str keys, **dangling `active`** pointing at a missing project) degrade to None/`UnknownProject` without raising.
- **Status:** todo

### T9 — Live verify + verify.md phone-checklist
- **Goal:** Verify against real Claude (not in CI) and hand the owner a phone-checklist.
- **Depends on:** T8
- **Files (expected):** non-production verify harness under `spikes/` or `scripts/`; `docs/features/p4-multi-project/verify.md`
- **Acceptance:**
  - WHEN run against a real streaming Claude session, the harness SHALL: `/new` two in-roots projects, `/switch` between them, show each resumes its **own** conversation, restart the process and **resume both**, and demonstrate RB3 (interrupted turn → clean recovery). Evidence contained to an **absolute temp dir** + **scrubbed** (SB3); UUID-grep before commit (per the live-verify memories).
  - `verify.md` SHALL enumerate the manual owner phone-checklist mirroring the above.
- **Tests:** this IS the live verification (manual/real-Claude; not in CI).
- **Status:** verify.md written (covers (a)–(j) + acceptance). **Live execution is the owner's phone-verify** at the Verify+QA gate — per the `relay-needs-concurrent-updates-and-phone-verify` memory, engine/unit probes bypass the real PTB callback path, so the phone-verify is the authoritative live check. (A contained programmatic engine live-verify can also be run on request, but is not a substitute.)

## QA fix tasks (post-Codex NO_SHIP — owner chose "fix all three")

### QF1 — B1: `/reset` must not corrupt the active project's cwd (D4)
- **Goal:** Stop `cmd_reset` writing the one-shot runner's stale cwd onto the active project in streaming mode.
- **Files:** `claude_tg/bot.py` (`cmd_reset`), `tests/test_bot_streaming.py`
- **Acceptance:** WHEN `ENGINE_MODE=streaming`, `/reset` SHALL call ONLY `streaming.reset(chat_id)` (NOT `runner.reset`), so the active project's cwd is preserved and only its session cleared; one-shot `/reset` unchanged.
- **Tests:** real store + runner; active `alpha`, `/switch beta`, `/reset` → `beta.cwd` unchanged, beta session cleared, `alpha` untouched. (Codex-suggested.)
- **Status:** todo

### QF2 — B2: `/switch` re-validates the target's stored cwd (SB2 conformance)
- **Goal:** Refuse activating a project whose stored cwd is now outside `ALLOWED_ROOTS` (close the design-doc "on switch" gap; resume path already enforces it).
- **Files:** `claude_tg/bot.py` (`cmd_switch`), `tests/test_bot_streaming.py`
- **Acceptance:** WHEN `/switch <name>` targets a project whose stored cwd fails `resolve_within_roots` (and `ALLOW_ANY_PATH` false), the system SHALL refuse and leave the active project unchanged; valid target switches as before; busy-guard ordering preserved.
- **Tests:** `/switch` to an out-of-root-cwd project → refused, active unchanged; in-roots target → switches. (Codex-suggested.)
- **Status:** todo

### QF3 — B3: streaming resume-failure-on-result fallback (RB3)
- **Goal:** A resumed session that connects then ERRORS on first use must not stay stuck — clear the persisted id, recover fresh, notify (port the one-shot `_is_resume_failure` heuristic to streaming).
- **Files:** `claude_tg/stream_session.py`, `tests/test_stream_session.py`
- **Acceptance:** WHEN the FIRST turn on a just-resumed session yields a resume-failure-shaped error/result (`_is_resume_failure`), the system SHALL clear the persisted `session_id`, recover (fresh — auto-retry once if clean to implement, else a clear "couldn't resume; started fresh — resend" notice), and NEVER hang (RB2); the stale id SHALL NOT remain persisted.
- **Tests:** resume() connects but first send yields a resume/session-not-found error → persisted id cleared, fresh recovery, notice, no hang. (Codex-suggested.)
- **Status:** todo
