## B2 status
- CLOSED | STILL-OPEN — why (cite file:line)
## Regressions
- (none, or list)
## Verdict
SHIP | NO_SHIP
## Reasoning
(2-3 sentences)
codex
I’ll inspect the current branch diff against `main` and focus only on the statusline paths you named. After that I’ll verify the exact await/check/write ordering and the previous regression cases.
exec
/bin/zsh -lc 'git diff $(git merge-base HEAD main)..HEAD --stat && git diff $(git merge-base HEAD main)..HEAD -- src || true' in /Users/ray/dev/claude-telegram-bot-statusline
 succeeded in 0ms:
 claude_tg/bot.py                     |  187 +++++-
 claude_tg/engine/adapter_sdk.py      |  193 ++++++
 claude_tg/engine/engine.py           |   31 +
 claude_tg/render.py                  |  155 +++--
 claude_tg/session_store.py           |   65 ++
 claude_tg/stream_session.py          |  511 +++++++++++++++-
 docs/features/statusline/design.md   |  464 ++++++++++++++
 docs/features/statusline/handoff.md  |   38 ++
 docs/features/statusline/progress.md |   31 +
 docs/features/statusline/qa.md       |   13 +
 docs/features/statusline/state.json  |    7 +
 tests/test_bot_streaming.py          |  135 +++-
