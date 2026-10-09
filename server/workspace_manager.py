"""
Per-user workspaces under <workspace_root>/<username>, isolated with Unix permissions.

    <workspace_root>            root:root   0711  (traverse only; users cannot list it)
    <workspace_root>/<user>            user:user   0700   the user's root folder
    <workspace_root>/<user>/<job-id>   user:user   0700   one folder per job

The workspace directory itself is created by the daemon (root) so users cannot
squat on someone else's name; everything inside it is created as the user.
"""
import datetime
import json
import os
import socket
import subprocess
import sys

from . import user_ops
from .permissions import valid_username

USER_OPS_PATH = os.path.abspath(user_ops.__file__)


class WorkspaceError(Exception):
    pass


class UserOpsRunner:
    """Run user_ops either in a privilege-dropped child (daemon is root) or in-process."""

    def __init__(self, python=None):
        self.python = python or sys.executable

    def expected_owner(self, ident):
        """(uid, gid) that must own the user's workspace."""
        return ident.uid, ident.gid

    def run(self, ident, op, **args):
        request = {"op": op, "args": args}
        if os.geteuid() == 0 and ident.uid != 0:
            proc = subprocess.run(
                [self.python, "-I", USER_OPS_PATH],
                input=json.dumps(request), capture_output=True, text=True, timeout=60,
                user=ident.uid, group=ident.gid, extra_groups=[],
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/",
            )
            try:
                response = json.loads(proc.stdout)
            except ValueError:
                raise WorkspaceError(f"user operation '{op}' failed")
            if not response.get("ok"):
                raise WorkspaceError(response.get("error", "user operation failed"))
            return response["result"]
        if ident.uid != os.geteuid():
            raise WorkspaceError("cannot act on another user's workspace without root")
        try:
            return user_ops.dispatch(request)
        except Exception as e:
            raise WorkspaceError(f"{type(e).__name__}: {e}")


def validate_idea(text, max_bytes=65536):
    if text is None or not text.strip():
        raise WorkspaceError("research idea is empty")
    if len(text.encode("utf-8")) > max_bytes:
        raise WorkspaceError(f"research idea exceeds {max_bytes} bytes")
    if "\x00" in text:
        raise WorkspaceError("research idea contains NUL bytes")
    return text


def format_research_idea(username, idea_text, host=None, created=None):
    created = created or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    host = host or socket.gethostname()
    return (
        f"Researcher: {username}\n"
        f"Created: {created}\n"
        f"Host: {host}\n"
        f"\n"
        f"Research Idea\n"
        f"=============\n"
        f"\n"
        f"{idea_text.rstrip()}\n"
    )


def parse_research_idea(file_text):
    """Return the idea body from a research_idea.txt (header stripped if present)."""
    marker = "Research Idea\n=============\n"
    if marker in file_text:
        return file_text.split(marker, 1)[1].strip("\n")
    return file_text.strip()


class WorkspaceManager:
    def __init__(self, config, runner=None):
        self.config = config
        self.runner = runner or UserOpsRunner()

    def path(self, username):
        if not valid_username(username):
            raise WorkspaceError("invalid username")
        return os.path.join(self.config.workspace_root, username)

    def exists(self, username):
        return os.path.isdir(self.path(username))

    def ensure_root(self):
        root = self.config.workspace_root
        os.makedirs(root, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(root, 0, 0)
            os.chmod(root, 0o711)

    def create(self, ident):
        """Create (or repair) the caller's workspace. Returns (path, created_bool)."""
        ws = self.path(ident.username)
        created = False
        if not os.path.isdir(ws):
            if os.path.islink(ws):
                raise WorkspaceError("workspace path is a symlink")
            self.ensure_root()
            os.mkdir(ws, 0o700)
            created = True
            if os.geteuid() == 0:
                os.chown(ws, *self.runner.expected_owner(ident))
        st = os.lstat(ws)
        if st.st_uid != self.runner.expected_owner(ident)[0]:
            raise WorkspaceError("workspace exists but is owned by another account")
        meta = {
            "username": ident.username,
            "uid": ident.uid,
            "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": socket.gethostname(),
        }
        self.runner.run(ident, "init_workspace", workspace=ws, metadata=meta)
        return ws, created

    def run_dir(self, username, run_id):
        return os.path.join(self.path(username), run_id)  # <root>/<user>/<job-id>

    def create_run_dir(self, ident, run_id, files):
        return self.runner.run(ident, "create_run", workspace=self.path(ident.username), run_id=run_id, files=files)

    def write_run_file(self, ident, run_id, name, content):
        return self.runner.run(ident, "write_run_file", workspace=self.path(ident.username), run_id=run_id, name=name, content=content)

    def read_run_state(self, ident, run_id):
        try:
            return self.runner.run(ident, "read_state", workspace=self.path(ident.username), run_id=run_id)["state"]
        except WorkspaceError:
            return None

    def tail_log(self, ident, run_id, name="stdout.log", max_bytes=8192):
        return self.runner.run(ident, "tail_log", workspace=self.path(ident.username), run_id=run_id, name=name, max_bytes=max_bytes)["text"]
