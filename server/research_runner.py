"""
Research worker: executes one run of the AgentLaboratory pipeline.

Started by agentlabd (as a transient systemd unit running as the researcher),
never by the SSH shell. It

  * runs entirely inside <workspace_root>/<user>/<job_id>/ (cwd = generated/), so the
    pipeline's cwd-relative files (Figure_*.png, downloaded papers, ...) can
    never collide with another run or another user
  * redirects its own stdout/stderr to the run's log files (opened as the user,
    O_NOFOLLOW -- never by root)
  * reads the topic from the run's research_idea.txt copy
  * checkpoints the LaboratoryWorkflow after every subtask and records per-step
    progress in state.json (atomic writes)
  * on --resume, loads the newest checkpoint that verifies and continues
  * converts SIGTERM/SIGINT into a graceful stop (state + exit 75); ignores SIGHUP
  * receives only a per-run proxy token, never a real provider API key

Exit codes: 0 completed, 75 stopped/interrupted by signal (resumable), 1 failed.
"""
import argparse
import json
import os
import random
import shutil
import signal
import socket
import sys
import time
import traceback

APP_ROOT = os.environ.get("AGENTLAB_APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from server.checkpoint_manager import CheckpointManager  # noqa: E402
from server.secret_guard import redact_text  # noqa: E402
from server.workspace_manager import parse_research_idea  # noqa: E402

EX_OK, EX_FAIL, EX_TEMPFAIL = 0, 1, 75
PROXY_KEY_VARS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")


class GracefulStop(BaseException):
    """Raised in the main thread when a stop signal arrives."""


_stop_signal = {"value": None, "critical": 0, "pending": None}


def _on_stop(signum, frame):
    if _stop_signal["value"] is not None:
        return
    _stop_signal["value"] = signum
    if _stop_signal["critical"]:
        _stop_signal["pending"] = signum  # raised when the checkpoint write finishes
        return
    raise GracefulStop(signal.Signals(signum).name)


def critical(fn):
    """Defer stop signals while `fn` runs, so checkpoint/state writes always complete."""
    def wrapper(*a, **kw):
        _stop_signal["critical"] += 1
        try:
            return fn(*a, **kw)
        finally:
            _stop_signal["critical"] -= 1
            pending = _stop_signal["pending"]
            if not _stop_signal["critical"] and pending is not None:
                _stop_signal["pending"] = None
                raise GracefulStop(signal.Signals(pending).name)
    return wrapper


def terminate_children():
    """Stop code-execution subprocesses so interpreter exit does not wait on them."""
    import multiprocessing
    for child in multiprocessing.active_children():
        child.terminate()
        child.join(5)


def _on_hup(signum, frame):
    print(f"[agentlab] SIGHUP received at {time.strftime('%H:%M:%S')}: ignored (run is detached from SSH)", flush=True)


def install_signal_handlers():
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGHUP, _on_hup)


def redirect_output(run_dir):
    for name, fd_target in (("stdout.log", 1), ("stderr.log", 2)):
        fd = os.open(os.path.join(run_dir, name), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.dup2(fd, fd_target)
        os.close(fd)
    sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
    sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)


def load_proxy_token(run_dir):
    """Per-run token issued by agentlabd. It only works against the local proxy, only while
    this run is active, and is never a real provider key."""
    path = os.path.join(run_dir, "proxy_token")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd) as f:
        token = f.read().strip()
    try:
        os.unlink(path)
    except OSError:
        pass
    for var in PROXY_KEY_VARS:
        os.environ[var] = token
    return token


def notify_daemon(event, **fields):
    """Best effort: submit an audit event (identity is stamped by the daemon)."""
    try:
        from server.client import DaemonClient
        DaemonClient().call("audit_event", event=event, fields=fields, timeout=5)
    except Exception:
        pass


def read_run_config(run_dir):
    import yaml
    with open(os.path.join(run_dir, "config.yaml")) as f:
        return yaml.safe_load(f) or {}


def read_topic(run_dir):
    with open(os.path.join(run_dir, "research_idea.txt")) as f:
        return parse_research_idea(f.read())


def seed_everything(seed):
    random.seed(seed)
    try:
        import numpy as np
        np.random.seed(seed % (2**32))
    except Exception:
        pass


# ---------------------------------------------------------------- pipelines
def run_agentlab_pipeline(run_dir, run_id, ckpt, resume):
    """The real AgentLaboratory LaboratoryWorkflow, unchanged research logic."""
    os.chdir(os.path.join(run_dir, "generated"))
    import agents
    import ai_lab_repo

    cfg = ai_lab_repo.parse_yaml_data(read_run_config(run_dir))

    def as_bool(v):
        return v.lower() == "true" if isinstance(v, str) else bool(v)

    lab_dir = os.path.join(run_dir, "outputs", "research_dir")
    for sub in ("", "src", "tex"):
        os.makedirs(os.path.join(lab_dir, sub), exist_ok=True)

    lab = None
    if resume:
        lab, pointer = ckpt.load_latest()
        if lab is not None:
            print(f"[agentlab] Resuming {run_id} from checkpoint {pointer.get('file')} "
                  f"(completed: {[k for k, v in lab.phase_status.items() if v]})", flush=True)
            lab.lab_dir = lab_dir
        else:
            print("[agentlab] No valid checkpoint found; starting from the beginning.", flush=True)

    if lab is None:
        topic = read_topic(run_dir)
        lab = ai_lab_repo.LaboratoryWorkflow(
            research_topic=topic,
            notes=ai_lab_repo.build_task_notes(cfg.task_notes, cfg.language),
            agent_model_backbone=ai_lab_repo.build_agent_models(cfg.llm_backend),
            human_in_loop_flag=ai_lab_repo.build_human_in_loop(False),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            compile_pdf=as_bool(cfg.compile_latex),
            num_papers_lit_review=int(cfg.num_papers_lit_review),
            papersolver_max_steps=int(cfg.papersolver_max_steps),
            mlesolver_max_steps=int(cfg.mlesolver_max_steps),
            paper_index=0,
            except_if_fail=as_bool(cfg.except_if_fail),
            agentRxiv=False,
            lab_index=0,
            lab_dir=lab_dir,
        )
    else:
        # token rotates on every launch
        lab.openai_api_key = os.environ.get("OPENAI_API_KEY")
        lab.set_agent_attr("openai_api_key", lab.openai_api_key)
        lab.reviewers.openai_api_key = lab.openai_api_key

    def checkpoint_hook(workflow, subtask):
        done = [k for k, v in workflow.phase_status.items() if v]
        ckpt.save(workflow, subtask, completed_subtasks=done)
        print(f"[agentlab] checkpoint saved after '{subtask}'", flush=True)

    ai_lab_repo.CHECKPOINT_HOOK = checkpoint_hook
    agents.PROGRESS_HOOK = ckpt.progress
    lab.perform_research()

    papers = os.path.join(run_dir, "papers")
    for name in ("report.txt", "readme.md", os.path.join("tex", "temp.pdf"), os.path.join("tex", "temp.tex")):
        src = os.path.join(lab_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(papers, os.path.basename(name)))


SIM_PHASES = ["literature review", "plan formulation", "data preparation", "running experiments",
              "results interpretation", "report writing", "report refinement"]


def run_simulated_pipeline(run_dir, run_id, ckpt, resume):
    """Deterministic stand-in for the LLM pipeline, used by tests and installer health checks.
    Exercises exactly the same checkpoint/resume/signal machinery."""
    cfg = read_run_config(run_dir)
    steps = int(cfg.get("simulated-steps-per-phase", 3))
    delay = float(cfg.get("simulated-step-seconds", 0.2))
    fail_at = cfg.get("simulated-fail-at")  # "phase:step" -> crash once (simulates a process failure)
    os.chdir(os.path.join(run_dir, "generated"))
    work = None
    if resume:
        work, _ = ckpt.load_latest()
    work = work or {"completed": [], "outputs": {}, "topic": read_topic(run_dir)}
    for phase in SIM_PHASES:
        if phase in work["completed"]:
            continue
        for step in range(steps):
            ckpt.progress(phase, step)
            marker = os.path.join(run_dir, ".simulated_failure_done")
            if fail_at == f"{phase}:{step}" and not os.path.exists(marker):
                open(marker, "w").close()
                os._exit(EX_FAIL)  # abrupt crash: no cleanup, no state update
            time.sleep(delay)
        work["outputs"][phase] = f"{phase} output for: {work['topic'][:60]}"
        work["completed"].append(phase)
        ckpt.save(work, phase, completed_subtasks=work["completed"])
        print(f"[agentlab] checkpoint saved after '{phase}'", flush=True)
    with open(os.path.join(run_dir, "papers", "report.txt"), "w") as f:
        f.write("\n".join(work["outputs"][p] for p in SIM_PHASES) + "\n")


PIPELINES = {"agentlab": run_agentlab_pipeline, "simulated": run_simulated_pipeline}


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentLaboratory research worker (started by agentlabd)")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)

    run_dir = os.path.realpath(args.run_dir)
    redirect_output(run_dir)
    install_signal_handlers()
    load_proxy_token(run_dir)
    seed_everything(int(os.environ.get("AGENTLAB_SEED", "0")))

    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    ckpt = CheckpointManager(run_dir, args.run_id, user)
    ckpt.save = critical(ckpt.save)
    ckpt.update_state = critical(ckpt.update_state)
    resume_count = int(ckpt.state.get("resume_count", 0)) + (1 if args.resume else 0)
    ckpt.update_state(status="running", pid=os.getpid(), host=socket.gethostname(),
                      resume_supported=True, resume_count=resume_count)
    pipeline = os.environ.get("AGENTLAB_PIPELINE", "agentlab")
    banner = "Resuming" if args.resume else "Starting"
    print(f"\n[agentlab] {banner} run {args.run_id} (pipeline={pipeline}, pid={os.getpid()}) "
          f"at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    try:
        PIPELINES[pipeline](run_dir, args.run_id, ckpt, args.resume)
    except GracefulStop as e:
        terminate_children()
        ckpt.update_state(status="interrupted", stop_signal=str(e))
        print(f"[agentlab] {e} received: state saved, last checkpoint kept. Resume with: agentlab resume {args.run_id}",
              flush=True)
        return EX_TEMPFAIL
    except Exception as e:
        terminate_children()
        msg = redact_text(f"{type(e).__name__}: {e}")
        ckpt.update_state(status="failed", error=msg[:1000])
        sys.stderr.write(redact_text(traceback.format_exc()))
        if "Max retries" in msg or "API" in msg or "api" in msg:
            notify_daemon("api_error", run_id=args.run_id, error=msg[:300])
        return EX_FAIL
    ckpt.update_state(status="completed", current_phase="done")
    print(f"[agentlab] Run {args.run_id} completed at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
