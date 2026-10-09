"""
agentlab-llm-proxy -- holds the provider API keys so research processes never do.

    research run (user uid)            proxy (uid agentlab-proxy)           provider
    OPENAI_BASE_URL=http://127.0.0.1:8765/openai/v1
    OPENAI_API_KEY=alr_<per-run token> ---> verify token hash --------> Authorization: Bearer <real key>

* Real keys come from the systemd EnvironmentFile /etc/agentlab/secrets.env
  (root:root 0600, read by systemd before it drops privileges). Lab users
  cannot read that file or this process's environment (different UID).
* Tokens are random per launch; only their SHA-256 is published by agentlabd
  in tokens.json, and only while the run is active. Ending a run revokes it.
* The proxy logs who/which run/which model/status/latency to the journal --
  never prompts, completions, tokens or keys.
"""
import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "server"

from .config import load_config  # noqa: E402

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "authorization", "x-api-key", "content-length"}


class TokenStore:
    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._tokens = {}
        self._lock = threading.Lock()

    def lookup(self, token):
        if not token:
            return None
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime_ns
            except OSError:
                self._tokens, self._mtime = {}, None
                return None
            if mtime != self._mtime:
                try:
                    with open(self.path) as f:
                        self._tokens = json.load(f).get("tokens", {})
                except (OSError, ValueError):
                    self._tokens = {}
                self._mtime = mtime
            return self._tokens.get(hashlib.sha256(token.encode()).hexdigest())


class ProxyState:
    def __init__(self, config, env=None):
        env = os.environ if env is None else env
        proxy = config.section("llm_proxy")
        self.tokens = TokenStore(proxy["tokens_file"])
        self.providers = {}
        for name, p in proxy["providers"].items():
            self.providers[name] = dict(p, key=env.get(p["env_key"]))
        self.max_per_user = int(proxy.get("max_concurrent_requests_per_user", 8))
        self.inflight = {}
        self.lock = threading.Lock()

    def acquire(self, user):
        with self.lock:
            if self.inflight.get(user, 0) >= self.max_per_user:
                return False
            self.inflight[user] = self.inflight.get(user, 0) + 1
            return True

    def release(self, user):
        with self.lock:
            self.inflight[user] = max(0, self.inflight.get(user, 1) - 1)


def _log(**fields):
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(fields, sort_keys=True), flush=True)


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "agentlab-llm-proxy"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # silence default access log (it would include paths only, but keep journal clean)
        pass

    def _error(self, code, message):
        body = json.dumps({"error": {"message": message, "type": "agentlab_proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def _proxy(self):
        state = self.server.state
        parts = self.path.lstrip("/").split("/", 1)
        provider = state.providers.get(parts[0])
        if provider is None:
            return self._error(404, f"unknown provider '{parts[0]}'")
        grant = state.tokens.lookup(self._client_token())
        if grant is None:
            _log(event="proxy_denied", provider=parts[0], reason="invalid or revoked run token")
            return self._error(401, "invalid or expired AgentLaboratory run token")
        if not provider.get("key"):
            return self._error(503, f"provider '{parts[0]}' is not configured on this server")
        if not state.acquire(grant["user"]):
            return self._error(429, "too many concurrent LLM requests for this user")
        started = time.time()
        status = None
        model = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024 * 1024:
                return self._error(413, "request too large")
            body = self.rfile.read(length) if length else None
            if body:
                try:
                    model = json.loads(body).get("model")
                except (ValueError, AttributeError):
                    pass
            up = urllib.parse.urlsplit(provider["upstream"])
            rest = parts[1] if len(parts) > 1 else ""
            path = up.path.rstrip("/") + "/" + rest
            # Clients put /v1 in their base URL; avoid /v1/v1 when the upstream already ends in /v1.
            if rest.startswith("v1/") and up.path.rstrip("/").endswith("/v1"):
                path = up.path.rstrip("/") + "/" + rest[3:]
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            if provider.get("auth") == "x-api-key":
                headers["x-api-key"] = provider["key"]
            else:
                headers["Authorization"] = f"Bearer {provider['key']}"
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(up.netloc, timeout=float(provider.get("timeout", 900)))
            try:
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                conn.close()
        except (OSError, http.client.HTTPException) as e:
            status = status or 502
            if not self.wfile.closed:
                try:
                    self._error(502, f"upstream error: {type(e).__name__}")
                except OSError:
                    pass
        finally:
            state.release(grant["user"])
            _log(event="proxy_request", user=grant["user"], run_id=grant["run_id"], provider=parts[0],
                 model=model, status=status, ms=int((time.time() - started) * 1000))


def make_server(config, env=None, host=None, port=None):
    proxy = config.section("llm_proxy")
    host = host or proxy["listen_host"]
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the LLM proxy must only listen on loopback")
    server = ThreadingHTTPServer((host, int(proxy["listen_port"] if port is None else port)), ProxyHandler)
    server.daemon_threads = True
    server.state = ProxyState(config, env)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentLaboratory LLM key proxy")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    server = make_server(config)
    configured = [n for n, p in server.state.providers.items() if p.get("key")]
    _log(event="proxy_started", providers_configured=configured)  # names only, never values
    server.serve_forever()


if __name__ == "__main__":
    main()
