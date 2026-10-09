"""
`agentlab-admin` -- administrator interface.

Authorization is enforced by agentlabd (membership of the admins group, checked
against the kernel-verified socket peer). The local group check below only
gives a friendlier error message; it is not a security boundary.
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "server"

from .agentlab_cli import CLI, _fmt_time, _out  # noqa: E402
from .client import DaemonError, default_client  # noqa: E402
from .config import load_config  # noqa: E402
from .permissions import current_identity, in_group  # noqa: E402


def cmd_status(client, args):
    return CLI(client=client).cmd_status(args)


def cmd_running(client, args):
    runs = [r for r in client.call("list_runs", all=True)["runs"]
            if r["status"] in ("QUEUED", "STARTING", "RUNNING", "STOPPING")]
    _out(f"{'USER':<10} {'RUN ID':<34} {'STATUS':<10} {'STARTED':<17} {'PHASE':<26} {'BACKEND'}")
    _out("-" * 110)
    for r in runs:
        phase = (r.get("current_phase") or r.get("waiting_reason") or "-")[:25]
        _out(f"{r['user']:<10} {r['run_id']:<34} {r['status']:<10} {_fmt_time(r.get('start_time')):<17} {phase:<26} {r.get('model_backend') or '-'}")
    _out(f"\n{len(runs)} active run(s)")
    return 0


def cmd_runs(client, args):
    runs = client.call("list_runs", all=True)["runs"]
    for r in sorted(runs, key=lambda r: r.get("created_time") or ""):
        _out(f"{r['user']:<10} {r['run_id']:<34} {r['status']:<12} {_fmt_time(r.get('created_time'))}  {(r.get('git_commit') or '-')[:10]}")
    return 0


def cmd_users(client, args):
    users = client.call("admin_users")["users"]
    _out(f"{'USER':<12} {'LOGGED IN':<10} {'ACTIVE':<7} {'TOTAL RUNS'}")
    for u in users:
        _out(f"{u['user']:<12} {'yes' if u['logged_in'] else 'no':<10} {u['active']:<7} {u['runs']}")
    return 0


def cmd_history(client, args):
    res = client.call("history", all=not args.user, user=args.user, limit=args.limit)
    for e in res["events"]:
        extra = " ".join(f"{k}={e[k]}" for k in ("run_id", "status", "reason", "actor", "rhost") if e.get(k))
        _out(f"{_fmt_time(e['timestamp'])}  {e.get('user', '-'):<10} {e['event']:<22} {extra}")
    return 0


def cmd_stop(client, args):
    r = client.call("stop", run_id=args.run_id)
    _out(f"Stop requested for {r['run_id']} owned by {r['user']} (status {r['status']}). Audited as an admin action.")
    return 0


def cmd_logs(client, args):
    res = client.call("admin_logs", limit=args.limit, user=args.user, run_id=args.run_id,
                      events=args.event or None)
    for e in res["events"]:
        _out(json.dumps(e, sort_keys=True) if args.json else
             f"{e['timestamp']}  {e.get('user', '-'):<10} {e['event']:<24} " +
             " ".join(f"{k}={v}" for k, v in e.items() if k not in ("timestamp", "user", "event", "host", "prev", "hash")))
    return 0


def cmd_run_log(client, args):
    res = client.call("tail", run_id=args.run_id, stderr=args.stderr, bytes=args.bytes)
    sys.stdout.write(res["text"])
    return 0


def cmd_deployments(client, args):
    deps = client.call("admin_deployments")["deployments"]
    _out(f"{'TIME':<17} {'DEPLOYER':<10} {'PREVIOUS':<12} {'NEW':<12} {'BRANCH':<8} TESTS")
    for d in deps:
        _out(f"{_fmt_time(d['time']):<17} {d.get('deployer', '-'):<10} {d.get('previous', '')[:10]:<12} "
             f"{d.get('commit', '')[:10]:<12} {d.get('branch', ''):<8} {d.get('tests', '')}")
    return 0


def cmd_verify_log(client, args):
    r = client.call("admin_verify_log")
    _out(f"Audit log hash chain: {'OK' if r['ok'] else 'TAMPERING DETECTED'} ({r['records']} records verified)")
    if not r["ok"]:
        _out(f"First bad line: {r['bad_line']} -- {r['message']}")
    return 0 if r["ok"] else 1


def cmd_health(client, args):
    _out(json.dumps(client.call("admin_health"), indent=2, default=str))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agentlab-admin", description="AgentLaboratory administration")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("users")
    sub.add_parser("running")
    sub.add_parser("runs")
    h = sub.add_parser("history")
    h.add_argument("--user")
    h.add_argument("-n", "--limit", type=int, default=100)
    s = sub.add_parser("stop")
    s.add_argument("run_id")
    lg = sub.add_parser("logs", help="central audit log")
    lg.add_argument("-n", "--limit", type=int, default=100)
    lg.add_argument("--user")
    lg.add_argument("--run-id")
    lg.add_argument("--event", action="append")
    lg.add_argument("--json", action="store_true")
    rl = sub.add_parser("run-log", help="any run's stdout/stderr")
    rl.add_argument("run_id")
    rl.add_argument("--stderr", action="store_true")
    rl.add_argument("--bytes", type=int, default=20000)
    sub.add_parser("deployments")
    sub.add_parser("verify-log")
    sub.add_parser("health")
    return p


def main(argv=None, client=None):
    args = build_parser().parse_args(argv)
    ident = current_identity()
    config = load_config()
    if not ident.is_root and not in_group(ident.username, config.admins_group):
        _out(f"agentlab-admin: you are not a member of '{config.admins_group}'.")
        return 2
    client = client or default_client()
    handlers = {"status": cmd_status, "users": cmd_users, "running": cmd_running, "runs": cmd_runs,
                "history": cmd_history, "stop": cmd_stop, "logs": cmd_logs, "run-log": cmd_run_log,
                "deployments": cmd_deployments, "verify-log": cmd_verify_log, "health": cmd_health}
    try:
        return handlers[args.command](client, args)
    except DaemonError as e:
        _out(f"agentlab-admin: {e}")
        return 2
    except BrokenPipeError:  # output piped into head/less that exited early
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
