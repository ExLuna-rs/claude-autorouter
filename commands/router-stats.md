---
description: Show routing statistics - escalation rates, cost per task, learned words
model: haiku
allowed-tools: Bash(python3 ~/.claude/autorouter/run.py stats:*)
---

Run `python3 ~/.claude/autorouter/run.py stats $ARGUMENTS` and show its output verbatim in a code block. Then add at most two sentences pointing out anything notable (a tier with a high escalation rate, a high overkill rate, the architect share near its cap). Nothing else.
