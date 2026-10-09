import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from helpers import ALICE, BOB, DaemonHarness
from server.audit_logger import AuditLogger, read_events, verify_chain
from server.daemon import RequestError
from server.secret_guard import REDACTED, redact, scrubbed_environment


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "logs", "audit.log")
        self.log = AuditLogger(self.path)
        self.log.ensure()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_jsonl_format_and_mode(self):
        self.log.log("login", user="alice", uid=1002, pid=12345)
        with open(self.path) as f:
            rec = json.loads(f.readline())
        for key in ("timestamp", "event", "user", "uid", "host", "pid", "prev", "hash"):
            self.assertIn(key, rec)
        self.assertTrue(rec["timestamp"].endswith("Z"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)  # not world-readable/writable

    def test_audit_log_protection_hash_chain_detects_tampering(self):
        for i in range(5):
            self.log.log("run_started", user="alice", run_id=f"r{i}")
        self.assertTrue(verify_chain(self.path)[0])
        lines = open(self.path).read().splitlines()

        # 1. modify an existing entry
        tampered = lines[:]
        tampered[2] = tampered[2].replace('"alice"', '"bob"')
        open(self.path, "w").write("\n".join(tampered) + "\n")
        ok, _, bad, msg = verify_chain(self.path)
        self.assertFalse(ok)
        self.assertEqual(bad, 3)

        # 2. delete an entry
        open(self.path, "w").write("\n".join(lines[:1] + lines[2:]) + "\n")
        self.assertFalse(verify_chain(self.path)[0])

        # 3. truncate the tail is detectable against an external anchor (last hash), check prefix still verifies
        open(self.path, "w").write("\n".join(lines[:3]) + "\n")
        ok, n, _, _ = verify_chain(self.path)
        self.assertTrue(ok)
        self.assertEqual(n, 3)  # admins compare the count/last hash with `agentlab-admin verify-log` output

    def test_concurrent_writers_keep_one_chain(self):
        other = AuditLogger(self.path)
        threads = [threading.Thread(target=lg.log, args=("e",), kwargs={"i": i})
                   for i in range(40) for lg in (self.log, other)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ok, n, _, _ = verify_chain(self.path)
        self.assertTrue(ok)
        self.assertEqual(n, 80)

    def test_api_secret_not_logged(self):
        fake = "sk-or-v1-" + "a" * 48
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": fake, "OPENAI_API_KEY": "sk-proj-" + "b" * 40}):
            self.log.log("api_error", user="alice", error=f"401 Incorrect API key provided: {fake}",
                         api_key="whatever", nested={"Authorization": "Bearer xyzxyzxyzxyzxyz"})
            text = open(self.path).read()
            self.assertNotIn(fake, text)
            self.assertNotIn("b" * 40, text)
            self.assertNotIn("xyzxyzxyzxyzxyz", text)
            self.assertIn(REDACTED, text)
            env = scrubbed_environment()
            self.assertNotIn("OPENROUTER_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(redact({"token": "abc", "count": 3}), {"token": REDACTED, "count": 3})


class DaemonAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness().start(serve_socket=False)

    def tearDown(self):
        self.h.cleanup()

    def test_user_events_are_stamped_with_peer_identity(self):
        self.h.call(ALICE, "audit_event", event="research_submitted",
                    fields={"file": "research_idea.txt", "user": "bob", "uid": 0})
        rec = read_events(self.h.config.audit_log, events={"research_submitted"})[-1]
        self.assertEqual(rec["user"], "alice")  # spoofed "user" field ignored
        self.assertEqual(rec["uid"], ALICE.uid)

    def test_users_cannot_forge_privileged_events(self):
        for ev in ("run_completed", "deployment", "workspace_created", "login", "server_reconciliation"):
            with self.assertRaises(RequestError):
                self.h.call(ALICE, "audit_event", event=ev, fields={})
        with self.assertRaises(RequestError):
            self.h.call(ALICE, "session_event", type="open_session", user="bob", pid=1)
        with self.assertRaises(RequestError):
            self.h.call(BOB, "record_deployment", commit="deadbeef")

    def test_history_scoped_to_caller(self):
        self.h.call(ALICE, "workspace_create")
        self.h.call(BOB, "workspace_create")
        events = self.h.call(ALICE, "history")["events"]
        self.assertTrue(events)
        self.assertTrue(all(e.get("user") == "alice" for e in events))
        with self.assertRaises(RequestError):
            self.h.call(ALICE, "history", user="bob")
        with self.assertRaises(RequestError):
            self.h.call(ALICE, "admin_logs")

    def test_admin_can_verify_log(self):
        from helpers import ADMIN
        self.h.call(ALICE, "workspace_create")
        self.assertTrue(self.h.call(ADMIN, "admin_verify_log")["ok"])


if __name__ == "__main__":
    unittest.main()
