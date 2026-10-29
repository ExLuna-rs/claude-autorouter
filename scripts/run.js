#!/usr/bin/env node
// Plugin entry point: finds a Python 3.9+ interpreter and runs run.py with
// the same arguments and stdin. Node is used because it is guaranteed in a
// plugin install (the Ponytail dependency needs it), while the Python
// command name differs by platform: python3, python or py.
// Exits 0 on any failure so a missing Python never blocks Claude Code.
"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const runPy = [path.join(__dirname, "run.py"), path.join(__dirname, "..", "run.py")]
  .find((p) => fs.existsSync(p));
const args = process.argv.slice(2);
const isHook = ["prompt", "agent-start", "agent", "subagent-stop", "stop"].includes(args[0]);
const isStatus = args[0] === "statusline";

let input = Buffer.alloc(0);
if (isHook || isStatus) {
  try { input = fs.readFileSync(0); } catch (_) { /* no stdin */ }
}

const candidates = process.platform === "win32"
  ? [["py", ["-3"]], ["python", []], ["python3", []]]
  : [["python3", []], ["python", []]];

for (const [cmd, pre] of candidates) {
  const r = spawnSync(cmd, [...pre, runPy, ...args], {
    input,
    stdio: ["pipe", "inherit", "inherit"],
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  // ENOENT: not installed. 9009: the Windows Store "python" stub.
  // 3: run.py's "Python too old" exit code. Try the next candidate.
  if (r.error || r.status === 9009 || r.status === 3) continue;
  process.exit(isHook ? 0 : (r.status ?? 0));
}

if (isStatus) {
  console.log("router (python not found)");
  process.exit(0);
}
if (!isHook) {
  console.error("claude-autorouter: Python 3.9+ not found (tried python3, python, py).");
  process.exit(1);
}
process.exit(0);
