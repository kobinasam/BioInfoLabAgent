import os
import threading
import unittest

from helpers import ALICE, BOB, CAROL, DaemonHarness, StaticProbe, rr
from server.daemon import RequestError
from server.run_registry import generate_run_id, valid_run_id


class RunIdTests(unittest.TestCase):
    def test_run_id_generation(self):
        import datetime
        rid = generate_run_id("alice", now=datetime.datetime(2026, 10, 8, 14, 36, 5))
        self.assertRegex(rid, r"^20261008_143605_alice_[0-9a-f]{6}$")
        self.assertTrue(valid_run_id(rid))
        ids = {generate_run_id("alice") for _ in range(2000)}
        self.assertGreater(len(ids), 1990)  # random suffix; daemon also re-draws on the rare collision
        for bad in ("../x", "20261008_143605_alice_zzzzzz", "", None, "20261008_143605_Alice_abcdef"):
            self.assertFalse(valid_run_id(bad))


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness().start()

    def tearDown(self):
        self.h.cleanup()

    def test_concurrent_users(self):
        results, errors = {}, []

        def go(ident):
            try:
                results[ident.username] = self.h.start_run(ident, idea=f"{ident.username}'s idea", steps=2, delay=0.05)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=go, args=(u,)) for u in (ALICE, BOB, CAROL)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        run_ids = {u: r["run_id"] for u, r in results.items()}
        self.assertEqual(len(set(run_ids.values())), 3)
        for ident in (ALICE, BOB, CAROL):
            rec = self.h.wait_status(ident, run_ids[ident.username], {rr.COMPLETED})
            self.assertTrue(rec["run_dir"].startswith(os.path.join(self.h.config.workspace_root, ident.username) + os.sep))
            with open(os.path.join(rec["run_dir"], "papers", "report.txt")) as f:
                self.assertIn(f"{ident.username}'s idea", f.read())  # nobody got another user's idea/output
        status = self.h.call(ALICE, "status")
        self.assertEqual(status["total_active_runs"], 0)

    def test_multiple_jobs_per_user_in_own_folder(self):
        """Default: a user can run several jobs at once, all inside their own workspace; others run alongside."""
        runs = [self.h.start_run(ALICE, idea=f"alice idea {i}", steps=6, delay=0.1) for i in range(3)]
        bob = self.h.start_run(BOB, idea="bob idea", steps=6, delay=0.1)
        ids = [r["run_id"] for r in runs]
        self.assertEqual(len(set(ids)), 3)
        for rid in ids:
            self.h.wait_status(ALICE, rid, {rr.RUNNING, rr.COMPLETED})
        running_now = [r for r in self.h.daemon.registry.active() if r["user"] == "alice"]
        self.assertGreaterEqual(len(running_now), 2)  # genuinely concurrent, not serialised
        with self.assertRaises(RequestError) as ctx:  # 4th active job exceeds max_runs_per_user (3)
            self.h.start_run(ALICE)
        self.assertIn("limit is 3", str(ctx.exception))
        alice_ws = os.path.join(self.h.config.workspace_root, "alice") + os.sep
        for i, rid in enumerate(ids):
            rec = self.h.wait_status(ALICE, rid, {rr.COMPLETED})
            self.assertEqual(rec["run_dir"], alice_ws + rid)  # <root>/alice/<job-id>
            with open(os.path.join(rec["run_dir"], "papers", "report.txt")) as f:
                self.assertIn(f"alice idea {i}", f.read())  # each job kept its own idea and outputs
        self.h.wait_status(BOB, bob["run_id"], {rr.COMPLETED})
        self.assertTrue(self.h.daemon.registry.get(bob["run_id"])["run_dir"].startswith(
            os.path.join(self.h.config.workspace_root, "bob") + os.sep))

    def test_single_active_run_per_user(self):
        """Admins can restrict everyone to one active job (one_active_run_per_user: true)."""
        self.h.cleanup()
        self.h = DaemonHarness(execution={"one_active_run_per_user": True}).start()
        first = self.h.start_run(ALICE, steps=20, delay=0.2)
        with self.assertRaises(RequestError) as ctx:
            self.h.start_run(ALICE)
        self.assertIn(first["run_id"], str(ctx.exception))
        # a different user is unaffected
        other = self.h.start_run(BOB, steps=1, delay=0.01)
        self.assertNotEqual(other["status"], rr.FAILED)
        self.h.call(ALICE, "stop", run_id=first["run_id"])
        self.h.wait_status(ALICE, first["run_id"], {rr.STOPPED})
        self.h.start_run(ALICE, steps=1, delay=0.01)  # allowed again once the first is no longer active

    def test_admin_can_allow_multiple_runs_per_user(self):
        self.h.cleanup()
        self.h = DaemonHarness(execution={"one_active_run_per_user": False, "max_runs_per_user": 2}).start()
        self.h.start_run(ALICE, steps=20, delay=0.2)
        self.h.start_run(ALICE, steps=20, delay=0.2)
        with self.assertRaises(RequestError):
            self.h.start_run(ALICE)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.probe = StaticProbe(ram=0.5)  # below min_free_ram_gb=1
        self.h = DaemonHarness(probe=self.probe).start()

    def tearDown(self):
        self.h.cleanup()

    def test_resource_waiting(self):
        run = self.h.start_run(ALICE, steps=1, delay=0.01)
        self.assertEqual(run["status"], rr.QUEUED)
        self.assertIn("RAM", run["waiting_reason"])
        waiting = self.h.events("resource_waiting")
        self.assertEqual(waiting[-1]["run_id"], run["run_id"])
        self.probe.ram = 32.0  # resources free up
        self.h.wait_status(ALICE, run["run_id"], {rr.STARTING, rr.RUNNING, rr.COMPLETED})
        self.assertEqual(self.h.events("resource_granted")[-1]["run_id"], run["run_id"])

    def test_resource_release(self):
        self.probe.ram = 32.0
        run = self.h.start_run(ALICE, steps=1, delay=0.01)
        self.h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})
        self.assertEqual(self.h.events("resource_released")[-1]["run_id"], run["run_id"])
        self.assertNotIn(run["run_id"], self.h.daemon.resources.allocations)

    def test_global_concurrency_limit_queues(self):
        self.h.cleanup()
        self.h = DaemonHarness(execution={"max_concurrent_runs": 1}).start()
        a = self.h.start_run(ALICE, steps=10, delay=0.2)
        b = self.h.start_run(BOB, steps=1, delay=0.01)
        self.assertEqual(b["status"], rr.QUEUED)
        self.assertIn("run limit", b["waiting_reason"])
        self.h.call(ALICE, "stop", run_id=a["run_id"])
        self.h.wait_status(BOB, b["run_id"], {rr.COMPLETED})

    def test_gpu_assignment(self):
        from server.resource_manager import LocalResourceManager
        from helpers import make_config
        cfg = make_config("/tmp", resources={"gpus_per_run": 1, "min_free_ram_gb": 0})
        rm = LocalResourceManager(cfg, probe=StaticProbe(gpus=[{"index": 0, "memory_free_mb": 9000, "memory_total_mb": 10000},
                                                              {"index": 1, "memory_free_mb": 9000, "memory_total_mb": 10000}]))
        self.assertEqual(rm.try_acquire("a", 0)[1]["gpus"], [0])
        self.assertEqual(rm.try_acquire("b", 1)[1]["gpus"], [1])
        granted, reason = rm.try_acquire("c", 2)
        self.assertFalse(granted)
        self.assertIn("GPU", reason)
        rm.release("a")
        self.assertEqual(rm.try_acquire("c", 2)[1]["gpus"], [0])


if __name__ == "__main__":
    unittest.main()
