import ast
import json
import os
import pickle
import subprocess
import sys
import tempfile
import unittest

from helpers import ADMIN, ALICE, DaemonHarness, REPO, rr


class GitRecordingTests(unittest.TestCase):
    def test_git_commit_recording(self):
        h = DaemonHarness().start(serve_socket=False)
        try:
            release_file = os.path.join(REPO, "RELEASE.json")
            if os.path.exists(release_file):  # deployed release tree (no .git): commit comes from RELEASE.json
                with open(release_file) as f:
                    head = json.load(f)["commit"]
            else:
                head = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            if not head:
                self.skipTest("not a git checkout and no RELEASE.json")
            run = h.start_run(ALICE, steps=1, delay=0.01)
            rec = h.daemon.registry.get(run["run_id"])
            self.assertEqual(rec["git_commit"], head)
            self.assertIn("git_dirty", rec)
            h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})
            with open(os.path.join(rec["run_dir"], "run_metadata.json")) as f:
                meta = json.load(f)
            self.assertEqual(meta["git_commit"], head)
            self.assertEqual(meta["status"], rr.COMPLETED)
            self.assertIsNotNone(meta["end_time"])
            self.assertEqual(h.events("run_created")[-1]["git_commit"], head)
        finally:
            h.cleanup()

    def test_release_file_takes_precedence_and_deployments_recorded(self):
        tmp = tempfile.mkdtemp(prefix="alr", dir="/tmp")
        release = os.path.join(tmp, "release")
        os.makedirs(os.path.join(release, "experiment_configs"))
        with open(os.path.join(release, "RELEASE.json"), "w") as f:
            json.dump({"commit": "abc123" * 6 + "abcd", "deployed": "2026-10-08T00:00:00Z"}, f)
        h = DaemonHarness(server={"application_root": release})
        h.start(serve_socket=False)
        try:
            self.assertEqual(h.daemon._release_info()["commit"], "abc123" * 6 + "abcd")
            from server.permissions import Identity
            root = Identity(0, 0, "root")
            h.call(root, "record_deployment", commit="new", previous="old", deployer="labadmin", branch="main", tests="passed")
            deps = h.call(ADMIN, "admin_deployments")["deployments"]
            self.assertEqual((deps[-1]["commit"], deps[-1]["previous"], deps[-1]["deployer"]), ("new", "old", "labadmin"))
            self.assertEqual(h.events("deployment")[-1]["commit"], "new")
        finally:
            h.cleanup()


class PipelineIntegrationTests(unittest.TestCase):
    """Static checks on the (minimally) modified research code -- no LLM calls."""

    def _src(self, name):
        with open(os.path.join(REPO, name)) as f:
            return f.read()

    def test_report_writing_uses_instance_state_not_globals(self):
        tree = ast.parse(self._src("ai_lab_repo.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "report_writing")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn("research_topic", names)  # was a NameError when imported as a module
        self.assertNotIn("compile_pdf", names)

    def test_hooks_exist_and_default_off(self):
        self.assertIn("CHECKPOINT_HOOK = None", self._src("ai_lab_repo.py"))
        self.assertIn("PROGRESS_HOOK = None", self._src("agents.py"))

    def test_agent_models_cover_every_phase(self):
        src = self._src("ai_lab_repo.py")
        tree = ast.parse(src)
        ns = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PHASE_NAMES") or \
               (isinstance(node, ast.FunctionDef) and node.name in ("build_agent_models", "build_task_notes", "build_human_in_loop")):
                exec(compile(ast.Module([node], []), "ai_lab_repo.py", "exec"), ns)
        models = ns["build_agent_models"]("openrouter:x")
        for phase in ns["PHASE_NAMES"]:
            self.assertEqual(models[phase], "openrouter:x")
        self.assertEqual(models["paper refinement"], "openrouter:x")  # backward compatible key kept
        notes = ns["build_task_notes"]({"plan-formulation": ["a"]}, "French")
        self.assertEqual(notes[0], {"phases": ["plan formulation"], "note": "a"})
        self.assertIn("French", notes[-1]["note"])

    def test_inference_base_urls_overridable(self):
        src = self._src("inference.py")
        self.assertIn('os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")', src)
        self.assertIn('os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")', src)

    @unittest.skipUnless(os.environ.get("AGENTLAB_HEAVY_TESTS"), "imports torch/tensorflow; set AGENTLAB_HEAVY_TESTS=1")
    def test_real_workflow_checkpoint_roundtrip(self):
        code = r'''
import os, sys, pickle, tempfile
sys.path.insert(0, sys.argv[1])
os.chdir(tempfile.mkdtemp())
import ai_lab_repo
lab = ai_lab_repo.LaboratoryWorkflow(research_topic="t", openai_api_key="x",
        agent_model_backbone=ai_lab_repo.build_agent_models("openrouter:m"),
        human_in_loop_flag=ai_lab_repo.build_human_in_loop(False), lab_dir=".")
lab.phase_status["literature review"] = True
saved = []
ai_lab_repo.CHECKPOINT_HOOK = lambda wf, phase: saved.append(pickle.dumps(wf))
lab.save_state("literature review")
restored = pickle.loads(saved[0])
assert restored.phase_status["literature review"] is True
assert restored.research_topic == "t"
ai_lab_repo.CHECKPOINT_HOOK = None
os.makedirs("state_saves")
lab.save_state("literature review")
assert os.path.exists("state_saves/Paper0.pkl") and not os.path.exists("state_saves/Paper0.pkl.tmp")
print("OK")
'''
        out = subprocess.run([sys.executable, "-c", code, REPO], capture_output=True, text=True, timeout=600)
        self.assertIn("OK", out.stdout, out.stderr[-3000:])


@unittest.skipUnless(os.environ.get("AGENTLAB_HEAVY_TESTS"), "imports torch/tensorflow; set AGENTLAB_HEAVY_TESTS=1")
class RealPipelineThroughProxyTest(unittest.TestCase):
    """Worker runs the *real* LaboratoryWorkflow; LLM calls go worker -> proxy -> fake provider."""

    def test_real_pipeline_uses_proxy_and_stops_gracefully(self):
        import hashlib
        import signal
        import threading
        import time
        from http.server import ThreadingHTTPServer
        from helpers import make_config
        from server import llm_proxy
        from test_llm_proxy import FakeUpstream, REAL_KEY

        up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="alp", dir="/tmp")
        cfg = make_config(tmp, llm_proxy={"providers": {"openrouter": {
            "upstream": f"http://127.0.0.1:{up.server_port}/api/v1", "env_key": "OPENROUTER_API_KEY", "auth": "bearer"}}})
        proxy = llm_proxy.make_server(cfg, env={"OPENROUTER_API_KEY": REAL_KEY}, port=0)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        token = "alr_" + "p" * 43
        os.makedirs(os.path.dirname(cfg.get("llm_proxy", "tokens_file")))
        with open(cfg.get("llm_proxy", "tokens_file"), "w") as f:
            json.dump({"tokens": {hashlib.sha256(token.encode()).hexdigest(): {"run_id": "r", "user": "alice", "uid": 1}}}, f)
        run_dir = os.path.join(tmp, "run")
        for sub in ("checkpoints", "outputs", "papers", "generated"):
            os.makedirs(os.path.join(run_dir, sub))
        with open(os.path.join(run_dir, "config.yaml"), "w") as f:
            f.write('llm-backend: "openrouter:test/model"\ncompile-latex: False\nnum-papers-lit-review: 1\n')
        with open(os.path.join(run_dir, "research_idea.txt"), "w") as f:
            f.write("Researcher: alice\nCreated: x\nHost: y\n\nResearch Idea\n=============\n\nStudy prompt ensembles.\n")
        with open(os.path.join(run_dir, "proxy_token"), "w") as f:
            f.write(token)
        base = f"http://127.0.0.1:{proxy.server_address[1]}"
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AGENTLAB_PIPELINE": "agentlab", "AGENTLAB_APP_ROOT": REPO,
               "OPENROUTER_BASE_URL": f"{base}/openrouter/v1", "OPENAI_BASE_URL": f"{base}/openai/v1",
               "TOKENIZERS_PARALLELISM": "false", "MPLCONFIGDIR": tmp}
        FakeUpstream.seen.clear()
        p = subprocess.Popen([sys.executable, "-I", os.path.join(REPO, "server", "research_runner.py"),
                              "--run-dir", run_dir, "--run-id", "20261008_000000_alice_abcdef"], env=env)
        try:
            deadline = time.time() + 300
            while not FakeUpstream.seen and time.time() < deadline and p.poll() is None:
                time.sleep(0.5)
            self.assertTrue(FakeUpstream.seen, open(os.path.join(run_dir, "stderr.log")).read()[-3000:])
            req = FakeUpstream.seen[0]
            self.assertEqual(req["auth"], f"Bearer {REAL_KEY}")
            self.assertEqual(json.loads(req["body"])["model"], "test/model")
            self.assertIn("Study prompt ensembles.", req["body"].decode())  # topic came from research_idea.txt
            self.assertFalse(os.path.exists(os.path.join(run_dir, "proxy_token")))
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(60), 75)
            with open(os.path.join(run_dir, "state.json")) as f:
                state = json.load(f)
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["current_phase"], "literature review")
            self.assertNotIn(REAL_KEY, open(os.path.join(run_dir, "stdout.log")).read())
        finally:
            if p.poll() is None:
                p.kill()
            proxy.shutdown()
            up.shutdown()


if __name__ == "__main__":
    unittest.main()
