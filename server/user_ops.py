"""
File operations inside a user's workspace, executed *as that user*.

The daemon runs as root, but it never touches files inside a user-owned
directory with root privileges: a user could plant symlinks there to redirect a
root write onto /etc/... Instead the daemon runs this module in a child process
with the user's UID/GID (subprocess user=/group=), so every operation is limited
to what the user could already do.

Protocol: JSON request on stdin -> JSON response on stdout.
Can also be called in-process (development mode, where daemon uid == user uid).
"""
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.fsutil import atomic_write_text, atomic_write_json  # noqa: E402
else:
    from .fsutil import atomic_write_text, atomic_write_json

# <workspace_root>/<user>/            the user's root folder
#     <job-id>/                      one folder per job (named by its job ID)
#     config/agentlab.yaml           optional personal experiment settings
#     ideas_archive/                 earlier versions of research_idea.txt
WORKSPACE_SUBDIRS = ("config", "ideas_archive")
RUN_SUBDIRS = ("checkpoints", "outputs", "papers", "generated")
MAX_STATE_BYTES = 1_000_000
JOB_ID_RE = re.compile(r"^\d{8}_\d{6}_[a-z_][a-z0-9_-]{0,31}_[0-9a-f]{6}$")


def job_dir(workspace, run_id):
    """<workspace>/<job-id>; jobs created before this layout live in <workspace>/runs/<job-id>."""
    if not JOB_ID_RE.match(run_id or ""):
        raise PermissionError("invalid job id")
    path = os.path.join(workspace, run_id)
    legacy = os.path.join(workspace, "runs", run_id)
    if not os.path.isdir(path) and os.path.isdir(legacy):
        path = legacy
    if not _inside(workspace, path):
        raise PermissionError("job directory escapes workspace")
    return path


def _inside(base, path):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base + os.sep)


def _mkdir_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise PermissionError(f"refusing symlink: {path}")
    os.chmod(path, 0o700)


def op_init_workspace(workspace, metadata):
    if os.path.islink(workspace):
        raise PermissionError("workspace is a symlink")
    os.chmod(workspace, 0o700)
    for sub in WORKSPACE_SUBDIRS:
        _mkdir_private(os.path.join(workspace, sub))
    meta_path = os.path.join(workspace, "workspace.json")
    if not os.path.exists(meta_path):
        atomic_write_json(meta_path, metadata, mode=0o600)
    return {"created": [os.path.join(workspace, s) for s in WORKSPACE_SUBDIRS]}


def op_create_run(workspace, run_id, files):
    if not JOB_ID_RE.match(run_id or ""):
        raise PermissionError("invalid job id")
    run_dir = os.path.join(workspace, run_id)
    if not _inside(workspace, run_dir):
        raise PermissionError("job directory escapes workspace")
    os.mkdir(run_dir, 0o700)  # fails if it exists: never overwrite another job
    for sub in RUN_SUBDIRS:
        _mkdir_private(os.path.join(run_dir, sub))
    for name, content in files.items():
        if "/" in name or name.startswith("."):
            raise PermissionError(f"bad file name {name}")
        target = os.path.join(run_dir, name)
        if isinstance(content, str):
            atomic_write_text(target, content, mode=0o600)
        else:
            atomic_write_json(target, content, mode=0o600)
    for log in ("stdout.log", "stderr.log"):
        fd = os.open(os.path.join(run_dir, log), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    return {"run_dir": run_dir}


def op_write_run_file(workspace, run_id, name, content):
    if "/" in name or name.startswith("."):
        raise PermissionError(f"bad file name {name}")
    run_dir = job_dir(workspace, run_id)
    target = os.path.join(run_dir, name)
    if isinstance(content, str):
        atomic_write_text(target, content, mode=0o600)
    else:
        atomic_write_json(target, content, mode=0o600)
    return {"written": target}


def op_read_state(workspace, run_id):
    path = os.path.join(job_dir(workspace, run_id), "state.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {"state": None}
    with os.fdopen(fd, "r") as f:
        raw = f.read(MAX_STATE_BYTES)
    try:
        state = json.loads(raw)
    except ValueError:
        return {"state": None}
    return {"state": state if isinstance(state, dict) else None}


def op_tail_log(workspace, run_id, name, max_bytes):
    if name not in ("stdout.log", "stderr.log"):
        raise PermissionError("bad log name")
    path = os.path.join(job_dir(workspace, run_id), name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {"text": ""}
    with os.fdopen(fd, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - int(max_bytes)))
        return {"text": f.read().decode("utf-8", "replace")}


OPS = {
    "init_workspace": op_init_workspace,
    "create_run": op_create_run,
    "write_run_file": op_write_run_file,
    "read_state": op_read_state,
    "tail_log": op_tail_log,
}


def dispatch(request):
    op = OPS[request["op"]]
    return op(**request["args"])


def main():
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.read())
        result = {"ok": True, "result": dispatch(request)}
    except Exception as e:  # report, never traceback with paths of other users
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
