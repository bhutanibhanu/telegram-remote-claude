# ADR-008 — Proactive scheduler: unattended action ON the gate

> Derived from the P14 design ([`docs/features/p14-proactive/design.md`](../features/p14-proactive/design.md))
> — roadmap-v2 **phase 6**, the last feature phase before the P15 public-release hard stop. A **delta** on
> the security-audited P0–P13 tree. Adds the bot's **push** half: useful work fired **on a schedule** with
> no operator prompt. Builds on **ADR-003** (the per-tool permission gate this feature runs *through*, never
> around — `on_tool_request`, the verdicts, `/yolo`, the fail-closed posture), **ADR-002** (the async
> answer-hold + ~60-min backstop that fail-safes an unattended risky tool), **ADR-005** (the per-chat send
> gate + concurrency cap + FIFO queue a proactive turn reuses), **ADR-007** (the durable body-free audit
> trail that must record proactive actions), and **ADR-001** (the atomic + `0600` persistence discipline the
> schedule store reuses). Sibling of [ADR-001](ADR-001-session-substrate.md) … [ADR-007](ADR-007-trust-layer.md).

- **Status:** **Proposed** — chosen model for P14; owner reviews with the P14 branch.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Related:** ADR-003 (the gate + classifier + `/yolo` this feature runs *through*; the per-turn
  force-gate is the proactive analogue of ADR-007's "a flagged Bash command re-prompts even under `/yolo`");
  ADR-002 (the ~60-min answer-backstop — RB4 — that auto-denies an unattended hold); ADR-005 (the per-chat
  `ChatSendGate`, the `MAX_CONCURRENT_RUNS` cap + FIFO queue, the per-project busy-guard a fire reuses);
  ADR-007 (the `AuditLog` / `ChatBoundSink` the proactive `proactive_fire`/`proactive_skip` records reuse —
  one writer, one schema); ADR-001 (the `JsonSessionStore` atomic/`0600` write discipline the schedule
  definitions persist through); `docs/features/p14-proactive/design.md` (the full design, the spike
  findings, and the O1–O3 scope calls); `docs/cross-cutting-requirements.md` (SB1/SB3/SB4/RB1/RB3/RB4).

---

## Context

Today the bot is **pull-only**: every turn is initiated by an operator message (or a tap). The roadmap's
phase-6 "proactive" capability is the **push** half — the bot does useful work **on a schedule** without
being prompted and notifies the operator with the result: *"run my tests at 9am and ping me if it's red,"*
*"every hour check the repo and tell me if CI is failing,"* *"summarize what changed each evening."* The
operator schedules a recurring prompt; at fire time the bot drives it as a normal turn through the
**existing** streaming engine — same gate, same audit, same send budget — and the result lands in the chat.

The dominant design concern, and the thing that makes this a **security-core** phase like P13, is that **a
proactive turn runs with no human present at fire time.** It is therefore the **highest-trust-stakes** path
in the whole bot, and the entire design is organized around one invariant: **proactive is NOT a bypass of
the approval gate.** A scheduled turn's tool-uses hit the exact same `on_tool_request` chokepoint a typed
turn uses; a risky tool with no one there to tap *Allow* **holds until the answer-backstop auto-DENIES it**
(RB4), so a proactive turn *cannot* silently do risky things — it fails safe.

Two capabilities were uncertain and were **spiked read-only first** (design §2, run against the ready P13
venv — `claude-agent-sdk==0.2.105`, `python-telegram-bot==21.11.1`):

1. **Scheduler fit.** The bot is a long-running PTB **polling** app on a single asyncio loop with a
   `post_init` / `post_shutdown` lifecycle. The obvious candidate — PTB's `JobQueue` — is **unavailable in
   this deployment**: `application.job_queue` is `None` at runtime and PTB warns *"install PTB via
   `python-telegram-bot[job-queue]`"*; the project pins PTB **without** the `[job-queue]` extra, and
   **`apscheduler` is not installed**. Using `JobQueue` (or `apscheduler` directly) would add an
   `apscheduler` runtime dependency we don't need — the one-bot-per-token invariant means we never need
   apscheduler's persistence/clustering. A plain `asyncio` task fits the existing loop with **zero new
   dependencies** and is trivially unit-testable with an injected clock + sleep (the project-wide pattern).
2. **MCP integration** (a sibling roadmap-6 candidate) — **FEASIBLE and gated-for-free** (the SDK accepts
   `mcp_servers`; MCP tools surface as `mcp__<server>__<tool>` and flow through the **same** `can_use_tool`
   the bot installs; the classifier already fail-closes *any* `mcp__*` name to RISKY). But the *gating* is
   the easy 10% — the config/auth/subprocess-lifecycle surface is an orthogonal lift, so MCP is **deferred**
   (below) while proactive — the distinctive headline — ships.

The bar (from the design): **never weaken the existing gate**, keep the safe default = current behavior (or
strictly safer), fail **closed**, stay **body-free (SB3)**, never let a persist/audit write wedge a turn
(**RB1**). The scope is deliberately the **one** high-value core (the proactive scheduler) with the other
two roadmap-6 areas explicitly deferred (below) — the ADR-006/P13 scope-discipline pattern.

## Decision drivers

- **ADR-003 (the gate this feature runs through).** `on_tool_request` is the single substrate-neutral
  chokepoint for every tool decision; safe tools auto-run, risky tools hold, `/yolo`/grants are the
  bypasses. P14 **runs proactive turns through this unchanged chokepoint** and *adds* one strictly-safer
  inversion (force-gate) — it rebuilds nothing.
- **ADR-002 (the fail-safe backstop).** A pending Allow/Deny is bounded by the ~60-min answer-backstop,
  which auto-**denies** + notifies. This is what makes an unattended risky hold safe by construction — the
  scheduler adds nothing to enable it.
- **ADR-005 (the concurrency/send substrate).** The per-chat `ChatSendGate`, the `MAX_CONCURRENT_RUNS` cap
  + FIFO queue, and the per-project busy-guard already make concurrent turns safe and rate-limited. A
  proactive turn **reuses** all of it by going through the existing `handle_message`.
- **ADR-007 (the durable audit).** A proactive turn's tool decisions are already audited at the chokepoint;
  the *lifecycle* (a fire, a skip) reuses the same `AuditLog`/`ChatBoundSink` — one writer, one schema.
- **ADR-001 (the persistence discipline).** Schedule definitions persist through `JsonSessionStore`'s atomic
  `0600` write, with corrupt/missing reads degrading to empty — the scheduler invents no new posture.
- **Cross-cutting:** **SB1** (only the allowlisted owner schedules/fires — re-checked at fire time, not just
  create), **SB3** (no bodies — the schedule prompt is never displayed or audited), **SB4** (the prompt is
  the engine's prompt, never a shell command; a schedule name is the safe `^[A-Za-z0-9_-]{1,32}$` shape),
  **RB1** (a persist/fire failure never wedges a turn or the loop), **RB3/RB4** (abandon-and-lazy-resume on
  restart; backstop-deny on an unattended hold).

## Decision

**Ship ONE cohesive primitive: an operator-managed proactive scheduler that fires a saved prompt as a
normal, fully-gated turn on a recurring interval, driven by a plain asyncio task (NOT PTB `JobQueue`),
persisted atomically (`0600`) and re-armed-from-now on restart. A proactive turn runs through the SAME
permission gate but with a per-turn FORCE-GATE that makes it ignore a standing `/yolo` and session-grants —
so an unattended fire can NEVER inherit allow-all; a risky tool with no human to approve holds and the
answer-backstop auto-DENIES it. Proactive is NOT a gate bypass.**

### 1. The scheduler — interval schedules, fired through the existing turn stack

- **The model (`claude_tg/scheduler.py`).** A **pure-ish** module mirroring `permissions.py` / `bash_policy.py`
  isolation (no telegram, no SDK, no real clock, no I/O): a frozen `Schedule`
  `(name, interval_seconds, prompt, chat_id, next_run, project, paused, created_at)`, an interval parser
  `parse_interval("30m"/"1h"/"2d"/"90s") -> seconds` (single count+unit; **no** compound `1h30m` in v1),
  and the pure scheduling math (`Schedule.due(now)`, `Schedule.compute_next_run(now)`, plus
  `due_schedules` / `next_wake` helpers). Everything takes an **injected** `now` so the whole thing is
  deterministically unit-testable.
- **The schedule is dormant data; the driver decides timing.** `claude_tg/scheduler_driver.py`'s `Scheduler`
  is the **single asyncio task** (started in `post_init`, cancelled in `post_shutdown`): each tick it reads
  the schedules **fresh from the store** (the source of truth — so a `/every`·`/pause`·`/unschedule` between
  ticks is honored with no cache to invalidate), fires every due (non-paused) one, reschedules each
  (`next_run = now + interval`, persisted), and sleeps until the next wake (`next_wake`) — capped by a
  ~30 s poll fallback so a schedule created while it slept is still picked up. Clock + sleep are injected.
- **A fire reuses the entire turn stack — it does not re-implement turn-driving.** A fire calls
  `StreamingSession.fire_schedule`, which calls the **existing**
  `handle_message(chat_id, prompt, send=…, edit=…, delete=…, command_initiated=True, proactive=True,
  project_override=name)`. That single call gives us, for free: the per-chat `ChatSendGate` (no flood), the
  `MAX_CONCURRENT_RUNS` cap + FIFO queue (a fire at the cap queues, never drops or runs over-cap), the
  per-project busy-guard (a fire into an already-running project is **skipped this tick** with a body-free
  `⏰ skipped <name> — still working` notice + a `proactive_skip` audit, never queued-on-itself, never
  wedged), and the full gate + audit + path confinement (the identical `engine.send → on_tool_request`
  chokepoint). `command_initiated=True` is **load-bearing** — it makes the fired turn a FRESH turn that can
  never be swallowed as the answer to some other project's pending free-text hold. The turn's normal
  streamed output **is** the notification; a small body-free `⏰ <name> (scheduled)…` header is prepended on
  fire so the operator knows the turn was machine-initiated and which task.
- **Streaming-only.** One-shot has no `StreamingSession`, no per-chat send gate, no holds — a proactive turn
  there has nowhere safe to land — so the six commands reply the standard "streaming mode only" notice in
  one-shot, exactly like `/projects`.

### 2. ⭐ The unattended-action security model (the core decision)

A scheduled turn calls the **identical** `handle_message → engine.send → on_tool_request` chokepoint a typed
turn uses. There is **no proactive-specific tool path** and **no pre-authorization that widens what runs.**
Therefore:

- A **safe** tool (Read/Glob/Grep/LS/WebSearch/TodoWrite) auto-runs — exactly as for a typed turn — so a
  read-only proactive task ("summarize what changed") completes fully unattended (the common, valuable case).
- A **risky** tool (Write/Edit/Bash/WebFetch/`mcp__*`/unknown) fired with **no operator present** is injected
  as a `PermissionEvent` and **HELD**; since no one taps Allow, the **answer-backstop auto-DENIES it**
  (ADR-002 / RB4) and the turn continues with that tool denied. The proactive turn **cannot silently perform
  a risky action** — fail-safe by construction, falling straight out of the existing gate. An out-of-root
  file tool re-prompts → backstop-denies the same way; a Bash-policy-flagged command (P13) is escalated →
  held → backstop-denied (or auto-denied outright under `BASH_POLICY_MODE=deny`).

- **A proactive turn must NOT inherit a stale `/yolo` or a session-grant — the per-turn FORCE-GATE.** `/yolo`
  and allow-session grants are per-session, in-memory, restart-cleared (ADR-003 D7). The subtle hazard: if
  the operator left a project in `/yolo` and a scheduled task then fires against it **while they are away**,
  the turn would inherit allow-all — an *unattended* allow-all, the exact posture forbidden. **Decision: a
  proactive fire runs with the gate FORCED ON for that turn regardless of the project's `/yolo` flag and any
  session-grant.** Mechanism: `handle_message(proactive=True)` sets a per-turn `_force_gate` flag on the
  engine for the duration of that `send` (reset in its `finally`); `on_tool_request` then treats
  `policy.yolo` **and** every allow-session grant as **off** — a tool holds **iff it is risky**
  (`is_risky`), period. Crucially this does **not** mutate the persistent `PermissionPolicy` — the
  operator's interactive `/yolo`/grants are untouched for their own later typed turns; the per-project turn
  lock means a proactive turn and a typed turn never run on the same engine at once. This is the small,
  explicit inversion at the heart of the phase — the proactive analogue of ADR-007's "a flagged Bash command
  re-prompts even under `/yolo`" — and it is **tested** (a proactive turn under a yolo'd project still holds
  a risky tool and backstop-denies it; the mutation-probe — dropping the `_force_gate` guard — flips the
  test).

- **SB1 is re-checked AT FIRE TIME (not just at create).** Every schedule command rides the `allowed` chat
  filter + the explicit `_ok` allowlist recheck, and a schedule is **bound to the chat that created it and
  only ever fires INTO that chat** (it can never target another chat). On top of that, `fire_schedule`
  **re-checks `schedule.chat_id` against the live allowlist before anything else** — a chat **removed** from
  `TELEGRAM_ALLOWED_CHAT_IDS` since the schedule was created can **never** still receive a proactive fire
  (skipped with no header, no turn, + a body-free `proactive_skip` `decision="unauthorized"`). SB1 holds at
  fire, not just at create.

- **The audit trail records proactive actions (ADR-007 tie-in).** Because a proactive turn goes through
  `on_tool_request`, **every tool decision it makes is already audited** at the chokepoint (auto-allow /
  backstop_deny / etc.) — body-free, with the redacted session tag. P14 **adds** two body-free lifecycle
  records via the **existing** `AuditLog`/`ChatBoundSink` (one writer, one schema — no new plumbing): a
  **`proactive_fire`** `session_event` when a scheduled turn starts and a **`proactive_skip`** when a fire is
  skipped (busy / unauthorized / error). Both carry the schedule **name** (the operator's own label) and the
  pinned project — **never the prompt text**. `/audit` then shows the proactive fires interleaved with the
  tool decisions, so the owner can reconstruct "what did the bot do while I was away" body-free.

- **`/schedules` is body-free.** The listing shows name / interval / relative next-run / a `⏸` paused marker
  / the pinned project — and a **truncated, HTML-escaped prompt preview** (the operator's own text, shown
  like `/macros` previews a macro; capped length, SB3). The prompt is **persisted-to-fire** but the durable
  audit log never records it.

### 3. The scope calls (cite the design's O1–O3)

- **O1 — interval-only, `SCHEDULER_ENABLED` default-on-but-empty.** v1 is **interval** (`/every 30m` /
  `/every 6h` / `/every 1d`), which covers the headline cadences ("every hour", "every morning" ≈ `every
  24h`) without a cron-expression parser. `SCHEDULER_ENABLED` defaults **ON** (design O1): the loop runs but
  does **nothing** until the owner creates a task (no schedules exist on a fresh install), so default-on is
  zero-risk and non-breaking, and requiring a flag to use the headline feature would be needless friction;
  `SCHEDULER_ENABLED=false` opts out entirely. The per-task `enabled`/`paused` is the real control regardless.
- **O2 — a calendar/cron parser is a fast-follow, not v1.** A natural next step is an **anchored daily**
  (`/daily 09:00 …`) so "at 9am" is precise (an interval drifts relative to wall-clock across restarts) —
  but it is deferred to keep v1 a single clean primitive. Interval drift is documented, not hidden.
- **O3 — gate-only, with NO per-task pre-authorized scope in v1.** This is the central trust call: we
  deliberately do **NOT** add a per-task "this task may write/run Bash unattended" grant. Reasons: (a) the
  gate already gives the safe-and-useful behavior (reads run; risky holds-then-denies) with **zero** new
  trust surface; (b) a per-task pre-authorization would be a *new, persistent, unattended* allow-all — the
  precise posture ADR-003 D7 keeps in-memory and restart-cleared *because* unattended allow-all is the
  cardinal risk. A proactive task that genuinely needs to write/run is the operator's signal to **be
  present** (`/runnow` and tap Allow, or run it as a typed turn). A write-heavy scheduled task simply
  **stalls on the first risky tool until it backstop-denies** — annoying, not dangerous — which is the
  correct **fail-safe bias.**

### 4. Persistence + restart (RB3) — re-arm-from-now, no replay

Schedule definitions persist through `JsonSessionStore` (ADR-001 atomic `0600`), namespaced per chat
alongside macros/projects, bounded per chat by `SCHEDULE_MAX_TASKS_PER_CHAT` (fail-closed — a create over
the cap is refused with a clean message, never silently over-cap). On restart the driver **re-arms** every
schedule's `next_run` to `now + interval` **before** the first tick: **missed fires while the bot was down
are NOT replayed.** Rationale (RB3): replaying a burst of missed unattended gated turns on startup is a
flood + thundering-herd hazard and rarely what the operator wants ("run tests at 9am" missed because the Mac
slept should fire at the *next* 9am, not immediately on wake with 14 stacked runs). This is the
**abandon-and-lazy-resume** posture P5/RB3 already uses for in-flight turns, applied to schedules. With **no**
state file (a stateless deploy) schedules are in-memory + documented transient — a clean, non-surprising
degrade (mirroring how the audit log is off with no state file). The schedule-store write is best-effort,
never on a turn's critical path (RB1): a failed persist logs body-free and is swallowed — the task degrades
to its in-memory cadence for this process, it never wedges the loop or a turn. `/yolo` and grants are
**never** persisted (ADR-003 D7).

## Consequences

**What P14 builds.** `claude_tg/scheduler.py` (the frozen `Schedule` + `parse_interval` + `format_interval`
+ the pure `due`/`compute_next_run`/`due_schedules`/`next_wake` math); `claude_tg/scheduler_driver.py` (the
single RB1-total asyncio `Scheduler` task — re-arm-on-start, tick-fire-reschedule-sleep, clean cancel on
shutdown); `StreamingSession.fire_schedule` + `_proactive_skip_busy` (the fire seam: SB1-at-fire re-check →
busy pre-check → body-free header + `proactive_fire` audit → drive via `handle_message`, RB1-total);
`handle_message`'s two additive default-off kwargs `proactive: bool = False` (the force-gate) +
`project_override: str | None = None` (pin the target project); the engine's `_force_gate` honoring of
`proactive` (`_needs_hold` returns `is_risky(...)` when forced, ignoring `/yolo`/grants — without mutating
the policy); the schedule CRUD on `JsonSessionStore` (`add_schedule` with the per-chat cap /
`remove_schedule` / `list_schedules` / `set_schedule_paused` / `get_schedule` / `get_active` /
`rearm_all_schedules`, atomic `0600`); the two new audit records (`proactive_fire` / `proactive_skip`) via
the existing sink; the six commands (`/every` `/schedules` `/unschedule` `/pause` `/resume` `/runnow`), each
`_ok`-gated + streaming-only, plus their `COMMAND_MENU` + `HELP_TEXT` entries (the lock-step test); the
config knobs (`SCHEDULER_ENABLED`, `SCHEDULE_MAX_TASKS_PER_CHAT`, `SCHEDULE_MIN_INTERVAL_SECONDS`, all
validated, all defaulting to the non-breaking value); the `render.schedule_listing` body-free renderer; and
the SB/RB test matrix (the force-gate test is the gate-blocking security bar).

**What P14 reuses (does not rebuild).** The streaming turn driver + per-project lock + `MAX_CONCURRENT_RUNS`
cap + FIFO queue + per-chat send gate (P5/ADR-005); the permission gate + classifier + `PermissionEvent` +
the ~60-min answer-backstop (P2/P6/ADR-002/ADR-003); the durable body-free `AuditLog` + `ChatBoundSink`
(P13/ADR-007); the `JsonSessionStore` atomic `0600` persistence (ADR-001); the `post_init`/`post_shutdown`
lifecycle hooks; and the SB1 `_ok` command-gating template. The force-gate is one per-turn flag; the fire
seam is one method; the audit is two records on the existing writer.

**The honest limits.** (a) **Interval drift** — an interval schedule drifts relative to wall-clock across
restarts (re-armed from now); "at exactly 9am" needs the deferred anchored-daily, and this is documented,
not hidden. (b) **No unattended writes in v1** — a write/Bash-heavy scheduled task stalls-then-denies
unattended (O3); that is the deliberate fail-safe bias, not a bug — the operator uses `/runnow` (present) or
a typed turn for write-heavy work. (c) **Single-writer** — one in-process asyncio task + one store/audit
writer, consistent with the one-bot-per-token invariant; no second process, no apscheduler clustering.

**Residual risk.** (a) **A long-lived unattended hold** consumes a concurrency slot until the backstop fires
— bounded by the cap + the backstop, body-free, and the correct fail-safe (deny). (b) **A proactive header /
skip notice is an extra send** — funnelled through the per-chat `ChatSendGate` so it can't burst past
Telegram's ceiling. (c) **A misbehaving fire** (a render/engine/store hiccup mid-turn) is caught in
`fire_schedule` (RB1-total) and the driver's per-tick guard (belt-and-braces) — a single bad schedule can
never kill the loop or the bot.

**Deferred — not in P14 (revisit triggers, ADR-006 style).**

- **MCP server integration.** The spike proves MCP is **gated + audited for free** — the SDK accepts
  `mcp_servers`, MCP tools surface as `mcp__<server>__<tool>` through the **same** `can_use_tool`, and the
  classifier already flags **any** `mcp__*` name RISKY (so an MCP tool-use holds for approval exactly like
  Bash, with zero new gate code). But the *gating* is the easy 10%; the real lift is the **config + auth +
  operational** surface — which servers ship, where their secrets live (a new secret class the SB3/secret-scan
  posture must cover), the **stdio subprocess lifecycle** (a `npx`-spawned server is a child process the bot
  now owns — a new RB surface), and per-project enablement. That is a cohesive *integration* phase of its
  own, orthogonal to proactive scheduling. **Trigger:** the owner names ≥1 concrete MCP server they want AND
  a place for its auth the secret-scan posture accepts; the build is then small — thread `mcp_servers` into
  `_build_options`, add a config knob, **keep MCP tools OUT of `allowed_tools`** (so they always gate — the
  spike caveat: a pre-allowed tool would bypass `can_use_tool`), and add a test that an `mcp__*` tool-use is
  held + audited. Lowest-risk first cut: an **in-process** `create_sdk_mcp_server` with a couple of read-only
  bot-defined tools (no external subprocess, no third-party secret).
- **A dedicated git/PR surface (`/diff` `/commit` `/pr` `/ci`).** The lowest-value of the three — the bot
  **already drives git** through **Bash** (P13-policy-guarded — force-push is flagged — AND audited); a typed
  *"commit this and open a PR"* already works, fully gated. A dedicated `/git` would be a thin, lower-trust
  wrapper over what Bash covers. The one genuinely new thing — **proactive notify-on-git-event** ("ping me
  when CI goes red") — is **subsumed by the scheduler this phase ships** (`/every 10m ci "check CI status,
  ping me only if red"`). **Trigger:** the owner wants a *one-tap* git affordance (a `/diff` rendering a diff
  as a file via `/get`, or a `/pr` wrapping `gh`) often enough that typing the Bash turn is friction.
- **True event-driven hooks (a CI webhook → fire).** A real push/webhook listener means the bot opens an
  **inbound network surface** (an HTTP endpoint) — a significant new attack surface and a posture change (the
  bot is currently outbound-only via PTB long-polling). The **interval scheduler covers the practical need**
  (a `/every 10m` CI check is the polling equivalent of a CI-fail hook, with no inbound surface); `/runnow`
  is the manual "event" trigger. **Trigger:** the owner needs sub-minute reaction to an external event AND
  accepts an inbound endpoint with its own SB1-equivalent authentication. A high bar; likely never for a
  single-operator personal bot.
