"""Atomic, crash-safe file helpers. A reader never observes a half-written file."""
import json
import os
import tempfile


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, mode=0o600):
    """Write to a temp file in the same directory, fsync, then rename over `path`."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text, mode=0o600):
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path, obj, mode=0o600):
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", mode=mode)


def read_json(path, default=None):
    """Read JSON; return `default` if the file is missing or unparseable."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
