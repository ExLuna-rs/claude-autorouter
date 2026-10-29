"""Command line: hook entry points and inspection tools.

    run.py prompt|agent|stop        hook handlers (JSON on stdin)
    run.py explain "<prompt>"       show how a prompt would be routed
    run.py stats [--days N]         escalation rates, cost per task, learned words
    run.py report [--days N]        misroutes, as material for /router-learn
    run.py statusline               status line: running agents, 24h tier mix, last route
    run.py reset --weights|--all --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, config, hooks, router, store

TIER_W = 10


def cmd_explain(args):
    d = router.route(args.prompt, cwd=args.cwd or os.getcwd())
    print(f"tier       {d.tier}")
    print(f"rules      {d.heuristic}  (score {d.score:+.1f}, learned {d.shift:+.1f})")
    print(f"why        {d.reason or '-'}")
    if d.expected:
        print("expected   " + "  ".join(f"{t}={v:,}" for t, v in d.expected.items()))
        print("p(escal.)  " + "  ".join(f"{t}={v}" for t, v in d.p_escalate.items()))
    print(f"similar    {d.neighbours}")
    line = router.context_line(d)
    print(f"injected   {line or '(nothing)'}")


def _since(days):
    return time.time() - days * 86400


def cmd_stats(args):
    s = config.load()
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM decisions WHERE ts>?", (_since(args.days),)).fetchall()
        outs = conn.execute("SELECT * FROM outcomes WHERE ts>?", (_since(args.days),)).fetchall()
        weights = conn.execute(
            "SELECT token, w, evidence FROM weights WHERE evidence>=? ORDER BY w DESC",
            (s.weight_min_evidence,)).fetchall()
        share, n_today = store.today_tier_share(conn, "architect")

    print(f"claude-autorouter {__version__} — last {args.days} days, {len(rows)} routed tasks\n")
    if not rows:
        print("No data yet. Use Claude Code normally; stats appear after a few tasks.")
        return

    print(f"{'tier':<{TIER_W}} {'tasks':>6} {'share':>6} {'escal.':>7} {'overkill':>9} {'cost/task':>10}")
    for t in config.DELEGATED:
        tr = [r for r in rows if r["tier"] == t]
        if not tr:
            continue
        esc = sum(r["escalations"] > 0 for r in tr) / len(tr)
        ok = sum(r["overkill"] for r in tr) / len(tr)
        costs = [r["cost"] for r in tr if r["cost"]]
        avg = f"{sum(costs) / len(costs):,.0f}" if costs else "-"
        print(f"{t:<{TIER_W}} {len(tr):>6} {len(tr) / len(rows):>6.0%} {esc:>7.0%} {ok:>9.0%} {avg:>10}")

    costed = [r for r in rows if r["cost"]]
    if costed:
        mean = sum(r["cost"] for r in costed) / len(costed)
        half = len(costed) // 2
        trend = ""
        if half >= 5:
            older = sorted(costed, key=lambda r: r["ts"])
            a = sum(r["cost"] for r in older[:half]) / half
            b = sum(r["cost"] for r in older[half:]) / (len(older) - half)
            trend = f"  (first half {a:,.0f} -> second half {b:,.0f}, {b / a - 1:+.0%})"
        print(f"\ncost per task (weighted tokens): {mean:,.0f}{trend}")

        # Rough baseline: the same tokens, priced as if every model were Opus.
        opus = config.model_weight(s, "opus")
        sub = {}
        for o in outs:
            if o["decision_id"] and o["cost"]:
                sub.setdefault(o["decision_id"], []).append(o)
        actual = baseline = 0.0
        for r in costed:
            runs = sub.get(r["id"], [])
            run_cost = sum(o["cost"] for o in runs)
            main = max(0.0, r["cost"] - run_cost)
            actual += r["cost"]
            baseline += main * opus / config.model_weight(s, "sonnet")
            baseline += sum(o["cost"] * opus / config.model_weight(s, config.AGENT_MODEL[o["agent"]])
                            for o in runs)
        if baseline:
            print(f"vs same tokens all on Opus (rough): {1 - actual / baseline:.0%} cheaper")

    print(f"architect share, last 24h: {share:.0%} of {n_today} (cap {s.opus_daily_cap:.0%})")

    if weights:
        up = [w for w in weights if w["w"] > 0.05][:8]
        down = [w for w in reversed(weights) if w["w"] < -0.05][:8]
        if up:
            print("\nlearned harder: " + ", ".join(f"{w['token']} {w['w']:+.2f}" for w in up))
        if down:
            print("learned easier: " + ", ".join(f"{w['token']} {w['w']:+.2f}" for w in down))


SHORT = {"quick": "quick", "builder": "builder", "architect": "architect", "direct": "direct"}


def statusline(session=None, now=None) -> str:
    """One line: running subagents, 24h tier shares (architect vs its cap), last route."""
    s = config.load()
    now = now or time.time()
    with store.connect() as conn:
        running = store.running_agents(conn)
        shares = store.tier_shares(conn, now - 86400)
        last = store.last_decision(conn, session)

    parts = []
    if running:
        counts = {}
        for a in running:
            counts[a] = counts.get(a, 0) + 1
        parts.append("▶ " + ", ".join(f"{a}×{n}" if n > 1 else a for a, n in counts.items()))
    else:
        parts.append("idle")

    total = sum(shares.values())
    if total:
        mix = []
        for t in config.DELEGATED:
            pct = shares.get(t, 0) / total
            cell = f"{t} {pct:.0%}"
            if t == "architect":
                cell += f"/{s.opus_daily_cap:.0%}"
            mix.append(cell)
        parts.append(f"24h {total}: " + " · ".join(mix))

    if last:
        tail = f"last: {last['tier']}"
        if last["final_tier"] and last["final_tier"] != last["tier"]:
            tail += f"→{last['final_tier']}"
        parts.append(tail)
    return "router " + " │ ".join(parts)


def cmd_statusline(args):
    session = None
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read() or "{}")
            session = data.get("session_id") if isinstance(data, dict) else None
        except ValueError:
            pass
    try:
        print(statusline(session))
    except Exception:  # noqa: BLE001 - a status line must always print something
        print("router")


def cmd_report(args):
    s = config.load()
    with store.connect() as conn:
        esc = conn.execute(
            "SELECT prompt, project, heuristic, tier, final_tier, reason FROM decisions"
            " WHERE ts>? AND escalations>0 ORDER BY ts DESC LIMIT 60", (_since(args.days),)).fetchall()
        over = conn.execute(
            "SELECT prompt, project, heuristic, tier, reason FROM decisions"
            " WHERE ts>? AND overkill=1 ORDER BY ts DESC LIMIT 40", (_since(args.days),)).fetchall()
        corrected = conn.execute(
            "SELECT prompt, heuristic, tier FROM decisions WHERE ts>? AND reason LIKE '%user corrected%'"
            " ORDER BY ts DESC LIMIT 40", (_since(args.days),)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM decisions WHERE ts>?", (_since(args.days),)).fetchone()[0]

    print(f"# Routing report — last {args.days} days, {total} tasks\n")
    print("## Escalated (routed too low)")
    for r in esc:
        print(f"- [{r['tier']} -> {r['final_tier']}] rules={r['heuristic']} | {r['prompt'] or '(prompt not stored)'}")
    print("\n## Overkill (routed too high)")
    for r in over:
        print(f"- [{r['tier']}] rules={r['heuristic']} | {r['prompt'] or '(prompt not stored)'}")
    print("\n## User overrides (router choice -> user choice)")
    for r in corrected:
        print(f"- [{r['heuristic']} -> {r['tier']}] {r['prompt'] or ''}")
    print("\n## Current extra patterns (config.json)")
    print(json.dumps({"extra_strong": s.extra_strong, "extra_weak": s.extra_weak,
                      "extra_quick": s.extra_quick}, indent=2))
    print(f"\nconfig file: {config.home() / 'config.json'}")


def cmd_reset(args):
    if not args.yes:
        print("Refusing without --yes.", file=sys.stderr)
        return 1
    with store.connect() as conn:
        conn.execute("DELETE FROM weights")
        if args.all:
            conn.execute("DELETE FROM outcomes")
            conn.execute("DELETE FROM decisions")
    print("Learned weights cleared." + (" History cleared." if args.all else ""))
    return 0


def main(argv=None) -> int:
    # Windows consoles default to a legacy code page; our output is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in hooks.HANDLERS:
        return hooks.run(argv[0])

    p = argparse.ArgumentParser(prog="autorouter", description="Cost-aware model router for Claude Code")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("explain", help="show how a prompt would be routed")
    e.add_argument("prompt")
    e.add_argument("--cwd")
    st = sub.add_parser("stats", help="routing and cost statistics")
    st.add_argument("--days", type=int, default=30)
    rp = sub.add_parser("report", help="misroutes, for /router-learn")
    rp.add_argument("--days", type=int, default=14)
    sub.add_parser("statusline", help="one-line status for Claude Code's status line")
    rs = sub.add_parser("reset", help="clear learned data")
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--weights", action="store_true")
    g.add_argument("--all", action="store_true")
    rs.add_argument("--yes", action="store_true")

    args = p.parse_args(argv)
    fn = {"explain": cmd_explain, "stats": cmd_stats, "report": cmd_report, "reset": cmd_reset,
          "statusline": cmd_statusline}[args.cmd]
    return fn(args) or 0
