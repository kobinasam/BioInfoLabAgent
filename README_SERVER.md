# AgentLaboratory — Multi-User SSH Research Server

This document covers running AgentLaboratory on a shared, in-house Linux server, where many lab
members SSH in and run research concurrently. They are isolated from each other, runs survive
disconnects and reboots, every action is audited, and API keys never leave the server side.

The research pipeline itself (`ai_lab_repo.py`, `agents.py`, `mlesolver.py`, `papersolver.py`, …)
is unchanged in behaviour. Local single-user use (`python ai_lab_repo.py --yaml-location …`) still
works exactly as before.

---

## 1. Architecture

```
 lab member ──ssh──► sshd ──PAM pam_exec──► agentlab-pam-session ──┐ login/logout
     │                                                             │
     │ $ agentlab   (runs as the user, no secrets, no privileges)  ▼
     └──────────► /run/agentlab/agentlabd.sock  ◄── SO_PEERCRED: kernel-verified uid
                          │
              ┌───────────┴──────────────────────────────────────────────┐
              │ agentlabd  (agentlab-runner.service, root)               │
              │  • identity & authorization (Linux groups)               │
              │  • workspaces (/opt/agentlab/users/<user>, 0700)         │
              │  • run registry  /opt/agentlab/system/state  (root)      │
              │  • audit log     /opt/agentlab/system/logs   (root, +a)  │
              │  • scheduler / resource admission / reconciliation       │
              └──────┬───────────────────────────────────────────────────┘
                     │ systemd-run --uid=<user>  (transient unit per run)
                     ▼
     agentlab-run-<run_id>.service   (runs AS THE USER, own cgroup, sandboxed)
       server/research_runner.py → ai_lab_repo.LaboratoryWorkflow (unchanged)
         cwd = <user>/<job_id>/generated   state.json + checkpoints/ (atomic)
         OPENAI/OPENROUTER/DEEPSEEK_BASE_URL = http://127.0.0.1:8765/<provider>/v1
         *_API_KEY = per-run token (alr_…), NOT a real key
                     │
                     ▼
     agentlab-llm-proxy.service  (user agentlab-proxy; EnvironmentFile=/etc/agentlab/secrets.env)
       verifies token hash (published by agentlabd only while the run is active)
       swaps it for the real provider key ──HTTPS──► OpenRouter / OpenAI / DeepSeek / Anthropic
```

The main design decisions:

| Concern | Mechanism |
|---|---|
| Who is the user? | `SO_PEERCRED` on the daemon socket (kernel). No `--username` option exists. |
| Admin rights | Membership of the Linux group `agentlab-admins`, checked by the daemon. |
| Isolation | Unix permissions. Workspace `0700` owned by the user; `users/` is `0711 root` (cannot be listed). Every job a user starts is its own folder `users/<user>/<job-id>/`, named by its job ID, inside *their* root folder. |
| Root writing into user dirs | Never done directly. The daemon drops to the user's UID (`server/user_ops.py`), so planted symlinks are harmless. |
| Survive SSH disconnect | Each run is a systemd transient unit, not a child of the shell. SIGHUP is ignored. |
| Survive crash/reboot | Checkpoint after every subtask (hash-verified, atomic) plus per-step `state.json`. The daemon reconciles at boot. |
| PID reuse | A run is alive only if `boot_id` + PID + process start time (or systemd unit state) all match. |
| Audit integrity | Root-owned file, `chattr +a`, single writer, SHA-256 hash chain (`agentlab-admin verify-log`). |
| Secrets | Only the proxy service has the keys. Runs get a revocable per-run token in a 0600 file, never in argv or the unit environment. |
| Reproducibility | Each run records the git commit, its release path, the config snapshot, a copy of the idea, the seed, and the environment. |
| Code safety | Releases are immutable directories built only from commits merged to `main` (`scripts/deploy.sh`). |

### Code map

| Path | Role |
|---|---|
| `server/daemon.py` | `agentlabd`: socket API, scheduling, monitoring, audit, sessions |
| `server/research_runner.py` | Per-run worker. Runs the real `LaboratoryWorkflow` (or the `simulated` test pipeline) |
| `server/agentlab_cli.py`, `server/admin_cli.py` | `agentlab`, `agentlab-admin` |
| `server/workspace_manager.py`, `server/user_ops.py` | Workspaces; privilege-dropped file operations |
| `server/checkpoint_manager.py` | `state.json`, verified checkpoints, resume |
| `server/run_registry.py` | Authoritative run records, run IDs, state machine |
| `server/launcher.py` | systemd / subprocess process supervision, process identity |
| `server/recovery_manager.py` | Restart/crash reconciliation |
| `server/resource_manager.py` | CPU/RAM/GPU admission (replaceable by Slurm/K8s) |
| `server/audit_logger.py`, `server/secret_guard.py` | Protected audit log, redaction |
| `server/llm_proxy.py` | Key-holding LLM proxy |
| `server/session_manager.py`, `server/pam_hook.py` | SSH session tracking |
| `server/permissions.py`, `server/config.py`, `server/fsutil.py` | Identity/authorization, `server.yaml`, atomic I/O |
| `scripts/` | Installer, deploy, user management, security self-test, CLI wrappers |
| `systemd/`, `deploy/` | Unit files, tmpfiles, logrotate, profile.d banner |
| `tests/` | Automated tests (`python -m unittest` from `tests/`) |

### Changes to the existing research code (minimal, backward compatible)

* `ai_lab_repo.py`
  * `save_state` writes atomically. It also calls an optional module-level `CHECKPOINT_HOOK`, which is unset by default.
  * Bug fix: `report_writing` used the `__main__` globals `research_topic`/`compile_pdf`, which caused a `NameError` whenever the workflow was imported. It now uses `self.research_topic`/`self.compile_pdf`.
  * Bug fix: the agent model map lacked `"report refinement"`, so the reviewers silently fell back to `o3-mini`. `"paper refinement"` is kept for compatibility.
  * The YAML/notes/model-map setup was factored into `parse_yaml_data`, `build_task_notes`, `build_agent_models` and `build_human_in_loop`. `__main__` behaves the same.
* `agents.py`: optional `PROGRESS_HOOK(phase, step)` in `BaseAgent.inference`, unset by default.
* `inference.py`, `utils.py`: OpenRouter/DeepSeek base URLs can be overridden through `OPENROUTER_BASE_URL`/`DEEPSEEK_BASE_URL`. The defaults are unchanged. The OpenAI and Anthropic SDKs already honour `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`.
* New file `experiment_configs/server_default.yaml`: the default experiment settings for server runs, with no key and no topic.

---

## 2. Server prerequisites (assumptions)

* **Linux** with **systemd ≥ 245**. Tested targets are Ubuntu 22.04/24.04 and Debian 12. RHEL 9 should work (`dnf` path).
* **Python ≥ 3.10** (development used 3.11).
* **OpenSSH** with PAM enabled (`UsePAM yes`, the default on Debian/Ubuntu/RHEL).
* A local filesystem that supports `chattr +a` (ext4/xfs/btrfs) for `/opt/agentlab/system`. On NFS the append-only bit is unavailable, and the protection relies on ownership plus the hash chain.
* GPUs are optional. NVIDIA GPUs are detected through `nvidia-smi` and assigned with `CUDA_VISIBLE_DEVICES`.
* Recommended: mount `/proc` with `hidepid=2` (`/etc/fstab`: `proc /proc proc defaults,hidepid=2 0 0`). This stops users seeing each other's process lists and run IDs in `ps`.

## 3. Installation

```bash
# as an administrator, on the server
sudo git clone https://github.com/<org>/AgentLaboratory.git /root/AgentLaboratory-src
cd /root/AgentLaboratory-src
sudo scripts/install_server.sh               # idempotent; re-run any time
sudoedit /etc/agentlab/secrets.env           # add OPENROUTER_API_KEY=... etc.
sudo systemctl restart agentlab-llm-proxy
sudo scripts/add_lab_user.sh alice
sudo scripts/add_lab_user.sh bob
sudo scripts/add_lab_user.sh pi --admin
sudo /opt/agentlab/app/scripts/security_check.sh alice bob   # live isolation/secret/audit test
```

Installer options: `--skip-deps` (no apt/pip), `--skip-pam`, `--no-start`, `--no-systemd`
(containers/CI: files only), and `--source DIR`.

What the installer does:
1. Installs packages.
2. Creates the groups `agentlab-users`, `agentlab-admins` and `agentlab-proxy`, plus the system user `agentlab-proxy`.
3. Creates `/opt/agentlab/{config,releases,users,system/{logs,state,scripts}}` with the permissions below.
4. Builds an immutable release, `/opt/agentlab/releases/<commit>`, and points `/opt/agentlab/app` at it.
5. Creates the venv and installs the requirements.
6. Installs `server.yaml` and the secrets template, but only if they don't exist yet.
7. Creates the audit log and sets `chattr +a` on it.
8. Installs the CLIs.
9. Installs logrotate, tmpfiles and the profile banner.
10. Adds an `optional` `pam_exec` line to `/etc/pam.d/sshd`, after backing the file up.
11. Installs and enables the systemd units.
12. Records the deployment and runs health checks.

Any system file it changes is first copied to `<file>.agentlab-backup-<timestamp>`. It never
touches existing workspaces, logs, `server.yaml` or secrets, and it does not modify
`sshd_config`.

## 4. Linux users and groups

| Account / group | Purpose |
|---|---|
| `agentlab-users` | Lab members. Can use the daemon socket. |
| `agentlab-admins` | Administrators. Can see all runs, stop any run, and read the audit log. Admins should also be in `agentlab-users`. |
| `agentlab-proxy` (user + group) | Runs the LLM proxy. The only non-root identity that holds keys. |
| `root` | Runs `agentlabd`. Trusted infrastructure. |

Removing someone: `sudo gpasswd -d alice agentlab-users`. Their workspace stays until an admin
archives it.

## 5. Permissions

| Path | Owner:group | Mode | Notes |
|---|---|---|---|
| `/opt/agentlab/app` → `releases/<sha>/` | root:root | 0755 / files 0644 | read-only production code |
| `/opt/agentlab/venv` | root:root | go-w | shared interpreter |
| `/opt/agentlab/config/server.yaml` | root:root | 0644 | no secrets |
| `/opt/agentlab/users` | root:root | **0711** | traverse only; no listing |
| `/opt/agentlab/users/<user>` and everything inside | user:user | **0700/0600** | created by daemon, filled as the user |
| `/opt/agentlab/system`, `logs/`, `state/` | root:agentlab-admins | 0750 | |
| `/opt/agentlab/system/logs/agentlab_audit.log` | root:agentlab-admins | 0640 **+a** | append-only |
| `/etc/agentlab/secrets.env` | root:root | **0600** | read by systemd only |
| `/run/agentlab/agentlabd.sock` | root:agentlab-users | 0660 | |
| `/run/agentlab/proxy/tokens.json` | root:agentlab-proxy | 0640 | token *hashes* only |

## 6. Secret management

* Put keys only in `/etc/agentlab/secrets.env`:
  ```
  OPENROUTER_API_KEY=sk-or-v1-...
  OPENAI_API_KEY=sk-...
  DEEPSEEK_API_KEY=...
  ANTHROPIC_API_KEY=...
  ```
  then run `sudo systemctl restart agentlab-llm-proxy`.
* systemd reads that file as root before starting the proxy as `agentlab-proxy`. Lab users
  cannot read the file, the proxy's `/proc/<pid>/environ` (different UID), or core dumps
  (`LimitCORE=0`).
* Each launch gets a fresh token (`alr_…`, 256 bits). It is written to
  `<user>/<job-id>/proxy_token` (0600, as the user). The worker reads it and deletes the file, then
  places it in its own environment as `OPENAI_API_KEY` etc. That way existing code, and the
  experiment code the LLM generates, keeps working without changes. Only the token's SHA-256
  goes to `tokens.json`, and only while the run is STARTING/RUNNING/STOPPING. When the run
  ends, the token stops working.
* The daemon refuses experiment configs that contain an `api-key`. It never passes `*_API_KEY`
  variables to runs, and it redacts key-shaped strings from the audit log and error messages.
* Vault or another secret manager: have it render `/etc/agentlab/secrets.env` (for example with
  vault-agent or a systemd `ExecStartPre`). Nothing else changes.
* Gemini (`google-generativeai`) cannot be pointed at a proxy. In server mode, use Gemini models
  through OpenRouter (`openrouter:google/...`).

## 7. SSH workflow (lab member)

```
$ ssh alice@research-server
AgentLaboratory is available on this server. ...
$ agentlab
Welcome to AgentLaboratory

User: alice
Workspace:
/opt/agentlab/users/alice

Workspace does not exist.

Create this workspace? [y/N]: y

Workspace created successfully.

Enter your research idea.
...
Type END on a separate line when finished.

> I propose a new ...
> END

Research idea saved.

Starting AgentLaboratory...

Run ID:
20261008_143605_alice_a81f23

AgentLaboratory is running.

If your SSH session disconnects, your research will continue.
Reconnect later using:

    agentlab status
    agentlab resume 20261008_143605_alice_a81f23
```

## 8. Research idea workflow

* `agentlab` (guided) or `agentlab start` prompts for the multi-line idea, ending with `END`.
  `agentlab start --idea-file idea.txt` reads it from a file instead.
* Empty ideas are rejected. Line breaks and indentation are preserved.
* The idea is saved to `<workspace>/research_idea.txt` with the `Researcher/Created/Host` header.
  Any previous version is archived to `ideas_archive/`. A `research_submitted` event is logged.
* At run creation the idea is copied into `<user>/<job-id>/research_idea.txt`. The run always
  reads its own copy, so editing the workspace file later changes nothing in an existing run.
* The YAML `research-topic` is ignored in server mode. The topic always comes from the idea file.
* Experiment settings are the server default (`experiment_configs/server_default.yaml`),
  overlaid with `<workspace>/config/agentlab.yaml` if present, or with `--config FILE`. API keys,
  `parallel-labs` and `agentRxiv` are rejected. `copilot-mode` is forced off because a detached
  run has no terminal, and `num-papers-to-write` is forced to 1 (one paper per run).

## 9. Running experiments — run layout

```
/opt/agentlab/users/alice/                       Alice's root folder (named after her Linux login)
├── config/agentlab.yaml   optional personal experiment settings
├── ideas_archive/         earlier versions of research_idea.txt
├── research_idea.txt      latest idea she submitted
├── 20261008_143605_alice_a81f23/   one folder per job, named by its job ID
│   ├── run_metadata.json   run_id, user, uid, host, pid, start/end, git commit, release, backend, config, seed, attempts
│   ├── research_idea.txt   immutable copy
│   ├── config.yaml         exact experiment config used
│   ├── state.json          status, current_phase, current_step, last_completed_*, checkpoint_time, resume_count
│   ├── stdout.log / stderr.log
│   ├── checkpoints/        ckpt_<seq>_<subtask>.pkl + latest.json (sha256)
│   ├── outputs/research_dir/{src,tex}/   (the pipeline's former MATH_research_dir/…)
│   ├── papers/             report.txt, readme.md, temp.pdf/tex copied at completion
│   └── generated/          working directory: Figure_*.png, downloaded papers, temp files
└── 20261009_101500_alice_9c1d2e/   another job (several can run at once)
```

Run IDs look like `YYYYMMDD_HHMMSS_<user>_<6 hex>`. They are immutable, a run directory is
never reused, and resuming keeps the same ID.

## 10. Resuming interrupted runs

```
$ agentlab
Previous run interrupted.

Run ID: 20261008_143605_alice_a81f23
Last completed phase: plan formulation
Last completed step: 4
Last checkpoint:
data preparation step 5

Resume from checkpoint? [Y/n]: y
Run 20261008_143605_alice_a81f23 queued for resume from its latest checkpoint
```

You can also resume directly with `agentlab resume <run-id> [--follow]`.

**Granularity.** A durable checkpoint, the full pickled `LaboratoryWorkflow`, is taken after
every subtask: literature review, plan formulation, data preparation, running experiments,
results interpretation, report writing, and report refinement. On resume, completed subtasks
are skipped. The subtask that was interrupted restarts from its beginning, but with all earlier
results intact. Per-step progress (`current_step`) is recorded on every agent call for
visibility. Resuming in the middle of a subtask would require restructuring the research
loops, which this change deliberately avoids.

If the newest checkpoint is corrupt, the worker falls back to the newest one that verifies. If
there is none at all, the run starts from the beginning and logs that it did so.

## 11. Checking status

```
$ agentlab status
AgentLaboratory Active Users
============================

USER       LOGIN TIME           RUN ID                             STATUS       PHASE
----------------------------------------------------------------------------------------------------
alice      2026-10-08 14:35     20261008_143605_alice_a81f23       RUNNING      literature review (step 3)
bob        2026-10-08 14:42     -                                  RUNNING

Total active users: 2
Total active research runs: 2
```

`agentlab` with no arguments is the everyday entry point. A **new user** creates their workspace
and their first job. A **returning user** sees a list of all their jobs and chooses either
`[c]` check on one, by `#` or job ID (a unique prefix is enough), or `[n]` start new research.
Checking shows the status, phase, last checkpoint and the end of the log. It then offers to follow
an active job, resume an interrupted, failed or stopped one, or shows where a completed job's results are.
`agentlab check [<job-id>]` goes straight to that step.

A user can run **several jobs at the same time** (`execution.max_runs_per_user`, default 3; set
`one_active_run_per_user: true` to allow only one). The whole server runs at most
`execution.max_concurrent_runs` jobs at once, and extra jobs wait as QUEUED until a slot frees up. Give jobs a
readable name with `agentlab start --name math-verifiers`, or answer the prompt.

Other commands: `agentlab runs`, `agentlab info <id>`, `agentlab logs <id> [-f] [--stderr]`,
`agentlab history [-n 100]`, `agentlab workspace [--create]`, and `agentlab stop <id>`.

Status comes from the daemon's root-owned registry and live process checks. A non-admin sees
their own runs in full. For other users they see only the username and status, controlled by
`security.status_visibility` (`own|usernames|full`). History shows only the caller's own events.

## 12. Administrator commands

```
agentlab-admin status          # everyone, with run IDs and phases
agentlab-admin users           # workspaces, logged in?, run counts
agentlab-admin running         # active runs: user, phase, backend
agentlab-admin runs            # all runs ever
agentlab-admin history [--user alice] [-n 200]
agentlab-admin stop <run-id>   # any user's run (audited as admin action)
agentlab-admin logs [-n 100] [--user U] [--run-id R] [--event E] [--json]
agentlab-admin run-log <run-id> [--stderr]
agentlab-admin deployments
agentlab-admin verify-log      # verify the audit hash chain
agentlab-admin health
```

Authorization is enforced inside `agentlabd` against the socket peer's group membership. There
is no username allow-list in Python.

## 13. Audit logging

`/opt/agentlab/system/logs/agentlab_audit.log` is JSON Lines. Every record carries `timestamp`
(UTC), `event`, `host`, `user`/`uid` where applicable, plus `prev` and `hash` (the chain).

| Event | When |
|---|---|
| `login`, `logout`, `session_disconnected` | PAM open/close; sshd vanished without a close |
| `session_attach` | a user reconnected and attached to an active run |
| `workspace_created`, `research_submitted` | first use; idea saved |
| `run_created`, `run_started`, `run_resume_requested`, `run_resumed` | lifecycle |
| `run_stop_requested`, `run_terminated`, `run_interrupted`, `run_completed`, `run_failed` | outcome |
| `resource_waiting`, `resource_granted`, `resource_released` | admission control |
| `api_error` | the worker failed on provider/API errors (redacted) |
| `authorization_denied` | someone tried to act on another user's run or an admin op |
| `daemon_started`, `daemon_stopped`, `server_reconciliation`, `daemon_error` | service lifecycle and recovery |
| `deployment`, `audit_log_verified` | admin actions |

Example:
```json
{"event":"login","host":"lab-server-01","pid":12345,"rhost":"10.0.0.7","service":"sshd","tty":"ssh","uid":1002,"user":"alice","timestamp":"2026-10-08T14:35:12.000000Z","prev":"…","hash":"…"}
{"event":"workspace_created","user":"alice","uid":1002,"workspace":"/opt/agentlab/users/alice", …}
{"event":"research_submitted","user":"alice","uid":1002,"file":"research_idea.txt", …}
{"event":"run_started","user":"alice","run_id":"20261008_143605_alice_a81f23","pid":12555,"unit":"agentlab-run-20261008_143605_alice_a81f23.service","gpus":[], …}
{"event":"run_completed","user":"alice","run_id":"20261008_143605_alice_a81f23","status":"completed","exit_code":0, …}
```

The proxy logs one line per request to the journal (`journalctl -u agentlab-llm-proxy`) with
the user, run, provider, model, status and latency. It never logs prompts, completions, tokens
or keys.

## 14. Security model

**Normal users** can operate their own workspace and runs: create, start, stop, resume and
read. They cannot read or modify other workspaces or the production code, cannot read or
modify the audit log, the registry or the secrets, cannot stop other users' runs, and cannot
impersonate anyone.

**Application (agentlabd, root)** is the only writer of the registry and the audit log. It
never runs research code, never holds keys, and never touches user files as root.

**Research processes** run as the user, in a sandboxed transient unit: `NoNewPrivileges`,
`ProtectSystem=strict` (only the user's own workspace is writable), `PrivateTmp`, plus memory
and task limits. Code that the LLM writes and executes during experiments runs here, so it has
the user's privileges and no more.

**Proxy (agentlab-proxy)** holds the keys. It is reachable only on loopback, and only with a
live run token.

**Administrators** are members of `agentlab-admins`, plus sudo for installation and
deployment.

**Limits, stated plainly:**
* **root is trusted.** A root administrator can read secrets, edit or remove logs (after
  `chattr -a`), and read workspaces. The protections in this document are against *normal lab
  users*. Tampering by root is *detectable* through the hash chain (`verify-log`) if the
  latest hash is stored elsewhere, for example by forwarding the log to a remote syslog or SIEM.
  It is not *preventable*.
* Truncating the end of the log keeps the remaining chain valid. Forward the log off-host to
  detect truncation.
* A user can use their *own* active run's token to make LLM calls directly while that run is
  active. Those calls are attributed to them in the proxy log. They still never see the real
  key.
* LLM-generated experiment code runs with the user's permissions. That is no more than the
  user could already do, but review generated code before trusting its outputs.
* Pickled checkpoints are only ever loaded by the user's own worker, never by root.

## 15. Git contributor workflow

1. An admin adds the lab member as a collaborator on the Git host.
2. The member clones the repository *outside* `/opt/agentlab`, either in their home directory
   or on their laptop:
   ```bash
   git clone git@github.com:<org>/AgentLaboratory.git ~/src/AgentLaboratory
   cd ~/src/AgentLaboratory
   git checkout -b feature/new-research-agent
   ```
3. Develop and test locally:
   ```bash
   python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   cd tests && python -m unittest -v          # server layer tests
   ```
   Research-logic changes can be exercised with `python ai_lab_repo.py --yaml-location ...`,
   using the developer's *own* key in their own environment. Server keys are not available
   outside the proxy.
4. ```bash
   git add -A
   git commit -m "Add new research-agent capability"
   git push origin feature/new-research-agent
   ```
5. Open a pull request into `main`.

## 16. Pull-request workflow

* Protect `main` on the Git host: require a PR, at least one approving review, passing CI, and
  no force-pushes.
* The PR author describes the change and any research-behaviour impact, and adds or updates
  tests.
* The reviewer checks for any secrets in the diff, any network or file access outside the run
  directory, and changes under `server/`, which are security-sensitive and need an admin
  reviewer.
* CI runs `cd tests && python -m unittest -v`.
* Merge (squash or merge commit). **Merging does not deploy anything.**

## 17. Deployment

```bash
sudo /opt/agentlab/app/scripts/deploy.sh origin/main          # or an exact SHA
sudo /opt/agentlab/app/scripts/deploy.sh <previous-sha>       # rollback
agentlab-admin deployments
```

`deploy.sh` refuses any commit that is not an ancestor of `main` in the root-owned mirror, which
means unreviewed branches can never deploy. It builds an immutable `releases/<sha>`, runs the
tests, switches the `app` symlink atomically, restarts the two services, and records `commit`,
`previous`, `deployer`, `branch`, `tests` and `time` as a `deployment` audit event.

**Running research is not interrupted.** Each run is its own unit and keeps executing the
release it started with (its `release_path` is recorded). New runs use the new release. A
resumed run uses its original release if that release still exists.

The venv is shared. If `requirements.txt` changes, `deploy.sh` installs the new requirements
into it, and that affects runs already in progress. Schedule dependency upgrades when no runs
are active (check with `agentlab-admin running`).

## 18. Backup and recovery

Back up:
* `/opt/agentlab/users/` (research data)
* `/opt/agentlab/system/` (audit log, registry, sessions, deployments)
* `/opt/agentlab/config/server.yaml`
* `/etc/agentlab/secrets.env` (to an encrypted store only)

```bash
sudo tar --xattrs --acls -czpf /backup/agentlab-$(date +%F).tgz /opt/agentlab/users /opt/agentlab/system /opt/agentlab/config
```

Restore by extracting the archive as root, re-running `install_server.sh` (which repairs
permissions and does not overwrite data), and then
`sudo systemctl restart agentlab-runner`. Reconciliation marks the restored RUNNING runs as
INTERRUPTED, and users can resume them.

## 19. Troubleshooting

| Symptom | Check |
|---|---|
| `the AgentLaboratory service is not running` | `systemctl status agentlab-runner`, `journalctl -u agentlab-runner -n 100` |
| `permission denied connecting...` | `id` must list `agentlab-users`. Log out and back in after `add_lab_user.sh`. |
| Run stays `QUEUED` | `agentlab info <id>` shows `waiting_reason`. Check `agentlab-admin health` for resources. |
| Run `FAILED` | `agentlab logs <id> --stderr`, then `agentlab resume <id>` after fixing |
| LLM 401 `invalid or expired AgentLaboratory run token` | the run is no longer active, or the daemon restarted while the token file was rewritten. Resume the run. |
| LLM 503 `provider ... not configured` | Add the key to `secrets.env` and restart the proxy |
| `journalctl -u agentlab-run-<id>` | systemd's view of a specific run |
| Login events missing | `grep agentlab /etc/pam.d/sshd`, `UsePAM yes` in `sshd_config` |
| `verify-log` fails | Treat as an incident. Compare with off-host copies. |

## 20. Resource management

Before a queued run starts, `agentlabd` checks:
* `max_concurrent_runs` (global) and the per-user limit
* free RAM (`min_free_ram_gb`) and CPU idle (`min_free_cpu_percent`)
* GPUs: `gpus_per_run` free devices with at least `min_free_gpu_memory_mb`. Assigned devices are
  exported as `CUDA_VISIBLE_DEVICES` and are never handed to two runs at once.

If a check fails, the run stays **QUEUED**. The user sees the reason, a `resource_waiting` event
is logged once per distinct reason, and the scheduler retries every `poll_interval_seconds`.
When the run is admitted, `resource_granted` is logged. When it ends, for any reason,
`resource_released` is logged. Each run also gets a cgroup `MemoryMax`, an optional `CPUQuota`,
and `TasksMax`.

To move to Slurm or Kubernetes, implement `probe/try_acquire/release` (`resource_manager.py`)
and `launch/is_alive/exit_info/stop/kill/cleanup` (`launcher.py`). The CLI, registry,
checkpointing and research pipeline stay as they are.

## 21. Server restart recovery

On every start, `agentlabd` performs these steps:
1. Loads every non-terminal run from the registry.
2. Checks each run's identity: `boot_id`, PID and start time, plus the systemd unit state.
3. Re-adopts runs that are still alive. This is the case after a daemon restart, where the
   runs never stopped.
4. Marks runs whose process is gone as **INTERRUPTED**, or COMPLETED or STOPPED if their exit
   status shows that. After a reboot, every previously running run becomes INTERRUPTED.
5. Leaves checkpoints untouched, so every one of those runs remains resumable.
6. Logs a `server_reconciliation` event listing still-running, interrupted and finalised runs.
7. If `execution.auto_resume_after_restart: true`, re-queues the interrupted runs
   automatically. Otherwise users are offered a resume when they next run `agentlab`.

The same liveness check runs every `poll_interval_seconds`, so a crashed run never stays
`RUNNING`.

---

## Testing

```bash
cd tests
python -m unittest -v                                        # 62 tests (3 skipped without root/heavy flags), ~30 s
AGENTLAB_HEAVY_TESTS=1 python -m unittest test_git_and_pipeline -v   # + real LaboratoryWorkflow via proxy
sudo AGENTLAB_E2E_USERS=alice,bob python -m unittest test_permissions -v  # live multi-account checks
```

The non-root suite runs a real `agentlabd` on a temporary socket, with real worker processes on
the `simulated` pipeline. Distinct lab members are simulated as separate identities. Real
cross-account permission checks run in `scripts/security_check.sh`.
