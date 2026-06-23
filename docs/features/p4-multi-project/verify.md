# P4 multi-project — owner phone-verify checklist

The authoritative live check for P4. The engine/unit/integration tests (576 green) mock the
substrate and **bypass the real PTB callback path** — so, exactly as P3 found, the relay-level
truth (held-turn + a real inline-keyboard tap, restart-resume against real Claude session ids)
is only proven on the phone. Run this against a real bot + real Claude before merging to `main`.

Everything here is **streaming mode only**. One-shot mode is unchanged (P4 is additive behind
`ENGINE_MODE=streaming`); leaving it the default keeps the live bot safe.

## 0. Setup

- [ ] `TELEGRAM_BOT_TOKEN` — the bot token (never logged; redacted in any evidence).
- [ ] `TELEGRAM_ALLOWED_CHAT_IDS` — your numeric chat id(s), comma-separated.
- [ ] **`ENGINE_MODE=streaming`** — the multi-project commands are streaming-only.
- [ ] **`STATE_FILE`** — **required for P4.** The named-project registry persists here (atomic, `0600`,
      schema v2). Without it the bot still runs, but `/new` / `/switch` reply "projects need
      persistence" and there is nothing to resume across a restart (RB1: it must NOT crash — verify
      that graceful notice if you want, then set `STATE_FILE` for the real run).
- [ ] **`ALLOWED_ROOTS`** — set to a dir that CONTAINS the project paths you'll create (e.g.
      `/Users/ray/dev`). SB2 confines `/new` and is re-checked on every turn. If you leave it unset,
      it defaults to the workdir (so the auto-`default` project still works); if you set it to
      exclude your workdir, the auto-`default` first turn is refused by design (see check (h)).
- [ ] `ALLOW_ANY_PATH` — the deliberate opt-out (skips SB2 confinement). Leave unset for this run so
      the SB2 checks below are meaningful.
- [ ] If upgrading an EXISTING `STATE_FILE` from P1–P3 (a flat `{chat_id:{session_id,cwd}}`): keep it
      — check (i) verifies it migrates to a `default` project preserving your session.
- [ ] Launch: `.venv/bin/python main.py` from the repo root. Confirm the startup log shows
      `engine_mode: streaming`. **One instance per token** (a second poller triggers a Telegram
      `Conflict` loop).

## 1. Checks

### (a) `/new` creates a project confined to the roots, and switches to it
- [ ] Send **`/new work <a real dir inside ALLOWED_ROOTS>`** (e.g. `/new work ~/dev/somerepo`).
- [ ] It confirms creation at the **resolved** path and that it's now active.
- [ ] Send a normal message (e.g. "what dir are you in?") — Claude responds from that project's cwd.

### (b) A second project keeps independent state
- [ ] Send **`/new notes <another real dir>`** → confirms + switches to `notes`.
- [ ] Send **`/projects`** — both `work` and `notes` are listed; the active one (`notes`) is marked.
- [ ] In `notes`, ask Claude to "remember the word BANANA". Then **`/switch work`**.
- [ ] In `work`, ask "what word did I just ask you to remember?" — it should **NOT** know BANANA
      (independent conversation). **`/switch notes`**, ask again — it **should** recall BANANA
      (each project resumed its own Claude session).

### (c) Restart resumes both projects
- [ ] Stop the bot (Ctrl-C) and relaunch `main.py`.
- [ ] Send **`/projects`** — both `work` and `notes` are still there with their dirs; the previously
      active one is still active.
- [ ] In `notes`, ask "what word?" again — BANANA is still recalled (the persisted session resumed
      across the restart). Switch to `work` and confirm it's still a separate conversation.

### (d) `/rm` removes a non-active project; the active one is protected
- [ ] With `notes` active, send **`/rm work`** — removed (it notes the transcript stays on disk).
- [ ] Send **`/rm notes`** (the active one) — **refused**, told to switch away first.
- [ ] **`/projects`** — only `notes` remains.

### (e) ⭐ A turn in flight blocks `/switch` and `/new` (the load-bearing guard)
- [ ] Send a prompt that makes Claude **ask you a question** or **request permission** (e.g. start
      `/grill a tiny tool` so it asks a question, or have it try a file write so the permission
      prompt appears) — i.e. get the turn **parked awaiting your tap**.
- [ ] WITHOUT answering, send **`/switch notes`** (or `/new x ~/dev`). It must be **REFUSED**
      ("a turn is in flight — finish it or /cancel first"). The active project must NOT change.
- [ ] Now **tap the pending button** (answer the question / Allow the tool). The parked turn must
      **resume and complete** — i.e. the relay is not deadlocked by the refusal.
- [ ] (If it ever hangs after the refusal, that's the deadlock this guard prevents — report it.)

### (f) SB2 confinement on `/new`
- [ ] Send **`/new escape /etc`** (or any path outside `ALLOWED_ROOTS`) — **refused** ("outside the
      permitted roots"); `/projects` shows no `escape` project.
- [ ] Send **`/new nodir ~/dev/definitely-not-a-real-dir`** — **refused** ("not a directory").

### (g) `/cd` is fixed-per-project; `/pwd` shows the active project
- [ ] Send **`/cd /tmp`** — it replies that the cwd is **fixed per project** in streaming mode (use
      `/new`), and does NOT change anything.
- [ ] Send **`/pwd`** — it shows the **active project's** name + its cwd.

### (h) (Optional) RB3 — an interrupted run fails clean
- [ ] Start a turn, and while Claude is working (mid-run), **kill the bot** (Ctrl-C).
- [ ] Relaunch. Send `/projects` — the project is back and **idle** (no auto-replay of the killed
      turn). Send a new message — it resumes that project's session (or, if the transcript was torn,
      you get "⚠️ Couldn't resume … started a fresh one" and it proceeds). It must never hang.

### (i) (Only if you upgraded an existing P1–P3 STATE_FILE) migration
- [ ] On first launch after the upgrade, send **`/projects`** — your prior single session appears as
      a project named **`default`** (active), at your previous cwd; sending a message resumes it.

### (j) One-shot mode is unaffected
- [ ] (Optional sanity) Set `ENGINE_MODE=oneshot`, relaunch. `/projects` / `/switch` / `/new` reply
      "applies to streaming mode only"; `/cd` works as before; ordinary messages run one-shot. Flip
      back to `streaming` (or leave oneshot as the safe default until you choose to cut over).

## 2. Acceptance

- [ ] **Two projects keep independent state** — distinct cwd AND distinct Claude conversation;
      `/switch` resumes the right one (check (b)).
- [ ] **A restart resumes both** projects with their sessions intact (check (c)) — RB6 persistence.
- [ ] **`/projects` / `/new` / `/switch` / `/rm`** all behave (create+confine+switch, list, switch,
      remove-non-active, protect-active) (checks (a),(b),(d)).
- [ ] **A turn in flight refuses `/switch` and `/new`, and the held turn still completes after you
      answer** — the relay is never deadlocked (check (e)). *The headline P4 reliability property.*
- [ ] **SB2 holds on `/new`** — out-of-roots and not-a-directory are refused; nothing is created
      (check (f)).
- [ ] **An interrupted run fails clean** — idle on restart, no auto-replay, no hang (check (h), RB3).
- [ ] **One-shot remained the documented safe default** (you only flipped to `streaming` to verify);
      one-shot behavior is unchanged (check (j)).
