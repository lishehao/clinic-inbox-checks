"""Paths and a dependency-free .env loader.

The API key is read from the environment or from .env at run time. It is
never written to run logs, reports or screenshots.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs"


def load_env(path=None):
    """Load KEY=VALUE lines into os.environ without overriding values already set."""
    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.exists():
        return
    values = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:  # quoted, maybe followed by a comment
            value = value[1:value.find(value[0], 1)]
        elif " #" in value:  # trailing comment after an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value  # a later line in the file wins over an earlier one
    for key, value in values.items():
        os.environ.setdefault(key, value)


def read_key_file(path):
    """Read an API key from a file of its own.

    The file holds KEY=VALUE lines (CIC_API_KEY, OPENROUTER_API_KEY or API_KEY, or a single value)
    or one bare key. Raises ValueError if the file is missing or holds no key. The key is never printed.
    """
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise ValueError(f"CIC_API_KEY_FILE points to {p}, which does not exist.")
    values, bare = {}, []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
        else:
            bare.append(line)
    for name in ("CIC_API_KEY", "OPENROUTER_API_KEY", "API_KEY"):
        if values.get(name):
            return values[name]
    filled = [v for v in values.values() if v]
    if len(filled) == 1:
        return filled[0]
    if not values and len(bare) == 1:
        return bare[0]
    raise ValueError(f"No API key found in {p} (expected a line like OPENROUTER_API_KEY=...).")


DEFAULT_KEY_FILES = {"openrouter.ai": "~/.secrets/openrouter/openrouter.env"}


def default_key_file(base_url):
    """With no key configured, a known key file for the endpoint's host, if it exists ("" otherwise)."""
    for host, path in DEFAULT_KEY_FILES.items():
        if host in base_url and Path(os.path.expanduser(path)).is_file():
            return path
    return ""


def model_settings():
    """Return the model settings from the environment.

    backend "api" (default): an OpenAI-compatible /chat/completions endpoint with an API key.
    backend "chatgpt": the signed-in user's ChatGPT plan (python -m cic login), via the Responses API.
    """
    load_env()
    return {
        "backend": os.environ.get("CIC_BACKEND", "api").strip().lower() or "api",
        "base_url": os.environ.get("CIC_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        "api_key": os.environ.get("CIC_API_KEY", ""),
        "api_key_file": os.environ.get("CIC_API_KEY_FILE", "").strip(),
        "model": os.environ.get("CIC_MODEL", "openai/gpt-6-luna"),
        "chatgpt_model": os.environ.get("CIC_CHATGPT_MODEL", "").strip(),
        "temperature": os.environ.get("CIC_TEMPERATURE", "0.7"),
        "json_mode": os.environ.get("CIC_JSON_MODE", "0") == "1",
    }


def model_name(settings=None):
    """The model id runs are labelled with, without any network call ("" if not chosen yet)."""
    s = settings or model_settings()
    return s["chatgpt_model"] if s["backend"] == "chatgpt" else s["model"]
