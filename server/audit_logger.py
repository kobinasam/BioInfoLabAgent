"""
Central, protected audit log (JSON Lines).

Only the daemon (root) writes this file. Lab users submit events through the
daemon socket, which stamps the kernel-verified identity, whitelists the event
type, and redacts secrets. Protection is layered:

  * filesystem: directory root:<admins> 0750, file root:<admins> 0640
  * append-only inode attribute (chattr +a) applied by the installer
  * O_APPEND writes under an exclusive flock, fsync per record
  * a SHA-256 hash chain ("prev" / "hash") so any edit, deletion or
    reordering of past records is detectable with `agentlab-admin verify-log`

root can still alter anything on the host; that is inherent and documented.
"""
import datetime
import fcntl
import hashlib
import json
import os
import socket
import threading

from .secret_guard import redact

GENESIS = "0" * 64

# Events a normal lab user may submit (identity is stamped by the daemon).
USER_SUBMITTABLE_EVENTS = {
    "research_submitted",
    "api_error",
    "session_attach",
    "client_error",
}


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record_hash(record_without_hash):
    canonical = json.dumps(record_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLogger:
    def __init__(self, path, file_mode=0o640, group_gid=None):
        self.path = path
        self.file_mode = file_mode
        self.group_gid = group_gid
        self.host = socket.gethostname()
        self._lock = threading.Lock()
        self._last_hash = None

    def ensure(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, self.file_mode)
            os.close(fd)
        # Only touch metadata when it is wrong: with the append-only attribute (chattr +a) set by the
        # installer, even root gets EPERM on chown/chmod -- which is the protection working.
        st = os.stat(self.path)
        try:
            if os.geteuid() == 0 and self.group_gid is not None and (st.st_uid, st.st_gid) != (0, self.group_gid):
                os.chown(self.path, 0, self.group_gid)
            if (st.st_mode & 0o777) != self.file_mode:
                os.chmod(self.path, self.file_mode)
        except PermissionError:
            pass

    def _tail_hash(self):
        """Hash of the last record in the file (GENESIS if empty)."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return GENESIS
                block = min(size, 65536)
                f.seek(size - block)
                lines = f.read(block).splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return json.loads(line).get("hash", GENESIS)
        except (OSError, ValueError):
            pass
        return GENESIS

    def log(self, event, **fields):
        record = {"timestamp": utc_now_iso(), "event": str(event), "host": self.host}
        record.update(redact(fields))
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, self.file_mode)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                # Re-read the tail under the lock so concurrent writers keep one chain.
                prev = self._tail_hash()
                record["prev"] = prev
                record["hash"] = _record_hash(record)
                os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
                self._last_hash = record["hash"]
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        return record


def read_events(path, user=None, run_id=None, events=None, limit=None):
    out = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if user is not None and rec.get("user") != user:
                    continue
                if run_id is not None and rec.get("run_id") != run_id:
                    continue
                if events is not None and rec.get("event") not in events:
                    continue
                out.append(rec)
    except OSError:
        return []
    if limit:
        out = out[-int(limit):]
    return out


def verify_chain(path):
    """Return (ok, number_of_records, first_bad_line_or_None, message)."""
    prev = GENESIS
    n = 0
    with open(path, "r") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                return False, n, lineno, "unparseable record"
            claimed = rec.pop("hash", None)
            if rec.get("prev") != prev:
                return False, n, lineno, "chain broken (record removed, inserted or reordered)"
            if _record_hash(rec) != claimed:
                return False, n, lineno, "record contents modified"
            prev = claimed
            n += 1
    return True, n, None, "ok"
