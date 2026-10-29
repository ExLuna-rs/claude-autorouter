<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" width="200" alt="claude-autorouter: the Claude mascot as an airport marshaller">
  </picture>
</p>

<h1 align="center">claude-autorouter</h1>

<p align="center">
  <em>He waves the wands. The right model lands.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT license">
  <img src="https://img.shields.io/badge/python-3.9%2B-111111?style=flat-square" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-111111?style=flat-square" alt="Standard library only">
  <img src="https://img.shields.io/badge/Claude%20Code-2.1.275%2B-111111?style=flat-square" alt="Claude Code 2.1.275+">
</p>

---

**Cost-aware, self-learning model routing for Claude Code.**
Every prompt is routed to Haiku, Sonnet or Opus based on its complexity. When a cheap tier is not enough, it escalates without starting over, and the router learns from it. Built for Pro/Max subscriptions: no proxy, no API key, standard library only.

```
you: "where is the auth middleware?"        -> quick     (Haiku)
you: "add a /health endpoint"               -> builder   (Sonnet)
you: "why does the app crash randomly?"     -> architect (Opus)
```

## Why

Running everything on Opus burns your usage limit; running everything on Sonnet fails on the hard problems. Picking a model by hand for every prompt does not scale. Claude Code cannot switch the main session's model per prompt, but it can delegate to subagents that each pin their own model. This project makes that delegation automatic, measures what it costs, and gets better with use.

## How it works

```mermaid
flowchart LR
    P[Your prompt] --> H[UserPromptSubmit hook]
    H -->|rules + learned weights<br/>+ similar past tasks<br/>+ expected cost| D{tier}
    D -->|quick| Q[Haiku subagent]
    D -->|builder| B[Sonnet subagent]
    D -->|architect| A[Opus subagent]
    Q -->|ESCALATE + HANDOFF| B
    B -->|ESCALATE + HANDOFF| A
    Q & B & A --> O[PostToolUse hook<br/>tokens, escalation, overkill]
    O --> DB[(router.db)]
    DB --> H
```

1. **Route.** A `UserPromptSubmit` hook scores the prompt (rules for English and French, plus experimental packs for other languages), adds learned per-word weights, looks up similar past tasks, and injects one short line telling the main session (Sonnet, the orchestrator) what to do. `quick` and `architect` tasks are delegated to their subagents. `builder` tasks are handled by the orchestrator itself, since it is Sonnet too and a subagent's cold start costs more than it saves; it delegates to the `builder` subagent only for large tasks whose exploration would clutter the main context, and to `architect` (with a hand-off) if it gets stuck. Follow-ups such as "ok go ahead" are not routed.
2. **Escalate.** Subagents that hit something beyond their tier stop and answer `ESCALATE:` with a `HANDOFF` block (files read, facts found, hypotheses ruled out). The orchestrator re-delegates one tier up, and the next tier starts from the hand-off instead of re-exploring.
3. **Measure.** A `PostToolUse` hook records each delegated run (tokens, tool calls, escalation). A `Stop` hook adds the main session's own token usage for the turn, read from the transcript.
4. **Learn.** Three mechanisms, all local:
   - **Nearest neighbours:** TF-IDF similarity over past tasks. If similar tasks kept escalating from `quick`, similar new ones start at `builder`.
   - **Per-word weights:** escalations push the words of a prompt towards harder tiers; *overkill* (a task finished in a couple of tool calls on a tier above what it needed) pulls them down. User overrides (`!architect ...`) teach too.
   - **Expected cost:** for each candidate tier, `cost(t) + P(escalate | t) × (cost(next) + penalty)`, with costs measured on your own runs. The cheapest tier wins.

### Guard rails

Learning only from escalations would ratchet everything up to Opus, so the router is deliberately conservative:

- It learns in **both directions**: escalations raise, overkill lowers.
- The cost model moves **at most one tier** away from the rules, and **only with evidence** from similar past tasks (successes one tier below, or escalations at the rules' tier). It never bets on a double escalation.
- Learned weights need **3 observations** before they count, are clipped, and **decay** (30-day half-life).
- A **daily Opus cap** (30% of tasks by default) routes to `builder` instead and asks before using `architect`, unless the prompt has strong signals.
- Rule changes are **never applied automatically**: `/router-learn` proposes regex patterns as a diff and waits for your approval.

### Token savings beyond model choice

- **Less code, not just fewer words.** `builder` and `architect` follow [Ponytail](https://github.com/DietrichGebert/ponytail) by Dietrich Gebert: before writing, they check whether the code is needed, already exists, or is covered by the standard library, and they make the smallest complete change. Less code written means fewer output tokens and less code to read back. The plugin install pulls in Ponytail as a dependency; the script install embeds its rules (MIT, see `THIRD_PARTY_NOTICES.md`).
- **Terse subagent reports.** A subagent's report stays in the orchestrator's context for every later turn, so reports have a hard word budget (60 / 150 / 250 words) and a fixed, filler-free format.
- **No duplicated exploration.** Escalations carry a `HANDOFF` block, so the next tier does not re-read the same files.
- **Small, stable injected context.** One line (about 30 tokens) with a constant prefix, so it plays well with prompt caching.
- **No needless delegation.** Follow-ups and ordinary `builder` work stay in the main session, because spawning a subagent costs a cold start. `/router-stats` shows how often each tier was handled "in main".
- **Optional context hygiene** (`--hygiene`): denies reads of lockfiles, `node_modules`, build output and minified bundles.

## Install

Two ways to install. Use one, not both.

### As a plugin (recommended)

Requires Claude Code 2.1.275+, Python 3.9+ and Node.js (Ponytail's hooks run on Node). Works on macOS, Linux and Windows. In Claude Code:

```
/plugin install autorouter --marketplace ExLuna-rs/claude-autorouter
```

On older versions, send these two commands separately:

```
/plugin marketplace add ExLuna-rs/claude-autorouter
/plugin install autorouter@exluna
```

This installs **Autorouter and its dependency [Ponytail](https://github.com/DietrichGebert/ponytail)** (pinned to a release), with the agents, hooks and commands enabled. Updates come through `/plugin`. Plugin components are namespaced: the agents are `autorouter:quick`, `autorouter:builder` and `autorouter:architect`, and the commands are `/autorouter:router-stats` and `/autorouter:router-learn`.

A plugin cannot change your settings, so set the orchestrator model yourself with `/model sonnet` (an Opus main session works but costs more), and remove `CLAUDE_CODE_SUBAGENT_MODEL` from your environment or settings if you set it, because it overrides per-agent models.

Ponytail has its own options in `/config`. Its task board, which writes a CSV of each task to `~/.config/ponytail/`, is on by default.

### With the install script (no Node.js)

Requires Python 3.9+.

```bash
git clone https://github.com/ExLuna-rs/claude-autorouter.git
cd claude-autorouter
python3 install.py            # Windows: py install.py. Add --hygiene to block reads of bulky files
```

The script copies the three agents (with Ponytail's rules built in, see `THIRD_PARTY_NOTICES.md`) and two commands into `~/.claude/`, installs the router in `~/.claude/autorouter/`, and registers three hooks in `~/.claude/settings.json`. A backup of that file is saved as `settings.json.bak`. The script is idempotent, so run it again to upgrade. It also:

- sets the main session model to `sonnet` if you have not set one;
- removes `CLAUDE_CODE_SUBAGENT_MODEL` from the settings;
- skips the built-in Ponytail rules if the Ponytail plugin is enabled, so they are not paid for twice.

The hooks call the Python interpreter that ran the script by its absolute path. If you later remove that interpreter (a deleted venv, for example), run the script again.

Restart Claude Code and check with `/agents`.

### Check which models your tiers really use

The agents ask for `haiku`, `sonnet` and `opus`, which are **aliases**. Claude Code resolves them through its own alias table, **not** through the model you picked with `/model`. Your main session can therefore run the latest models while the subagents still get older ones, for example when `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL` or `ANTHROPIC_DEFAULT_OPUS_MODEL` is set to an older ID in your shell or settings, or when your Claude Code version is out of date.

To check, open a subagent transcript in `~/.claude/projects/<project>/<session>/subagents/` and look at the `"model"` field of its responses.

To fix it, update Claude Code (`claude update`) and set the aliases explicitly in `~/.claude/settings.json`, using the current model IDs of your account (shown here for the 5.5 generation):

```json
"env": {
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5"
}
```

Remove or update these lines when a newer generation comes out, or the aliases will stay pinned. Also look for the same variables in your shell profile (`~/.zshrc`, `~/.bashrc`, PowerShell profile), which can override the settings file.

## Usage

Nothing to do: every prompt is routed. To see why a prompt goes where it goes:

Ask Claude to run `/autorouter:router-stats`, or call the router directly. The CLI below is the script-install path; with the plugin, run the same `run.py` from the plugin folder.

```bash
python3 ~/.claude/autorouter/run.py explain "migrate the auth module to OAuth"
# Windows: py $HOME\.claude\autorouter\run.py explain "migrate the auth module to OAuth"
```

Force a tier by starting a prompt with a prefix. The router also learns from these corrections:

| Prefix       | Tier                                   |
|--------------|----------------------------------------|
| `!quick`     | Haiku                                  |
| `!builder`   | Sonnet                                 |
| `!architect` | Opus                                   |
| `!direct`    | no delegation, the main session answers |

### Band above the prompt (plugin install)

The plugin draws a row above the prompt, the way Ponytail does, without touching your status line:

```
⇄ router  ▶ architect opus-5-5  ▶ quick haiku-5-5  24h quick 50% builder 25% architect 25%/30%  last quick→builder  ↑1    Stats  Learn  Hide
```

- **`▶ tier model`**: subagents running now, with the **model they really run on** as Claude Code resolved it. If you see an older generation here, see [Check which models your tiers really use](#check-which-models-your-tiers-really-use).
- **`24h`**: the share of each tier over the last 24 hours. The architect share is shown against its daily cap, in red once over it.
- **`last`**: the tier chosen for your last prompt, and where it ended up if it escalated. **`↑n`**: escalations in the last 24 hours.
- **Stats** and **Learn** run `/autorouter:router-stats` and `/autorouter:router-learn`. **Hide** hides the band; `/autorouter:band` toggles it.

It shares the slot with other mods: Ponytail's band, if installed, is drawn just below it. The band needs a Claude Code version with plugin mods (terminal and desktop app).

### Status line (script install, or without mods)

The same information fits on one status line:

```
router ▶ architect, quick │ 24h 37: quick 46% · builder 35% · architect 19%/30% │ last: quick→builder
```

The script install sets it up when you have no status line, refreshed every 2 seconds (`refreshInterval`), since Claude Code does not otherwise re-run it while a subagent works. It never replaces a status line you already have. With the plugin, the band makes it unnecessary; `/autorouter:statusline` sets it up anyway if you prefer it.

Slash commands:

- **`/router-stats`** (`/autorouter:router-stats` with the plugin): tasks per tier, escalation and overkill rates, cost per task and its trend, savings against the same tokens on Opus, and learned words.
- **`/router-learn`** (`/autorouter:router-learn` with the plugin): reviews recent misroutes and proposes new patterns for `config.json`, which you approve before they are applied.

## Configuration

Optional, in `~/.claude/autorouter/config.json`. Every key is optional; see `autorouter/config.py` for the full list.

```json
{
  "opus_daily_cap": 0.25,
  "store_prompt_chars": 0,
  "projects": {
    "~/code/licence-server": { "min_tier": "builder" }
  },
  "extra_strong": ["\\bterraform state\\b"],
  "extra_quick": ["^\\s*explain\\b"]
}
```

Patterns are Python regexes matched against the prompt lower-cased with Latin accents removed.

### Languages

Rules live in language packs, `autorouter/lang/<code>.json`, and all packs are active at once, so no language setting is needed. English and French are the reference packs. Spanish, German, Portuguese, Chinese and Japanese are **experimental** starters, checked against only a handful of prompts each; pull requests that improve them or add a language are welcome. A pack is a JSON object with any of `strong`, `weak`, `quick`, `multi_file`, `follow_up` (regexes) and `stopwords` (words); copy `en.json` as a model. Write Latin-script patterns without accents. For a language of your own, put the file in `~/.claude/autorouter/lang/` (same format) without touching the plugin. A language with no pack still gets length-based routing, learned word weights and similar-task lookup (CJK text is split into character pairs), and the `!quick`, `!builder`, `!architect` overrides work everywhere.

## Data and privacy

Everything stays local, in `~/.claude/autorouter/router.db` (SQLite). For each task the router stores its content words, the first 200 characters of the prompt (set `store_prompt_chars` to `0` to store none), the tiers involved and token counts. Nothing is sent anywhere.

```bash
python3 ~/.claude/autorouter/run.py reset --weights --yes   # forget learned weights
python3 ~/.claude/autorouter/run.py reset --all --yes       # forget everything
```

## Limitations

- **Delegation, not switching.** Hooks cannot change the main session's model, so the router tells the orchestrator which subagent to use. Models follow this reliably, but it is not a hard guarantee.
- **Keyword rules to start.** Until it has data, the router relies on rules that will sometimes misjudge a prompt. Prefixes and `/router-learn` exist for that.
- **Approximate costs.** Costs are weighted tokens (input 1, cache write 1.25, cache read 0.1, output 5, multiplied by 1/3/5 for Haiku/Sonnet/Opus). They are meant for comparing tiers, not for computing a bill. Older Claude Code versions report only total tokens for subagents, which makes the estimate rougher.
- **Opus spends a Pro quota fast.** Keep an eye on `/usage` while the router calibrates.

## Uninstall

```
/plugin uninstall autorouter@exluna          # plugin install
python3 install.py --uninstall               # script install
```

Learned data stays in `~/.claude/autorouter/`. Delete that folder to erase it.

## Development

```bash
python3 -m unittest discover -s tests     # router, hooks, installer
claude plugin test .                      # the band (hooks/band.tsx)
claude plugin validate .                  # manifests and the band's API use
```

## Related projects

- [tzachbon/claude-model-router-hook](https://github.com/tzachbon/claude-model-router-hook), [misterkante/cc-model-router](https://github.com/misterkante/cc-model-router), [IuriiTurok/auto-model-router](https://github.com/IuriiTurok/auto-model-router): hook-based tier routing for Claude Code.
- [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router): a proxy that routes Claude Code requests across providers (API keys).
- Caveman: a skill that makes Claude answer tersely to save output tokens. It is complementary to this project.
- [Ponytail](https://github.com/DietrichGebert/ponytail): the "lazy senior developer" plugin that Autorouter depends on.
- rtk: filters noisy shell output (tests, builds, logs) before it reaches the context. It is complementary, and often the largest saving in long sessions.

## License

MIT
