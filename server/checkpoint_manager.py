"""
Durable checkpoints for a run.

  <run_dir>/state.json                     progress (phase/step), atomically replaced
  <run_dir>/checkpoints/ckpt_<seq>_<subtask>.pkl   pickled LaboratoryWorkflow
  <run_dir>/checkpoints/latest.json        pointer {file, sha256, seq, ...}

Checkpoints are written with write-temp + fsync + rename, and each one is
verified by SHA-256 before it is loaded, so a crash mid-write can never leave a
corrupt "latest" checkpoint: resume falls back to the newest one that verifies.
"""
import datetime
import hashlib
import json
import os
import pickle

from .fsutil import atomic_write_bytes, atomic_write_json, read_json

STATE_FILE = "state.json"
LATEST_FILE = "latest.json"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CheckpointManager:
    def __init__(self, run_dir, run_id, user, keep=5):
        self.run_dir = run_dir
        self.run_id = run_id
        self.user = user
        self.keep = keep
        self.ckpt_dir = os.path.join(run_dir, "checkpoints")
        os.makedirs(self.ckpt_dir, mode=0o700, exist_ok=True)
        self.state_path = os.path.join(run_dir, STATE_FILE)
        self.state = read_json(self.state_path) or {
            "run_id": run_id,
            "user": user,
            "status": "created",
            "current_phase": None,
            "current_step": 0,
            "last_completed_step": None,
            "last_completed_phase": None,
            "completed_subtasks": [],
            "checkpoint_time": None,
            "checkpoint_file": None,
            "resume_supported": True,
            "resume_count": 0,
        }

    # ------------------------------------------------------------- state.json
    def update_state(self, **fields):
        self.state.update(fields)
        self.state["updated"] = utc_now()
        atomic_write_json(self.state_path, self.state, mode=0o600)
        return self.state

    def progress(self, phase, step):
        """Called on every agent step; cheap enough to run each LLM turn."""
        if phase != self.state.get("current_phase"):
            self.state["last_completed_step"] = None
        elif self.state.get("current_step") is not None and step > self.state["current_step"]:
            self.state["last_completed_step"] = self.state["current_step"]
        self.update_state(current_phase=phase, current_step=step, status="running")

    # ----------------------------------------------------------- checkpoints
    def _next_seq(self):
        latest = read_json(os.path.join(self.ckpt_dir, LATEST_FILE)) or {}
        return int(latest.get("seq", 0)) + 1

    def save(self, obj, subtask, completed_subtasks=None):
        payload = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        seq = self._next_seq()
        safe = "".join(c if c.isalnum() else "_" for c in subtask)
        name = f"ckpt_{seq:05d}_{safe}.pkl"
        atomic_write_bytes(os.path.join(self.ckpt_dir, name), payload, mode=0o600)
        pointer = {
            "seq": seq,
            "file": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "subtask": subtask,
            "time": utc_now(),
        }
        atomic_write_json(os.path.join(self.ckpt_dir, LATEST_FILE), pointer, mode=0o600)
        done = list(completed_subtasks) if completed_subtasks is not None else self.state.get("completed_subtasks", [])
        self.update_state(
            checkpoint_time=pointer["time"], checkpoint_file=name,
            last_completed_phase=subtask, completed_subtasks=done,
        )
        self._prune()
        return pointer

    def _candidates(self):
        files = sorted(f for f in os.listdir(self.ckpt_dir) if f.startswith("ckpt_") and f.endswith(".pkl"))
        return list(reversed(files))

    def _prune(self):
        for old in self._candidates()[self.keep:]:
            try:
                os.unlink(os.path.join(self.ckpt_dir, old))
            except OSError:
                pass

    def load_latest(self):
        """Return (obj, pointer) for the newest checkpoint that verifies, else (None, None)."""
        pointer = read_json(os.path.join(self.ckpt_dir, LATEST_FILE))
        tried = set()
        if pointer and pointer.get("file"):
            obj = self._load_verified(pointer["file"], pointer.get("sha256"))
            if obj is not None:
                return obj, pointer
            tried.add(pointer["file"])
        # Fall back to older checkpoints (no hash available -> must unpickle cleanly).
        for name in self._candidates():
            if name in tried:
                continue
            obj = self._load_verified(name, None)
            if obj is not None:
                return obj, {"file": name, "fallback": True}
        return None, None

    def _load_verified(self, name, sha256):
        path = os.path.join(self.ckpt_dir, os.path.basename(name))
        try:
            with open(path, "rb") as f:
                payload = f.read()
        except OSError:
            return None
        if sha256 and hashlib.sha256(payload).hexdigest() != sha256:
            return None
        try:
            return pickle.loads(payload)
        except Exception:
            return None

    def has_checkpoint(self):
        return bool(self._candidates())


def describe_state(state):
    if not state:
        return {}
    return {k: state.get(k) for k in (
        "status", "current_phase", "current_step", "last_completed_phase",
        "last_completed_step", "checkpoint_time", "resume_count")}


def dumps_state(state):
    return json.dumps(describe_state(state))
