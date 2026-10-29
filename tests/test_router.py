import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autorouter import config, features, hooks, learn, router, store, usage  # noqa: E402


class Isolated(unittest.TestCase):
    """Every test gets its own empty router home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["AUTOROUTER_HOME"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("AUTOROUTER_HOME", None)
        self._tmp.cleanup()

    def tier(self, prompt, cwd="/work/app"):
        return router.route(prompt, cwd=cwd).tier


class RulesTest(Isolated):
    CASES = [
        ("where is the auth middleware defined?", "quick"),
        ("find all calls to fetchUser", "quick"),
        ("rename userId to accountId in utils.ts", "quick"),
        ("what does this docker-compose file do?", "quick"),
        ("où est défini le middleware d'auth ?", "quick"),
        ("add a GET /health endpoint returning the database status", "builder"),
        ("write unit tests for the CSV parser", "builder"),
        ("fix the bug in the orders list pagination", "builder"),
        ("corrige le bug dans la pagination de la liste des commandes", "builder"),
        ("why does the app crash randomly at startup?", "architect"),
        ("design the architecture of a distributed job queue", "architect"),
        ("security audit of the licence server, look for vulnerabilities", "architect"),
        ("audit de sécurité du serveur de licences", "architect"),
        ("migrate the project from Vue 2 to Vue 3", "architect"),
        ("optimise the render thread performance, there is a concurrency issue", "architect"),
    ]

    def test_rule_routing(self):
        for prompt, expected in self.CASES:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.tier(prompt), expected)

    def test_follow_up_is_direct(self):
        for p in ["ok", "yes go ahead", "oui vas-y", "merci !", "continue"]:
            with self.subTest(p=p):
                self.assertEqual(self.tier(p), "direct")

    def test_long_follow_up_is_routed(self):
        self.assertNotEqual(self.tier("ok now design the architecture of the sync engine"), "direct")

    def test_overrides(self):
        self.assertEqual(self.tier("!architect fix this typo"), "architect")
        self.assertEqual(self.tier("!quick audit everything"), "quick")
        self.assertEqual(self.tier("!direct design a system"), "direct")

    def test_bad_user_pattern_is_ignored(self):
        s = features.signals("design a cache", extra_strong=["(unclosed"])
        self.assertGreaterEqual(s.strong, 1)

    def test_project_min_tier(self):
        cfg = config.home() / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"projects": {"/work/secure": {"min_tier": "builder"}}}))
        self.assertEqual(self.tier("rename foo to bar", cwd="/work/secure/api"), "builder")
        self.assertEqual(self.tier("rename foo to bar", cwd="/work/other"), "quick")

    def test_windows_paths(self):
        cfg = config.home() / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"projects": {"C:/Code/Secure": {"min_tier": "builder"}}}))
        cwd = "C:\\Code\\Secure\\api" if os.name != "nt" else "c:\\code\\secure\\api"
        self.assertEqual(self.tier("rename foo to bar", cwd=cwd.replace("c:", "C:") if os.name != "nt" else cwd),
                         "builder")
        self.assertEqual(router.project_name("C:\\Code\\Secure\\api\\"), "api")

    def test_utf8_stdin(self):
        r = subprocess.run([sys.executable, str(ROOT / "run.py"), "prompt"],
                           input=json.dumps({"prompt": "conçois l'architecture d'un système distribué",
                                             "session_id": "u"}, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=dict(os.environ), timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"tier=architect", r.stdout)

    def test_context_line_is_short(self):
        for p, _ in self.CASES:
            line = router.context_line(router.route(p, cwd="/w"))
            self.assertLess(len(line.split()), 40, line)


def run_task(session, prompt, runs, cwd="/work/app"):
    """Simulate one task: prompt hook, then delegated runs [(agent, response)]."""
    out = hooks.on_prompt({"prompt": prompt, "session_id": session, "cwd": cwd})
    for agent, resp in runs:
        hooks.on_agent({"session_id": session, "tool_name": "Agent",
                        "tool_input": {"subagent_type": agent}, "tool_response": resp})
    return out


def escalation(text="ESCALATE: touches several modules\nHANDOFF:\n- files: a.py"):
    return {"content": [{"type": "text", "text": text}], "totalTokens": 9000, "totalToolUseCount": 4}


def done(tokens=20000, tools=6):
    return {"content": [{"type": "text", "text": "done: x\nfiles: a.py"}],
            "totalTokens": tokens, "totalToolUseCount": tools}


class LearningTest(Isolated):
    def test_repeated_escalations_route_higher(self):
        prompt = "show the retry logic in the http client"
        self.assertEqual(self.tier(prompt), "quick")
        variants = ["show the retry logic in the http client module",
                    "show me the retry logic of the http client",
                    "show the http client retry logic and backoff",
                    "show where the http client retry logic lives"]
        for i, v in enumerate(variants):
            run_task("s1", v, [("quick", escalation()), ("builder", done())])
        hooks.on_prompt({"prompt": "unrelated: rename a to b", "session_id": "s1", "cwd": "/work/app"})
        self.assertEqual(self.tier(prompt), "builder")

    def test_cheap_history_never_skips_two_tiers(self):
        # Cheap measured runs must not tempt the router to send hard work to Haiku.
        for i in range(6):
            run_task("s9", f"find the session store {i}", [("quick", done(2000, 1))])
            run_task("s9", f"add pagination to endpoint {i}", [("builder", done(4000, 3))])
        self.assertEqual(self.tier("design the plugin architecture"), "architect")

    def test_downgrade_needs_evidence(self):
        for i in range(4):
            run_task("s10", f"design the cache invalidation scheme {i}",
                     [("architect", done(tokens=3000, tools=1))])
        # Rules alone say architect; overkill history (weights + neighbours) says builder.
        self.assertEqual(features.tier_from_score(features.base_score(
            features.signals("design the cache invalidation scheme"))[0]), "architect")
        self.assertEqual(self.tier("design the cache invalidation scheme"), "builder")

    def test_escalation_counts_once_and_sets_final(self):
        run_task("s2", "list the config loaders", [("quick", escalation()), ("builder", done())])
        with store.connect() as conn:
            d = conn.execute("SELECT * FROM decisions").fetchone()
        self.assertEqual(d["escalations"], 1)
        self.assertEqual(d["final_tier"], "builder")

    def test_orchestrator_climb_counts_as_escalation(self):
        run_task("s3", "list the config loaders", [("architect", done())])
        with store.connect() as conn:
            d = conn.execute("SELECT * FROM decisions").fetchone()
        self.assertEqual((d["tier"], d["final_tier"], d["escalations"]), ("quick", "architect", 1))

    def test_overkill_is_flagged_and_lowers_weights(self):
        run_task("s4", "design the architecture of the cache layer",
                 [("architect", done(tokens=3000, tools=1))])
        with store.connect() as conn:
            d = conn.execute("SELECT * FROM decisions").fetchone()
            w = conn.execute("SELECT w FROM weights WHERE token='cache'").fetchone()
        self.assertEqual(d["overkill"], 1)
        self.assertLess(w["w"], 0)

    def test_weights_need_evidence(self):
        run_task("s5", "find the token parser", [("quick", escalation()), ("builder", done())])
        with store.connect() as conn:
            self.assertEqual(router.learned_shift(conn, ["token", "parser"], config.load()), 0.0)

    def test_user_override_teaches(self):
        for _ in range(3):
            hooks.on_prompt({"prompt": "!builder find the token parser", "session_id": "s6", "cwd": "/w"})
        with store.connect() as conn:
            shift = router.learned_shift(conn, ["find", "token", "parser"], config.load())
        self.assertGreater(shift, 0)

    def test_opus_cap(self):
        for i in range(8):
            run_task("s7", f"design the architecture of module {i} storage", [])
        d = router.route("design the cache layer", cwd="/work/app")
        self.assertEqual(d.tier, "builder")
        self.assertTrue(d.capped)
        self.assertIn("Opus daily budget", router.context_line(d))
        # Two strong signals bypass the cap.
        self.assertEqual(self.tier("design the architecture of a distributed queue"), "architect")


class UsageTest(Isolated):
    def test_agent_run_variants(self):
        s = config.load()
        full = usage.agent_run({"usage": {"input_tokens": 100, "output_tokens": 10}}, "haiku", s)
        self.assertEqual(full["cost"], 150)
        totals = usage.agent_run({"totalTokens": 1000}, "opus", s)
        self.assertEqual(totals["cost"], 2500)
        text_only = usage.agent_run("ESCALATE: hard", "sonnet", s)
        self.assertIn("ESCALATE:", text_only["text"])
        self.assertIsNone(usage.agent_run(None, "sonnet", s)["tool_uses"])

    def test_stop_adds_main_session_cost(self):
        tr = Path(self._tmp.name) / "t.jsonl"
        lines = [
            {"type": "user", "message": {"content": "old prompt"}},
            {"type": "assistant", "message": {"id": "m0", "model": "claude-sonnet-x",
                                              "usage": {"input_tokens": 999999}}},
            {"type": "user", "message": {"content": "design the queue"}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-x",
                                              "usage": {"input_tokens": 100, "output_tokens": 20}}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-x",
                                              "usage": {"input_tokens": 100, "output_tokens": 20}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "assistant", "message": {"id": "m2", "model": "claude-sonnet-x",
                                              "usage": {"cache_read_input_tokens": 1000}}},
        ]
        tr.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        hooks.on_prompt({"prompt": "design the queue", "session_id": "s8", "cwd": "/w"})
        hooks.on_stop({"session_id": "s8", "transcript_path": str(tr)})
        with store.connect() as conn:
            cost = conn.execute("SELECT cost FROM decisions").fetchone()["cost"]
        self.assertAlmostEqual(cost, (100 + 5 * 20 + 0.1 * 1000) * 3)


class HookProcessTest(Isolated):
    def run_hook(self, event, payload):
        env = dict(os.environ)
        return subprocess.run([sys.executable, str(ROOT / "run.py"), event], input=payload,
                              capture_output=True, text=True, env=env, timeout=30)

    def test_prompt_hook_emits_context(self):
        r = self.run_hook("prompt", json.dumps({"prompt": "find the auth middleware",
                                                 "session_id": "p1", "cwd": "/w"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tier=quick", ctx)

    def test_slash_commands_and_follow_ups_are_silent(self):
        for p in ["/router-stats", "ok vas-y"]:
            r = self.run_hook("prompt", json.dumps({"prompt": p, "session_id": "p2"}))
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_hooks_never_fail(self):
        for event in ["prompt", "agent", "stop"]:
            for payload in ["", "{not json", "[]", json.dumps({"tool_input": "weird"})]:
                with self.subTest(event=event, payload=payload):
                    r = self.run_hook(event, payload)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stderr, "")

    def test_cli_commands(self):
        for args in (["explain", "design a queue"], ["stats"], ["report"]):
            r = subprocess.run([sys.executable, str(ROOT / "run.py"), *args],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)


class AgentPromptTest(unittest.TestCase):
    def test_agents_carry_their_rules(self):
        agents = ROOT / "agents"
        for name in ["builder.md", "architect.md"]:
            text = (agents / name).read_text(encoding="utf-8")
            self.assertIn("<!-- ponytail:start -->", text, name)
            self.assertIn("<!-- ponytail:end -->", text, name)
            self.assertIn("ESCALATE" if name == "builder.md" else "conclusion:", text, name)
        for name, model in [("quick.md", "haiku"), ("builder.md", "sonnet"), ("architect.md", "opus")]:
            self.assertIn(f"model: {model}", (agents / name).read_text(encoding="utf-8"))


class InstallTest(unittest.TestCase):
    def install(self, settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        claude = Path(tmp.name) / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps(settings))
        env = {**os.environ, "CLAUDE_HOME": str(claude)}
        r = subprocess.run([sys.executable, str(ROOT / "install.py")], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return claude, r.stdout

    def test_ponytail_kept_without_plugin(self):
        claude, _ = self.install({})
        self.assertIn("lazy senior developer", (claude / "agents" / "builder.md").read_text(encoding="utf-8"))

    def test_ponytail_skipped_when_plugin_enabled(self):
        claude, out = self.install({"enabledPlugins": {"ponytail@ponytail": True}})
        for name in ["builder.md", "architect.md"]:
            text = (claude / "agents" / name).read_text(encoding="utf-8")
            self.assertNotIn("ponytail:start", text)
            self.assertIn("## Report format", text)
            self.assertNotIn("\n\n\n", text)
        self.assertIn("Ponytail plugin detected", out)

    def test_install_is_idempotent_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as home:
            claude = Path(home) / ".claude"
            claude.mkdir()
            (claude / "settings.json").write_text(json.dumps({
                "model": "opus",
                "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "inherit"},
                "hooks": {"UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/autorouter.py"}]},
                    {"hooks": [{"type": "command", "command": "my-own-hook"}]}]}}))
            env = {**os.environ, "CLAUDE_HOME": str(claude)}
            env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
            for _ in range(2):
                r = subprocess.run([sys.executable, str(ROOT / "install.py"), "--hygiene"],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            cfg = json.loads((claude / "settings.json").read_text())
            cmds = [h["command"] for e in cfg["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
            self.assertEqual(cmds[0], "my-own-hook")
            self.assertEqual(len(cmds), 2)
            self.assertTrue(cmds[1].endswith('autorouter/run.py" prompt'), cmds[1])
            self.assertNotIn("\\", cmds[1])
            stats_cmd = (claude / "commands" / "router-stats.md").read_text(encoding="utf-8")
            self.assertNotIn("{{RUN}}", stats_cmd)
            # The installed hook command actually runs.
            r = subprocess.run(cmds[1].replace(" prompt", " explain \"find x\""), shell=True,
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("tier", r.stdout)
            self.assertEqual(len(cfg["hooks"]["PostToolUse"]), 1)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", cfg["env"])
            self.assertEqual(cfg["model"], "opus")  # user choice is kept
            self.assertEqual(len(cfg["permissions"]["deny"]), len(set(cfg["permissions"]["deny"])))
            self.assertTrue((claude / "agents" / "architect.md").exists())
            self.assertTrue((claude / "autorouter" / "run.py").exists())

            r = subprocess.run([sys.executable, str(ROOT / "install.py"), "--uninstall"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            cfg = json.loads((claude / "settings.json").read_text())
            self.assertEqual(cfg["hooks"], {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "my-own-hook"}]}]})
            self.assertNotIn("Read(**/node_modules/**)", cfg["permissions"]["deny"])
            self.assertFalse((claude / "agents" / "quick.md").exists())


if __name__ == "__main__":
    unittest.main()
