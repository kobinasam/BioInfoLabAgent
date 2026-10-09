"""
Secret hygiene: redaction for anything that may reach logs, and the list of
environment variables that must never be passed to user-owned processes.
"""
import os
import re

SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "SEMANTIC_SCHOLAR_API_KEY",
)

_SECRET_KEY_NAMES = re.compile(r"(api[-_]?key|secret|password|passwd|token|authorization|credential)", re.I)

_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-(?:or-|ant-|proj-)?[A-Za-z0-9_\-]{16,}"),   # OpenAI / OpenRouter / Anthropic style
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                     # Google
    re.compile(r"hf_[A-Za-z0-9]{20,}"),                         # HuggingFace
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"alr_[A-Za-z0-9_\-]{20,}"),                     # AgentLab per-run proxy tokens
]

REDACTED = "[REDACTED]"


def _known_secret_values():
    values = []
    for name in SECRET_ENV_VARS:
        v = os.environ.get(name)
        if v and len(v) >= 8:
            values.append(v)
    return values


def redact_text(text):
    if not isinstance(text, str):
        return text
    for value in _known_secret_values():
        text = text.replace(value, REDACTED)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(obj):
    """Recursively redact secrets from a JSON-like structure."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_NAMES.search(k) and not isinstance(v, (dict, list, bool)) and v is not None:
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def scrubbed_environment(base=None, keep=None):
    """Copy of the environment without any secret variables."""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in SECRET_ENV_VARS or (_SECRET_KEY_NAMES.search(name) and name not in (keep or ())):
            env.pop(name, None)
    return env


def config_contains_secrets(data):
    """True if a YAML experiment config tries to carry API keys."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and re.search(r"api[-_]?key", k, re.I) and v:
                return True
            if config_contains_secrets(v):
                return True
    elif isinstance(data, list):
        return any(config_contains_secrets(v) for v in data)
    return False
