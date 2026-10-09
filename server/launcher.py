"""
Process supervision for research runs.

The interactive SSH session is never the parent of a research process.

* SystemdLauncher (production): each run is a transient system unit
  `agentlab-run-<run_id>.service` started with `systemd-run --uid=<user>`.
  It lives in its own cgroup, survives SSH disconnects and daemon restarts, has
  memory/task limits and sandboxing, and its exit status is retained
  (RemainAfterExit=yes) until the daemon reconciles it.
* SubprocessLauncher (development/tests): a detached child in a new session.

A run's identity is (pid, process create_time, boot_id[, unit]); a bare PID is
never trusted because PIDs are reused and do not survive reboots.
"""
import os
import signal
import subprocess
import time

import psutil


def current_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return f"boot-{int(psutil.boot_time())}"


def process_matches(pid, create_time, tolerance=1.0):
    """True if `pid` is alive *and* is the same process we started."""
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        return create_time is None or abs(p.create_time() - float(create_time)) <= tolerance
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def unit_name_for(run_id):
    return f"agentlab-run-{run_id}.service"


class SubprocessLauncher:
    name = "subprocess"

    def __init__(self, config):
        self.config = config
        self._children = {}

    def launch(self, run_id, argv, env, uid, gid, cwd, properties=None):
        kwargs = {}
        if os.geteuid() == 0 and uid != 0:
            kwargs.update(user=uid, group=gid, extra_groups=[])
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
        self._children[run_id] = proc
        try:
            ctime = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            ctime = None
        return {"launcher": self.name, "pid": proc.pid, "create_time": ctime, "boot_id": current_boot_id()}

    def is_alive(self, handle, run_id=None):
        proc = self._children.get(run_id)
        if proc is not None:
            return proc.poll() is None
        return handle.get("boot_id") == current_boot_id() and process_matches(handle.get("pid"), handle.get("create_time"))

    def exit_info(self, handle, run_id=None):
        proc = self._children.get(run_id)
        if proc is not None and proc.poll() is not None:
            code = proc.returncode
            return {"known": True, "code": code if code >= 0 else 128 - code}
        return {"known": False, "code": None}

    def stop(self, handle, run_id=None, sig=signal.SIGTERM):
        pid = handle.get("pid")
        if not self.is_alive(handle, run_id):
            return False
        try:
            os.killpg(os.getpgid(pid), sig)  # whole session: worker + code-execution children
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return False
        return True

    def kill(self, handle, run_id=None):
        return self.stop(handle, run_id, sig=signal.SIGKILL)

    def cleanup(self, handle, run_id=None):
        self._children.pop(run_id, None)


class SystemdLauncher:
    name = "systemd"

    def __init__(self, config, systemctl="systemctl", systemd_run="systemd-run"):
        self.config = config
        self.systemctl = systemctl
        self.systemd_run = systemd_run

    def launch(self, run_id, argv, env, uid, gid, cwd, properties=None):
        unit = unit_name_for(run_id)
        exe = self.config.section("execution")
        props = {
            "RemainAfterExit": "yes",
            "KillMode": "mixed",          # SIGTERM to main, then the rest of the cgroup
            "KillSignal": "SIGTERM",
            "TimeoutStopSec": str(exe.get("stop_timeout_seconds", 60)),
            "NoNewPrivileges": "yes",
            "PrivateTmp": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "read-only",
            "ReadWritePaths": self.config.workspace_for(_username(uid)),
            "ProtectKernelTunables": "yes",
            "ProtectControlGroups": "yes",
            "RestrictSUIDSGID": "yes",
            "TasksMax": str(exe.get("tasks_max", 4096)),
        }
        if exe.get("memory_max"):
            props["MemoryMax"] = str(exe["memory_max"])
        if exe.get("cpu_quota"):
            props["CPUQuota"] = str(exe["cpu_quota"])
        props.update(properties or {})
        cmd = [self.systemd_run, f"--unit={unit}", f"--uid={uid}", f"--gid={gid}",
               "--description=AgentLaboratory research run " + run_id, "--quiet"]
        for k, v in props.items():
            cmd.append(f"--property={k}={v}")
        for k, v in env.items():  # only non-secret values ever reach here
            cmd.append(f"--setenv={k}={v}")
        cmd.append(f"--working-directory={cwd}")
        cmd += ["--"] + list(argv)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        info = {}
        for _ in range(50):
            info = self._show(unit)
            if info.get("MainPID", "0") != "0":
                break
            time.sleep(0.1)
        pid = int(info.get("MainPID", "0") or 0)
        try:
            ctime = psutil.Process(pid).create_time() if pid else None
        except psutil.Error:
            ctime = None
        return {"launcher": self.name, "unit": unit, "pid": pid, "create_time": ctime, "boot_id": current_boot_id()}

    def _show(self, unit):
        props = "LoadState,ActiveState,SubState,Result,MainPID,ExecMainStatus,ExecMainCode"
        try:
            out = subprocess.run([self.systemctl, "show", unit, "-p", props],
                                 capture_output=True, text=True, timeout=30).stdout
        except (subprocess.SubprocessError, OSError):
            return {}
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def is_alive(self, handle, run_id=None):
        if handle.get("boot_id") != current_boot_id():
            return False
        info = self._show(handle["unit"])
        if info.get("LoadState") != "loaded":
            return False
        return info.get("SubState") in ("running", "start", "start-pre", "start-post", "stop-sigterm", "stop-sigkill")

    def exit_info(self, handle, run_id=None):
        info = self._show(handle["unit"])
        if info.get("LoadState") != "loaded" or info.get("SubState") in ("running", "start"):
            return {"known": False, "code": None}
        code_kind = info.get("ExecMainCode", "")
        try:
            status = int(info.get("ExecMainStatus", ""))
        except ValueError:
            return {"known": False, "code": None}
        # ExecMainCode: 1=exited, 2=killed, 3=dumped (status is a signal number)
        if code_kind in ("2", "3"):
            status = 128 + status
        return {"known": True, "code": status}

    def stop(self, handle, run_id=None, sig=None):
        subprocess.run([self.systemctl, "stop", "--no-block", handle["unit"]], capture_output=True, timeout=30)
        return True

    def kill(self, handle, run_id=None):
        subprocess.run([self.systemctl, "kill", "--signal=SIGKILL", handle["unit"]], capture_output=True, timeout=30)
        return True

    def cleanup(self, handle, run_id=None):
        unit = handle.get("unit")
        if unit:
            subprocess.run([self.systemctl, "stop", unit], capture_output=True, timeout=60)
            subprocess.run([self.systemctl, "reset-failed", unit], capture_output=True, timeout=30)


def _username(uid):
    import pwd
    return pwd.getpwuid(uid).pw_name


def make_launcher(config):
    kind = config.section("execution").get("launcher", "systemd")
    if kind == "systemd":
        return SystemdLauncher(config)
    if kind == "subprocess":
        return SubprocessLauncher(config)
    raise ValueError(f"unknown launcher {kind!r}")
