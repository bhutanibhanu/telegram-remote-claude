# Claude Telegram Bot

Control **Claude Code** on your Mac from your phone via **Telegram**. Send a message, it
runs through Claude Code on the machine, and the reply comes back in the chat — including
interactive tool-approval prompts you answer with a tap.

```
You (Telegram)  ──▶  this bot (on your Mac)  ──▶  Claude Code  ──▶  reply back to Telegram
```

The bot is a thin transport (`claude_tg/bot.py`). Two engines sit behind it: a simple
**one-shot** runner (`claude -p`, `claude_tg/claude_runner.py`) and an interactive
**streaming** engine (the Claude Agent SDK, `claude_tg/stream_session.py` + `engine/`).
You choose which with one environment variable.

> **This bot runs Claude Code with tool access on your machine** — it can read/edit files
> and run shell commands. Read **[Security](#security)** before you expose it. Defaults are
> safe (gate ON, path confinement ON, allowlist required), but the trust you place in it is
> the trust you place in whoever can message it.

## Contents

- [Requirements](#requirements)
- [Security](#security)
- [Modes: one-shot vs streaming](#modes-one-shot-vs-streaming)
- [Setup](#setup)
- [Install & run](#install--run)
- [Keeping it running (keep-alive)](#keeping-it-running-keep-alive)
- [Commands](#commands)
- [Multi-project & concurrency](#multi-project--concurrency-streaming-mode)
- [Configuration](#configuration-env)
- [Troubleshooting](#troubleshooting)
- [Docs](#docs)
- [Development](#development)

## Requirements

- **macOS** with **Claude Code already installed and logged in** — `claude` on your `PATH`
  (test with `claude -p "hi"`). The bot drives your existing Claude Code; it does not
  bundle or authenticate it. (A Linux host works too; macOS launchd is the supported
  keep-alive target.)
- **Python 3.11+**.
- A **Telegram bot token** (from @BotFather) and **your numeric chat id** (from
  @userinfobot). See [Setup](#setup).

## Security

This bot lets an allowlisted Telegram chat run Claude Code on your Mac. The defences, in
layers (defaults are the safe state — you opt *out* explicitly):

- **Allowlist (always on).** Only the chat id(s) in `TELEGRAM_ALLOWED_CHAT_IDS` are served.
  The check runs on **every** inbound message *and* every inline-button tap; all other
  chats are silently ignored. There is no public command surface.
- **Approval gate — ON by default.** In **streaming** mode, Claude's safe read/search tools
  (`Read`, `Glob`, `Grep`, `LS`, …) run automatically inside your roots, but risky tools
  (`Write`, `Edit`, `Bash`, …) are **held** and surface an **Allow / Deny** prompt in
  Telegram — nothing risky runs until you tap. The allow-all bypass
  (`CLAUDE_SKIP_PERMISSIONS=true`) is **off by default** and logs a loud `⚠️ SECURITY`
  warning at startup when enabled.
- **Path confinement — ON by default.** `/cd`, `/new`, **and Claude's own file/search
  tools** are confined to `ALLOWED_ROOTS` (which defaults to `CLAUDE_WORKDIR`). A tool
  targeting a path *outside* the roots is held for approval — even a normally-auto safe
  tool, even one you granted for the session. Paths are canonicalised (`..` and symlinks
  resolved) before the check, so traversal can't escape. Set a **narrow** `ALLOWED_ROOTS`.
  - **`Bash` caveat (documented, not a bug).** An arbitrary shell command has no reliable
    static path to check, so `Bash` is **not** path-confined. It is gated by name (prompts
    unless granted or `/yolo`), but a `Bash` command you *approve* can act on **any** path.
    Grant `Bash` only when you mean it. (See
    [`docs/features/p6-security-audit/findings.md`](docs/features/p6-security-audit/findings.md),
    finding C2.)
- **Secret hygiene.** `TELEGRAM_BOT_TOKEN` lives only in `.env` (git-ignored — never commit
  it). The token is kept out of logs (the httpx request-URL log is suppressed) and is never
  written into the launchd plist. Error text that could carry file contents or secrets is
  surfaced to Telegram **body-free** (the raw detail goes only to the local debug log,
  with session ids redacted).
- **Trust boundary.** Anyone who has your token **and** is on the allowlist can drive Claude
  Code on your Mac. Treat the token like an SSH key, and run the bot on a machine and a
  working directory you trust.

**Honest limits** (don't over-trust): one-shot mode is **non-interactive** — it has no
in-Telegram approval prompt, so with the gate on (the default) a risky tool has no one to
approve it and simply won't run; to actually use tools in one-shot you must set the
loud `CLAUDE_SKIP_PERMISSIONS=true` bypass. And the `Bash` caveat above applies in
streaming mode. For day-to-day use the recommended posture is **streaming + the default
gate + a narrow `ALLOWED_ROOTS`**.

## Modes: one-shot vs streaming

The bot has two engines, selected by `ENGINE_MODE` (default `oneshot`):

| | **one-shot** (`ENGINE_MODE=oneshot`, default) | **streaming** (`ENGINE_MODE=streaming`, recommended) |
|---|---|---|
| How it runs | One `claude -p` subprocess per message, run to completion | Persistent Claude Agent SDK session, streamed |
| Interactive approval | **No** prompts. Risky tools only run if you set the loud `CLAUDE_SKIP_PERMISSIONS=true` bypass | **Yes** — Allow / Deny tool prompts and ask / plan prompts answered by tap in Telegram |
| Multi-project | No (one implicit session per chat) | **Yes** — named projects, each with its own session + working dir |
| Concurrency | One message at a time per chat | **Yes** — concurrent runs across projects + a FIFO queue |
| Working directory | Per-chat, changeable with `/cd` | Fixed per project (use `/new` to work elsewhere) |
| Best for | A quick, low-risk "ask Claude" relay | Real work where Claude needs tools — **use this** |

**Which should I use?** Use **streaming** unless you specifically want the minimal
non-interactive relay. Streaming is the mode the security model (interactive gate + path
confinement) is built around. Set it in `.env`:

```ini
ENGINE_MODE=streaming
```

The streaming-only commands and the streaming-only config vars are flagged as such below;
in one-shot mode those commands reply with a short "streaming mode only" notice and the
streaming config vars are ignored.

## Setup

Do the Telegram side on your phone first:

1. **Create the bot** — message **@BotFather** → `/newbot` → pick a name + username → copy
   the **token**. Keep it secret.
2. **Get your chat id** — message **@userinfobot** → it replies with your numeric **Id**.
3. **Create `.env`** in the repo checkout (`cp .env.example .env`) and fill in the two
   required values (everything else has a safe default):

   ```ini
   TELEGRAM_BOT_TOKEN=<token from @BotFather>
   TELEGRAM_ALLOWED_CHAT_IDS=<your id from @userinfobot>

   # Recommended:
   ENGINE_MODE=streaming
   # Where Claude starts + the default confinement root:
   CLAUDE_WORKDIR=/Users/you/dev
   ```

   `.env.example` documents every variable at its default — uncomment only what you want to
   change. See [Configuration](#configuration-env).

## Install & run

The repo is a normal Python package (`pyproject.toml`); the bot loads `.env` from its
**current working directory**, so start it from the checkout that holds your `.env`. Three
equivalent ways to start it — pick one:

```bash
# 1. pip install → console command (recommended for a stable install)
python3 -m venv .venv && source .venv/bin/activate
pip install .                 # or: pip install -e .   (editable)
claude-telegram-bot

# 2. run as a module (no console script needed)
python -m claude_tg

# 3. quick-start script: creates the venv, installs deps, runs in the foreground
./run.sh
```

All three run the same entry point (`claude_tg.cli:main`). `./run.sh` is the zero-setup
path — it makes `.venv`, installs `requirements.txt` (which installs this project), checks
`.env` exists, and execs `python -m claude_tg` in the foreground (Ctrl-C to stop).

Then message your bot from your phone and send `/help`.

## Keeping it running (keep-alive)

`./run.sh` (and the bare commands) run in the **foreground** and stop when you close the
terminal. To keep the bot alive across logout/reboot/crash, install the macOS **launchd**
LaunchAgent:

```bash
deploy/install-launchd.sh           # install + load from this checkout
deploy/install-launchd.sh --print   # preview the rendered plist (no install)
deploy/install-launchd.sh --uninstall
```

It auto-detects the start command (the `claude-telegram-bot` console script in your
`.venv`, else `python -m claude_tg`), uses this checkout as the working directory, and
loads the agent with `RunAtLoad` + `KeepAlive` (starts now, on login, and on crash). The
token is **never** written into the plist — it stays in `.env`. Full launchd command
reference (status, logs, the systemd-on-Linux alternative) is in
**[`deploy/README.md`](deploy/README.md)**.

> **One token = one poller.** Telegram allows only one poller per token. If the launchd
> agent is running, `launchctl unload` it **before** you run `./run.sh` by hand (and load it
> again after) — otherwise the two pollers collide with a Telegram `Conflict`. The installer
> aborts if it detects a bot already running manually.

## Commands

Every command is allowlist-gated. Anything that isn't a registered command below — e.g.
`/grill`, `/pipeline`, `/scaffold` — is **forwarded verbatim to Claude** and runs as a skill
in the session (it is not a bot command). Plain (non-command) text is sent to Claude as a
turn. You can also **send a photo** (Claude sees it), **a file** (saved into the project for
Claude to read; `/get` pulls one back), or **a voice note** — with a transcriber configured
(see [Voice notes](#voice-notes-streaming-mode)) it's transcribed and run as a turn.

| Command | What it does |
|---|---|
| *(any text)* | Run as a Claude turn; the reply comes back in chat |
| `/help`, `/start` | Show the built-in help |
| `/reset` | Start a fresh Claude session (drop context). Streaming: resets the **active** project; refused while that project's own turn is in flight (`/cancel` it first) |
| `/pwd` | Show the working directory (streaming: the active project's name + its fixed cwd) |
| `/cd <path>` | Change the working directory — **one-shot mode only**; confined to `ALLOWED_ROOTS`. In streaming mode cwd is fixed per project (use `/new`) |
| `/cancel [name\|all]` | **Streaming.** Abort the in-flight run: the active project (no arg), a named project, or every running/queued project (`all`) |
| `/to <name> <text>` | **Streaming.** Send a free-text answer/feedback to a named project's pending "Other"/"Reject" prompt (or just reply to the prompt) |
| `/yolo` | **Streaming.** Run every tool this session with **no** approval prompt (loud ⚠️ banner). Disables the gate for the session |
| `/unyolo` | **Streaming.** Restore the per-tool approval gate (turn `/yolo` off) |
| `/projects` | **Streaming.** List your projects with the active marker, each project's cwd, run status, and last-active time |
| `/new <name> <path>` | **Streaming.** Create a project at `<path>` and switch to it; `<path>` must be an existing directory inside `ALLOWED_ROOTS`. Needs `CLAUDE_STATE_FILE` set |
| `/switch <name>` | **Streaming.** Switch the active project; your next message resumes it |
| `/rm <name>` | **Streaming.** Drop a project from the registry (its Claude transcript is left on disk). Can't remove the active or an in-flight project |

The **Streaming**-tagged commands reply with a short "streaming mode only" notice when
`ENGINE_MODE=oneshot`. Long replies are auto-split into Telegram-sized chunks; a typing
indicator shows while Claude works.

## Multi-project & concurrency (streaming mode)

Streaming mode (`ENGINE_MODE=streaming` **and** a `CLAUDE_STATE_FILE` so projects persist)
turns one chat into many independent workspaces:

- **Named projects.** `/new work /Users/you/dev/app` creates a project, confined to
  `ALLOWED_ROOTS`, and switches to it. Each project has **its own Claude session and its own
  fixed working directory** — the `(session_id, cwd)` pairing resumes cleanly across bot
  restarts. `/switch`, `/projects`, and `/rm` manage them; `/pwd` shows the active one.
- **Concurrent runs.** Different projects run **at the same time**. Switch to another project
  and send it work while the first keeps going in the background — its prompt still resolves
  when you tap it. Up to `MAX_CONCURRENT_RUNS` (default **3**) turns execute at once across
  the whole process; a turn started while at the cap is **queued** (FIFO per chat) and starts
  when a slot frees — never dropped. (One turn per project at a time: a second message to a
  project that's already running gets a "still working" notice.)
- **Foreground vs background.** The project you're actively watching streams its status
  inline; a run you switched away from notifies you when it finishes or needs an answer. All
  outbound messages for a chat are spaced by `RENDER_CHAT_SEND_INTERVAL_SECONDS` (default
  **1s**) so concurrent projects don't burst past Telegram's rate ceiling — your actual
  answers are prioritised over status churn, never dropped.
- **Routing answers.** With several projects awaiting input, each Allow/Deny or ask/plan
  prompt is tagged with its project. A tap routes to the owning project automatically;
  for a free-text answer, **reply** to the prompt or use `/to <name> <text>`.

See [ADR-004](docs/adr/ADR-004-multi-project-sessions.md) (projects) and
[ADR-005](docs/adr/ADR-005-concurrency-correlation.md) (concurrency) for the design.

## Voice notes (streaming mode)

Voice transcription is **pluggable and off by default** — no transcriber is bundled. Send a
voice note with nothing configured and the bot replies with a one-line "install a transcriber
and set `TRANSCRIBE_CMD`" message (it never crashes, and you can always just type). To enable
it, point `TRANSCRIBE_CMD` at any local or API transcriber:

1. Install a transcriber + a model, e.g. [whisper.cpp](https://github.com/ggml-org/whisper.cpp):

   ```sh
   brew install whisper-cpp
   # download a model (e.g. ggml-base.en.bin) per the whisper.cpp README
   ```

2. Set `TRANSCRIBE_CMD` (and `ENGINE_MODE=streaming`) in `.env`. It is a command **template**
   with two placeholders — `{audio}` (the input audio path) and `{out}` (an optional output
   basename):

   ```sh
   # whisper.cpp writes <out>.txt; the bot reads it:
   TRANSCRIBE_CMD=whisper-cli -m /Users/you/models/ggml-base.en.bin -f {audio} -otxt -of {out}

   # an STT CLI that prints the transcript to stdout (no {out}):
   TRANSCRIBE_CMD=my-stt --file {audio}
   ```

If the template mentions `{out}` the transcript is read from `<out>.txt`; otherwise it's read
from the command's **stdout**. When a voice note arrives the bot downloads it to a temp file,
runs the transcriber over it, **echoes the transcript back quoted** (so you see what it heard),
and runs that text as a normal turn. The template is split with `shlex` and run via `exec` —
**never** a shell — so the audio path can't be used for command injection. The audio (and any
transcript file) is cleaned up after each note. `TRANSCRIBE_TIMEOUT_SECONDS` (default `120`)
bounds the transcriber subprocess.

## Configuration (`.env`)

Loaded by `claude_tg/config.py` (`Config.from_env`). `.env.example` lists every variable at
its default; only the first two are required.

| Key | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather. Keep secret. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | ✅ | — | Comma/semicolon-separated numeric chat ids (the allowlist). At least one. |
| `ENGINE_MODE` | | `oneshot` | `oneshot` \| `streaming`. `streaming` enables interactive approval, multi-project, concurrency (recommended). Invalid value → fails loud at startup. |
| `CLAUDE_WORKDIR` | | home dir | Directory Claude starts in; also the default `ALLOWED_ROOTS` root. |
| `CLAUDE_BIN` | | `claude` | Path to the `claude` binary if not on `PATH`. |
| `CLAUDE_MODEL` | | Claude Code default | Force a specific model, e.g. `claude-opus-4-8`. |
| `CLAUDE_TIMEOUT_SECONDS` | | `600` | Per-turn timeout (one-shot subprocess). Positive integer. |
| `CLAUDE_SKIP_PERMISSIONS` | | `false` | **Allow-all bypass — OFF by default (gate ON).** `true` runs every tool with no approval prompt (`--dangerously-skip-permissions`); logs a loud ⚠️ warning at startup. Prefer streaming + per-tool approval. |
| `ALLOWED_ROOTS` | | `CLAUDE_WORKDIR` | Comma/`os.pathsep`-separated roots that `/cd`, `/new`, and Claude's file/search tools are confined to. Out-of-root tool use prompts. Set narrow. |
| `ALLOW_ANY_PATH` | | `false` | `true` disables path confinement entirely (you take the wheel). |
| `CLAUDE_STATE_FILE` | | none (in-memory) | Persist per-chat/project session id + cwd across restarts. **Required for multi-project** (`/new`). Written atomically, `0600`. |
| `MAX_CONCURRENT_RUNS` | | `3` | *(streaming)* Max turns executing at once process-wide; extra turns queue (FIFO). `0`/unset → default; negative → fails loud. |
| `RENDER_CHAT_SEND_INTERVAL_SECONDS` | | `1.0` | *(streaming)* Min seconds between outbound sends to one chat (rate-limit spacing). `0` disables spacing; negative → fails loud. |
| `STREAM_MESSAGE_TIMEOUT_SECONDS` | | `300.0` | *(streaming)* Max seconds to wait for Claude's next message before declaring it wedged. Generous (also bounds approved long-running tools); suspended while an approval prompt is open. Positive; else fails loud. |
| `ANSWER_BACKSTOP_SECONDS` | | `3600` | *(streaming)* Seconds to hold a pending Allow/Deny or ask/plan prompt before auto-denying + notifying. Positive integer. |
| `IMAGE_MAX_BYTES` | | `5242880` (5 MB) | *(streaming)* Max size of an inbound photo/screenshot threaded to Claude as an image. Larger → refused. Positive integer; else fails loud. |
| `FILE_MAX_BYTES` | | `20971520` (20 MB) | *(streaming)* Max size of an inbound saved file / outbound `/get` file. Larger → refused. Positive integer; else fails loud. |
| `TRANSCRIBE_CMD` | | empty (voice off) | *(streaming)* Command **template** to transcribe a voice note. Placeholders `{audio}` (input path) and `{out}` (output basename → read `<out>.txt`; omit → read stdout). Unset → voice gracefully off. See [Voice notes](#voice-notes-streaming-mode). |
| `TRANSCRIBE_TIMEOUT_SECONDS` | | `120.0` | *(streaming)* Max seconds the `TRANSCRIBE_CMD` subprocess may run before it's killed. Positive number; else fails loud. |

The `*(streaming)*` variables are only consulted when `ENGINE_MODE=streaming`.

## Troubleshooting

- **Telegram `Conflict` / the bot keeps disconnecting** — two pollers are running on one
  token. You can have only one. If you installed the launchd keep-alive, `launchctl unload`
  it before running `./run.sh` by hand (see [keep-alive](#keeping-it-running-keep-alive)).
- **`TELEGRAM_BOT_TOKEN is required` / `TELEGRAM_ALLOWED_CHAT_IDS is required`** — no `.env`,
  or it's missing the two required values, or the bot was started from a directory that
  doesn't contain `.env` (it loads `.env` from the **CWD**). `cp .env.example .env` and fill
  it in, in the directory you start from.
- **The bot ignores my messages** — your chat id isn't in `TELEGRAM_ALLOWED_CHAT_IDS`
  (re-check it with @userinfobot), or you typed it wrong. Unauthorized chats are silently
  ignored by design.
- **A `/projects` / `/new` / `/switch` command says "streaming mode only"** — you're in
  one-shot mode. Set `ENGINE_MODE=streaming` (and `CLAUDE_STATE_FILE` for `/new`).
- **Claude won't run a tool in one-shot mode** — one-shot has no interactive approval, so
  with the default gate on, a risky tool can't be approved and won't run. Switch to
  `ENGINE_MODE=streaming` (recommended) or, only if you accept the risk, set
  `CLAUDE_SKIP_PERMISSIONS=true`.
- **"Path not allowed (outside the permitted roots)"** — the target is outside
  `ALLOWED_ROOTS`. Widen `ALLOWED_ROOTS`, `/new` a project inside an allowed root, or (last
  resort) set `ALLOW_ANY_PATH=true`.
- **`claude: command not found` in the logs** — Claude Code isn't on the bot's `PATH`. Set
  `CLAUDE_BIN` to its absolute path (under launchd, the installer adds `claude`'s directory
  to the agent `PATH`).
- **Logs** — under launchd: `~/Library/Logs/com.claude-telegram-bot.{out,err}.log`. In the
  foreground, logs go to the terminal.

## Docs

- **[Docs index](docs/README.md)** — the full map.
- ADRs: [001 substrate](docs/adr/ADR-001-session-substrate.md) ·
  [002 answer-hold](docs/adr/ADR-002-async-answer-hold.md) ·
  [003 permission gating](docs/adr/ADR-003-permission-gating.md) ·
  [004 multi-project](docs/adr/ADR-004-multi-project-sessions.md) ·
  [005 concurrency](docs/adr/ADR-005-concurrency-correlation.md) ·
  [006 known limitations](docs/adr/ADR-006-known-limitations.md).
- Security: [P6 audit findings + remediation](docs/features/p6-security-audit/findings.md).
- Keep-alive: [`deploy/README.md`](deploy/README.md).
- Per-feature specs live under [`docs/features/`](docs/features/).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # runtime + test/lint/type-check tools
pytest                           # tests
ruff check .                     # lint
mypy claude_tg                   # type-check
python scripts/secret_scan.py    # secret scan
```

Layout: `claude_tg/config.py` (env), `bot.py` (Telegram transport), `claude_runner.py`
(one-shot subprocess), `stream_session.py` + `engine/` (streaming engine, permission
gating, concurrency), `session_store.py` (project/session persistence), `permissions.py`
(tool gating + path confinement), `paths.py` (root confinement), `render.py` / `tg_html.py`
(Telegram rendering), `util.py` (chunking). The Claude subprocess call is isolated in
`ClaudeRunner._invoke` so the logic is unit-tested without invoking Claude or the network.

Runtime deps are declared once in `pyproject.toml` (`python-telegram-bot`,
`claude-agent-sdk`); `requirements.txt` / `requirements-dev.txt` install this project so
the versions can't drift.
