"""
Central server configuration (server.yaml).

Every path used by the server layer is derived from this file so nothing is
hard-coded throughout the source. The daemon reads the file named on its command
line (set by the systemd unit); the CLIs only need it to find the daemon socket.
"""
import copy
import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = "/opt/agentlab/config/server.yaml"
CONFIG_ENV_VAR = "AGENTLAB_SERVER_CONFIG"

DEFAULTS = {
    "server": {
        "install_root": "/opt/agentlab",
        "application_root": "/opt/agentlab/app",
        "workspace_root": "/opt/agentlab/users",
        "system_root": "/opt/agentlab/system",
        "audit_log": "/opt/agentlab/system/logs/agentlab_audit.log",
        "state_dir": "/opt/agentlab/system/state",
        "socket_path": "/run/agentlab/agentlabd.sock",
        "python": "/opt/agentlab/venv/bin/python",
        "users_group": "agentlab-users",
        "admins_group": "agentlab-admins",
    },
    "execution": {
        # A user may run several jobs at once (each in its own run directory inside their workspace).
        # Set one_active_run_per_user: true to restrict everyone to a single active job.
        "one_active_run_per_user": False,
        "max_runs_per_user": 3,
        "max_concurrent_runs": 4,
        "resume_enabled": True,
        "auto_resume_after_restart": False,
        # systemd | subprocess  (subprocess is for development/tests only)
        "launcher": "systemd",
        # agentlab = real LaboratoryWorkflow, simulated = deterministic test pipeline
        "pipeline": "agentlab",
        "poll_interval_seconds": 5,
        "stop_timeout_seconds": 60,
        "memory_max": "32G",
        "cpu_quota": "",
        "tasks_max": 4096,
    },
    "resources": {
        "min_free_ram_gb": 4.0,
        "min_free_cpu_percent": 10.0,
        "gpus_per_run": 0,
        "min_free_gpu_memory_mb": 0,
    },
    "security": {
        "protected_audit_log": True,
        "require_user_isolation": True,
        "secrets_server_side_only": True,
        # own | usernames | full   -- what a non-admin sees for other users in `agentlab status`
        "status_visibility": "usernames",
        "max_request_bytes": 1048576,
    },
    "research": {
        "idea_filename": "research_idea.txt",
        "max_idea_bytes": 65536,
        "default_experiment_config": "experiment_configs/server_default.yaml",
        "allowed_llm_backends": [],
    },
    "llm_proxy": {
        "enabled": True,
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "tokens_file": "/run/agentlab/proxy/tokens.json",
        "providers": {
            "openai": {"upstream": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY", "auth": "bearer"},
            "openrouter": {"upstream": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY", "auth": "bearer"},
            "deepseek": {"upstream": "https://api.deepseek.com/v1", "env_key": "DEEPSEEK_API_KEY", "auth": "bearer"},
            "anthropic": {"upstream": "https://api.anthropic.com", "env_key": "ANTHROPIC_API_KEY", "auth": "x-api-key"},
        },
    },
    "git": {
        "require_review_before_deploy": True,
        "deploy_branch": "main",
        "repo_mirror": "/opt/agentlab/repo.git",
        "releases_dir": "/opt/agentlab/releases",
    },
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class ServerConfig:
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: str = None

    def section(self, name):
        return self.data.get(name, {})

    def get(self, section, key):
        return self.data[section][key]

    # Frequently used paths --------------------------------------------------
    @property
    def install_root(self): return self.get("server", "install_root")
    @property
    def application_root(self): return self.get("server", "application_root")
    @property
    def workspace_root(self): return self.get("server", "workspace_root")
    @property
    def system_root(self): return self.get("server", "system_root")
    @property
    def audit_log(self): return self.get("server", "audit_log")
    @property
    def state_dir(self): return self.get("server", "state_dir")
    @property
    def socket_path(self): return self.get("server", "socket_path")
    @property
    def users_group(self): return self.get("server", "users_group")
    @property
    def admins_group(self): return self.get("server", "admins_group")
    @property
    def idea_filename(self): return self.get("research", "idea_filename")

    def workspace_for(self, username):
        return os.path.join(self.workspace_root, username)


def load_config(path=None):
    """Load server.yaml merged over DEFAULTS. Missing file -> defaults."""
    path = path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        data = _deep_merge(DEFAULTS, loaded)
    return ServerConfig(data=data, path=path)


def config_from_dict(override):
    """Build a config from a dict (used by tests and the installer health check)."""
    return ServerConfig(data=_deep_merge(DEFAULTS, override), path=None)
