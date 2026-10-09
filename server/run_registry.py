"""
Authoritative run registry.

One JSON record per run in <state_dir>/runs/<run_id>.json, owned by the daemon
(root) and not writable by lab users. `agentlab status` is answered from here,
never from user-editable files. The user-visible run_metadata.json inside the
run directory is a copy for reproducibility.
"""
import datetime
import os
import re
import secrets
import threading

from .fsutil import atomic_write_json, read_json

QUEUED = "QUEUED"          # waiting for resources / a free slot
STARTING = "STARTING"      # launch issued, process identity not yet confirmed
RUNNING = "RUNNING"
STOPPING = "STOPPING"      # stop requested, waiting for graceful exit
COMPLETED = "COMPLETED"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"  # process vanished / killed / host restarted; resumable
STOPPED = "STOPPED"          # stopped on request; resumable

ACTIVE_STATES = {QUEUED, STARTING, RUNNING, STOPPING}
RESUMABLE_STATES = {INTERRUPTED, STOPPED, FAILED}
TERMINAL_STATES = {COMPLETED, FAILED, INTERRUPTED, STOPPED}

ALLOWED_TRANSITIONS = {
    QUEUED: {STARTING, STOPPED, FAILED},
    STARTING: {RUNNING, STOPPING, FAILED, INTERRUPTED, STOPPED, COMPLETED},
    RUNNING: {STOPPING, COMPLETED, FAILED, INTERRUPTED, STOPPED},
    STOPPING: {STOPPED, COMPLETED, FAILED, INTERRUPTED},
    INTERRUPTED: {QUEUED},
    STOPPED: {QUEUED},
    FAILED: {QUEUED},
    COMPLETED: set(),
}

RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_[a-z_][a-z0-9_-]{0,31}_[0-9a-f]{6}$")


class TransitionError(Exception):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_run_id(username, now=None):
    """YYYYMMDD_HHMMSS_<username>_<6 hex>; random part from the OS CSPRNG."""
    now = now or datetime.datetime.now()
    return f"{now:%Y%m%d_%H%M%S}_{username}_{secrets.token_hex(3)}"


def valid_run_id(run_id):
    return isinstance(run_id, str) and bool(RUN_ID_RE.match(run_id))


class RunRegistry:
    def __init__(self, state_dir):
        self.runs_dir = os.path.join(state_dir, "runs")
        os.makedirs(self.runs_dir, mode=0o750, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, run_id):
        if not valid_run_id(run_id):
            raise ValueError("invalid run id")
        return os.path.join(self.runs_dir, run_id + ".json")

    def create(self, record):
        with self._lock:
            path = self._path(record["run_id"])
            if os.path.exists(path):
                raise FileExistsError(record["run_id"])  # run IDs are immutable
            record.setdefault("history", []).append({"status": record["status"], "time": utc_now()})
            atomic_write_json(path, record, mode=0o640)
            return record

    def get(self, run_id):
        try:
            return read_json(self._path(run_id))
        except ValueError:
            return None

    def all(self):
        out = []
        for name in sorted(os.listdir(self.runs_dir)):
            if name.endswith(".json"):
                rec = read_json(os.path.join(self.runs_dir, name))
                if rec:
                    out.append(rec)
        return out

    def for_user(self, uid):
        return [r for r in self.all() if r.get("uid") == uid]

    def active(self):
        return [r for r in self.all() if r.get("status") in ACTIVE_STATES]

    def update(self, run_id, **fields):
        with self._lock:
            rec = self.get(run_id)
            if rec is None:
                raise KeyError(run_id)
            rec.update(fields)
            atomic_write_json(self._path(run_id), rec, mode=0o640)
            return rec

    def transition(self, run_id, new_status, reason=None, **fields):
        with self._lock:
            rec = self.get(run_id)
            if rec is None:
                raise KeyError(run_id)
            old = rec["status"]
            if new_status != old and new_status not in ALLOWED_TRANSITIONS.get(old, set()):
                raise TransitionError(f"{run_id}: {old} -> {new_status} not allowed")
            rec.update(fields)
            rec["status"] = new_status
            entry = {"status": new_status, "time": utc_now()}
            if reason:
                entry["reason"] = reason
            rec.setdefault("history", []).append(entry)
            atomic_write_json(self._path(run_id), rec, mode=0o640)
            return rec
