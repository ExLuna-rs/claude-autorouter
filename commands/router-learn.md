---
description: Review recent misroutes and propose routing rule changes (applied only after approval)
model: sonnet
allowed-tools: Bash({{RUN}} report:*), Bash({{RUN}} explain:*), Read, Edit
---

1. Run `{{RUN}} report $ARGUMENTS` and read it.
2. Find recurring patterns:
   - escalated tasks: wording that should have routed higher -> candidates for `extra_strong` or `extra_weak`;
   - overkill tasks: wording that should have routed lower -> candidates for `extra_quick`;
   - user overrides: what the router got wrong.
   Ignore one-off cases. Propose a pattern only if it explains at least 3 cases and would not obviously catch unrelated prompts.
3. Patterns are Python regexes matched against the prompt lower-cased with accents removed.
4. Show the proposal as a JSON diff of `~/.claude/autorouter/config.json`, with, for each pattern, the cases it explains. Before proposing, check each one with `{{RUN}} explain "<example prompt>"`.
5. Stop and wait for the user to approve. Only then edit `config.json` (create it if missing; keep every existing key).

Never edit the router's Python code.
