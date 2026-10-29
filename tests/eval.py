"""Routing quality on tests/eval.jsonl: rules only, no learned data.

    python3 tests/eval.py [-v]

Each line is {"p": prompt, "t": expected tier or list of acceptable tiers}.
Add a line whenever a prompt is misrouted in real use.
"""
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autorouter import router  # noqa: E402

ORDER = {"direct": 0, "quick": 0, "builder": 1, "architect": 2}


def evaluate():
    """Return (rows, summary). Each row: (prompt, accepted, got, verdict)."""
    with tempfile.TemporaryDirectory() as home:
        os.environ["AUTOROUTER_HOME"] = home
        try:
            rows = []
            for line in (ROOT / "tests" / "eval.jsonl").read_text(encoding="utf-8").splitlines():
                case = json.loads(line)
                want = case["t"] if isinstance(case["t"], list) else [case["t"]]
                got = router.route(case["p"], cwd="/w").tier
                if got in want:
                    verdict = "ok"
                elif "direct" in want or got == "direct":
                    verdict = "wrong"
                elif ORDER[got] < min(ORDER[w] for w in want):
                    verdict = "under"  # too weak a model: the costly mistake
                else:
                    verdict = "over"   # too strong a model: wasted tokens
                rows.append((case["p"], want, got, verdict))
        finally:
            os.environ.pop("AUTOROUTER_HOME", None)
    return rows, Counter(r[3] for r in rows)


def main():
    rows, n = evaluate()
    for p, want, got, v in rows:
        if v != "ok" or "-v" in sys.argv:
            print(f"{v:<6} want {'/'.join(want):<17} got {got:<9} {p}")
    total = len(rows)
    print(f"\n{n['ok']}/{total} correct ({n['ok'] / total:.0%}); "
          f"under-routed {n['under']}, over-routed {n['over']}, wrong direct {n['wrong']}")


if __name__ == "__main__":
    main()
