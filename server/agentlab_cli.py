"""
`agentlab` -- the researcher-facing command line.

Every request goes to agentlabd, which identifies the caller from the socket's
kernel credentials. There is deliberately no --user/--username option: the
operating-system account is the only identity.
"""
import argparse
import datetime
import os
import socket
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "server"

from .client import DaemonError, default_client  # noqa: E402
from .config import load_config  # noqa: E402
from .fsutil import atomic_write_text  # noqa: E402
from .permissions import current_identity  # noqa: E402
from .run_registry import valid_run_id  # noqa: E402
from .workspace_manager import format_research_idea, validate_idea  # noqa: E402

ACTIVE = ("QUEUED", "STARTING", "RUNNING", "STOPPING")

IDEA_PROMPT = """
Enter your research idea.

Include:
- the research problem
- motivation
- proposed approach
- expected contribution

Type END on a separate line when finished.
"""


def _out(msg=""):
    print(msg, flush=True)


def _ask(prompt, default=False, stream=None):
    stream = stream or sys.stdin
    sys.stdout.write(prompt)
    sys.stdout.flush()
    answer = stream.readline()
    if not answer:
        _out()
        return default
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def job_dir(workspace, run_id):
    """<root>/<user>/<job-id> (jobs from the older layout live in <user>/runs/<job-id>)."""
    path = os.path.join(workspace, run_id)
    legacy = os.path.join(workspace, "runs", run_id)
    return legacy if not os.path.isdir(path) and os.path.isdir(legacy) else path


def _fmt_time(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class CLI:
    def __init__(self, client=None, stdin=None, config=None):
        self.client = client or default_client()
        self.stdin = stdin or sys.stdin
        self.config = config or load_config()
        self.ident = current_identity()

    # ------------------------------------------------------------- helpers
    def whoami(self):
        return self.client.call("whoami")

    def ensure_workspace(self, interactive=True, greet=True):
        info = self.whoami()
        if info["workspace_exists"]:
            return info
        if greet:
            _out("Welcome to AgentLaboratory.\n")
            _out("No research workspace exists for your account.\n")
            _out(f"Workspace:\n{info['workspace']}\n")
        else:
            _out("Workspace does not exist.\n")
        if not interactive or not _ask("Create this workspace? [y/N]: ", default=False, stream=self.stdin):
            _out("\nA workspace is required to run research. No run was started.")
            _out("Create it later with:  agentlab workspace --create")
            return None
        res = self.client.call("workspace_create")
        _out("\nWorkspace created successfully." if res["created"] else "\nWorkspace already existed.")
        info["workspace_exists"] = True
        return info

    def read_idea(self):
        _out(IDEA_PROMPT)
        lines = []
        interactive = self.stdin.isatty() if hasattr(self.stdin, "isatty") else False
        while True:
            if interactive:
                sys.stdout.write("> ")
                sys.stdout.flush()
            line = self.stdin.readline()
            if not line:
                break
            if line.rstrip("\r\n") == "END":
                break
            lines.append(line.rstrip("\r\n"))
        return "\n".join(lines).strip("\n")

    def save_idea(self, workspace, idea):
        """Write <workspace>/research_idea.txt as the user, archiving the previous one."""
        path = os.path.join(workspace, self.config.idea_filename)
        if os.path.exists(path):
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = os.path.join(workspace, "ideas_archive", f"research_idea_{stamp}.txt")
            os.makedirs(os.path.dirname(archive), mode=0o700, exist_ok=True)
            with open(path) as src:
                atomic_write_text(archive, src.read(), mode=0o600)
        atomic_write_text(path, format_research_idea(self.ident.username, idea), mode=0o600)
        return path

    def follow(self, run_dir, run_id, from_start=False):
        path = os.path.join(run_dir, "stdout.log")
        _out(f"Following {path}  (Ctrl-C detaches; the run keeps going)\n")
        try:
            with open(path, "r", errors="replace") as f:
                if not from_start:
                    f.seek(max(0, os.path.getsize(path) - 4000))
                idle = 0
                while True:
                    chunk = f.read()
                    if chunk:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                        idle = 0
                        continue
                    time.sleep(1)
                    idle += 1
                    if idle % 5 == 0:
                        status = self.client.call("run_info", run_id=run_id)["status"]
                        if status not in ("QUEUED", "STARTING", "RUNNING", "STOPPING"):
                            _out(f"\nRun {run_id} finished with status {status}.")
                            return
        except KeyboardInterrupt:
            _out(f"\nDetached. The run continues. Reattach with: agentlab logs {run_id} --follow")

    # ------------------------------------------------------------ commands
    def cmd_session(self, args):
        """Default `agentlab`: identify the user, then check on a job or start new research."""
        info = self.whoami()
        _out("Welcome to AgentLaboratory\n")
        _out(f"User: {info['username']}")
        _out(f"Workspace:\n{info['workspace']}\n")
        if not info["workspace_exists"]:  # new user: create their folder, then their first research job
            if self.ensure_workspace(greet=False) is None:
                return 1
            return self.cmd_start(argparse.Namespace(config=None, idea_file=None, follow=False, name=None))
        runs = sorted(self.client.call("list_runs", all=False)["runs"], key=lambda r: r.get("created_time") or "")
        if not runs:
            _out(f"No research jobs found for {info['username']} yet. Let's start your first one.\n")
            return self.cmd_start(argparse.Namespace(config=None, idea_file=None, follow=False, name=None))
        active = [r for r in runs if r["status"] in ACTIVE]
        _out(f"Welcome back, {info['username']}. You have {len(runs)} research job(s), {len(active)} active:\n")
        self._print_jobs(runs)
        while True:
            choice = (self._readline("[c] check on an existing job   [n] start new research   [q] quit\nChoice [c]: ")
                      or "c").lower()
            if choice in ("c", "n", "q"):
                break
        if choice == "q":
            return 0
        if choice == "n":
            return self.cmd_start(argparse.Namespace(config=None, idea_file=None, follow=False, name=None))
        return self._check(runs)

    def _readline(self, prompt):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return self.stdin.readline().strip()

    def _print_jobs(self, runs):
        _out(f"  {'#':<3} {'JOB ID':<34} {'NAME':<20} {'STATUS':<12} {'PHASE':<30} CREATED")
        _out("  " + "-" * 115)
        for i, r in enumerate(runs, 1):
            phase = r.get("current_phase") or "-"
            if r.get("current_step") is not None and phase not in ("-", "done"):
                phase += f" (step {r['current_step']})"
            if r["status"] == "QUEUED" and r.get("waiting_reason"):
                phase = "waiting: " + r["waiting_reason"]
            _out(f"  {i:<3} {r['run_id']:<34} {(r.get('name') or '-')[:19]:<20} {r['status']:<12} {phase[:29]:<30} "
                 f"{_fmt_time(r.get('created_time'))}")
        _out("")

    def _check(self, runs, ref=None):
        while True:
            if ref is None:
                ref = self._readline(f"Enter job ID (or # from the list) [{len(runs)}]: ") or str(len(runs))
            match = None
            if ref.isdigit() and 1 <= int(ref) <= len(runs):
                match = runs[int(ref) - 1]
            else:
                hits = [r for r in runs if r["run_id"] == ref] or [r for r in runs if r["run_id"].startswith(ref)]
                match = hits[0] if len(hits) == 1 else None
                if len(hits) > 1:
                    _out(f"  '{ref}' matches {len(hits)} jobs; type more of the job ID or use its # from the list.")
                    ref = None
                    continue
            if match:
                break
            _out(f"  No job '{ref}' in your workspace.")
            ref = None
        return self.cmd_check(argparse.Namespace(run_id=match["run_id"]))

    def cmd_check(self, args):
        """Show one job and offer the sensible next step (follow / resume / outputs)."""
        ref = getattr(args, "run_id", None)
        if not ref or not valid_run_id(ref):  # no ID, a list number or an ID prefix: resolve among your jobs
            runs = sorted(self.client.call("list_runs", all=False)["runs"], key=lambda r: r.get("created_time") or "")
            if not runs:
                _out("You have no research jobs yet. Start one with: agentlab start")
                return 0
            if not ref:
                self._print_jobs(runs)
            return self._check(runs, ref=ref)
        r = self.client.call("run_info", run_id=args.run_id)
        info = self.whoami()
        run_dir = r.get("run_dir") or job_dir(info["workspace"], r["run_id"])
        _out("\n" + "=" * 60)
        _out(f"Job ID:          {r['run_id']}")
        _out(f"Name:            {r.get('name') or '-'}")
        _out(f"Folder:          {run_dir}")
        _out(f"Status:          {r['status']}" + (f"  (waiting: {r['waiting_reason']})" if r.get("waiting_reason") else ""))
        _out(f"Current phase:   {r.get('current_phase') or '-'} (step {r.get('current_step') if r.get('current_step') is not None else '-'})")
        _out(f"Last completed:  {r.get('last_completed_phase') or '-'}")
        _out(f"Last checkpoint: {_fmt_time(r.get('checkpoint_time'))}")
        _out(f"Created:         {_fmt_time(r.get('created_time'))}   resumes: {r.get('resume_count', 0)}")
        _out("=" * 60)
        log = os.path.join(run_dir, "stdout.log")
        if os.path.exists(log):
            with open(log, errors="replace") as f:
                tail = f.readlines()[-12:]
            if tail:
                _out("Last lines of the job log:")
                _out("".join("  | " + line for line in tail).rstrip())
        _out("")
        self.client.call("audit_event", event="session_attach", fields={"run_id": r["run_id"]})
        if r["status"] in ACTIVE:
            if _ask("This job is active. Follow its log now? [Y/n]: ", default=True, stream=self.stdin):
                self.follow(run_dir, r["run_id"])
            return 0
        if r["status"] in ("INTERRUPTED", "FAILED", "STOPPED"):
            if _ask("Resume this job from its last checkpoint? [Y/n]: ", default=True, stream=self.stdin):
                return self.cmd_resume(argparse.Namespace(run_id=r["run_id"], follow=False))
            _out(f"Not resumed. Later: agentlab resume {r['run_id']}")
            return 0
        if r["status"] == "COMPLETED":
            _out(f"Results: {os.path.join(run_dir, 'papers')}  and  {os.path.join(run_dir, 'outputs')}")
        return 0

    def cmd_start(self, args):
        info = self.ensure_workspace()
        if info is None:
            return 1
        if args.idea_file:
            with open(args.idea_file) as f:
                idea = f.read()
        else:
            idea = self.read_idea()
        try:
            validate_idea(idea, self.config.get("research", "max_idea_bytes"))
        except Exception as e:
            _out(f"\n{e}. Nothing was submitted.")
            return 1
        path = self.save_idea(info["workspace"], idea)
        self.client.call("audit_event", event="research_submitted", fields={"file": os.path.basename(path)})
        _out("\nResearch idea saved.\n")
        config_text = None
        config_path = args.config or os.path.join(info["workspace"], "config", "agentlab.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                config_text = f.read()
            _out(f"Using experiment configuration: {config_path}")
        _out("Starting AgentLaboratory...\n")
        name = getattr(args, "name", None)
        if name is None and not args.idea_file:
            name = self._readline("Short name for this job (optional, e.g. math-verifiers): ") or None
        run = self.client.call("create_run", idea_text=idea, config_text=config_text, name=name,
                               client_host=os.environ.get("SSH_CLIENT", "").split(" ")[0] or socket.gethostname())
        _out(f"Run ID:\n{run['run_id']}\n")
        _out(f"Job folder:\n{job_dir(info['workspace'], run['run_id'])}\n")
        if run["status"] == "QUEUED":
            _out("Your run is QUEUED waiting for server resources:")
            _out(f"    {run.get('waiting_reason') or 'waiting for a free slot'}")
            _out("It will start automatically when resources are available.\n")
        else:
            _out("AgentLaboratory is running.\n")
        _out("If your SSH session disconnects, your research will continue.\nReconnect later using:\n")
        _out("    agentlab status")
        _out(f"    agentlab resume {run['run_id']}\n")
        if args.follow:
            run_dir = job_dir(info["workspace"], run["run_id"])
            self.follow(run_dir, run["run_id"], from_start=True)
        return 0

    def cmd_status(self, args):
        data = self.client.call("status")
        _out("AgentLaboratory Active Users")
        _out("============================\n")
        _out(f"{'USER':<10} {'LOGIN TIME':<20} {'RUN ID':<34} {'STATUS':<12} {'PHASE'}")
        _out("-" * 100)
        for row in sorted(data["rows"], key=lambda r: (r["user"], r.get("run_id") or "")):
            phase = row.get("current_phase") or ""
            if row.get("current_step") is not None and phase:
                phase += f" (step {row['current_step']})"
            if row.get("waiting_reason") and row["status"] == "QUEUED":
                phase = "waiting: " + row["waiting_reason"]
            _out(f"{row['user']:<10} {_fmt_time(row.get('login_time')):<20} {row.get('run_id') or '-':<34} "
                 f"{row['status']:<12} {phase}")
        _out(f"\nTotal active users: {data['total_active_users']}")
        _out(f"Total active research runs: {data['total_active_runs']}")
        return 0

    def cmd_runs(self, args):
        runs = self.client.call("list_runs", all=False)["runs"]
        if not runs:
            _out("You have no research runs yet. Start one with: agentlab start")
            return 0
        _out(f"{'RUN ID':<34} {'STATUS':<12} {'CREATED':<17} {'ENDED':<17} {'COMMIT':<10} {'RESUMES'}")
        _out("-" * 100)
        for r in sorted(runs, key=lambda r: r.get("created_time") or ""):
            _out(f"{r['run_id']:<34} {r['status']:<12} {_fmt_time(r.get('created_time')):<17} "
                 f"{_fmt_time(r.get('end_time')):<17} {(r.get('git_commit') or '-')[:8]:<10} {r.get('resume_count', 0)}")
        return 0

    def cmd_info(self, args):
        r = self.client.call("run_info", run_id=args.run_id)
        for k in ("run_id", "status", "run_dir", "model_backend", "git_commit", "created_time", "start_time",
                  "end_time", "current_phase", "current_step", "last_completed_phase", "checkpoint_time",
                  "resume_count", "waiting_reason", "exit_code"):
            _out(f"{k:<22} {r.get(k) if r.get(k) is not None else '-'}")
        _out("\nHistory:")
        for h in r.get("history", []):
            _out(f"  {_fmt_time(h['time'])}  {h['status']:<12} {h.get('reason', '')}")
        return 0

    def cmd_history(self, args):
        events = self.client.call("history", limit=args.limit)["events"]
        for e in events:
            extra = " ".join(f"{k}={e[k]}" for k in ("run_id", "status", "reason", "file", "rhost") if e.get(k))
            _out(f"{_fmt_time(e['timestamp'])}  {e['event']:<22} {extra}")
        if not events:
            _out("No activity recorded for your account yet.")
        return 0

    def cmd_workspace(self, args):
        info = self.whoami()
        if args.create and not info["workspace_exists"]:
            res = self.client.call("workspace_create")
            _out("Workspace created successfully." if res["created"] else "Workspace already existed.")
            info = self.whoami()
        _out(f"User:      {info['username']} (uid {info['uid']})")
        _out(f"Workspace: {info['workspace']}")
        _out(f"Exists:    {'yes' if info['workspace_exists'] else 'no  (create with: agentlab workspace --create)'}")
        if info["workspace_exists"]:
            idea = os.path.join(info["workspace"], self.config.idea_filename)
            _out(f"Idea file: {idea if os.path.exists(idea) else '(none yet)'}")
            _out(f"Config:    {os.path.join(info['workspace'], 'config', 'agentlab.yaml')} (optional override)")
        return 0

    def cmd_resume(self, args):
        r = self.client.call("resume", run_id=args.run_id)
        _out(f"Run {r['run_id']} queued for resume from its latest checkpoint (status {r['status']}).")
        if r.get("waiting_reason"):
            _out(f"Waiting for: {r['waiting_reason']}")
        _out("Check progress with: agentlab status")
        if getattr(args, "follow", False):
            info = self.whoami()
            self.follow(job_dir(info["workspace"], r["run_id"]), r["run_id"])
        return 0

    def cmd_stop(self, args):
        r = self.client.call("stop", run_id=args.run_id)
        _out(f"Stop requested for {r['run_id']} (status {r['status']}). State and checkpoints are preserved;"
             f" resume later with: agentlab resume {r['run_id']}")
        return 0

    def cmd_logs(self, args):
        info = self.whoami()
        run_dir = job_dir(info["workspace"], args.run_id)
        if not os.path.isdir(run_dir):
            _out(f"No such run in your workspace: {args.run_id}")
            return 1
        if args.follow:
            self.follow(run_dir, args.run_id)
            return 0
        name = "stderr.log" if args.stderr else "stdout.log"
        with open(os.path.join(run_dir, name), errors="replace") as f:
            data = f.read()
        sys.stdout.write(data[-args.bytes:])
        return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agentlab", description="AgentLaboratory shared research server")
    sub = p.add_subparsers(dest="command")
    s = sub.add_parser("start", help="submit a research idea and start a run")
    s.add_argument("--config", help="experiment YAML (no API keys); default: <workspace>/config/agentlab.yaml or server default")
    s.add_argument("--idea-file", help="read the idea from a file instead of the prompt")
    s.add_argument("--follow", action="store_true", help="stream the run log after starting")
    s.add_argument("--name", help="short name for this job (shown in listings)")
    s.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    ck = sub.add_parser("check", help="check on one of your jobs (by job ID or pick from a list)")
    ck.add_argument("run_id", nargs="?")
    sub.add_parser("status", help="active users and runs")
    h = sub.add_parser("history", help="your recorded activity")
    h.add_argument("-n", "--limit", type=int, default=50)
    w = sub.add_parser("workspace", help="show (or create) your workspace")
    w.add_argument("--create", action="store_true")
    sub.add_parser("runs", help="list your runs")
    i = sub.add_parser("info", help="details for one run")
    i.add_argument("run_id")
    r = sub.add_parser("resume", help="resume an interrupted run from its latest checkpoint")
    r.add_argument("run_id")
    r.add_argument("--follow", action="store_true")
    st = sub.add_parser("stop", help="gracefully stop one of your runs (checkpoint kept)")
    st.add_argument("run_id")
    lg = sub.add_parser("logs", help="show or follow a run's log")
    lg.add_argument("run_id")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("--stderr", action="store_true")
    lg.add_argument("--bytes", type=int, default=20000)
    return p


def main(argv=None, client=None, stdin=None):
    args = build_parser().parse_args(argv)
    cli = CLI(client=client, stdin=stdin)
    handlers = {None: cli.cmd_session, "start": cli.cmd_start, "status": cli.cmd_status, "history": cli.cmd_history,
                "workspace": cli.cmd_workspace, "runs": cli.cmd_runs, "info": cli.cmd_info, "resume": cli.cmd_resume,
                "stop": cli.cmd_stop, "logs": cli.cmd_logs, "check": cli.cmd_check}
    try:
        return handlers[args.command](args)
    except DaemonError as e:
        _out(f"agentlab: {e}")
        return 2
    except KeyboardInterrupt:
        _out("\nCancelled.")
        return 130
    except BrokenPipeError:  # output piped into head/less that exited early
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
