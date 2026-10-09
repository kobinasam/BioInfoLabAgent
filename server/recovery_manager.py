"""
Reconciliation of persisted run state with the processes that actually exist.

Runs at daemon start (covers host reboot and daemon restart) and on every
monitor tick. A run is only considered alive if its recorded process identity
(boot_id + pid + create_time, or systemd unit state) still matches; otherwise
its state is moved RUNNING -> INTERRUPTED/COMPLETED/FAILED. Checkpoints are
never touched, so every interrupted run stays resumable.
"""
from . import run_registry as rr

EX_TEMPFAIL = 75  # research_runner exits with this after a graceful signal stop


def status_from_exit(code, stop_requested):
    if code == 0:
        return rr.COMPLETED
    if code in (EX_TEMPFAIL, 130, 137, 143):
        return rr.STOPPED if stop_requested else rr.INTERRUPTED
    return rr.FAILED


def determine_final_status(record, exit_info, worker_state):
    """Pick the final status for a run whose process is gone."""
    stop_requested = bool(record.get("stop_requested"))
    if exit_info.get("known"):
        return status_from_exit(exit_info["code"], stop_requested), f"exit code {exit_info['code']}"
    # Exit status lost (daemon restarted, host rebooted, unit garbage-collected).
    hint = (worker_state or {}).get("status")
    if hint == "completed":
        return rr.COMPLETED, "worker reported completion"
    if stop_requested:
        return rr.STOPPED, "stop requested; exit status unavailable"
    return rr.INTERRUPTED, "process disappeared (exit status unavailable)"


def reconcile_on_startup(daemon):
    """Called once when the daemon starts."""
    summary = {"checked": 0, "still_running": [], "interrupted": [], "finalized": [], "queued": []}
    for rec in daemon.registry.active():
        summary["checked"] += 1
        run_id = rec["run_id"]
        if rec["status"] == rr.QUEUED:
            summary["queued"].append(run_id)
            continue
        handle = rec.get("process") or {}
        if handle and daemon.launcher.is_alive(handle, run_id):
            daemon.resources.restore(run_id, rec.get("allocation"))
            if rec["status"] == rr.STARTING:
                daemon.registry.transition(run_id, rr.RUNNING, reason="confirmed after daemon restart")
            summary["still_running"].append(run_id)
            continue
        final = daemon.finalize_run(rec, reason_prefix="reconciled at startup")
        (summary["interrupted"] if final == rr.INTERRUPTED else summary["finalized"]).append(run_id)
    daemon.audit.log("server_reconciliation", **summary)
    return summary
