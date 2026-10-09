import os
import subprocess
import sys
import unittest

from helpers import ADMIN, ALICE, BOB, DaemonHarness, Identity, REPO, rr
from server.daemon import RequestError


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness().start()

    def tearDown(self):
        self.h.cleanup()

    def test_non_admin_cannot_stop_other_user(self):
        run = self.h.start_run(ALICE, steps=20, delay=0.2)
        with self.assertRaises(RequestError) as ctx:
            self.h.call(BOB, "stop", run_id=run["run_id"])
        self.assertIn("no such run", str(ctx.exception))  # does not even confirm the run exists
        self.assertIn(self.h.daemon.registry.get(run["run_id"])["status"], (rr.STARTING, rr.RUNNING))
        denied = self.h.events("authorization_denied")[-1]
        self.assertEqual((denied["user"], denied["action"], denied["owner"]), ("bob", "stop", "alice"))
        for op in ("resume", "run_info", "tail"):
            with self.assertRaises(RequestError):
                self.h.call(BOB, op, run_id=run["run_id"])
        # Bob's own run list never includes Alice's run
        self.assertEqual(self.h.call(BOB, "list_runs")["runs"], [])
        with self.assertRaises(RequestError):
            self.h.call(BOB, "list_runs", all=True)

    def test_admin_can_stop_run(self):
        run = self.h.start_run(ALICE, steps=20, delay=0.2)
        self.h.wait_status(ALICE, run["run_id"], {rr.RUNNING})
        self.h.call(ADMIN, "stop", run_id=run["run_id"])
        self.h.wait_status(ALICE, run["run_id"], {rr.STOPPED})
        ev = self.h.events("run_stop_requested")[-1]
        self.assertEqual((ev["actor"], ev["user"], ev["admin_action"]), ("labadmin", "alice", True))
        self.assertEqual(len(self.h.call(ADMIN, "list_runs", all=True)["runs"]), 1)
        self.assertTrue(self.h.call(ADMIN, "admin_logs", limit=5)["events"])

    def test_identity_comes_from_socket_not_request(self):
        """A client that claims to be someone else in the request body is still itself."""
        from server.client import DaemonClient
        c = DaemonClient(self.h.config.socket_path)
        me = c.call("whoami", username="bob", user="bob", uid=0)
        self.assertEqual(me["uid"], os.getuid())
        self.assertNotEqual(me["username"], "bob")

    def test_non_member_rejected(self):
        mallory = Identity(uid=50099, gid=50099, username="mallory")
        with self.assertRaises(RequestError) as ctx:
            self.h.call(mallory, "workspace_create")
        self.assertIn("agentlab-users", str(ctx.exception))

    def test_status_visibility(self):
        a = self.h.start_run(ALICE, steps=20, delay=0.2)
        view = self.h.call(BOB, "status")
        row = [r for r in view["rows"] if r["user"] == "alice"][0]
        self.assertIsNone(row["run_id"])            # default 'usernames': other users' run IDs hidden
        self.assertNotIn("current_phase", row)
        admin_row = [r for r in self.h.call(ADMIN, "status")["rows"] if r["user"] == "alice"][0]
        self.assertEqual(admin_row["run_id"], a["run_id"])

    def test_cli_has_no_username_override(self):
        from server.agentlab_cli import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["start", "--username", "bob"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--user", "bob", "status"])


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0 and os.environ.get("AGENTLAB_E2E_USERS"),
                     "real multi-account checks need root on an installed Linux server "
                     "(AGENTLAB_E2E_USERS=alice,bob python -m unittest tests.test_permissions)")
class InstalledServerSecurityTests(unittest.TestCase):
    """Runs scripts/security_check.sh against the live installation with real Unix accounts."""

    def test_security_check_script(self):
        a, b = os.environ["AGENTLAB_E2E_USERS"].split(",")[:2]
        proc = subprocess.run(["bash", os.path.join(REPO, "scripts", "security_check.sh"), a, b],
                              capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
