"""
PAM session hook (pam_exec). Runs as root for every SSH session open/close and
forwards the event to agentlabd. It must never block or break SSH logins: every
failure is swallowed and the hook always exits 0 (and the PAM line is `optional`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    try:
        kind = os.environ.get("PAM_TYPE")
        user = os.environ.get("PAM_USER")
        if kind not in ("open_session", "close_session") or not user:
            return 0
        from server.client import DaemonClient
        from server.config import load_config
        config = load_config()
        from server.permissions import in_group
        if not (in_group(user, config.users_group) or in_group(user, config.admins_group)):
            return 0  # not a lab member: nothing to track
        DaemonClient(config=config).call(
            "session_event", timeout=3, type=kind, user=user, pid=int(os.environ.get("AGENTLAB_SESSION_PID") or os.getppid()),
            tty=os.environ.get("PAM_TTY"), rhost=os.environ.get("PAM_RHOST"), service=os.environ.get("PAM_SERVICE"),
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
