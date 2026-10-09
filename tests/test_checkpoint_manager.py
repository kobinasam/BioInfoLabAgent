import json
import os
import shutil
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path)
from server.checkpoint_manager import CheckpointManager
from server.fsutil import atomic_write_json


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ck = CheckpointManager(self.tmp, "20261008_000000_alice_abcdef", "alice", keep=3)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_state_json_fields_and_atomicity(self):
        self.ck.progress("literature review", 0)
        self.ck.progress("literature review", 1)
        self.ck.progress("literature review", 2)
        with open(os.path.join(self.tmp, "state.json")) as f:
            state = json.load(f)
        for key in ("run_id", "user", "status", "current_phase", "current_step", "last_completed_step",
                    "checkpoint_time", "resume_supported"):
            self.assertIn(key, state)
        self.assertEqual(state["current_phase"], "literature review")
        self.assertEqual(state["current_step"], 2)
        self.assertEqual(state["last_completed_step"], 1)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".tmp")], [])  # no temp leftovers

    def test_atomic_write_leaves_old_file_on_failure(self):
        path = os.path.join(self.tmp, "x.json")
        atomic_write_json(path, {"v": 1})
        # Simulate a crash between writing the temp file and the rename.
        from unittest import mock
        with mock.patch("server.fsutil.os.replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"v": 2})
        with open(path) as f:
            self.assertEqual(json.load(f), {"v": 1})
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".tmp")], [])

    def test_checkpoint_recovery(self):
        self.ck.save({"completed": ["a"]}, "a")
        self.ck.save({"completed": ["a", "b"]}, "b")
        obj, ptr = self.ck.load_latest()
        self.assertEqual(obj["completed"], ["a", "b"])
        # Corrupt the newest checkpoint: resume must fall back to the previous valid one.
        with open(os.path.join(self.tmp, "checkpoints", ptr["file"]), "wb") as f:
            f.write(b"\x80truncated")
        obj, ptr2 = self.ck.load_latest()
        self.assertEqual(obj["completed"], ["a"])
        self.assertTrue(ptr2.get("fallback"))

    def test_prunes_old_checkpoints(self):
        for i in range(6):
            self.ck.save({"i": i}, f"s{i}")
        files = [f for f in os.listdir(os.path.join(self.tmp, "checkpoints")) if f.endswith(".pkl")]
        self.assertEqual(len(files), 3)
        self.assertEqual(self.ck.load_latest()[0], {"i": 5})

    def test_no_checkpoint(self):
        self.assertEqual(self.ck.load_latest(), (None, None))
        self.assertFalse(self.ck.has_checkpoint())


if __name__ == "__main__":
    unittest.main()
