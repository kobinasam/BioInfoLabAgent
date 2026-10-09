"""Client for the agentlabd Unix socket. Identity is taken from the socket by the daemon."""
import json
import os
import socket

from .config import load_config


class DaemonError(Exception):
    pass


class DaemonUnavailable(DaemonError):
    pass


class DaemonClient:
    def __init__(self, socket_path=None, config=None):
        if socket_path is None:
            socket_path = (config or load_config()).socket_path
        self.socket_path = socket_path

    def call(self, op, timeout=60, **args):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            raise DaemonUnavailable(
                f"the AgentLaboratory service is not running ({self.socket_path}). Contact a lab administrator.")
        except PermissionError:
            sock.close()
            raise DaemonUnavailable("permission denied connecting to the AgentLaboratory service: "
                                    "your account is probably not in the 'agentlab-users' group.")
        try:
            sock.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        try:
            resp = json.loads(buf or b"{}")
        except ValueError:
            raise DaemonError("malformed response from service")
        if not resp.get("ok"):
            raise DaemonError(resp.get("error", "request failed"))
        return resp.get("result")


def default_client():
    path = os.environ.get("AGENTLAB_SOCKET")  # only changes *where* we connect, never who we are
    return DaemonClient(socket_path=path) if path else DaemonClient()
