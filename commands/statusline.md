---
description: Show the router in Claude Code's status line (running agents, 24h tier mix, last route)
model: haiku
---

Set up Claude Code's status line to show the router.

1. First run `node "${CLAUDE_PLUGIN_ROOT}/scripts/run.js" statusline` once and show its output to the user as a preview.
2. Read `~/.claude/settings.json` (it may not exist yet).
3. The status line command to use is `node "<HOME>/.claude/autorouter/run.js" statusline`, where `<HOME>` is the user's absolute home directory written with forward slashes (find it with `echo $HOME`, or `$env:USERPROFILE` on Windows). This path is stable across plugin updates.
4. If `statusLine` is not set, propose adding `"statusLine": {"type": "command", "command": "<that command>", "padding": 0}`. If one is already set, show it and ask whether to replace it; do not combine them yourself.
5. Show the exact change and wait for the user to approve before editing the file. Keep every other key unchanged.
6. Tell the user the status line appears on the next prompt.
