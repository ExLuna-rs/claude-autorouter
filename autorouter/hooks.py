"""Claude Code hook handlers. Each reads the hook JSON on stdin.

A router must never get in the way: every handler swallows its own errors
and exits 0, so a bug here degrades to "no routing", never to a blocked prompt.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from . import config, features, learn, router, store, usage


def _log_error(where: str):
    try:
        path = config.home() / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"--- {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _add_cost(conn, decision_id, cost):
    if cost:
        conn.execute("UPDATE decisions SET cost=COALESCE(cost,0)+? WHERE id=?", (cost, decision_id))


def on_prompt(data: dict) -> dict | None:
    """UserPromptSubmit: pick a tier, record the decision, inject one short line."""
    prompt = (data.get("prompt") or "").strip()
    if not prompt or prompt.startswith("/"):
        return None
    settings = config.load()
    session, cwd = data.get("session_id"), data.get("cwd") or os.getcwd()

    with store.connect() as conn:
        decision = router.route(prompt, session=session, cwd=cwd, conn=conn, settings=settings)

        if "follow-up" in decision.reasons:
            return None  # continuation of the current task: keep its decision open

        sig = features.signals(prompt)
        if sig.override:
            if decision.tier == "direct":
                return None
            # Learn from the user's correction: what would the router have done?
            stripped = prompt.split(None, 1)[1] if " " in prompt else ""
            auto = router.route(stripped, cwd=cwd, conn=conn, settings=settings) if stripped else None
            decision.tokens = features.tokenize(stripped)
            decision.heuristic = auto.tier if auto else decision.tier
            if auto and auto.tier in router.RANK and auto.tier != "direct":
                diff = router.RANK[decision.tier] - router.RANK[auto.tier]
                if diff:
                    learn._nudge(conn, decision.tokens, settings.learning_rate * (1 if diff > 0 else -0.5),
                                 settings)
                    decision.reasons.append(f"user corrected {auto.tier}->{decision.tier}")

        if session:
            store.close_open_decisions(conn, session)
        store.add_decision(
            conn, session=session, project=router.project_name(cwd),
            prompt=prompt[:settings.store_prompt_chars] if settings.store_prompt_chars else None,
            tokens=decision.tokens, heuristic=decision.heuristic, tier=decision.tier,
            reason=decision.reason)

    line = router.context_line(decision)
    if not line:
        return None
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}}


def on_agent(data: dict) -> None:
    """PostToolUse on Agent/Task: measure the run, detect escalation, learn."""
    tool_input = data.get("tool_input") or {}
    agent_tier = learn.tier_of_agent(tool_input.get("subagent_type"))
    if not agent_tier:
        return  # not one of our tiers (Explore, general-purpose, ...)
    settings = config.load()
    run = usage.agent_run(data.get("tool_response"), config.AGENT_MODEL[agent_tier], settings)
    with store.connect() as conn:
        decision = store.open_decision(conn, data.get("session_id"))
        learn.record_agent_run(conn, decision, agent_tier, run, settings)
        if decision:
            _add_cost(conn, decision["id"], run["cost"])


def on_stop(data: dict) -> None:
    """Stop: add the main session's cost for this turn to the open decision."""
    settings = config.load()
    with store.connect() as conn:
        decision = store.open_decision(conn, data.get("session_id"))
        if not decision:
            return
        cost = usage.last_turn_cost(data.get("transcript_path"), settings)
        _add_cost(conn, decision["id"], cost)


HANDLERS = {"prompt": on_prompt, "agent": on_agent, "stop": on_stop}


def script_install_active() -> bool:
    """True when install.py registered hooks in the user's settings.json."""
    claude = os.environ.get("CLAUDE_HOME") or os.path.join(os.path.expanduser("~"), ".claude")
    try:
        with open(os.path.join(claude, "settings.json"), encoding="utf-8") as f:
            return "autorouter/run.py" in f.read()
    except OSError:
        return False


def run(event: str) -> int:
    try:
        if os.environ.get("CLAUDE_PLUGIN_ROOT") and script_install_active():
            _read_stdin()
            return 0  # the script install handles routing; do not route twice
        out = HANDLERS[event](_read_stdin())
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - never block Claude Code
        _log_error(event)
    return 0
