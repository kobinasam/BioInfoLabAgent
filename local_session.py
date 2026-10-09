"""
Interactive start-up for local runs of ai_lab_repo.py.

    $ python ai_lab_repo.py
      -> who are you?            OS login name (never typed, so nobody can pick another user's jobs)
      -> returning user?         list your jobs, then
             [c] check on a job  (by job ID or list number): status, phase, log tail,
                                 follow it if running, resume it from its checkpoint if interrupted
             [n] start new research
      -> new user / new research:
             first time: create the user's root folder; type the research idea (END to finish);
             the job gets its own folder named by its job ID and starts

Layout (per user, never shared or overwritten):

    research_projects/
    ├── activity.log                      JSON lines: who did what, when
    └── <user>/                           the user's root folder
        └── <job-id>/                     one folder per job, named by its job ID
            ├── job.json                  job ID, name, owner, status, pid (for liveness checks)
            ├── research_idea.txt         the idea the agents work on
            ├── config.yaml               settings this job was started with (reused on resume)
            ├── state.json                current phase / step (updated every agent step)
            ├── checkpoints/              workflow checkpoint after every subtask (resume point)
            ├── run.log                   everything the run printed
            └── research_dir/{src,tex}/   the agents' outputs and figures
"""
import datetime
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

import psutil
import yaml

from server.checkpoint_manager import CheckpointManager
from server.fsutil import atomic_write_json, read_json
from server.run_registry import generate_run_id
from server.workspace_manager import WorkspaceError, format_research_idea, parse_research_idea, validate_idea

PROJECTS_ROOT = "research_projects"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ACTIVE = "RUNNING"

IDEA_PROMPT = """
Enter your research idea.

Include:
- the research problem
- motivation
- proposed approach
- expected contribution

Type END on a separate line when finished.
"""


# ----------------------------------------------------------------- utilities
def current_user():
    """The OS account running this process (not $USER, which anyone can change)."""
    return pwd.getpwuid(os.getuid()).pw_name


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _ask(prompt, default=""):
    try:
        answer = input(prompt)
    except EOFError:
        return default
    return answer.strip() or default


def _yes(prompt, default=True):
    return _ask(prompt, "y" if default else "n").lower() in ("y", "yes")


def log_activity(event, **fields):
    os.makedirs(PROJECTS_ROOT, exist_ok=True)
    record = {"timestamp": _now(), "event": event, "user": current_user(), "host": socket.gethostname(), **fields}
    with open(os.path.join(PROJECTS_ROOT, "activity.log"), "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _process_alive(pid, create_time):
    """Alive *and* the same process we recorded (PIDs get reused)."""
    try:
        p = psutil.Process(int(pid))
        return p.status() != psutil.STATUS_ZOMBIE and (create_time is None or abs(p.create_time() - create_time) < 1.0)
    except (psutil.Error, TypeError, ValueError):
        return False


# ----------------------------------------------------------------------- jobs
class Job:
    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.path = os.path.join(project_dir, "job.json")
        self.data = read_json(self.path) or {}

    # identity / files
    @property
    def job_id(self): return self.data.get("job_id")
    @property
    def lab_dir(self): return os.path.join(self.project_dir, "research_dir")
    @property
    def config_path(self): return os.path.join(self.project_dir, "config.yaml")
    @property
    def log_path(self): return os.path.join(self.project_dir, "run.log")

    def idea(self):
        with open(os.path.join(self.project_dir, "research_idea.txt")) as f:
            return parse_research_idea(f.read())

    def save(self, **fields):
        self.data.update(fields, last_update=_now())
        atomic_write_json(self.path, self.data, mode=0o600)

    def state(self):
        return read_json(os.path.join(self.project_dir, "state.json")) or {}

    def status(self):
        """Stored status, corrected by checking whether the recorded process really exists."""
        status = self.data.get("status", "CREATED")
        if status == ACTIVE and not _process_alive(self.data.get("pid"), self.data.get("pid_create_time")):
            status = "INTERRUPTED"  # terminal closed, machine slept/rebooted, or the process crashed
            self.save(status=status, interrupted_reason="process no longer running")
            log_activity("job_interrupted", job_id=self.job_id, reason="process disappeared")
        return status

    def has_checkpoint(self):
        d = os.path.join(self.project_dir, "checkpoints")
        return os.path.isdir(d) and any(f.endswith(".pkl") for f in os.listdir(d))


def user_jobs(user):
    root = os.path.join(PROJECTS_ROOT, user)
    jobs = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            job = Job(os.path.join(root, name))
            if job.job_id and job.data.get("user") == user:
                jobs.append(job)
    return sorted(jobs, key=lambda j: j.data.get("created", ""))


def find_job(user, ref, jobs):
    """Accept a list number (1, 2, ...) or a job ID (full or unique prefix)."""
    ref = ref.strip()
    if ref.isdigit() and 1 <= int(ref) <= len(jobs):
        return jobs[int(ref) - 1]
    matches = [j for j in jobs if j.job_id == ref] or [j for j in jobs if j.job_id.startswith(ref)]
    return matches[0] if len(matches) == 1 else None


# -------------------------------------------------------------------- display
def print_jobs(jobs):
    print(f"\n  {'#':<3} {'JOB ID':<36} {'NAME':<22} {'STATUS':<12} {'PHASE':<32} UPDATED")
    print("  " + "-" * 120)
    for i, job in enumerate(jobs, 1):
        st = job.state()
        phase = st.get("current_phase") or "-"
        if st.get("current_step") is not None and phase not in ("-", "done"):
            phase = f"{phase} (step {st['current_step']})"
        print(f"  {i:<3} {job.job_id:<36} {(job.data.get('name') or '-')[:21]:<22} {job.status():<12} "
              f"{phase[:31]:<32} {_fmt(job.data.get('last_update'))}")
    print()


def show_job(job):
    status = job.status()
    st = job.state()
    idea_first_line = job.idea().splitlines()[0] if job.idea() else ""
    print("\n" + "=" * 60)
    print(f"Job ID:          {job.job_id}")
    print(f"Name:            {job.data.get('name') or '-'}")
    print(f"Job folder:      {os.path.abspath(job.project_dir)}")
    print(f"Research idea:   {idea_first_line[:70]}")
    print(f"Status:          {status}")
    print(f"Current phase:   {st.get('current_phase') or '-'} (step {st.get('current_step', '-')})")
    print(f"Completed:       {', '.join(st.get('completed_subtasks') or []) or '-'}")
    print(f"Last checkpoint: {_fmt(st.get('checkpoint_time'))}")
    print(f"Started:         {_fmt(job.data.get('created'))}   resumes: {job.data.get('resume_count', 0)}")
    if job.data.get("error"):
        print(f"Last error:      {job.data['error'][:200]}")
    print("=" * 60)
    if os.path.exists(job.log_path):
        with open(job.log_path, errors="replace") as f:
            tail = f.readlines()[-15:]
        print("Last lines of run.log:")
        print("".join("  | " + line for line in tail).rstrip())
    print()
    return status


def follow_log(job):
    print(f"Following {job.log_path}  (Ctrl-C to stop watching; the job keeps running)\n")
    try:
        with open(job.log_path, errors="replace") as f:
            f.seek(max(0, os.path.getsize(job.log_path) - 3000))
            while True:
                chunk = f.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                elif job.status() != ACTIVE:
                    print(f"\nJob finished with status {job.status()}.")
                    return
                else:
                    time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped watching.")


# -------------------------------------------------------------- new research
def ensure_user_folder(user):
    """The user's root folder research_projects/<user>/ (created on first use, private to them)."""
    base = os.path.join(PROJECTS_ROOT, user)
    if not os.path.isdir(base):
        print(f"\nYour root folder:\n{os.path.abspath(base)}\n")
        if not _yes("Create it? [Y/n]: ", default=True):
            print("A root folder is required. Nothing was started.")
            sys.exit(1)
        os.makedirs(base, mode=0o700)
        print("Root folder created.\n")
        log_activity("user_folder_created", folder=os.path.abspath(base))
    os.chmod(base, 0o700)
    return base


def ask_job_name(name=None):
    while True:
        if name is None:
            name = _ask("Short name for this job (optional, e.g. math-verifiers): ", "")
        if not name or NAME_RE.match(name):
            return name or None
        print("  Use letters, digits, '-' or '_' only (max 64 characters).")
        name = None


def read_research_idea():
    print(IDEA_PROMPT)
    while True:
        lines = []
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            if line.strip() == "END":
                break
            lines.append(line.rstrip())
        try:
            return validate_idea("\n".join(lines).strip("\n"))
        except WorkspaceError as e:
            print(f"  {e}. Please enter your research idea again (finish with END).")


def new_job(user, yaml_location, job_name=None, idea_file=None):
    """Every job gets its own folder named by its job ID: research_projects/<user>/<job-id>/."""
    base = ensure_user_folder(user)
    if idea_file:
        with open(idea_file) as f:
            idea = validate_idea(parse_research_idea(f.read()))
        print(f"Research idea read from {idea_file}")
    else:
        idea = read_research_idea()
    name = ask_job_name(job_name)
    job_id = generate_run_id(user)
    while os.path.exists(os.path.join(base, job_id)):
        job_id = generate_run_id(user)
    folder = os.path.join(base, job_id)
    os.makedirs(folder, mode=0o700)
    with open(os.path.join(folder, "research_idea.txt"), "w") as f:
        f.write(format_research_idea(user, idea))
    shutil.copyfile(yaml_location, os.path.join(folder, "config.yaml"))
    job = Job(folder)
    job.save(job_id=job_id, user=user, uid=os.getuid(), host=socket.gethostname(),
             name=name, created=_now(), status="CREATED", resume_count=0)
    log_activity("job_created", job_id=job_id, name=name)
    print(f"\nJob ID: {job_id}   (use it to check on this job later)")
    print(f"Job folder: {os.path.abspath(folder)}")
    print(f"Research idea saved to {os.path.join(folder, 'research_idea.txt')}\n")
    return job


# ------------------------------------------------------------ session entry
class SessionResult:
    """What ai_lab_repo should run: a job, and whether to resume it from its checkpoint."""

    def __init__(self, job, resume):
        self.job, self.resume = job, resume


def _copilot_enabled(yaml_location):
    with open(yaml_location) as f:
        v = (yaml.safe_load(f) or {}).get("copilot-mode", False)
    return v.lower() == "true" if isinstance(v, str) else bool(v)


def _launch_background(job, resume):
    """Detach the job from this terminal; output goes to run.log."""
    cmd = [sys.executable, os.path.abspath(sys.argv[0]), "--yaml-location", job.config_path,
           "--run-job", job.job_id] + (["--resume"] if resume else [])
    with open(job.log_path, "a") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=os.getcwd())
    print(f"Job {job.job_id} is running in the background (pid {proc.pid}).")
    print("You can close this terminal. Check on it any time with:  python ai_lab_repo.py")
    log_activity("job_backgrounded", job_id=job.job_id, pid=proc.pid)


def check_existing(user, jobs, ref=None):
    """Interactive 'check on a job'. Returns SessionResult to resume, or None."""
    while True:
        if ref is None:
            ref = _ask(f"Enter job ID (or # from the list) [{len(jobs)}]: ", str(len(jobs)))
        job = find_job(user, ref, jobs)
        if job is None:
            print(f"  No job '{ref}' found for {user}.")
            ref = None
            continue
        break
    log_activity("job_checked", job_id=job.job_id)
    status = show_job(job)
    if status == ACTIVE:
        if _yes("This job is running. Follow its log now? [Y/n]: ", True):
            follow_log(job)
        return None
    if status in ("INTERRUPTED", "FAILED", "CREATED"):
        where = "its last checkpoint" if job.has_checkpoint() else "the beginning (no checkpoint yet)"
        if _yes(f"Resume this job from {where}? [Y/n]: ", True):
            return SessionResult(job, resume=True)
        return None
    if status == "COMPLETED":
        print(f"Outputs: {os.path.abspath(job.lab_dir)}")
    return None


def start_session(yaml_location, idea_file=None, job_name=None, job_ref=None, background=False):
    """Decide what to run. Returns SessionResult, or None when there is nothing to run now."""
    user = current_user()
    print("=" * 50)
    print("Welcome to AgentLaboratory")
    print("=" * 50)
    log_activity("session_start")
    jobs = user_jobs(user)

    if background and _copilot_enabled(yaml_location):
        print("--background needs copilot-mode: False in the YAML (copilot asks you questions in the terminal).")
        sys.exit(2)

    result = None
    if job_ref is not None:                                  # python ai_lab_repo.py --job <ID>
        if not jobs:
            print(f"No jobs found for {user}.")
            return None
        result = check_existing(user, jobs, ref=job_ref)
    elif jobs and job_name is None and idea_file is None:  # returning user
        print(f"\nWelcome back, {user}. You have {len(jobs)} research job(s):")
        print_jobs(jobs)
        while True:
            choice = _ask("[c] check on an existing job   [n] start new research   [q] quit\nChoice [c]: ", "c").lower()
            if choice in ("c", "n", "q"):
                break
        if choice == "q":
            return None
        if choice == "c":
            result = check_existing(user, jobs)
        else:
            result = SessionResult(new_job(user, yaml_location), resume=False)
    else:                                                    # new user (or explicit new project)
        if not jobs:
            print(f"\nHello {user}, no research jobs found for you yet. Let's create your first one.\n")
        result = SessionResult(new_job(user, yaml_location, job_name, idea_file), resume=False)

    if result is not None and background:
        _launch_background(result.job, result.resume)
        return None
    return result


def load_job_for_worker(job_id):
    """--run-job: the detached background process re-opens its job (no prompts)."""
    user = current_user()
    for job in user_jobs(user):
        if job.job_id == job_id:
            return job
    raise SystemExit(f"job {job_id} not found for {user}")


# ------------------------------------------------------- running the workflow
class _Tee:
    """Echo output to the terminal (if any) and append it to run.log."""

    def __init__(self, stream, log_file):
        self.stream, self.log = stream, log_file

    def write(self, data):
        try:
            self.stream.write(data)
        except (OSError, ValueError):
            pass  # terminal went away; keep logging
        self.log.write(data)
        self.log.flush()
        return len(data)

    def flush(self):
        for s in (self.stream, self.log):
            try:
                s.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt(signal.Signals(signum).name)


def attach(job, workflow_module, agents_module, background_worker=False):
    """Hook a job into the workflow: checkpoints, per-step progress, run.log, liveness record."""
    ckpt = CheckpointManager(job.project_dir, job.job_id, job.data["user"])

    def checkpoint_hook(workflow, subtask):
        ckpt.save(workflow, subtask, completed_subtasks=[k for k, v in workflow.phase_status.items() if v])
        job.save(last_checkpoint=subtask)

    workflow_module.CHECKPOINT_HOOK = checkpoint_hook
    agents_module.PROGRESS_HOOK = ckpt.progress
    if not background_worker:  # background worker's stdout is already run.log
        log = open(job.log_path, "a", buffering=1)
        sys.stdout = _Tee(sys.stdout, log)
        sys.stderr = _Tee(sys.stderr, log)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGHUP, _raise_interrupt)  # terminal closed
    me = psutil.Process()
    job.save(status=ACTIVE, pid=me.pid, pid_create_time=me.create_time(),
             mode="background" if background_worker else "foreground")
    print(f"\n[agentlab] job {job.job_id} running (pid {me.pid}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return ckpt


def load_checkpoint(job):
    ckpt = CheckpointManager(job.project_dir, job.job_id, job.data["user"])
    lab, pointer = ckpt.load_latest()
    if lab is not None:
        done = [k for k, v in lab.phase_status.items() if v]
        print(f"[agentlab] resuming {job.job_id} from checkpoint {pointer.get('file')} (already done: {', '.join(done) or 'nothing'})")
    else:
        print(f"[agentlab] no usable checkpoint for {job.job_id}; starting it from the beginning")
    return lab


def finish(job, status, error=None):
    fields = {"status": status, "ended": _now()}
    if error:
        fields["error"] = error
    job.save(**fields)
    log_activity(f"job_{status.lower()}", job_id=job.job_id, **({"error": error[:200]} if error else {}))
    print(f"\n[agentlab] job {job.job_id} {status}.  Check it any time with: python ai_lab_repo.py")
