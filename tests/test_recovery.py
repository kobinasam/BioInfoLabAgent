import os
import signal
import subprocess
import sys
import time
import unittest

from helpers import ALICE, BOB, DaemonHarness, REPO, rr


def _progress_events(stdout_text):
    return stdout_text.count("checkpoint saved after")


class InterruptionTests(unittest.TestCase):
    def setUp(self):
        self.h = DaemonHarness().start()

    def tearDown(self):
        self.h.cleanup()

    def _pid(self, run_id):
        return self.h.daemon.registry.get(run_id)["process"]["pid"]

    def test_resume_after_interruption(self):
        """Process killed abruptly (SIGKILL, OOM, power loss) -> INTERRUPTED -> resume from checkpoint."""
        run = self.h.start_run(ALICE, steps=4, delay=0.15)
        run_id = run["run_id"]
        self.h.wait_status(ALICE, run_id, {rr.RUNNING})
        self.h.wait_phase(ALICE, run_id, "plan formulation")
        os.kill(self._pid(run_id), signal.SIGKILL)
        rec = self.h.wait_status(ALICE, run_id, {rr.INTERRUPTED})
        self.assertEqual(rec["exit_code"], 137)
        self.assertEqual(self.h.events("run_interrupted")[-1]["run_id"], run_id)

        attention = self.h.call(ALICE, "attention")
        self.assertEqual(attention["interrupted"][0]["run_id"], run_id)
        self.assertIn(attention["interrupted"][0]["last_completed_phase"],
                      ("plan formulation", "data preparation"))

        self.h.call(ALICE, "resume", run_id=run_id)
        rec = self.h.wait_status(ALICE, run_id, {rr.COMPLETED})
        self.assertEqual(rec["resume_count"], 1)
        self.assertEqual(len(rec["attempts"]), 2)
        out = open(os.path.join(rec["run_dir"], "stdout.log")).read()
        resumed_part = out.split("Resuming run")[1]
        self.assertNotIn("after 'literature review'", resumed_part)  # completed work was not redone
        self.assertNotIn("after 'plan formulation'", resumed_part)
        self.assertIn("after 'report refinement'", resumed_part)
        self.assertEqual(self.h.events("run_resumed")[-1]["run_id"], run_id)

    def test_crash_then_resume(self):
        """Worker crashes with an error mid-phase -> FAILED (resumable) -> resume completes."""
        run = self.h.start_run(ALICE, steps=2, delay=0.05, extra="simulated-fail-at: 'data preparation:1'\n")
        rec = self.h.wait_status(ALICE, run["run_id"], {rr.FAILED})
        self.assertEqual(rec["exit_code"], 1)
        self.h.call(ALICE, "resume", run_id=run["run_id"])
        rec = self.h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})
        out = open(os.path.join(rec["run_dir"], "stdout.log")).read().split("Resuming run")[1]
        self.assertNotIn("after 'plan formulation'", out)

    def test_graceful_stop_saves_state(self):
        run = self.h.start_run(ALICE, steps=30, delay=0.1)
        self.h.wait_worker_ready(ALICE, run["run_id"])
        self.h.call(ALICE, "stop", run_id=run["run_id"])
        rec = self.h.wait_status(ALICE, run["run_id"], {rr.STOPPED})
        self.assertEqual(rec["exit_code"], 75)
        state = self.h.daemon.workspaces.read_run_state(ALICE, run["run_id"])
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["stop_signal"], "SIGTERM")
        self.assertEqual(self.h.events("run_terminated")[-1]["run_id"], run["run_id"])
        # still resumable
        self.h.call(ALICE, "resume", run_id=run["run_id"])
        self.h.wait_status(ALICE, run["run_id"], {rr.STARTING, rr.RUNNING})

    def test_sighup_does_not_kill_run(self):
        """An SSH hang-up must not end the research."""
        run = self.h.start_run(ALICE, steps=4, delay=0.1)
        self.h.wait_worker_ready(ALICE, run["run_id"])
        os.kill(self._pid(run["run_id"]), signal.SIGHUP)
        rec = self.h.wait_status(ALICE, run["run_id"], {rr.COMPLETED, rr.INTERRUPTED, rr.FAILED})
        self.assertEqual(rec["status"], rr.COMPLETED)
        self.assertIn("SIGHUP received", open(os.path.join(rec["run_dir"], "stdout.log")).read())

    def test_run_survives_client_disconnect(self):
        """The CLI/SSH session is not the parent: killing the client process leaves the run alive."""
        self.h.call(ALICE, "workspace_create")
        client = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        run = self.h.start_run(ALICE, steps=3, delay=0.1)
        client.kill()
        client.wait()
        self.assertEqual(self.h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})["status"], rr.COMPLETED)
        # the run was started in its own session, not the caller's
        self.assertTrue(self.h.daemon.registry.get(run["run_id"])["process"]["pid"] != os.getsid(0))


class RestartReconciliationTests(unittest.TestCase):
    def test_server_restart_reconciliation(self):
        h = DaemonHarness().start()
        try:
            alive = h.start_run(ALICE, steps=30, delay=0.1)
            dead = h.start_run(BOB, steps=30, delay=0.1)
            h.wait_status(ALICE, alive["run_id"], {rr.RUNNING})
            h.wait_status(BOB, dead["run_id"], {rr.RUNNING})
            alive_pid = h.daemon.registry.get(alive["run_id"])["process"]["pid"]
            dead_pid = h.daemon.registry.get(dead["run_id"])["process"]["pid"]
            # Daemon goes away (crash / deploy restart) without touching runs ...
            h.stop(kill_runs=False)
            # ... and meanwhile one run's process dies.
            os.kill(dead_pid, signal.SIGKILL)
            time.sleep(0.3)

            h2 = DaemonHarness(tmp=h.tmp).start()  # same state directory, fresh daemon/launcher
            summary = h2.events("server_reconciliation")[-1]
            self.assertIn(alive["run_id"], summary["still_running"])   # re-adopted, not duplicated
            self.assertIn(dead["run_id"], summary["interrupted"])
            self.assertEqual(h2.daemon.registry.get(dead["run_id"])["status"], rr.INTERRUPTED)
            self.assertEqual(h2.daemon.registry.get(alive["run_id"])["status"], rr.RUNNING)
            # checkpoints preserved -> resumable
            h2.call(BOB, "resume", run_id=dead["run_id"])
            h2.wait_status(BOB, dead["run_id"], {rr.STARTING, rr.RUNNING})
            # the adopted run is still monitored: when it dies it is reconciled
            os.kill(alive_pid, signal.SIGKILL)
            h2.wait_status(ALICE, alive["run_id"], {rr.INTERRUPTED})
            h2.cleanup()
        finally:
            h.cleanup()

    def test_reboot_and_pid_reuse_detection(self):
        """After a reboot (new boot_id) or PID reuse, a recorded PID is never trusted."""
        h = DaemonHarness().start(serve_socket=False)
        try:
            run = h.start_run(ALICE, steps=1, delay=0.01)
            h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})
            # Forge a RUNNING record whose PID now belongs to an unrelated live process (this test process).
            rec = h.daemon.registry.get(run["run_id"])
            fake = dict(rec, status=rr.RUNNING, process={"launcher": "subprocess", "pid": os.getpid(),
                                                         "create_time": 1.0, "boot_id": rec["process"]["boot_id"]})
            fake["run_id"] = run["run_id"][:-6] + "ffffff"
            fake.pop("history", None)
            h.daemon.registry.create(fake)
            other = dict(fake, run_id=run["run_id"][:-6] + "eeeeee",
                         process={"launcher": "subprocess", "pid": os.getpid(), "create_time": None,
                                  "boot_id": "a-previous-boot"})
            h.daemon.registry.create(other)
            os.makedirs(os.path.join(os.path.dirname(rec["run_dir"]), fake["run_id"]), exist_ok=True)
            os.makedirs(os.path.join(os.path.dirname(rec["run_dir"]), other["run_id"]), exist_ok=True)
            h2 = DaemonHarness(tmp=h.tmp).start(serve_socket=False)
            self.assertEqual(h2.daemon.registry.get(fake["run_id"])["status"], rr.INTERRUPTED)
            self.assertEqual(h2.daemon.registry.get(other["run_id"])["status"], rr.INTERRUPTED)
            h2.stop()
        finally:
            h.cleanup()


class WorkerSignalTests(unittest.TestCase):
    def test_worker_exit_codes_and_atomic_state(self):
        import tempfile, json, shutil
        tmp = tempfile.mkdtemp()
        try:
            run_id = "20261008_000000_alice_abcdef"
            for sub in ("checkpoints", "outputs", "papers", "generated"):
                os.makedirs(os.path.join(tmp, sub))
            open(os.path.join(tmp, "config.yaml"), "w").write("simulated-steps-per-phase: 50\nsimulated-step-seconds: 0.05\n")
            open(os.path.join(tmp, "research_idea.txt"), "w").write("idea")
            env = dict(os.environ, AGENTLAB_PIPELINE="simulated", AGENTLAB_APP_ROOT=REPO)
            p = subprocess.Popen([sys.executable, "-I", os.path.join(REPO, "server", "research_runner.py"),
                                  "--run-dir", tmp, "--run-id", run_id], env=env)
            time.sleep(1.5)
            p.send_signal(signal.SIGINT)
            self.assertEqual(p.wait(20), 75)
            with open(os.path.join(tmp, "state.json")) as f:
                state = json.load(f)  # parseable -> never half-written
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["stop_signal"], "SIGINT")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
