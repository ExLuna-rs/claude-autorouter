"""Claude Code hook handlers. Each reads the hook JSON on stdin.

A router must never get in the way: every handler swallows its own errors
and exits 0, so a bug here degrades to "no routing", never to a blocked prompt.
"""
from __future__ import annotations

import json
import os
import sys
import time
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


def _run_id(data):
    return data.get("tool_use_id") or (data.get("tool_input") or {}).get("description")


def on_agent_start(data: dict) -> None:
    """PreToolUse on Agent/Task: mark the subagent as running (for the status line)."""
    tool_input = data.get("tool_input") or {}
    agent = learn.tier_of_agent(tool_input.get("subagent_type")) or \
        (tool_input.get("subagent_type") or "general").split(":")[-1]
    with store.connect() as conn:
        store.start_run(conn, _run_id(data) or f"{data.get('session_id')}:{agent}:{time.time()}",
                        data.get("session_id"), agent, bool(tool_input.get("run_in_background")))


def on_subagent_stop(data: dict) -> None:
    """SubagentStop: clear a finished background subagent.

    Foreground subagents fire this too, and are cleared by PostToolUse instead.
    When the event names the agent, clear a background run of that agent; when
    it does not, clear one only if no foreground run could be the one stopping.
    """
    session = data.get("session_id")
    agent_type = data.get("agent_type") or data.get("subagent_type")
    with store.connect() as conn:
        if agent_type:
            agent = learn.tier_of_agent(agent_type) or agent_type.split(":")[-1]
            store.end_run(conn, session=session, agent=agent, background_only=True)
        elif not conn.execute("SELECT 1 FROM running WHERE session IS ? AND background=0",
                              (session,)).fetchone():
            store.end_run(conn, session=session, background_only=True)


def on_agent(data: dict) -> None:
    """PostToolUse on Agent/Task: measure the run, detect escalation, learn."""
    tool_input = data.get("tool_input") or {}
    background = bool(tool_input.get("run_in_background"))
    if not background:
        with store.connect() as conn:
            store.end_run(conn, _run_id(data), data.get("session_id"),
                          learn.tier_of_agent(tool_input.get("subagent_type")))
    agent_tier = learn.tier_of_agent(tool_input.get("subagent_type"))
    if not agent_tier or background:
        # Not one of our tiers, or launched in the background: the result here is
        # only "started", so there is nothing to measure or learn from yet.
        return
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


def write_band_snapshot():
    """Leave a small JSON for the terminal band (hooks/band.tsx): the 24h tier
    mix, escalations and the last route. Written atomically, read by the mod."""
    settings = config.load()
    since = time.time() - 86400
    with store.connect() as conn:
        shares = store.tier_shares(conn, since)
        last = store.last_decision(conn)
        esc = conn.execute("SELECT COUNT(*) FROM decisions WHERE ts>? AND escalations>0",
                           (since,)).fetchone()[0]
    snap = {
        "total": sum(shares.get(t, 0) for t in config.DELEGATED),
        "shares": {t: shares.get(t, 0) for t in config.DELEGATED},
        "cap": settings.opus_daily_cap,
        "escalations": esc,
        "last": {"tier": last["tier"], "final": last["final_tier"] or last["tier"]} if last else None,
    }
    path = config.home() / "band.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    os.replace(tmp, path)


HANDLERS = {"prompt": on_prompt, "agent-start": on_agent_start, "agent": on_agent,
            "subagent-stop": on_subagent_stop, "stop": on_stop}


def script_install_active() -> bool:
    """True when install.py registered hooks in the user's settings.json."""
    claude = os.environ.get("CLAUDE_HOME") or os.path.join(os.path.expanduser("~"), ".claude")
    try:
        with open(os.path.join(claude, "settings.json"), encoding="utf-8") as f:
            return "autorouter/run.py" in f.read()
    except OSError:
        return False


def sync_runtime():
    """Plugin installs live in a versioned folder that moves on every update.
    Keep a copy at a stable path so the status line command never breaks."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return
    import shutil
    from . import __version__
    dest = config.home()
    stamp = dest / "runtime-version"
    try:
        if stamp.read_text(encoding="utf-8").strip() == __version__:
            return
    except OSError:
        pass
    dest.mkdir(parents=True, exist_ok=True)
    pkg = dest / "autorouter"
    if pkg.exists():
        shutil.rmtree(pkg)
    shutil.copytree(os.path.join(root, "autorouter"), pkg, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(root, "run.py"), dest / "run.py")
    shutil.copy2(os.path.join(root, "scripts", "run.js"), dest / "run.js")
    stamp.write_text(__version__, encoding="utf-8")


def run(event: str) -> int:
    try:
        if os.environ.get("CLAUDE_PLUGIN_ROOT") and script_install_active():
            _read_stdin()
            return 0  # the script install handles routing; do not route twice
        sync_runtime()
        out = HANDLERS[event](_read_stdin())
        if event != "agent-start":
            write_band_snapshot()
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - never block Claude Code
        _log_error(event)
    return 0
