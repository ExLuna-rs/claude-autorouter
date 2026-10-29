"""Paths, tiers and tunable settings.

Everything here can be overridden in ``$AUTOROUTER_HOME/config.json``
(default ``~/.claude/autorouter/config.json``). Unknown keys are ignored.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Ordered from cheapest to most capable. "direct" means: no delegation,
# the main session (the orchestrator) answers itself.
TIERS = ["direct", "quick", "builder", "architect"]
DELEGATED = ["quick", "builder", "architect"]

AGENT_MODEL = {"direct": "sonnet", "quick": "haiku", "builder": "sonnet", "architect": "opus"}


def home() -> Path:
    return Path(os.environ.get("AUTOROUTER_HOME", Path.home() / ".claude" / "autorouter"))


@dataclass
class Settings:
    # Relative price of one token per model family (Haiku input token = 1).
    model_weight: dict = field(default_factory=lambda: {
        "haiku": 1.0, "sonnet": 3.0, "opus": 5.0, "default": 5.0})

    # Prior cost of a task per tier, in weighted tokens, used until real
    # measurements exist. Replaced by observed means as data comes in.
    prior_cost: dict = field(default_factory=lambda: {
        "direct": 9_000, "quick": 15_000, "builder": 75_000, "architect": 200_000})

    # Main-session cost of delegating once (prompt, hand-off, relaying the report).
    delegation_overhead: float = 6_000
    # Successful runs per tier needed before measured costs replace the priors.
    min_runs_for_cost: int = 3

    # Similarity mass of past outcomes needed to route away from the rules
    # (successes one tier below, or escalations at the rules' tier).
    min_evidence_to_move: float = 1.5

    # Extra cost of an escalation (lost work + hand-off), weighted tokens.
    escalation_penalty: float = 20_000

    # Prior escalation probability of a tier, relative to the heuristic tier.
    p_below: float = 0.85   # tier below what the heuristics suggest
    p_at: float = 0.15      # the suggested tier
    p_above: float = 0.05   # a tier above

    # Learning.
    knn_k: int = 8
    knn_min_similarity: float = 0.25
    knn_history: int = 2000
    prior_strength: float = 2.0     # pseudo-observations behind the escalation prior
    cost_blend: float = 3.0         # pseudo-observations behind the cost model
    learning_rate: float = 0.15
    weight_clip: float = 1.5
    weight_min_evidence: int = 3
    weight_half_life_days: float = 30.0
    score_shift_clip: float = 3.0

    # A delegated run counts as "overkill" (task was easier than its tier)
    # when it finishes without escalating, with few tool calls and tokens.
    overkill_max_tool_uses: int = 2
    overkill_max_tokens: int = 8_000

    # Daily cap on the share of decisions routed to the architect tier.
    opus_daily_cap: float = 0.30
    opus_cap_min_decisions: int = 5

    # Privacy: how much of each prompt is stored (0 = tokens only).
    store_prompt_chars: int = 200

    # Extra patterns (regexes), e.g. proposed by /router-learn.
    extra_strong: list = field(default_factory=list)
    extra_weak: list = field(default_factory=list)
    extra_quick: list = field(default_factory=list)

    # Per-project settings keyed by absolute path prefix:
    # {"/home/me/secure-app": {"min_tier": "builder"}}
    projects: dict = field(default_factory=dict)


def load() -> Settings:
    s = Settings()
    path = home() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return s
    for key, value in data.items():
        if hasattr(s, key) and not key.startswith("_"):
            current = getattr(s, key)
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
            else:
                setattr(s, key, value)
    return s


def model_weight(settings: Settings, model: str | None) -> float:
    m = (model or "").lower()
    for family, weight in settings.model_weight.items():
        if family != "default" and family in m:
            return weight
    return settings.model_weight.get("default", 5.0)


def norm_path(path: str) -> str:
    """Forward slashes, no trailing slash, case-folded on Windows."""
    p = os.path.expanduser(path).replace("\\", "/").rstrip("/")
    return p.lower() if os.name == "nt" else p


def project_settings(settings: Settings, cwd: str | None) -> dict:
    if not cwd:
        return {}
    cwd = norm_path(cwd)
    best, best_len = {}, -1
    for prefix, conf in settings.projects.items():
        p = norm_path(prefix)
        if (cwd == p or cwd.startswith(p + "/")) and len(p) > best_len:
            best, best_len = conf, len(p)
    return best
