#!/usr/bin/env python3
"""Install or uninstall claude-autorouter into ~/.claude (all projects).

    python3 install.py               install / upgrade (idempotent)
    python3 install.py --hygiene     also deny reads of lockfiles, node_modules, build output
    python3 install.py --uninstall   remove hooks, agents, commands (keeps learned data)

On Windows use `py install.py` (or `python install.py`).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
CLAUDE = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
DEST = CLAUDE / "autorouter"
# Absolute, forward-slash paths work in bash, zsh, cmd and PowerShell alike,
# and pin the hooks to the interpreter that ran the installer
# (python3 / python / py differ across platforms).
PYTHON = Path(sys.executable).resolve().as_posix()
RUN_PY = (DEST / "run.py").as_posix()
RUN = f'"{PYTHON}" "{RUN_PY}"'
MARK = "autorouter/run.py"
LEGACY = ("autorouter.py", "route.py")  # hooks from the earlier single-file version

AGENTS = ["quick.md", "builder.md", "architect.md"]
COMMANDS = ["router-stats.md", "router-learn.md"]

HOOKS = {
    "UserPromptSubmit": {"hooks": [{"type": "command", "command": f"{RUN} prompt", "timeout": 10}]},
    "PostToolUse": {"matcher": "Agent|Task",
                    "hooks": [{"type": "command", "command": f"{RUN} agent", "timeout": 10}]},
    "Stop": {"hooks": [{"type": "command", "command": f"{RUN} stop", "timeout": 10}]},
}

HYGIENE_DENY = [
    "Read(**/node_modules/**)", "Read(**/package-lock.json)", "Read(**/yarn.lock)",
    "Read(**/pnpm-lock.yaml)", "Read(**/poetry.lock)", "Read(**/Cargo.lock)",
    "Read(**/dist/**)", "Read(**/build/**)", "Read(**/.next/**)", "Read(**/*.min.js)",
]


def _ours(entry) -> bool:
    return any(MARK in h.get("command", "") or h.get("command", "").endswith(LEGACY)
               for h in entry.get("hooks", []))


def load_settings(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"error: {path} is not valid JSON; fix it first, nothing was changed.")


def save_settings(path: Path, cfg: dict):
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def strip_hooks(cfg: dict):
    hooks = cfg.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not _ours(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        cfg.pop("hooks", None)


def install(hygiene: bool):
    for d in ("agents", "commands"):
        (CLAUDE / d).mkdir(parents=True, exist_ok=True)
    for name in AGENTS:
        shutil.copy2(SRC / "agents" / name, CLAUDE / "agents" / name)
    for name in COMMANDS:
        text = (SRC / "commands" / name).read_text(encoding="utf-8")
        (CLAUDE / "commands" / name).write_text(text.replace("{{RUN}}", RUN), encoding="utf-8")

    DEST.mkdir(parents=True, exist_ok=True)
    if (DEST / "autorouter").exists():
        shutil.rmtree(DEST / "autorouter")
    shutil.copytree(SRC / "autorouter", DEST / "autorouter",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(SRC / "run.py", DEST / "run.py")

    path = CLAUDE / "settings.json"
    cfg = load_settings(path)
    strip_hooks(cfg)
    for event, entry in HOOKS.items():
        cfg.setdefault("hooks", {}).setdefault(event, []).append(entry)

    notes = []
    if "model" not in cfg:
        cfg["model"] = "sonnet"
        notes.append("main session model set to sonnet (the orchestrator)")
    elif "opus" in str(cfg["model"]).lower():
        notes.append(f"main session model is {cfg['model']}: sonnet is a cheaper orchestrator")
    env = cfg.get("env", {})
    if env.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        notes.append(f"removed CLAUDE_CODE_SUBAGENT_MODEL={env.pop('CLAUDE_CODE_SUBAGENT_MODEL')}"
                     " (it overrides per-agent models)")
    if hygiene:
        deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
        added = [r for r in HYGIENE_DENY if r not in deny]
        deny.extend(added)
        notes.append(f"added {len(added)} read-deny rules for bulky files")
    save_settings(path, cfg)

    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        notes.append("CLAUDE_CODE_SUBAGENT_MODEL is set in your shell: unset it, or agent models are ignored")

    print(f"claude-autorouter installed in {DEST}")
    print("  agents   quick (haiku), builder (sonnet), architect (opus)")
    print("  hooks    UserPromptSubmit, PostToolUse(Agent|Task), Stop")
    print("  commands /router-stats, /router-learn")
    for n in notes:
        print(f"  note     {n}")
    if path.with_suffix(".json.bak").exists():
        print(f"  backup   {path.with_suffix('.json.bak')}")
    print(f"Restart Claude Code, then try: {RUN} explain \"your prompt\"")


def uninstall():
    path = CLAUDE / "settings.json"
    cfg = load_settings(path)
    strip_hooks(cfg)
    deny = cfg.get("permissions", {}).get("deny")
    if deny:
        cfg["permissions"]["deny"] = [r for r in deny if r not in HYGIENE_DENY]
    save_settings(path, cfg)
    for name in AGENTS:
        (CLAUDE / "agents" / name).unlink(missing_ok=True)
    for name in COMMANDS:
        (CLAUDE / "commands" / name).unlink(missing_ok=True)
    for p in (DEST / "autorouter", DEST / "run.py"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    print("claude-autorouter removed. Learned data kept in "
          f"{DEST} (delete that folder to erase it).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hygiene", action="store_true")
    a = ap.parse_args()
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9+ is required.")
    uninstall() if a.uninstall else install(a.hygiene)


if __name__ == "__main__":
    main()
