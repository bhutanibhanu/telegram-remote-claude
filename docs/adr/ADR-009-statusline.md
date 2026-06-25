# ADR-009 — A live mobile statusline pinned at the top of the chat

> Derived from the STATUSLINE design ([`docs/features/statusline/design.md`](../features/statusline/design.md))
> and the owner's "bring Claude Code's terminal statusline to the phone" vision. A **delta** on the
> security-audited P0–P14 tree. Builds on **ADR-005** (the per-chat `ChatSendGate` + the per-project
> `_ProjectRuntime.status` enum + the foreground-vs-background send-decision this line reuses and obeys),
> **ADR-004** (the per-project model override + the `(session_id, cwd)` working-dir naming the worktree
> field reads), **ADR-003** (the gate/yolo posture the `🔒 mode` field surfaces), **ADR-001** (the atomic
> `0600` persistence the new effort override reuses), and **ADR-007** (P12's `/thinking` visibility toggle
> the new `/effort` depth dial is deliberately kept distinct from). Sibling of
> [ADR-001](ADR-001-session-substrate.md) … [ADR-008](ADR-008-proactive-scheduler.md).

- **Status:** **Proposed** — chosen model for STATUSLINE; owner reviews with the `feat/statusline` branch.
- **Date:** 2026-06-24
- **Deciders:** repo owner
- **Related:** ADR-005 (the `ChatSendGate` the pin/edit funnel through as **non-verbatim**; the
  `_ProjectRuntime.status` enum that drives the working marker; the `_is_foreground`/active-project rule
  the line obeys); ADR-004 (the per-project model override read back for the `🤖` field; the per-project
  persistence schema the effort override extends); ADR-003 (the gate/`/yolo`/plan posture the `🔒` field
  reflects — the line makes the mode *more* visible, never mutates it); ADR-001 (the `JsonSessionStore`
  atomic/`0600` write the effort override persists through); ADR-007 / P12 (the `/thinking` *visibility*
  toggle the new `/effort` *depth* dial is orthogonal to); `docs/features/statusline/design.md` (the full
  design, the two SDK spikes, and the §3.4 scope deferrals); `docs/cross-cutting-requirements.md`
  (SB1/SB3/SB4/RB1/RB3/RB5).

---

## Context

Claude Code's **terminal** statusline is a single always-present line condensing the session's state —
cwd, model, context-window usage, mode. The bot had no phone equivalent: the operator's only persistent
"what is the bot doing and how is it configured" signal was a per-turn `✅ done (success) · 1 turn · $0.20`
**footer** that scrolled away with the next message, and the *only* live state surface was the transient
mid-turn 💭/🧠/▶️ status line (deleted at turn end). There was no calm, persistent answer to "what is the
bot doing right now," and routine output was sprinkled with **dollar amounts** the owner did not want on
every turn.

This feature brings the terminal statusline to the **phone**: one message **pinned at the top of the
Telegram chat and edited in place**, so the operator always sees the bot's current state at a glance
without scrolling and without a re-ping on every update:

```
📁 claude-telegram-bot-statusline · 🤖 opus·max · 🧠 ctx 6% · 🔒 gate
```

(with a leading `⚙️` while a turn runs). It **replaces** the per-turn done-footer and **removes dollar
amounts from all routine output** — cost survives only on the explicit `/status` health view.

Two capabilities were uncertain and were **spiked read-only first** against the installed SDK
(`claude-agent-sdk==0.2.105`, `python-telegram-bot==21.11.1`) with **real probes** (a live `query()` turn
+ `ClaudeAgentOptions` introspection; design §2 records the verbatim output):

1. **Context-window-used %** — is there an honest figure, or must we fabricate a ratio? The SDK exposes
   `ClaudeSDKClient.get_context_usage()` returning a `percentage` field that is the **same number the CLI
   `/context` shows** — first-class and honest, no model-id→window table needed.
2. **Reasoning effort** — `ClaudeAgentOptions` has a dedicated `effort` field
   (`Literal['low','medium','high','xhigh','max']`) accepted by the installed SDK end-to-end — a real,
   settable knob distinct from P12's `/thinking`. **Both unknowns resolved; no fabrication, no fallback
   table needed.**

The bar (the project-wide posture): never weaken the gate, keep the safe default = current behavior or
strictly safer, stay **body-free (SB3)**, and never let a render-side write wedge a turn (**RB1**). The
statusline is an **observer off the turn's critical path** — the binding constraint is SB3, and the
make-or-break invariant is RB1.

## Decision drivers

- **ADR-005 (the send/concurrency substrate).** The per-chat `ChatSendGate` (~1 msg/s) already makes every
  outbound send rate-safe; the per-project `_ProjectRuntime.status` enum already tracks running/awaiting/
  idle; the `_is_foreground` rule already decides inline-vs-notify. The statusline **reuses** all three —
  its pin/edit go through the gate as **non-verbatim** edits (so it can never flood and never starves a
  real prompt/answer), its working marker reads the status enum, and it writes **only** for the foreground
  project. It rebuilds none of this.
- **ADR-004 (the per-project knobs + persistence).** The model override and the worktree name are already
  per-project + persisted; the effort knob is a **parallel** per-project override on the same store.
- **ADR-003 (the gate posture).** The mode field is read-back state — the line makes `🔒 yolo`/`plan`/`gate`
  *always visible* (reinforcing D6 "loud throughout"), but it never mutates the policy.
- **ADR-001 (the persistence discipline).** The effort override persists through `JsonSessionStore`'s
  atomic `0600` write; the live pin reference does not (RB3).
- **Cross-cutting:** **SB1** (the line is sent/pinned/edited only in the allowlisted chat — reusing the
  existing send closures, no new outbound surface), **SB3** (body-free by construction — the line carries
  only a validated name, a model label, a fixed effort word, an integer %, and a fixed mode word),
  **RB1** (a pin/edit/send failure never breaks a turn), **RB3** (the pin id is transient; the effort
  override persists), **RB5** (all line I/O funnels through the gate).

## Decision

**Ship ONE pinned mobile statusline — a single message pinned once (silently) at the top of the chat and
edited in place through the per-chat send-gate — that condenses the foreground project's live state
(`📁 worktree · 🤖 model·effort · 🧠 ctx X% · 🔒 mode`, with `⚙️` while a turn runs). It is FOREGROUND-ONLY
(a background concurrent turn never rewrites it), RB1 best-effort (any pin/edit failure is swallowed —
never breaks a turn — with orphan recovery if the user unpins and pin-retry if a pin fails), and it
REPLACES the per-turn done-footer (removing dollars from all routine output — cost moves to `/status`).
Add a parallel `/effort` per-project knob (the SDK's `ClaudeAgentOptions.effort`) and source `ctx %` from
the live, honest `get_context_usage().percentage`.**

### 1. The pinned statusline — pin once, edit in place, foreground-only

- **The line.** A pure formatter `render.format_statusline(*, worktree, model_label, effort, ctx_pct,
  mode, working) -> str` (body-free, no I/O) renders the owner-locked format
  `📁 <worktree> · 🤖 <model>·<effort> · 🧠 ctx <X%> · 🔒 <mode>`, with a leading `⚙️ ` when `working`.
  A helper `model_short_label(model_id)` reduces a model id to `opus`/`sonnet`/`haiku` by family (an
  unrecognized id falls back to the raw id verbatim — RB1; `None`/blank → `default`, so the bar shows
  `🤖 default` when no model is set, never a bare `🤖 `).
- **Pin once, edit silently thereafter** (`stream_session._update_statusline`, per-chat
  `_ChatState.statusline_message_id` + `statusline_text`, transient). **First update** for a chat: `send`
  the body then `pin_chat_message(disable_notification=True)` — a **silent** pin. **Subsequent updates:**
  `edit_message_text` only — no re-pin, no re-send, no notification (a pinned message edited in place stays
  pinned and silent). **Identical-text skip:** if the rebuilt body equals what's pinned, the update is
  skipped entirely (before the gate) — a no-op edit raises "message is not modified" and wastes a send
  slot. There is **one** pinned message per chat — we only ever edit the one id, and re-point it on
  recovery.
- **When it updates** (the trigger set, kept small — the line is *state*, not a progress bar): **turn
  start** (working marker on; refresh model/effort/mode/worktree), **turn end** (working marker off;
  refresh `ctx %` — the context just grew and the client is alive), **`/switch`** (worktree + all
  per-project fields change), and each **knob change** (`/effort`, `/fast`·`/deep`·`/auto`, `/yolo`·
  `/unyolo`, `/plan`). It does **NOT** update on every event/delta — mid-turn progress stays on the
  existing transient 💭/🧠/▶️ line (unchanged). This bounds edits to a handful per turn, far under the
  gate budget (RB5).
- **Foreground-only.** Under concurrency (N projects per chat, ADR-005) the line reflects the chat's
  **active (foreground)** project — the one `store.get_active` returns, the one the operator is watching. A
  **background** project's turn start/end does **not** rewrite the line (its progress is its 🔔/✅ ping, D4),
  so the single pinned line always describes "what you're looking at." This is enforced at the wiring
  (`_maybe_update_statusline(for_project=…)` skips when the named project is not foreground) **and** —
  critically — by the foreground-write invariant below.
- **It replaces the done-footer + dollars.** `render._render_result` no longer appends the `· N turns ·
  $X.XX` footer: a result with prose renders the prose alone; a result with no prose renders a bare
  `✅ done (<subtype>)`. **Dollars are gone from every routine path.** The cumulative per-project cost
  survives only on `/status` (`bot.cmd_status` reads `store.get_cost` directly — untouched).

### 2. `ctx %` — the live, honest figure (never fabricated)

- **Primary source:** `Engine.context_percentage()` (the SDK adapter, `adapter_sdk.py`) calls the live
  `ClaudeSDKClient.get_context_usage()` and returns `round(resp["percentage"])` — the **same number the
  CLI `/context` shows** (spike-proven, design §2.1). The SDK reports the model's raw context window itself
  (`rawMaxTokens`/per-model `contextWindow`), so **no model-id→window mapping table is hard-coded** (a `1M`
  beta would track automatically).
- **⭐ The call is a COROUTINE.** The installed SDK's `get_context_usage()` is `async`
  (`inspect.iscoroutinefunction` is True), so it **must be awaited** — `context_percentage()` awaits the
  awaitable (and still accepts a plain dict, defensively, so a future sync build keeps working). This
  threaded the whole way up: `_statusline_text` is `async` and awaits it. Missing this would silently
  degrade every line to the fallback.
- **Honest fallback.** If there is no live client, the method is absent, or it raises, the adapter derives
  `round(100 * tokens / window)` from the **last `ResultMessage`'s** usage — `tokens = input + cache_read +
  cache_creation` (the last turn's input ≈ the current context size) and `window` = the per-model
  `contextWindow` the SDK reports (`_capture_usage` stashes both, fully defensively; the cache is dropped
  when the session stops — RB3, no stale carryover).
- **`🧠 ctx —` when unavailable.** If neither path yields a figure (no client AND no completed turn yet),
  the value is `None` and the line shows `🧠 ctx —` (an em dash) — **never a fabricated `0%`** (design
  §2.1 forbids it). The whole call is best-effort (RB1): any raise → fallback → `None`, never an exception
  escaping to the turn.

### 3. The `/effort` knob — a depth dial, distinct from `/thinking`

- **What it is.** `ClaudeAgentOptions.effort` (`Literal['low','medium','high','xhigh','max']`) is a
  reasoning **depth/intensity** dial (low = minimal/fastest … `max` = maximum effort). A new per-project
  `/effort <level>` command sets it; a bare `/effort` (or `/effort default`) clears it back to the SDK
  default (`high`). It is **per-project + persisted** (`session_store.set_effort`/`get_effort`, atomic
  `0600`, validated against `{low,medium,high,xhigh,max}` — an unknown value normalizes to `None`, RB1) —
  exactly parallel to the model override (`/fast`·`/deep`·`/auto`). It is threaded into
  `ClaudeAgentOptions(effort=…)` only when an override is present (a default-effort turn never sets the
  kwarg, so behavior is byte-for-byte unchanged when unused), on every session-creation path (start AND
  resume).
- **Distinct from P12 `/thinking`.** `/thinking` is a **visibility** toggle — it sets
  `thinking={"type":"adaptive","display":"summarized"}` + `include_partial_messages=True` so Claude's
  reasoning *streams as the 🧠 line*; it does not dial depth and it adds partial-message wire traffic.
  `/effort` is orthogonal: it changes *how hard* Claude thinks, costs nothing extra in wire traffic, and is
  what "opus at **max**" refers to (it supersedes the deprecated `max_thinking_tokens` budget knob). The two
  are independent per-project knobs.
- **Applies on the NEXT fresh session, never mid-turn.** Effort is a session-creation param baked into
  `ClaudeAgentOptions`; a turn in flight keeps its current effort. The warm-engine fast-path includes
  `effort` in its match-key (`_ProjectRuntime.engine_effort`), so a change rebuilds the session on the next
  turn (mirroring the model/thinking rebuild). Streaming-mode only (one-shot replies the standard
  "streaming mode only" notice).
- **Displayed as `🤖 model·effort`** (e.g. `🤖 opus·max`), or `🤖 model` when no effort override is set, or
  `🤖 default` when no model is set either. The current level is therefore always visible.

### 4. ⭐ The foreground-write invariant (the QA-hardened bit)

A statusline line is **written only if the project it was built for is still the chat's foreground at the
synchronous instant just before the write.** This closes the `/switch`-during-`ctx`-await race: the line
body is built from the foreground project's state, but **two awaits** precede the write — the `ChatSendGate`
wait AND the `ctx %` `await` inside the rebuild — and **both are `/switch` windows**. So the gated write
helpers (`_statusline_gated_edit` / `_statusline_send_and_pin`):

1. **Reserve the gate slot and await its wait** (non-verbatim — RB5), then
2. **Rebuild the body + the project it was built for from CURRENT state** (`_statusline_text` returns
   `(text, built_for)`), then
3. do a **FINAL synchronous foreground re-check** — `_is_foreground(built_for)` — with **NO await** between
   that check and issuing the `edit`/`send`.

If a `/switch` happened during any await, `built_for` is no longer foreground → the **stale write is
skipped** (the `/switch`'s own statusline trigger writes the correct line — no loop, no stale write). An
empty rebuild (the foreground vanished, e.g. `/rm`) or identical text also skips. This is the make-or-break
correctness property under concurrency and is the bit Codex QA hardened.

**SB1 (allowlisted chat only).** The line is sent/pinned/edited only in the operator's allowlisted chat —
driven from `StreamingSession` turn/command paths already behind the bot's `_ok`/`_authorized` recheck, and
the injected `pin`/`unpin` closures (over `Bot.pin_chat_message`/`unpin_chat_message`) target the **same
`chat_id`** as every other send. **No new outbound surface** — it reuses the existing send/edit closure
pattern.

**SB3 (body-free).** Every field is bot-derived state, never a body or secret: `worktree` is an
SB4-validated project NAME (`^[A-Za-z0-9_-]{1,32}$` — no `/`, so inert; a path-shaped value would be wrapped
via `code_path` so its segments can't linkify, the P8 fix); `model` is a config/SDK constant reduced to
`opus`/`sonnet`/`haiku`/`default`; `effort` is one of five fixed SDK literals; `ctx %` is an integer 0–100
(or `—`); `mode` is one of three fixed words. No tool input, file content, command text, session id, or
dollar amount ever reaches the line. The formatter is pure and HTML-escapes every interpolated value once
(escape-once insurance, mirroring `cmd_status`); the result is sent with `parse_mode="HTML"`.

### 5. RB1 / RB3 / RB5 — fail-safe, transient, rate-safe

- **RB1 (never crash a turn).** The whole of `_update_statusline` is wrapped so ANY exception (a raising
  `send`/`edit`/`pin`/`unpin`, a build error, a `get_context_usage()` that raises) is logged at debug and
  **swallowed** — identical discipline to the transient-status-line delete and `add_cost`. The line is an
  observer off the turn's critical path.
  - **Orphan recovery (user unpins / deletes it):** the next `edit_message_text` raises ("message to edit
    not found") → caught → clear the stored id, best-effort `unpin` the stale one (the one-pin invariant —
    Telegram's current pin is the newest, so the bar self-corrects), then re-send + re-pin a fresh line. The
    operator can unpin freely; the line reappears on the next state change. We do **not** fight the user by
    re-pinning on every edit — only when the edit target is gone.
  - **Pin-retry (a failed pin self-heals):** a send that succeeded while its pin *raised* leaves the line
    unpinned (`statusline_pinned = False`); a later update **retries the pin even on identical text**, so a
    transient pin failure self-heals instead of sticking unpinned forever.
- **RB3 (transient pin, persisted override).** `statusline_message_id`/`statusline_text`/`statusline_pinned`
  are **in-memory only** — a restart drops the pin reference (the line is re-created on the first
  post-restart update). The effort **override IS persisted** on the project (like the model override) so it
  survives a restart — consistent with ADR-004/ADR-003: posture knobs the owner sets deliberately persist;
  live-turn scaffolding does not.
- **RB5 (rate-safe).** All line I/O funnels through `ChatSendGate` as **non-verbatim** edits; the trigger
  set is a handful of updates per turn. It cannot flood and cannot starve verbatim output (a real
  prompt/answer is always prioritized over a statusline refresh).

## Consequences

**What the feature builds.** `render.format_statusline` + `render.model_short_label` (the pure formatter +
label helper); the per-chat pin lifecycle on `StreamingSession` (`_ChatState.statusline_message_id`/
`statusline_text`/`statusline_pinned`; `_update_statusline` + `_statusline_text` + the gated write helpers
`_statusline_gated_edit`/`_statusline_send_and_pin`/`_statusline_pin`; `_maybe_update_statusline` as the
foreground gate); the trigger wiring (turn start/end in the drive loop, plus `_refresh_statusline` after
`/switch` and every knob command in `bot.py`); the `ctx %` source (`Engine.context_percentage` awaiting the
SDK coroutine + `_capture_usage`'s honest fallback); the effort knob end-to-end
(`session_store.set_effort`/`get_effort`; `ClaudeAgentOptions(effort=…)` threading + `engine_effort` in the
warm-engine match-key; `StreamingSession.set_effort`; `bot.cmd_effort` + its `HELP_TEXT`/`COMMAND_MENU`
entries); the injected `pin`/`unpin` closures over `Bot.pin_chat_message`/`unpin_chat_message`; and the
done-footer removal in `render._render_result`.

**What it reuses (does not rebuild).** The per-chat `ChatSendGate` + `_gated_send`/`_gated_edit` (ADR-005);
the per-project `_ProjectRuntime.status` enum + `_is_foreground` (ADR-005); `store.get_active` + the
worktree-name/model-override plumbing (ADR-004); the `JsonSessionStore` atomic `0600` write (ADR-001); the
gate/`/yolo`/plan posture read back for the mode field (ADR-003); and the existing send/edit/delete closure
injection pattern (the pin/unpin closures are the same shape). The `/status` cost view is untouched.

**The owner-confirmed decisions.**
- **`⚙️` working marker.** A leading `⚙️` while a turn runs (dropped when idle), chosen over a trailing
  `· ⏳`. Foreground-only, so a background turn never flips it.
- **All five effort levels.** `/effort` exposes the full SDK set (`low/medium/high/xhigh/max`) — the SDK
  validates, and `xhigh` is documented as model-dependent (Opus-4.7 only; falls back to `high`) rather than
  hidden.
- **ctx refresh at turn-end.** `ctx %` is refreshed at turn end only (cheap, accurate — the context just
  grew and the client is alive). An on-demand `/context` full breakdown is the deferred §3.4 item.

**The honest limits.** (a) **ctx fallback is approximate** — when the live `get_context_usage()` is
unavailable the usage-derived ratio uses the *last completed turn's* input tokens, so it lags the live
figure slightly; it is honest (never fabricated) but is a best-effort approximation, and shows `—` rather
than a guess when even that is absent. (b) **Pin reference is transient** — a restart drops the pin id and
the line is re-created on the next state change (a brief gap until the first post-restart trigger). (c)
**One line, foreground only** — under concurrency the line describes only the foreground project; a
background turn's state is not on the line (by design — its progress is its ping). (d) **Best-effort
placement** — a persistent Telegram API failure on pin/edit degrades the line silently (the turn is
unaffected); the line is never load-bearing.

**Deferred — not in this feature (revisit triggers, ADR-006 style).**
- **`/context` full breakdown.** `get_context_usage()` returns per-category detail (`categories`,
  `mcpTools`, `memoryFiles`); the line shows only the headline `%`. **Trigger:** the owner asks to see
  per-category usage → a future `/context` command surfaces the breakdown.
- **Rate-limit display on the line.** The SDK exposes `RateLimitInfo`/`RateLimitStatus`; not in the locked
  format. **Trigger:** the owner hits rate limits and wants them on the line.
- **Session-id / cwd on the line.** The locked format shows the worktree *name*, not the full cwd or
  session id (those stay on `/status`/`/projects`) — keeps the line phone-sized.
- **Proactive `⏰` marker.** A proactive turn (ADR-008) could mark the line; deferred (the per-turn `⏰`
  header already exists for proactive fires). **Trigger:** the owner asks for it.
