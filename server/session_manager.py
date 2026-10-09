"""
SSH session tracking.

Login/logout events come from PAM (pam_exec in /etc/pam.d/sshd, see
scripts/agentlab-pam-session), which runs as root for *every* SSH session --
interactive, `ssh host cmd`, sftp -- so it cannot be bypassed the way .bashrc
can. The daemon only accepts session events from root peers.

Sessions whose sshd process vanished without a close event (network drop,
sshd crash) are reaped and logged as `session_disconnected`.
"""
import threading

import psutil

from .fsutil import atomic_write_json, read_json
from .launcher import current_boot_id, process_matches


class SessionManager:
    def __init__(self, path, audit):
        self.path = path
        self.audit = audit
        self._lock = threading.Lock()
        data = read_json(path) or {}
        if data.get("boot_id") != current_boot_id():
            data = {"boot_id": current_boot_id(), "sessions": {}}  # host restarted: no live sessions
        self.data = data
        self._save()

    def _save(self):
        atomic_write_json(self.path, self.data, mode=0o640)

    @staticmethod
    def _key(user, session_pid):
        return f"{user}:{session_pid}"

    def opened(self, user, uid, session_pid, tty=None, rhost=None, service=None, login_time=None):
        try:
            ctime = psutil.Process(int(session_pid)).create_time()
        except (psutil.Error, ValueError, TypeError):
            ctime = None
        with self._lock:
            self.data["sessions"][self._key(user, session_pid)] = {
                "user": user, "uid": uid, "pid": session_pid, "pid_create_time": ctime,
                "tty": tty, "rhost": rhost, "service": service, "login_time": login_time,
            }
            self._save()
        self.audit.log("login", user=user, uid=uid, pid=session_pid, tty=tty, rhost=rhost, service=service)

    def closed(self, user, uid, session_pid, tty=None, rhost=None, service=None):
        with self._lock:
            existed = self.data["sessions"].pop(self._key(user, session_pid), None)
            self._save()
        self.audit.log("logout", user=user, uid=uid, pid=session_pid, tty=tty, rhost=rhost, service=service,
                       tracked=existed is not None)

    def reap(self):
        """Drop sessions whose sshd process disappeared without a logout."""
        gone = []
        with self._lock:
            for key, s in list(self.data["sessions"].items()):
                if not process_matches(s.get("pid"), s.get("pid_create_time")):
                    gone.append(self.data["sessions"].pop(key))
            if gone:
                self._save()
        for s in gone:
            self.audit.log("session_disconnected", user=s["user"], uid=s.get("uid"), pid=s.get("pid"), rhost=s.get("rhost"))
        return gone

    def active(self):
        with self._lock:
            return list(self.data["sessions"].values())

    def first_login(self, user):
        times = [s["login_time"] for s in self.active() if s["user"] == user and s.get("login_time")]
        return min(times) if times else None
