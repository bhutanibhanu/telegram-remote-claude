# Cross-Cutting Requirements — Security, Reliability, CI, Test Policy

> Requirement sets that apply to **every** pipeline (P0–P9) of the Interactive Claude Code Remote.
> Owned from **P1**, audited/consolidated at **P6**, finalized at **P9**. Each pipeline's
> `progress.md` acceptance criteria must explicitly reference the applicable items below. These are
> **not** standalone pipelines — they are obligations carried by all of them.
>
> Parent doc: [`docs/interactive-remote-design.md`](interactive-remote-design.md).

---

## Security Baseline (SB)

| ID | Requirement | First applies at |
|---|---|---|
| **SB1** | **Authn on every inbound.** Messages, commands, **and button-callback taps** are restricted to allowlisted chat ids; everything else is silently ignored. Button callbacks are new attack surface and must be checked from the first feature that adds buttons. | P2 (callbacks); allowlist already exists |
| **SB2** | **Path confinement.** Any operator-supplied path (`/cd`, `/new`) is **canonicalized with symlinks fully resolved** and checked for **containment** within an `ALLOWED_ROOTS` entry (the resolved path must equal, or be a descendant of, a resolved root); traversal (`..`), symlink escape, and out-of-root paths are rejected. `ALLOW_ANY_PATH=true` is the explicit opt-out (skips containment; canonicalization still applies). | **P1** (config + `/cd` enforcement), **P4** (extends to `/new` + project sessions) |
| **SB3** | **Secret hygiene.** The bot token is never logged; sensitive tool output is kept out of logs; `.env` is git-ignored; state files are written `0600`. | P1 |
| **SB4** | **No injection.** Never build shell commands or arguments from message text; treat all Telegram input as untrusted; no string interpolation of user content into commands. | P1 |
| **SB5** | **Bypass is explicit and visible.** `/yolo` is off by default, scoped per-session, and loudly indicated when on; `--dangerously-skip-permissions` is removed from the default path. | P2 |
| **SB6** | **Safe defaults + documented blast radius.** Defaults fail closed (e.g. public deployments with no `ALLOWED_ROOTS` and no `ALLOW_ANY_PATH` refuse path operations); the trust model and blast radius are documented. | P1 (defaults + path-policy fail-closed), P6 (docs) |

---

## Reliability Baseline (RB)

| ID | Requirement | First applies at |
|---|---|---|
| **RB1** | **Never crash on bad input** (preserve existing invariant). | P1 |
| **RB2** | **Clean failure** on engine/substrate errors and timeouts — a clear message, never a silent hang or stack trace to the user. | P1 |
| **RB3** | **Restart/resume correctness.** Sessions reload after a bot/host restart; a turn that was in-flight when the process died fails *clean*, never silently "continues." | P4 |
| **RB4** | **Cancel + backstop timeout don't wedge state.** `/cancel` cleanly aborts a waiting run; the 60-min backstop auto-denies + notifies and leaves the session usable. | P2 |
| **RB5** | **Rate-limit safety.** Update throttling/coalescing holds under bursts (P1) and under concurrent runs (P5) without flooding or tripping Telegram limits. | P1, P5 |
| **RB6** | **Persistence integrity.** State writes are atomic, `0600`, and forward-compatible (schema accommodates multi-project + concurrency without rewrite). | P4 |
| **RB7** | **Each of RB1–RB6 has a dedicated test.** Reliability tests are pipeline exit criteria, not optional. | every pipeline |

---

## CI track

- **Provider:** GitHub Actions. **Stood up in P1**; gates all subsequent merges.
- **Runs:** tests + lint + type-check + security scan on every push/PR.
- **Branch protection:** merges to `main` require green CI.
- **Coverage focus:** core logic (engine, permissions, rendering, session manager). Coverage is a
  visibility tool, not a numeric gate (see Test policy).
- Optional per-feature: a mocked end-to-end smoke test exercising the bot against a mocked substrate.

---

## Test policy

The repository's **53 passing tests are a pre-P0 snapshot, not a permanent numeric floor.** The
architecture changes from a one-shot runner to bidirectional streaming, so **obsolete
implementation tests may be rewritten, replaced, or removed** as the runner is superseded.

**Rules:**
1. **Preserve existing user-facing behavior** where it still applies (allowlist enforcement,
   ignoring non-allowlisted chats, message chunking, never-crash-on-bad-input, command behavior)
   with **equivalent or stronger behavioral coverage** than the snapshot provides.
2. **Add new coverage** in four categories as features land:
   - **Streaming** — event normalization, one-liner vs verbatim rendering, throttling/coalescing.
   - **Interaction** — questions, plan approve/reject + feedback, free-text Q&A, answer routing.
   - **Security (SB)** — non-allowlisted ignored (messages + callbacks), path traversal rejected,
     no secrets in logs, bypass visibility.
   - **Reliability (RB)** — restart/resume, crash-mid-run clean failure, cancel, timeout,
     concurrency routing, rate-limit safety.
3. **Substrate is mocked** in unit tests (mirror the existing `_invoke`-style isolation) so logic is
   tested without invoking Claude or the network.
4. **Never use raw test count as an acceptance criterion.** A pipeline is accepted on its behavioral
   + SB/RB checklist, not on a number increasing. Removing an obsolete test while adding stronger
   behavioral coverage is a valid, expected outcome.
