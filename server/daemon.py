"""
agentlabd -- the AgentLaboratory supervisor daemon (runs as root under systemd).

Responsibilities
  * single writer of the protected audit log and the authoritative run registry
  * authenticates every request by kernel peer credentials (SO_PEERCRED)
  * creates workspaces, creates/queues/launches/stops/resumes runs
  * admission control (resources, per-user and global run limits)
  * monitors run processes and reconciles state (crash, reboot, PID reuse)
  * tracks SSH sessions reported by PAM
  * issues per-run LLM proxy tokens (real API keys never reach user processes)

Protocol: one JSON object per line over a Unix stream socket; one request per
connection. Identity fields inside requests are ignored.
"""
import argparse
import datetime
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "server"

from . import run_registry as rr  # noqa: E402
from .audit_logger import AuditLogger, USER_SUBMITTABLE_EVENTS, read_events, verify_chain  # noqa: E402
from .config import load_config  # noqa: E402
from .fsutil import atomic_write_json, read_json  # noqa: E402
from .launcher import make_launcher, current_boot_id  # noqa: E402
from .permissions import Authorizer, Identity, identity_from_uid, peer_identity, valid_username  # noqa: E402
from .recovery_manager import determine_final_status, reconcile_on_startup  # noqa: E402
from .resource_manager import LocalResourceManager  # noqa: E402
from .secret_guard import config_contains_secrets, redact_text  # noqa: E402
from .session_manager import SessionManager  # noqa: E402
from .workspace_manager import WorkspaceError, WorkspaceManager, format_research_idea, validate_idea  # noqa: E402

TOKEN_PREFIX = "alr_"


class RequestError(Exception):
    """An error whose message is safe to return to the client."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group_gid(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


class AgentLabDaemon:
    def __init__(self, config, launcher=None, resources=None, authorizer=None, workspaces=None):
        self.config = config
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.is_root = os.geteuid() == 0
        self.admins_gid = _group_gid(config.admins_group)
        self.users_gid = _group_gid(config.users_group)

        os.makedirs(config.state_dir, mode=0o750, exist_ok=True)
        self.audit = AuditLogger(config.audit_log, group_gid=self.admins_gid)
        self._secure_system_dirs()
        self.audit.ensure()

        self.registry = rr.RunRegistry(config.state_dir)
        self.sessions = SessionManager(os.path.join(config.state_dir, "sessions.json"), self.audit)
        self.launcher = launcher or make_launcher(config)
        self.resources = resources or LocalResourceManager(config)
        self.auth = authorizer or Authorizer(config)
        self.workspaces = workspaces or WorkspaceManager(config)
        self.deployments_path = os.path.join(config.state_dir, "deployments.json")

    # ------------------------------------------------------------------ setup
    def _secure_system_dirs(self):
        if not self.is_root:
            return
        gid = self.admins_gid if self.admins_gid is not None else 0
        for d in (self.config.system_root, os.path.dirname(self.config.audit_log), self.config.state_dir):
            os.makedirs(d, exist_ok=True)
            os.chown(d, 0, gid)
            os.chmod(d, 0o750)
        # system_root itself must be traversable for nothing but admins
        os.chmod(self.config.system_root, 0o750)

    def startup(self):
        self.audit.log("daemon_started", pid=os.getpid(), launcher=self.launcher.name,
                       boot_id=current_boot_id(), version=self._release_info()["commit"])
        with self.lock:
            summary = reconcile_on_startup(self)
            if self.config.get("execution", "auto_resume_after_restart"):
                for run_id in summary["interrupted"]:
                    rec = self.registry.get(run_id)
                    self._queue_resume(rec, actor=Identity(0, 0, "root"), reason="auto-resume after restart")
            self._sync_proxy_tokens()
        return summary

    # -------------------------------------------------------------- utilities
    def _release_info(self):
        """Exact code version a new run will execute (realpath of the app root)."""
        app = os.path.realpath(self.config.application_root)
        info = {"path": app, "commit": "unknown", "dirty": None}
        release = read_json(os.path.join(app, "RELEASE.json"))
        if release and release.get("commit"):
            info.update(commit=release["commit"], dirty=False, deployed=release.get("deployed"))
            return info
        try:
            commit = subprocess.run(["git", "-c", f"safe.directory={app}", "-C", app, "rev-parse", "HEAD"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
            dirty = subprocess.run(["git", "-c", f"safe.directory={app}", "-C", app, "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
            if commit:
                info.update(commit=commit, dirty=bool(dirty))
        except (OSError, subprocess.SubprocessError):
            pass
        return info

    def _user_active_runs(self, uid):
        return [r for r in self.registry.for_user(uid) if r["status"] in rr.ACTIVE_STATES]

    def _per_user_limit(self):
        if self.config.get("execution", "one_active_run_per_user"):
            return 1
        return max(1, int(self.config.get("execution", "max_runs_per_user")))

    def _running_count(self):
        return sum(1 for r in self.registry.active() if r["status"] in (rr.STARTING, rr.RUNNING, rr.STOPPING))

    def _require_admin(self, ident):
        if not self.auth.is_admin(ident):
            self.audit.log("authorization_denied", user=ident.username, uid=ident.uid, action="admin")
            raise RequestError(f"permission denied: requires membership of '{self.config.admins_group}'")

    def _get_run_for(self, ident, run_id, action):
        if not rr.valid_run_id(run_id or ""):
            raise RequestError("invalid run id")
        rec = self.registry.get(run_id)
        if rec is None or not self.auth.can_manage_run(ident, rec):
            if rec is not None:
                self.audit.log("authorization_denied", user=ident.username, uid=ident.uid, action=action,
                               run_id=run_id, owner=rec.get("user"))
            # Same message whether the run exists or not: no probing of other users' run IDs.
            raise RequestError(f"no such run for your account: {run_id}")
        return rec

    def _owner_ident(self, rec):
        return Identity(uid=rec["uid"], gid=rec["gid"], username=rec["user"])

    # ------------------------------------------------- experiment config check
    def _load_experiment_config(self, config_text):
        """Server default experiment config, overlaid with the user's (validated) config."""
        rel = self.config.get("research", "default_experiment_config")
        path = rel if os.path.isabs(rel) else os.path.join(self.config.application_root, rel)
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if config_text is not None:
            if len(config_text.encode()) > 262144:
                raise RequestError("experiment config too large")
            try:
                user_data = yaml.safe_load(config_text) or {}
            except yaml.YAMLError as e:
                raise RequestError(f"experiment config is not valid YAML: {e}")
            if not isinstance(user_data, dict):
                raise RequestError("experiment config must be a YAML mapping")
            data.update(user_data)
        if config_contains_secrets(data):
            raise RequestError("experiment config contains an API key. Remove it: keys are configured "
                               "server-side by administrators and must never be in research files.")
        for key in ("parallel-labs", "agentRxiv", "construct-agentRxiv"):
            if str(data.get(key, False)).lower() == "true":
                raise RequestError(f"'{key}' is not supported in server mode")
        data["copilot-mode"] = False  # detached runs have no terminal
        data["num-papers-to-write"] = 1  # one run = one paper; start another run for more
        allowed = self.config.get("research", "allowed_llm_backends") or []
        for key in ("llm-backend", "lit-review-backend"):
            if allowed and data.get(key) and data[key] not in allowed:
                raise RequestError(f"{key} '{data[key]}' is not allowed on this server; allowed: {', '.join(allowed)}")
        data.pop("research-topic", None)  # the topic always comes from research_idea.txt
        return data

    # ------------------------------------------------------------------- runs
    def create_run(self, ident, idea_text, config_text=None, client_host=None, name=None):
        ws = self.workspaces.path(ident.username)
        if not os.path.isdir(ws):
            raise RequestError("no workspace: run `agentlab workspace --create` first")
        try:
            validate_idea(idea_text, self.config.get("research", "max_idea_bytes"))
        except WorkspaceError as e:
            raise RequestError(str(e))
        exp_config = self._load_experiment_config(config_text)
        with self.lock:
            limit = self._per_user_limit()
            active = self._user_active_runs(ident.uid)
            if len(active) >= limit:
                listing = ", ".join(f"{r['run_id']} ({r['status']})" for r in active)
                raise RequestError(f"you already have {len(active)} active job(s), the limit is {limit}: {listing}. "
                                   f"Wait for one to finish or stop one with `agentlab stop <run-id>`.")
            if name is not None and not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$", str(name)):
                raise RequestError("job name: use letters, digits, spaces, '.', '-' or '_' (max 64 characters)")
            run_id = rr.generate_run_id(ident.username)
            while self.registry.get(run_id) is not None:
                run_id = rr.generate_run_id(ident.username)
            release = self._release_info()
            seed = secrets.randbelow(2**31)
            created = _now()
            run_dir = self.workspaces.run_dir(ident.username, run_id)
            metadata = {
                "run_id": run_id, "username": ident.username, "uid": ident.uid, "hostname": socket.gethostname(),
                "pid": None, "start_time": None, "created_time": created, "end_time": None,
                "research_idea_file": os.path.join(run_dir, self.config.idea_filename),
                "git_commit": release["commit"], "git_dirty": release["dirty"], "release_path": release["path"],
                "model_backend": {"llm-backend": exp_config.get("llm-backend"),
                                  "lit-review-backend": exp_config.get("lit-review-backend")},
                "configuration": exp_config, "random_seed": seed, "status": rr.QUEUED,
                "python": platform.python_version(), "platform": platform.platform(),
                "pipeline": self.config.get("execution", "pipeline"),
                "name": name,
            }
            idea_file = format_research_idea(ident.username, idea_text, host=socket.gethostname(), created=created)
            try:
                self.workspaces.create_run_dir(ident, run_id, {
                    self.config.idea_filename: idea_file,
                    "config.yaml": yaml.safe_dump(exp_config, sort_keys=False),
                    "run_metadata.json": metadata,
                })
            except WorkspaceError as e:
                raise RequestError(f"could not create run directory: {e}")
            record = dict(metadata)
            record.update(user=ident.username, gid=ident.gid, run_dir=run_dir, queued_time=created,
                          attempts=[], resume_count=0, stop_requested=False)
            self.registry.create(record)
            self.audit.log("run_created", user=ident.username, uid=ident.uid, run_id=run_id,
                           git_commit=release["commit"], backend=exp_config.get("llm-backend"), client_host=client_host)
            self.schedule()
            return self.registry.get(run_id)

    def _queue_resume(self, rec, actor, reason):
        run_id = rec["run_id"]
        self.registry.transition(run_id, rr.QUEUED, reason=reason, stop_requested=False,
                                 resume_count=rec.get("resume_count", 0) + 1, queued_time=_now(),
                                 waiting_reason=None, resume=True, end_time=None, exit_code=None)
        self.audit.log("run_resume_requested", user=rec["user"], run_id=run_id, actor=actor.username, reason=reason)

    def resume_run(self, ident, run_id):
        if not self.config.get("execution", "resume_enabled"):
            raise RequestError("resume is disabled by the administrator")
        with self.lock:
            rec = self._get_run_for(ident, run_id, "resume")
            if rec["status"] in rr.ACTIVE_STATES:
                raise RequestError(f"run is already {rec['status']}; nothing to resume")
            if rec["status"] not in rr.RESUMABLE_STATES:
                raise RequestError(f"run is {rec['status']} and cannot be resumed")
            others = [r for r in self._user_active_runs(rec["uid"]) if r["run_id"] != run_id]
            if len(others) >= self._per_user_limit():
                raise RequestError(f"{rec['user']} already has {len(others)} active job(s) (limit {self._per_user_limit()}); "
                                   f"wait for one to finish or stop one first")
            self._queue_resume(rec, ident, "requested by " + ident.username)
            self.schedule()
            return self.registry.get(run_id)

    def stop_run(self, ident, run_id):
        with self.lock:
            rec = self._get_run_for(ident, run_id, "stop")
            admin_action = rec["uid"] != ident.uid
            if rec["status"] == rr.QUEUED:
                self.resources.release(run_id)
                rec = self.registry.transition(run_id, rr.STOPPED, reason=f"dequeued by {ident.username}", stop_requested=True)
                self.audit.log("run_terminated", user=rec["user"], run_id=run_id, actor=ident.username, was="QUEUED",
                               admin_action=admin_action)
                self._sync_proxy_tokens()
                return rec
            if rec["status"] not in (rr.STARTING, rr.RUNNING, rr.STOPPING):
                raise RequestError(f"run is {rec['status']}, not running")
            rec = self.registry.transition(run_id, rr.STOPPING, reason=f"stop requested by {ident.username}",
                                           stop_requested=True, stop_requested_time=time.time())
            self.launcher.stop(rec.get("process") or {}, run_id)
            self.audit.log("run_stop_requested", user=rec["user"], run_id=run_id, actor=ident.username,
                           admin_action=admin_action)
            return rec

    def _launch(self, rec, allocation):
        run_id = rec["run_id"]
        owner = self._owner_ident(rec)
        resume = bool(rec.get("resume"))
        release_path = rec.get("release_path") or os.path.realpath(self.config.application_root)
        if not os.path.isdir(release_path):  # original release was garbage-collected
            release_path = os.path.realpath(self.config.application_root)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.workspaces.write_run_file(owner, run_id, "proxy_token", token + "\n")
        python = self.config.get("server", "python")
        if not os.path.exists(python):
            python = sys.executable
        argv = [python, "-I", os.path.join(release_path, "server", "research_runner.py"),
                "--run-dir", rec["run_dir"], "--run-id", run_id]
        if resume:
            argv.append("--resume")
        proxy = self.config.section("llm_proxy")
        base = f"http://{proxy['listen_host']}:{proxy['listen_port']}"
        workspace = self.workspaces.path(owner.username)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": workspace,
            "USER": owner.username,
            "AGENTLAB_RUN_ID": run_id,
            "AGENTLAB_RUN_DIR": rec["run_dir"],
            "AGENTLAB_APP_ROOT": release_path,
            "AGENTLAB_PIPELINE": self.config.get("execution", "pipeline"),
            "AGENTLAB_SEED": str(rec.get("random_seed", 0)),
            "HF_HOME": os.path.join(workspace, ".cache", "huggingface"),
            "MPLCONFIGDIR": os.path.join(workspace, ".cache", "matplotlib"),
            "TOKENIZERS_PARALLELISM": "false",
            "CUDA_VISIBLE_DEVICES": ",".join(str(g) for g in allocation.get("gpus", [])),
        }
        if proxy.get("enabled"):
            env.update({
                "AGENTLAB_LLM_PROXY": base,
                "OPENAI_BASE_URL": f"{base}/openai/v1",
                "OPENROUTER_BASE_URL": f"{base}/openrouter/v1",
                "DEEPSEEK_BASE_URL": f"{base}/deepseek/v1",
                "ANTHROPIC_BASE_URL": f"{base}/anthropic",
            })
        handle = self.launcher.launch(run_id, argv, env, owner.uid, owner.gid, cwd=release_path)
        started = _now()
        attempts = rec.get("attempts", []) + [{"start_time": started, "pid": handle.get("pid"), "resume": resume,
                                               "release_path": release_path}]
        rec = self.registry.transition(
            run_id, rr.STARTING, reason="launched", process=handle, allocation=allocation, attempts=attempts,
            start_time=rec.get("start_time") or started, pid=handle.get("pid"),
            token_sha256=hashlib.sha256(token.encode()).hexdigest(), waiting_reason=None,
        )
        self.audit.log("run_resumed" if resume else "run_started", user=owner.username, uid=owner.uid,
                       run_id=run_id, pid=handle.get("pid"), unit=handle.get("unit"), gpus=allocation.get("gpus"))
        self._sync_proxy_tokens()
        self._write_user_metadata(rec)
        return rec

    def schedule(self):
        """Start queued runs, FIFO, while resources allow."""
        with self.lock:
            queued = sorted((r for r in self.registry.active() if r["status"] == rr.QUEUED),
                            key=lambda r: r.get("queued_time", ""))
            for rec in queued:
                granted, info = self.resources.try_acquire(rec["run_id"], self._running_count())
                if not granted:
                    if rec.get("waiting_reason") != info:
                        self.registry.update(rec["run_id"], waiting_reason=info)
                        self.audit.log("resource_waiting", user=rec["user"], run_id=rec["run_id"], reason=info)
                    continue
                self.audit.log("resource_granted", user=rec["user"], run_id=rec["run_id"], gpus=info.get("gpus"))
                try:
                    self._launch(rec, info)
                except Exception as e:
                    self.resources.release(rec["run_id"])
                    msg = redact_text(f"{type(e).__name__}: {e}")
                    self.registry.transition(rec["run_id"], rr.FAILED, reason=f"launch failed: {msg}", end_time=_now())
                    self.audit.log("run_failed", user=rec["user"], run_id=rec["run_id"], reason="launch failed", error=msg)
                    self.audit.log("resource_released", user=rec["user"], run_id=rec["run_id"])

    def finalize_run(self, rec, reason_prefix="process exited"):
        """Process is gone: record the final status, release everything."""
        run_id = rec["run_id"]
        handle = rec.get("process") or {}
        exit_info = self.launcher.exit_info(handle, run_id) if handle else {"known": False}
        worker_state = self.workspaces.read_run_state(self._owner_ident(rec), run_id)
        final, why = determine_final_status(rec, exit_info, worker_state)
        rec = self.registry.transition(run_id, final, reason=f"{reason_prefix}: {why}", end_time=_now(),
                                       exit_code=exit_info.get("code"))
        released = self.resources.release(run_id)
        self.launcher.cleanup(handle, run_id)
        event = {rr.COMPLETED: "run_completed", rr.FAILED: "run_failed",
                 rr.INTERRUPTED: "run_interrupted", rr.STOPPED: "run_terminated"}[final]
        phase = (worker_state or {}).get("current_phase")
        self.audit.log(event, user=rec["user"], run_id=run_id, status=final.lower(), reason=why,
                       exit_code=exit_info.get("code"), last_phase=phase)
        if released or rec.get("allocation") is not None:
            self.audit.log("resource_released", user=rec["user"], run_id=run_id)
        self._sync_proxy_tokens()
        self._write_user_metadata(rec)
        return final

    def monitor_once(self):
        with self.lock:
            for rec in self.registry.active():
                if rec["status"] == rr.QUEUED:
                    continue
                handle = rec.get("process") or {}
                alive = bool(handle) and self.launcher.is_alive(handle, rec["run_id"])
                if alive:
                    if rec["status"] == rr.STARTING:
                        self.registry.transition(rec["run_id"], rr.RUNNING, reason="process confirmed")
                    elif rec["status"] == rr.STOPPING:
                        timeout = float(self.config.get("execution", "stop_timeout_seconds")) + 15
                        if time.time() - float(rec.get("stop_requested_time", time.time())) > timeout:
                            self.launcher.kill(handle, rec["run_id"])
                    continue
                self.finalize_run(rec)
            self.schedule()
            self.sessions.reap()

    def _write_user_metadata(self, rec):
        keys = ("run_id", "username", "uid", "hostname", "pid", "start_time", "created_time", "end_time",
                "research_idea_file", "git_commit", "git_dirty", "release_path", "model_backend", "configuration",
                "random_seed", "status", "python", "platform", "pipeline", "attempts", "resume_count", "exit_code", "name")
        meta = {k: rec.get(k) for k in keys}
        meta["resources"] = (rec.get("allocation") or {}).get("gpus")
        try:
            self.workspaces.write_run_file(self._owner_ident(rec), rec["run_id"], "run_metadata.json", meta)
        except WorkspaceError:
            pass

    def _sync_proxy_tokens(self):
        """Publish hashes of tokens for runs that may call the LLM; all others are revoked."""
        proxy = self.config.section("llm_proxy")
        path = proxy.get("tokens_file")
        if not proxy.get("enabled") or not path:
            return
        tokens = {}
        for r in self.registry.active():
            if r.get("token_sha256") and r["status"] in (rr.STARTING, rr.RUNNING, rr.STOPPING):
                tokens[r["token_sha256"]] = {"run_id": r["run_id"], "user": r["user"], "uid": r["uid"]}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_json(path, {"updated": _now(), "tokens": tokens}, mode=0o640)
        if self.is_root:
            proxy_gid = _group_gid("agentlab-proxy")
            if proxy_gid is not None:
                os.chown(path, 0, proxy_gid)

    # ------------------------------------------------------------ status views
    def _run_view(self, rec, ident, with_state=True):
        view = {k: rec.get(k) for k in ("run_id", "user", "status", "created_time", "start_time", "end_time",
                                         "git_commit", "resume_count", "waiting_reason", "exit_code", "name")}
        view["model_backend"] = (rec.get("model_backend") or {}).get("llm-backend")
        view["login_time"] = self.sessions.first_login(rec["user"])
        if with_state:
            state = self.workspaces.read_run_state(self._owner_ident(rec), rec["run_id"]) or {}
            for k in ("current_phase", "current_step", "last_completed_phase", "last_completed_step", "checkpoint_time"):
                v = state.get(k)
                view[k] = v if isinstance(v, (str, int, float, type(None))) else None
        return view

    def status_view(self, ident):
        admin = self.auth.is_admin(ident)
        visibility = self.config.get("security", "status_visibility")
        rows = []
        active = self.registry.active()
        for rec in active:
            if admin or rec["uid"] == ident.uid:
                rows.append(self._run_view(rec, ident))
            elif visibility == "full":
                rows.append(self._run_view(rec, ident, with_state=False))
            elif visibility == "usernames":
                rows.append({"user": rec["user"], "status": rec["status"], "run_id": None,
                             "login_time": self.sessions.first_login(rec["user"])})
        sessions = self.sessions.active()
        users_logged_in = sorted({s["user"] for s in sessions})
        if not admin and visibility == "own":
            users_logged_in = [u for u in users_logged_in if u == ident.username]
        run_users = {r["user"] for r in rows}
        for u in users_logged_in:
            if u not in run_users:
                rows.append({"user": u, "status": "LOGGED IN", "run_id": None, "login_time": self.sessions.first_login(u)})
        return {
            "rows": rows,
            "total_active_users": len({r["user"] for r in rows}),
            "total_active_runs": sum(1 for r in rows if r["status"] in rr.ACTIVE_STATES),
            "is_admin": admin,
        }

    # ----------------------------------------------------------- dispatching
    def handle(self, ident, req):
        op = req.get("op")
        handler = getattr(self, f"op_{op}", None) if isinstance(op, str) and op.isidentifier() else None
        if handler is None:
            raise RequestError(f"unknown operation {op!r}")
        if op not in ("session_event",) and not ident.is_root and not self.auth.is_lab_user(ident):
            raise RequestError(f"permission denied: your account is not in '{self.config.users_group}'")
        return handler(ident, req.get("args") or {})

    def op_ping(self, ident, args):
        return {"pong": True, "time": _now()}

    def op_whoami(self, ident, args):
        ws = self.workspaces.path(ident.username)
        return {"username": ident.username, "uid": ident.uid, "is_admin": self.auth.is_admin(ident),
                "workspace": ws, "workspace_exists": os.path.isdir(ws), "host": socket.gethostname()}

    def op_workspace_create(self, ident, args):
        try:
            ws, created = self.workspaces.create(ident)
        except WorkspaceError as e:
            raise RequestError(str(e))
        if created:
            self.audit.log("workspace_created", user=ident.username, uid=ident.uid, workspace=ws)
        return {"workspace": ws, "created": created}

    def op_audit_event(self, ident, args):
        event = args.get("event")
        if event not in USER_SUBMITTABLE_EVENTS:
            raise RequestError("event type not accepted from clients")
        fields = {k: v for k, v in (args.get("fields") or {}).items()
                  if k not in ("user", "uid", "timestamp", "event", "host", "prev", "hash") and isinstance(v, (str, int, float, bool, type(None)))}
        fields = {k: (v[:500] if isinstance(v, str) else v) for k, v in list(fields.items())[:20]}
        self.audit.log(event, user=ident.username, uid=ident.uid, pid=ident.pid, **fields)
        return {"logged": True}

    def op_session_event(self, ident, args):
        if not ident.is_root:
            raise RequestError("session events are accepted from PAM (root) only")
        user = args.get("user")
        if not valid_username(user or ""):
            raise RequestError("invalid user")
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            raise RequestError("unknown user")
        kind = args.get("type")
        common = dict(user=user, uid=uid, session_pid=args.get("pid"), tty=args.get("tty"),
                      rhost=args.get("rhost"), service=args.get("service"))
        if kind == "open_session":
            self.sessions.opened(login_time=_now(), **common)
        elif kind == "close_session":
            self.sessions.closed(**common)
        else:
            raise RequestError("bad session event type")
        return {"recorded": True}

    def op_create_run(self, ident, args):
        rec = self.create_run(ident, args.get("idea_text"), args.get("config_text"), args.get("client_host"),
                              name=args.get("name") or None)
        return self._run_view(rec, ident)

    def op_list_runs(self, ident, args):
        if args.get("all"):
            self._require_admin(ident)
            recs = self.registry.all()
        else:
            recs = self.registry.for_user(ident.uid)
        return {"runs": [self._run_view(r, ident, with_state=r["status"] in rr.ACTIVE_STATES or args.get("detail"))
                         for r in recs]}

    def op_run_info(self, ident, args):
        rec = self._get_run_for(ident, args.get("run_id"), "inspect")
        view = self._run_view(rec, ident)
        view["history"] = rec.get("history", [])
        view["attempts"] = rec.get("attempts", [])
        view["run_dir"] = rec.get("run_dir")
        return view

    def op_attention(self, ident, args):
        """What should a reconnecting user be told? (active or interrupted runs)"""
        recs = sorted(self.registry.for_user(ident.uid), key=lambda r: r.get("created_time", ""))
        active = [self._run_view(r, ident) for r in recs if r["status"] in rr.ACTIVE_STATES]
        resumable = [self._run_view(r, ident) for r in recs
                     if r["status"] in (rr.INTERRUPTED,) and not r.get("resume_dismissed")]
        return {"active": active, "interrupted": resumable[-1:]}

    def op_dismiss_resume(self, ident, args):
        rec = self._get_run_for(ident, args.get("run_id"), "dismiss")
        self.registry.update(rec["run_id"], resume_dismissed=True)
        return {"dismissed": rec["run_id"]}

    def op_resume(self, ident, args):
        return self._run_view(self.resume_run(ident, args.get("run_id")), ident)

    def op_stop(self, ident, args):
        return self._run_view(self.stop_run(ident, args.get("run_id")), ident)

    def op_tail(self, ident, args):
        rec = self._get_run_for(ident, args.get("run_id"), "tail")
        name = "stderr.log" if args.get("stderr") else "stdout.log"
        nbytes = max(1024, min(int(args.get("bytes", 16384)), 1_000_000))
        try:
            text = self.workspaces.tail_log(self._owner_ident(rec), rec["run_id"], name, nbytes)
        except WorkspaceError as e:
            raise RequestError(str(e))
        return {"text": redact_text(text), "status": rec["status"]}

    def op_status(self, ident, args):
        return self.status_view(ident)

    def op_history(self, ident, args):
        limit = max(1, min(int(args.get("limit", 50)), 5000))
        if args.get("all") or args.get("user"):
            self._require_admin(ident)
            user = args.get("user")
        else:
            user = ident.username
        return {"events": read_events(self.config.audit_log, user=user, limit=limit)}

    # admin operations ------------------------------------------------------
    def op_admin_users(self, ident, args):
        self._require_admin(ident)
        root = self.config.workspace_root
        users = []
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            if not valid_username(name):
                continue
            runs = [r for r in self.registry.all() if r["user"] == name]
            users.append({"user": name, "runs": len(runs),
                          "active": sum(1 for r in runs if r["status"] in rr.ACTIVE_STATES),
                          "logged_in": any(s["user"] == name for s in self.sessions.active())})
        return {"users": users}

    def op_admin_logs(self, ident, args):
        self._require_admin(ident)
        limit = max(1, min(int(args.get("limit", 100)), 100000))
        return {"events": read_events(self.config.audit_log, user=args.get("user"), run_id=args.get("run_id"),
                                      events=set(args["events"]) if args.get("events") else None, limit=limit)}

    def op_admin_verify_log(self, ident, args):
        self._require_admin(ident)
        ok, n, bad, msg = verify_chain(self.config.audit_log)
        self.audit.log("audit_log_verified", user=ident.username, ok=ok, records=n, bad_line=bad)
        return {"ok": ok, "records": n, "bad_line": bad, "message": msg}

    def op_admin_deployments(self, ident, args):
        self._require_admin(ident)
        return {"deployments": (read_json(self.deployments_path) or {}).get("deployments", [])}

    def op_record_deployment(self, ident, args):
        if not ident.is_root:
            raise RequestError("deployments are recorded by scripts/deploy.sh running as root")
        entry = {k: str(args.get(k, ""))[:200] for k in ("commit", "previous", "deployer", "branch", "release_path", "tests")}
        entry["time"] = _now()
        with self.lock:
            data = read_json(self.deployments_path) or {"deployments": []}
            data["deployments"].append(entry)
            atomic_write_json(self.deployments_path, data, mode=0o640)
        self.audit.log("deployment", user=entry["deployer"] or "root", **{k: v for k, v in entry.items() if k != "time"})
        return entry

    def op_admin_health(self, ident, args):
        self._require_admin(ident)
        snap = self.resources.probe()
        checks = {
            "audit_log_exists": os.path.exists(self.config.audit_log),
            "audit_log_mode": oct(os.stat(self.config.audit_log).st_mode & 0o777),
            "workspace_root": os.path.isdir(self.config.workspace_root),
            "launcher": self.launcher.name,
            "release": self._release_info(),
            "running_runs": self._running_count(),
            "resources": {k: v for k, v in snap.items()},
        }
        return checks


# --------------------------------------------------------------- socket server
class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        daemon = self.server.daemon
        limit = daemon.config.get("security", "max_request_bytes")
        try:
            ident = daemon.peer_identity(self.request)
        except Exception:
            self._reply({"ok": False, "error": "could not determine caller identity"})
            return
        line = self.rfile.readline(limit + 1)
        if len(line) > limit:
            self._reply({"ok": False, "error": "request too large"})
            return
        try:
            req = json.loads(line or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            self._reply({"ok": False, "error": "malformed request"})
            return
        try:
            result = daemon.handle(ident, req)
            self._reply({"ok": True, "result": result})
        except RequestError as e:
            self._reply({"ok": False, "error": str(e)})
        except Exception as e:  # never leak internals or secrets to clients
            daemon.audit.log("daemon_error", user=ident.username, op=str(req.get("op"))[:50],
                             error=redact_text(f"{type(e).__name__}: {e}")[:500])
            self._reply({"ok": False, "error": "internal error (logged for administrators)"})

    def _reply(self, obj):
        try:
            self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(daemon, socket_path=None):
    socket_path = socket_path or daemon.config.socket_path
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    old_umask = os.umask(0o117)
    try:
        server = _Server(socket_path, _Handler)
    finally:
        os.umask(old_umask)
    if daemon.is_root and daemon.users_gid is not None:
        os.chown(socket_path, 0, daemon.users_gid)
        os.chmod(socket_path, 0o660)
    else:
        os.chmod(socket_path, 0o600)
    server.daemon = daemon

    def monitor_loop():
        interval = float(daemon.config.get("execution", "poll_interval_seconds"))
        while not daemon.stop_event.wait(interval):
            try:
                daemon.monitor_once()
            except Exception as e:
                daemon.audit.log("daemon_error", where="monitor", error=redact_text(f"{type(e).__name__}: {e}")[:500])

    t = threading.Thread(target=monitor_loop, name="monitor", daemon=True)
    t.start()
    return server, t


AgentLabDaemon.peer_identity = staticmethod(peer_identity)


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentLaboratory supervisor daemon")
    parser.add_argument("--config", default=None, help="path to server.yaml")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    os.umask(0o027)
    daemon = AgentLabDaemon(config)
    daemon.startup()
    server, _ = serve(daemon)

    def _shutdown(signum, frame):
        daemon.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        # Research runs are separate units/sessions: stopping the daemon never stops them.
        daemon.audit.log("daemon_stopped", pid=os.getpid())
        server.server_close()


if __name__ == "__main__":
    main()
