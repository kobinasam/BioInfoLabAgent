import os
import stat
import unittest

from helpers import ALICE, BOB, DaemonHarness, ME
from server.daemon import RequestError
from server.workspace_manager import (WorkspaceError, WorkspaceManager, format_research_idea, parse_research_idea,
                                      validate_idea)
from server import user_ops


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness().start(serve_socket=False)

    def tearDown(self):
        self.h.cleanup()

    def test_workspace_creation(self):
        info = self.h.call(ALICE, "whoami")
        self.assertFalse(info["workspace_exists"])
        res = self.h.call(ALICE, "workspace_create")
        self.assertTrue(res["created"])
        ws = res["workspace"]
        self.assertEqual(ws, os.path.join(self.h.config.workspace_root, "alice"))
        for sub in ("config", "ideas_archive"):
            path = os.path.join(ws, sub)
            self.assertTrue(os.path.isdir(path), sub)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, sub)
        self.assertEqual(stat.S_IMODE(os.stat(ws).st_mode), 0o700)
        self.assertTrue(os.path.exists(os.path.join(ws, "workspace.json")))
        self.assertEqual(len(self.h.events("workspace_created")), 1)
        # idempotent: second call does not re-create or re-log
        self.assertFalse(self.h.call(ALICE, "workspace_create")["created"])
        self.assertEqual(len(self.h.events("workspace_created")), 1)

    def test_run_requires_workspace(self):
        with self.assertRaises(RequestError):
            self.h.call(BOB, "create_run", idea_text="idea")

    def test_research_idea_submission(self):
        text = format_research_idea("alice", "Problem: X\n\n  indented line\nApproach: Y", host="lab-01",
                                    created="2026-10-08T14:36:01Z")
        self.assertTrue(text.startswith("Researcher: alice\nCreated: 2026-10-08T14:36:01Z\nHost: lab-01\n\n"
                                        "Research Idea\n=============\n\n"))
        self.assertEqual(parse_research_idea(text), "Problem: X\n\n  indented line\nApproach: Y")  # formatting kept
        for bad in ("", "   \n\n", None):
            with self.assertRaises(WorkspaceError):
                validate_idea(bad)
        with self.assertRaises(WorkspaceError):
            validate_idea("x" * 70000, max_bytes=65536)

        run = self.h.start_run(ALICE, idea="My multi\nline idea")
        run_dir = self.h.daemon.registry.get(run["run_id"])["run_dir"]
        with open(os.path.join(run_dir, "research_idea.txt")) as f:
            self.assertEqual(parse_research_idea(f.read()), "My multi\nline idea")
        # the idea never lands in the shared application tree
        self.assertFalse(os.path.exists(os.path.join(self.h.config.application_root, "research_idea.txt")))

    def test_run_dir_layout_and_metadata(self):
        run = self.h.start_run(ALICE)
        rec = self.h.daemon.registry.get(run["run_id"])
        # every job is a folder named by its job ID directly inside the user's root folder
        self.assertEqual(rec["run_dir"], os.path.join(self.h.config.workspace_root, "alice", run["run_id"]))
        for name in ("run_metadata.json", "research_idea.txt", "config.yaml", "stdout.log", "stderr.log"):
            self.assertTrue(os.path.exists(os.path.join(rec["run_dir"], name)), name)
        for sub in ("checkpoints", "outputs", "papers", "generated"):
            self.assertTrue(os.path.isdir(os.path.join(rec["run_dir"], sub)), sub)
        for key in ("run_id", "username", "uid", "hostname", "created_time", "research_idea_file", "git_commit",
                    "model_backend", "configuration", "status", "random_seed"):
            self.assertIn(key, rec)

    def test_user_ops_refuse_escape_and_overwrite(self):
        self.h.call(ALICE, "workspace_create")
        ws = os.path.join(self.h.config.workspace_root, "alice")
        with self.assertRaises(PermissionError):
            user_ops.op_create_run(ws, "../../bob", {})
        with self.assertRaises(PermissionError):  # only real job IDs can become folders
            user_ops.op_create_run(ws, "config", {})
        user_ops.op_create_run(ws, "20261008_000000_alice_aaaaaa", {})
        with self.assertRaises(FileExistsError):  # never overwrite an existing run
            user_ops.op_create_run(ws, "20261008_000000_alice_aaaaaa", {})
        with self.assertRaises(PermissionError):
            user_ops.op_write_run_file(ws, "20261008_000000_alice_aaaaaa", "../x", "data")

    def test_workspace_owned_by_other_account_is_rejected(self):
        wm = WorkspaceManager(self.h.config)  # real runner: expects owner == caller uid
        os.makedirs(os.path.join(self.h.config.workspace_root, "alice"), exist_ok=True)
        if ME.uid != ALICE.uid:
            with self.assertRaises(WorkspaceError):
                wm.create(ALICE)

    def test_invalid_usernames_rejected(self):
        wm = WorkspaceManager(self.h.config)
        for bad in ("../etc", "Alice", "a/b", "", "x" * 40):
            with self.assertRaises(WorkspaceError):
                wm.path(bad)


if __name__ == "__main__":
    unittest.main()
