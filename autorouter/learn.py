"""Online learning from outcomes: escalations push words up, overkill pulls them down."""
from __future__ import annotations

import json
import math
import time

from . import config, store
from .router import RANK

ESCALATE_MARK = "ESCALATE:"


def tier_of_agent(subagent_type: str | None) -> str | None:
    name = (subagent_type or "").split(":")[-1]
    return name if name in config.DELEGATED else None


def _nudge(conn, tokens, delta, settings):
    if not tokens:
        return
    step = delta / math.sqrt(len(tokens))
    now = time.time()
    existing = store.get_weights(conn, set(tokens))
    for tok in set(tokens):
        row = existing.get(tok)
        if row:
            age = (now - row["updated"]) / 86400
            w = row["w"] * 0.5 ** (age / settings.weight_half_life_days)
            ev = row["evidence"]
        else:
            w, ev = 0.0, 0
        w = max(-settings.weight_clip, min(settings.weight_clip, w + step))
        store.upsert_weight(conn, tok, w, ev + 1)


def record_agent_run(conn, decision, agent_tier, run, settings) -> dict:
    """Store a delegated run, update the decision and learn from it."""
    escalated = ESCALATE_MARK in (run.get("text") or "")
    conn.execute(
        "INSERT INTO outcomes (decision_id, ts, agent, tokens, tool_uses, duration_ms, escalated, cost)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (decision["id"] if decision else None, time.time(), agent_tier, run.get("tokens"),
         run.get("tool_uses"), run.get("duration_ms"), int(escalated), run.get("cost")))
    if not decision:
        return {"escalated": escalated, "learned": None}

    tokens = json.loads(decision["tokens"])
    final = decision["final_tier"] or decision["tier"]
    learned = None

    # Two ways to detect that the routed tier was not enough:
    #  - the agent itself answered ESCALATE: (final becomes the next tier);
    #  - the orchestrator ran a higher tier on its own (final becomes that tier).
    # After an ESCALATE, final is already the next tier, so the follow-up
    # run does not count twice.
    climbed = RANK[agent_tier] > RANK.get(final, 0)
    if escalated or climbed:
        new_final = _next(agent_tier, final) if escalated else agent_tier
        conn.execute("UPDATE decisions SET escalations=escalations+1, final_tier=? WHERE id=?",
                     (new_final, decision["id"]))
        _nudge(conn, tokens, +settings.learning_rate, settings)
        learned = "up"
    else:
        tool_uses = run.get("tool_uses")
        overkill = (agent_tier != "quick"
                    and tool_uses is not None and tool_uses <= settings.overkill_max_tool_uses
                    and (run.get("tokens") or 0) <= settings.overkill_max_tokens
                    and RANK[agent_tier] == RANK[decision["tier"]])
        if overkill:
            conn.execute("UPDATE decisions SET overkill=1 WHERE id=?", (decision["id"],))
            _nudge(conn, tokens, -settings.learning_rate / 2, settings)
            learned = "down"
    return {"escalated": escalated, "learned": learned}


def _next(tier, final) -> str:
    order = config.DELEGATED
    nxt = order[min(order.index(tier) + 1, len(order) - 1)]
    return nxt if RANK[nxt] > RANK.get(final, 0) else final
