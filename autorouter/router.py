"""Tier selection: rules + learned weights + nearest neighbours + expected cost."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import config, features, memory, store

RANK = {t: i for i, t in enumerate(config.TIERS)}


@dataclass
class Decision:
    tier: str
    heuristic: str = ""
    score: float = 0.0
    shift: float = 0.0
    reasons: list = field(default_factory=list)
    expected: dict = field(default_factory=dict)
    p_escalate: dict = field(default_factory=dict)
    neighbours: int = 0
    tokens: list = field(default_factory=list)
    capped: bool = False

    @property
    def reason(self) -> str:
        return ", ".join(self.reasons)


def _decay(row, settings, now) -> float:
    age_days = (now - row["updated"]) / 86400
    return row["w"] * 0.5 ** (age_days / settings.weight_half_life_days)


def learned_shift(conn, tokens, settings) -> float:
    """Sum of learned per-word weights (positive = harder), with evidence and decay."""
    now = time.time()
    total = 0.0
    for row in store.get_weights(conn, set(tokens)).values():
        if row["evidence"] >= settings.weight_min_evidence:
            total += _decay(row, settings, now)
    clip = settings.score_shift_clip
    return max(-clip, min(clip, total))


def _prior_p(tier, heuristic, settings) -> float:
    if tier == "architect":
        return 0.0
    if RANK[tier] < RANK[heuristic]:
        return settings.p_below
    if tier == heuristic:
        return settings.p_at
    return settings.p_above


def expected_costs(heuristic, nbs, run_means, settings):
    """Expected weighted-token cost of starting at each delegated tier.

    E(t) = C(t) + P(escalate | t) * (E(next tier) + penalty), blended with the
    costs actually measured on similar past tasks that started at t.
    """
    p, measured, evidence = {}, {}, {}  # evidence[t] = (sufficient, insufficient) similarity mass
    for t in config.DELEGATED:
        insufficient = sufficient = 0.0
        costs = []
        for nb in nbs:
            r0, rf, rt = RANK[nb.tier], RANK[nb.final_tier], RANK[t]
            if r0 <= rt:
                # Started at or below t: did it need more than t?
                if rf > rt:
                    insufficient += nb.similarity
                else:
                    sufficient += nb.similarity
            elif nb.overkill and rt == r0 - 1:
                # Finished trivially one tier above: t was probably enough.
                sufficient += 0.5 * nb.similarity
            if nb.tier == t and nb.cost is not None:
                costs.append(nb.cost)
        a = settings.prior_strength
        prior = _prior_p(t, heuristic, settings)
        p[t] = 0.0 if t == "architect" else (insufficient + a * prior) / (insufficient + sufficient + a)
        measured[t] = costs
        evidence[t] = (sufficient, insufficient)

    # Cost of a tier that succeeds first time: measured successful runs plus
    # the orchestrator's overhead, or the prior until there is enough data.
    base = {}
    for t in config.DELEGATED:
        mean, n = run_means.get(t, (None, 0))
        if mean is not None and n >= settings.min_runs_for_cost:
            base[t] = mean + settings.delegation_overhead
        else:
            base[t] = settings.prior_cost[t]

    expected = {}
    nxt = None
    for t in reversed(config.DELEGATED):
        model = base[t] + (p[t] * (expected[nxt] + settings.escalation_penalty) if nxt else 0.0)
        costs = measured[t]
        k = settings.cost_blend
        expected[t] = (sum(costs) + k * model) / (len(costs) + k) if costs else model
        nxt = t
    return expected, p, evidence


def candidates(heuristic, evidence, settings):
    """Tiers the cost model may pick from.

    At most one tier away from the rules, and only with evidence from similar
    past tasks: going down needs successes at the lower tier, going up needs
    escalations at the rules' tier. The cost model refines the rules; it never
    bets on a double escalation or moves on priors alone.
    """
    need = settings.min_evidence_to_move
    h = config.DELEGATED.index(heuristic)
    out = [heuristic]
    if h + 1 < len(config.DELEGATED) and evidence[heuristic][1] >= need:
        out.append(config.DELEGATED[h + 1])
    if h > 0 and evidence[config.DELEGATED[h - 1]][0] >= need:
        out.append(config.DELEGATED[h - 1])
    return out


def route(prompt: str, *, session=None, cwd=None, conn=None, settings=None) -> Decision:
    settings = settings or config.load()
    sig = features.signals(prompt, settings.extra_strong, settings.extra_weak, settings.extra_quick)

    if sig.override:
        return Decision(tier=sig.override, heuristic=sig.override, reasons=["override"])
    if sig.follow_up:
        return Decision(tier="direct", heuristic="direct", reasons=["follow-up"])

    if conn is None:
        with store.connect() as c:
            return _route(prompt, sig, cwd, c, settings)
    return _route(prompt, sig, cwd, conn, settings)


def _route(prompt, sig, cwd, conn, settings) -> Decision:
    tokens = features.tokenize(prompt)
    score, reasons = features.base_score(sig)

    shift = learned_shift(conn, tokens, settings)
    if abs(shift) >= 0.05:
        reasons.append(f"learned {shift:+.1f}")
    heuristic = features.tier_from_score(score + shift)

    project = project_name(cwd)
    nbs = memory.neighbours(tokens, project, store.history(conn, settings.knn_history),
                            settings.knn_k, settings.knn_min_similarity)
    expected, p, evidence = expected_costs(heuristic, nbs, store.agent_cost_means(conn), settings)

    tier = min(candidates(heuristic, evidence, settings), key=lambda t: (expected[t], RANK[t]))
    if nbs:
        reasons.append(f"{len(nbs)} similar")
    if tier != heuristic:
        reasons.append(f"cost-optimal vs rules={heuristic}")

    min_tier = config.project_settings(settings, cwd).get("min_tier")
    if min_tier in RANK and RANK[tier] < RANK[min_tier]:
        tier = min_tier
        reasons.append(f"project min={min_tier}")

    capped = False
    if tier == "architect" and sig.strong < 2:
        share, n = store.today_tier_share(conn, "architect")
        if n >= settings.opus_cap_min_decisions and share > settings.opus_daily_cap:
            tier, capped = "builder", True
            reasons.append(f"opus cap {share:.0%}")

    return Decision(tier=tier, heuristic=heuristic, score=score, shift=shift, reasons=reasons,
                    expected={t: round(v) for t, v in expected.items()},
                    p_escalate={t: round(v, 2) for t, v in p.items()},
                    neighbours=len(nbs), tokens=tokens, capped=capped)


def project_name(cwd) -> str | None:
    return (cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or None


PLUGIN_NAME = "autorouter"

INSTRUCTION = {
    "quick": "delegate to subagent `{agent}`; relay its result in 1-2 sentences.",
    # The orchestrator is Sonnet too: delegating to a Sonnet subagent only pays
    # off when it keeps a lot of exploration out of the main context.
    "builder": ("do it yourself; delegate to `{agent}` only if it spans many files or a long exploration."
                " If you get stuck, or it turns out to involve architecture or security,"
                " delegate to `{up}` with a HANDOFF block (files, found, ruled out)."),
    "architect": "delegate to subagent `{agent}` with the full context it needs.",
}


def agent_name(tier: str) -> str:
    """Plugin installs namespace agents under the plugin name."""
    return f"{PLUGIN_NAME}:{tier}" if os.environ.get("CLAUDE_PLUGIN_ROOT") else tier


def context_line(d: Decision) -> str | None:
    """Short, stable-prefix instruction injected before the model sees the prompt."""
    if d.tier == "direct":
        return None
    line = f"[router] tier={d.tier}: " + INSTRUCTION[d.tier].format(
        agent=agent_name(d.tier), up=agent_name("architect"))
    if d.tier == "quick":
        line += " On `ESCALATE:`, re-delegate one tier up with its HANDOFF block."
    if d.capped:
        line += f" Opus daily budget reached: ask the user before using `{agent_name('architect')}`."
    return line

