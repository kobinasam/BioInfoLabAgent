import contextlib
import hashlib
import io
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ALICE, DaemonHarness, make_config, rr
from server import llm_proxy

REAL_KEY = "sk-or-v1-" + "R" * 48


class FakeUpstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeUpstream.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.h = DaemonHarness(llm_proxy={"providers": {"openrouter": {
            "upstream": f"http://127.0.0.1:{self.up.server_port}/api/v1", "env_key": "OPENROUTER_API_KEY",
            "auth": "bearer"}}})
        self.h.start(serve_socket=False)
        self.proxy = llm_proxy.make_server(self.h.config, env={"OPENROUTER_API_KEY": REAL_KEY}, port=0)
        self.port = self.proxy.server_address[1]
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        FakeUpstream.seen.clear()

    def tearDown(self):
        self.proxy.shutdown()
        self.up.shutdown()
        self.h.cleanup()

    def _post(self, token):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/openrouter/v1/chat/completions",
            data=json.dumps({"model": "m", "messages": []}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    def _token_of(self, run_id):
        rec = self.h.daemon.registry.get(run_id)
        return rec["token_sha256"]

    def test_proxy_injects_server_key_and_requires_run_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("alr_not-a-real-token")
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(FakeUpstream.seen, [])

        # Register a token the way agentlabd does for an active run.
        token = "alr_" + "t" * 43
        tokens = {hashlib.sha256(token.encode()).hexdigest(): {"run_id": "r", "user": "alice", "uid": 1}}
        os.makedirs(os.path.dirname(self.h.config.get("llm_proxy", "tokens_file")), exist_ok=True)
        with open(self.h.config.get("llm_proxy", "tokens_file"), "w") as f:
            json.dump({"tokens": tokens}, f)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            resp = self._post(token)
            body = resp.read()
            # the proxy writes its access-log line after the response is sent
            for _ in range(50):
                if "proxy_request" in log.getvalue():
                    break
                time.sleep(0.05)
        resp.read = lambda: body
        self.assertEqual(json.loads(resp.read())["choices"][0]["message"]["content"], "pong")
        seen = FakeUpstream.seen[-1]
        self.assertEqual(seen["path"], "/api/v1/chat/completions")
        self.assertEqual(seen["auth"], f"Bearer {REAL_KEY}")          # real key added server-side
        self.assertNotIn(token, json.dumps({k: str(v) for k, v in seen.items()}))  # client token not forwarded
        self.assertNotIn(REAL_KEY, log.getvalue())                    # proxy log never contains keys
        self.assertNotIn(token, log.getvalue())
        self.assertIn('"user": "alice"', log.getvalue())

    def test_token_lifecycle_tied_to_run(self):
        run = self.h.start_run(ALICE, steps=1, delay=0.01)
        rec = self.h.daemon.registry.get(run["run_id"])
        with open(self.h.config.get("llm_proxy", "tokens_file")) as f:
            published = json.load(f)["tokens"]
        self.assertIn(rec["token_sha256"], published)                 # active run -> token valid
        self.assertNotIn("proxy_token", json.dumps(published))
        self.h.wait_status(ALICE, run["run_id"], {rr.COMPLETED})
        with open(self.h.config.get("llm_proxy", "tokens_file")) as f:
            self.assertNotIn(rec["token_sha256"], json.load(f)["tokens"])  # finished -> revoked
        # the worker consumed and deleted its token file
        self.assertFalse(os.path.exists(os.path.join(rec["run_dir"], "proxy_token")))

    def test_worker_environment_has_no_real_keys(self):
        captured = {}
        launcher = self.h.daemon.launcher
        original = launcher.launch

        def spy(run_id, argv, env, uid, gid, cwd, properties=None):
            captured.update(env=env, argv=argv)
            return original(run_id, argv, env, uid, gid, cwd, properties)
        launcher.launch = spy
        os.environ["OPENROUTER_API_KEY"] = REAL_KEY  # even if the daemon had a key in its env ...
        try:
            self.h.start_run(ALICE, steps=1, delay=0.01)
        finally:
            os.environ.pop("OPENROUTER_API_KEY")
        blob = json.dumps(captured)
        self.assertNotIn(REAL_KEY, blob)                               # ... it is never passed to a run
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
            self.assertNotIn(var, captured["env"])
        self.assertTrue(captured["env"]["OPENROUTER_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertNotIn("alr_", " ".join(captured["argv"]))           # token not in argv (visible in ps)

    def test_proxy_refuses_non_loopback(self):
        cfg = make_config("/tmp", llm_proxy={"listen_host": "0.0.0.0"})
        with self.assertRaises(ValueError):
            llm_proxy.make_server(cfg, env={})


if __name__ == "__main__":
    unittest.main()
