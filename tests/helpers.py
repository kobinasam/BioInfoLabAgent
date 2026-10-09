"""
Shared test fixtures.

Most tests run without root: a real agentlabd (subprocess launcher, simulated
pipeline) is started on a temporary socket. Several lab members are simulated
by calling the daemon with distinct Identity objects; their workspaces live in
the temp tree. Tests that need genuinely different Unix accounts are in
test_permissions.py and only run as root on Linux with AGENTLAB_E2E_USERS set.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from server import run_registry as rr  # noqa: E402
from server.config import config_from_dict  # noqa: E402
from server.daemon import AgentLabDaemon, serve  # noqa: E402
from server.permissions import Authorizer, Identity, current_identity  # noqa: E402
from server.launcher import SubprocessLauncher  # noqa: E402
from server.resource_manager import LocalResourceManager  # noqa: E402
from server.workspace_manager import UserOpsRunner, WorkspaceManager  # noqa: E402

ME = current_identity()
ALICE = Identity(uid=50001, gid=50001, username="alice")
BOB = Identity(uid=50002, gid=50002, username="bob")
CAROL = Identity(uid=50003, gid=50003, username="carol")
ADMIN = Identity(uid=50010, gid=50010, username="labadmin")

GROUPS = {
    "alice": {"agentlab-users"},
    "bob": {"agentlab-users"},
    "carol": {"agentlab-users"},
    "labadmin": {"agentlab-users", "agentlab-admins"},
    "mallory": set(),  # not a lab member
    ME.username: {"agentlab-users"},
}

SIM_CONFIG = "simulated-steps-per-phase: {steps}\nsimulated-step-seconds: {delay}\n"


class SimulatedUsersRunner(UserOpsRunner):
    """All simulated users share the test process's real UID."""

    def expected_owner(self, ident):
        return os.getuid(), os.getgid()

    def run(self, ident, op, **args):
        from server import user_ops
        from server.workspace_manager import WorkspaceError
        try:
            return user_ops.dispatch({"op": op, "args": args})
        except Exception as e:
            raise WorkspaceError(f"{type(e).__name__}: {e}")


class SameUserLauncher(SubprocessLauncher):
    """Simulated users have no real Unix account: run their workers as the test process's user."""

    def launch(self, run_id, argv, env, uid, gid, cwd, properties=None):
        return super().launch(run_id, argv, env, os.getuid(), os.getgid(), cwd, properties)


class StaticProbe:
    def __init__(self, ram=64.0, cpu_idle=90.0, gpus=None):
        self.ram, self.cpu_idle, self.gpus = ram, cpu_idle, gpus or []

    def __call__(self):
        return {"cpu_count": 8, "cpu_idle_percent": self.cpu_idle, "ram_total_gb": 128.0,
                "ram_available_gb": self.ram, "gpus": self.gpus}


def make_config(tmp, **overrides):
    data = {
        "server": {
            "install_root": tmp, "application_root": REPO, "workspace_root": os.path.join(tmp, "users"),
            "system_root": os.path.join(tmp, "system"), "audit_log": os.path.join(tmp, "system", "logs", "audit.log"),
            "state_dir": os.path.join(tmp, "system", "state"), "socket_path": os.path.join(tmp, "d.sock"),
            "python": sys.executable, "users_group": "agentlab-users", "admins_group": "agentlab-admins",
        },
        "execution": {"launcher": "subprocess", "pipeline": "simulated", "poll_interval_seconds": 0.2,
                      "max_concurrent_runs": 8},
        "resources": {"min_free_ram_gb": 1, "min_free_cpu_percent": 0},
        "llm_proxy": {"tokens_file": os.path.join(tmp, "proxy", "tokens.json")},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return config_from_dict(data)


class DaemonHarness:
    def __init__(self, probe=None, groups=None, tmp=None, **config_overrides):
        # AF_UNIX paths are limited to ~104 bytes on macOS: keep the temp dir short.
        self.tmp = tmp or tempfile.mkdtemp(prefix="alt", dir="/tmp")
        self.config = make_config(self.tmp, **config_overrides)
        self.probe = probe or StaticProbe()
        self.groups = dict(GROUPS, **(groups or {}))
        self.daemon = None
        self.server = None

    def start(self, serve_socket=True):
        cfg = self.config
        self.daemon = AgentLabDaemon(
            cfg,
            launcher=SameUserLauncher(cfg),
            resources=LocalResourceManager(cfg, probe=self.probe),
            authorizer=Authorizer(cfg, group_lookup=lambda u: self.groups.get(u, set())),
            workspaces=WorkspaceManager(cfg, runner=SimulatedUsersRunner()),
        )
        self.daemon.startup()
        if serve_socket:
            self.server, self.monitor = serve(self.daemon)
            threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        return self

    def stop(self, kill_runs=True):
        if self.daemon:
            self.daemon.stop_event.set()
            if kill_runs and os.path.isdir(self.daemon.registry.runs_dir):
                for rec in self.daemon.registry.active():
                    if rec.get("process"):
                        self.daemon.launcher.kill(rec["process"], rec["run_id"])
            for proc in getattr(self.daemon.launcher, "_children", {}).values():
                if kill_runs and proc.poll() is None:
                    proc.kill()
                if proc.poll() is not None or kill_runs:
                    proc.wait()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # convenience --------------------------------------------------------
    def call(self, ident, op, **args):
        return self.daemon.handle(ident, {"op": op, "args": args})

    def start_run(self, ident, idea="A research idea", steps=3, delay=0.15, extra=""):
        self.call(ident, "workspace_create")
        return self.call(ident, "create_run", idea_text=idea,
                         config_text=SIM_CONFIG.format(steps=steps, delay=delay) + extra)

    def wait_status(self, ident, run_id, statuses, timeout=60):
        statuses = set(statuses)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server is None:
                self.daemon.monitor_once()
            rec = self.daemon.registry.get(run_id)
            if rec["status"] in statuses:
                return rec
            time.sleep(0.1)
        raise AssertionError(f"{run_id} did not reach {statuses}; is {self.daemon.registry.get(run_id)['status']}")

    def wait_worker_ready(self, ident, run_id, timeout=60):
        """Worker has installed its signal handlers (it writes pid into state.json right after)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.daemon.workspaces.read_run_state(ident, run_id) or {}
            if state.get("pid"):
                return state
            time.sleep(0.05)
        raise AssertionError(f"{run_id} worker never became ready")

    def wait_phase(self, ident, run_id, phase, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.daemon.workspaces.read_run_state(ident, run_id) or {}
            if phase in (state.get("completed_subtasks") or []):
                return state
            time.sleep(0.05)
        raise AssertionError(f"{run_id} never completed phase {phase}")

    def events(self, name=None):
        from server.audit_logger import read_events
        return read_events(self.config.audit_log, events={name} if name else None)


__all__ = ["rr", "DaemonHarness", "StaticProbe", "ALICE", "BOB", "CAROL", "ADMIN", "ME", "REPO", "Identity"]
