# Keeping the bot running (deploy)

Supervised keep-alive so the Claude Telegram bot (re)starts on **login/boot** and on
**crash**, on the owner's Mac. The target is a macOS **LaunchAgent** (`launchd`); a
`systemd` unit is included as an optional Linux bonus.

> **One instance per token.** The bot polls Telegram, and **one token allows only one
> poller** — a second poller gets a Telegram `Conflict` and neither runs reliably. The
> keep-alive runs the *only* instance. **Before you start the bot by hand (`./run.sh`),
> `launchctl unload` the agent first** (and load it again when you're done). The fixed
> agent `Label` (`com.claude-telegram-bot`) also stops you from loading two copies of the
> agent itself.

> **No token in the plist.** The token stays in `.env`, read from the agent's
> `WorkingDirectory`. Nothing here contains a secret; the rendered plist is safe to keep.

---

## macOS (launchd) — quick path

From your repo checkout (the dir that holds `.env`):

```bash
deploy/install-launchd.sh
```

That detects how to start the bot (the `claude-telegram-bot` console script in your
`.venv`, else `python -m claude_tg`), uses this checkout as the working directory, writes
`~/Library/LaunchAgents/com.claude-telegram-bot.plist`, and `launchctl load`s it. It is
**idempotent** — re-run it any time (e.g. after `git pull` / re-install) to refresh and
reload.

Options:

```bash
deploy/install-launchd.sh --workdir /path/to/checkout   # if .env lives elsewhere
deploy/install-launchd.sh --print                       # print the rendered plist, don't install
deploy/install-launchd.sh --uninstall                   # unload + remove the agent
```

### Everyday commands

```bash
# Status (PID + last exit code; nothing printed = not loaded)
launchctl list | grep com.claude-telegram-bot

# Start at login + now, and restart on crash — this is what install does:
launchctl load   ~/Library/LaunchAgents/com.claude-telegram-bot.plist

# Stop + disable (do this BEFORE running ./run.sh by hand):
launchctl unload ~/Library/LaunchAgents/com.claude-telegram-bot.plist

# Kick it once without a full reload (KeepAlive also restarts it on crash):
launchctl start com.claude-telegram-bot
launchctl stop  com.claude-telegram-bot

# Logs (stdout + stderr):
tail -f ~/Library/Logs/com.claude-telegram-bot.out.log
tail -f ~/Library/Logs/com.claude-telegram-bot.err.log
```

> On recent macOS, `launchctl load`/`unload` still works and is the simplest path; the
> newer equivalents are `launchctl bootstrap gui/$(id -u) <plist>` and
> `launchctl bootout gui/$(id -u) <plist>` if you prefer them.

### Verify keep-alive (restart on crash)

```bash
launchctl list | grep com.claude-telegram-bot          # note the PID
kill <that-pid>                                         # simulate a crash
sleep 12                                                # ThrottleInterval is 10s
launchctl list | grep com.claude-telegram-bot          # a NEW PID == it restarted
```

### Manual template (no script)

`deploy/com.claude-telegram-bot.plist` is a template with `__PLACEHOLDERS__`. To do it by
hand: copy it to `~/Library/LaunchAgents/com.claude-telegram-bot.plist`, replace
`__BOT_COMMAND__` (absolute path to `.venv/bin/claude-telegram-bot`, or split
`<venv>/bin/python` + `-m` + `claude_tg` across three `<string>` lines), `__WORKING_DIR__`
(the dir with `.env`), `__PATH__` (include the dir holding `claude` — `dirname $(which
claude)` — plus your `.venv/bin`), and the two `__*_LOG__` paths, then `launchctl load` it.

---

## Linux (systemd) — optional bonus

macOS launchd is the supported target; this is a convenience for a Linux host. As a
**user** service (no root), create `~/.config/systemd/user/claude-telegram-bot.service`:

```ini
[Unit]
Description=Claude Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# The dir that holds .env (the bot loads .env from its CWD):
WorkingDirectory=%h/dev/claude-telegram-bot
# Absolute path to the console script, or: %h/dev/claude-telegram-bot/.venv/bin/python -m claude_tg
ExecStart=%h/dev/claude-telegram-bot/.venv/bin/claude-telegram-bot
Restart=always
RestartSec=10
# No token here — it stays in .env.

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram-bot     # start + run on login
journalctl --user -u claude-telegram-bot -f           # logs
systemctl --user stop claude-telegram-bot             # before running by hand (one poller!)
```

(For boot-without-login, `loginctl enable-linger $USER`.) Same one-instance rule applies:
stop the service before starting the bot manually.
