import contextlib
import io
import os
import time
import unittest

from helpers import DaemonHarness, ME, rr
from server import agentlab_cli
from server.client import DaemonClient


class FakeTTY(io.StringIO):
    def isatty(self):
        return False


class CLITests(unittest.TestCase):
    """Drives the real `agentlab` CLI against a live daemon over its Unix socket as the current OS user."""

    def setUp(self):
        self.h = DaemonHarness().start()
        self.client = DaemonClient(self.h.config.socket_path)
        os.environ["AGENTLAB_SERVER_CONFIG"] = "/nonexistent"  # CLI config defaults; socket is injected
        self.cfgfile = os.path.join(self.h.tmp, "sim.yaml")
        with open(self.cfgfile, "w") as f:
            f.write("simulated-steps-per-phase: 2\nsimulated-step-seconds: 0.05\n")

    def tearDown(self):
        self.h.cleanup()
        os.environ.pop("AGENTLAB_SERVER_CONFIG", None)

    def run_cli(self, argv, stdin_text=""):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = agentlab_cli.main(argv, client=self.client, stdin=FakeTTY(stdin_text))
        return code, out.getvalue()

    def _patch_workspace_root(self):
        # The CLI writes research_idea.txt itself (as the user) at the path the daemon reports.
        pass

    def test_cli_first_login_flow(self):
        code, out = self.run_cli(["start", "--config", self.cfgfile],
                                 "y\nI propose a new\nmulti-line idea\n\n  with indentation\nEND\n")
        self.assertEqual(code, 0, out)
        self.assertIn("No research workspace exists for your account.", out)
        self.assertIn("Create this workspace? [y/N]: ", out)
        self.assertIn("Workspace created successfully.", out)
        self.assertIn("Research idea saved.", out)
        self.assertIn("If your SSH session disconnects, your research will continue.", out)
        run_id = [l for l in out.splitlines() if l.startswith("2026") or l[:8].isdigit()][0].strip()
        ws = os.path.join(self.h.config.workspace_root, ME.username)
        with open(os.path.join(ws, "research_idea.txt")) as f:
            text = f.read()
        self.assertIn(f"Researcher: {ME.username}", text)
        self.assertIn("multi-line idea\n\n  with indentation", text)
        self.h.wait_status(ME, run_id, {rr.COMPLETED})
        self.assertEqual(self.h.events("research_submitted")[-1]["user"], ME.username)

        code, out = self.run_cli(["runs"])
        self.assertIn(run_id, out)
        self.assertIn("COMPLETED", out)
        code, out = self.run_cli(["history"])
        self.assertIn("run_completed", out)
        code, out = self.run_cli(["info", run_id])
        self.assertIn("COMPLETED", out)
        code, out = self.run_cli(["logs", run_id])
        self.assertIn("completed", out)

    def test_cli_declining_workspace_starts_nothing(self):
        code, out = self.run_cli(["start"], "n\n")
        self.assertEqual(code, 1)
        self.assertIn("A workspace is required", out)
        self.assertEqual(self.h.daemon.registry.all(), [])

    def test_cli_empty_idea_rejected(self):
        code, out = self.run_cli(["start", "--config", self.cfgfile], "y\n\n   \nEND\n")
        self.assertEqual(code, 1)
        self.assertIn("research idea is empty", out)
        self.assertEqual(self.h.daemon.registry.all(), [])

    def test_cli_new_user_guided_flow(self):
        code, out = self.run_cli([], "y\nMy first idea\nEND\nmath-verifiers\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Workspace does not exist.", out)
        self.assertIn("Workspace created successfully.", out)
        self.assertIn("Short name for this job", out)
        run = self.h.daemon.registry.all()[0]
        self.assertEqual(run["name"], "math-verifiers")

    def test_cli_returning_user_checks_job_by_id_and_resumes(self):
        long_cfg = os.path.join(self.h.tmp, "long.yaml")
        with open(long_cfg, "w") as f:
            f.write("simulated-steps-per-phase: 40\nsimulated-step-seconds: 0.1\n")
        self.run_cli(["start", "--config", long_cfg, "--name", "first"], "y\nidea one\nEND\n")
        self.run_cli(["start", "--config", long_cfg, "--name", "second"], "idea two\nEND\n")
        runs = sorted(self.h.daemon.registry.all(), key=lambda r: r["created_time"] + r["run_id"])
        self.assertEqual(len(runs), 2)  # two jobs for the same user at the same time
        first = [r for r in runs if r["name"] == "first"][0]["run_id"]
        self.h.wait_status(ME, first, {rr.RUNNING})
        time.sleep(0.5)

        code, out = self.run_cli([], f"c\n{first}\nn\n")  # plain `agentlab`, check by job ID
        self.assertIn(f"Welcome back, {ME.username}. You have 2 research job(s), 2 active", out)
        self.assertIn("first", out)
        self.assertIn("second", out)
        self.assertIn(f"Job ID:          {first}", out)
        self.assertIn("This job is active. Follow its log now?", out)
        self.assertEqual(len(self.h.daemon.registry.all()), 2)  # checking never starts a duplicate

        os.kill(self.h.daemon.registry.get(first)["process"]["pid"], 9)
        self.h.wait_status(ME, first, {rr.INTERRUPTED})
        code, out = self.run_cli(["check", first[:-2]], "Y\n")  # a unique prefix works too
        self.assertIn("Status:          INTERRUPTED", out)
        self.assertIn("Resume this job from its last checkpoint?", out)
        self.assertIn("queued for resume", out)
        self.h.wait_status(ME, first, {rr.STARTING, rr.RUNNING, rr.COMPLETED})

    def test_cli_returning_user_can_start_another_job(self):
        long_cfg = os.path.join(self.h.tmp, "long.yaml")
        with open(long_cfg, "w") as f:
            f.write("simulated-steps-per-phase: 40\nsimulated-step-seconds: 0.1\n")
        with open(os.path.join(self.h.tmp, "x"), "w"):
            pass
        self.run_cli(["start", "--config", long_cfg, "--name", "a"], "y\nidea a\nEND\n")
        code, out = self.run_cli([], "n\nidea b\nEND\nb\n")
        self.assertIn("[n] start new research", out)
        self.assertEqual(sorted(r["name"] for r in self.h.daemon.registry.all()), ["a", "b"])

    def test_cli_status_table(self):
        self.run_cli(["start", "--config", self.cfgfile], "y\nidea\nEND\n")
        code, out = self.run_cli(["status"])
        self.assertIn("AgentLaboratory Active Users", out)
        self.assertIn("USER", out)
        self.assertIn("Total active research runs:", out)

    def test_cli_stop_and_resume(self):
        long_cfg = os.path.join(self.h.tmp, "long.yaml")
        with open(long_cfg, "w") as f:
            f.write("simulated-steps-per-phase: 40\nsimulated-step-seconds: 0.1\n")
        self.run_cli(["start", "--config", long_cfg], "y\nidea\nEND\n")
        run_id = self.h.daemon.registry.all()[0]["run_id"]
        self.h.wait_status(ME, run_id, {rr.RUNNING})
        code, out = self.run_cli(["stop", run_id])
        self.assertIn("Stop requested", out)
        self.h.wait_status(ME, run_id, {rr.STOPPED})
        code, out = self.run_cli(["resume", run_id])
        self.assertEqual(code, 0, out)
        code, out = self.run_cli(["resume", "20261008_000000_someone_abcdef"])
        self.assertEqual(code, 2)
        self.assertIn("no such run", out)

    def test_config_with_api_key_rejected(self):
        bad = os.path.join(self.h.tmp, "bad.yaml")
        with open(bad, "w") as f:
            f.write('api-key: "sk-proj-abcdefabcdefabcdefabcdef"\n')
        code, out = self.run_cli(["start", "--config", bad], "y\nidea\nEND\n")
        self.assertEqual(code, 2)
        self.assertIn("contains an API key", out)
        self.assertNotIn("sk-proj-abcdef", open(self.h.config.audit_log).read())


if __name__ == "__main__":
    unittest.main()
