"""
Identity and authorization.

Identity always comes from the operating system: the current process's real UID
(for the CLIs) or the kernel-supplied peer credentials of a Unix socket (for the
daemon). Usernames passed on a command line or inside a request are never trusted.
Administrator rights come from membership of a Linux group, never from a list of
usernames in Python.
"""
import grp
import os
import pwd
import re
import socket
import struct
import sys
from dataclasses import dataclass

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int
    username: str
    pid: int = None

    @property
    def is_root(self):
        return self.uid == 0


def valid_username(name):
    return bool(name) and bool(USERNAME_RE.match(name))


def identity_from_uid(uid, gid=None, pid=None):
    pw = pwd.getpwuid(uid)
    return Identity(uid=uid, gid=pw.pw_gid if gid is None else gid, username=pw.pw_name, pid=pid)


def current_identity():
    """Identity of this process, derived from the real UID (not $USER)."""
    return identity_from_uid(os.getuid(), os.getgid(), os.getpid())


def peer_identity(sock):
    """Kernel-verified identity of the process on the other end of a Unix socket."""
    if hasattr(socket, "SO_PEERCRED"):  # Linux
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return identity_from_uid(uid, gid, pid)
    if hasattr(socket, "LOCAL_PEERCRED"):  # macOS/BSD (development only)
        # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
        size = struct.calcsize("IIh16I") + 8
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, size)
        _version, uid = struct.unpack_from("II", raw)
        pid = None
        if hasattr(socket, "LOCAL_PEERPID"):
            pid = struct.unpack("i", sock.getsockopt(0, socket.LOCAL_PEERPID, 4))[0]
        return identity_from_uid(uid, pid=pid)
    raise OSError(f"peer credentials are not supported on {sys.platform}")


def user_groups(username):
    """Names of all groups (primary + supplementary) the user belongs to."""
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return set()
    names = set()
    for gid in os.getgrouplist(username, pw.pw_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def in_group(username, group_name):
    return group_name in user_groups(username)


class Authorizer:
    """Policy decisions, evaluated inside the daemon against peer identity."""

    def __init__(self, config, group_lookup=None):
        self.config = config
        self._group_lookup = group_lookup or user_groups

    def is_admin(self, ident):
        return ident.is_root or self.config.admins_group in self._group_lookup(ident.username)

    def is_lab_user(self, ident):
        if self.is_admin(ident):
            return True
        return self.config.users_group in self._group_lookup(ident.username)

    def can_manage_run(self, ident, run_record):
        return self.is_admin(ident) or run_record.get("uid") == ident.uid
