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
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def model_settings():
    """Return the OpenAI-compatible endpoint settings from the environment."""
    load_env()
    return {
        "base_url": os.environ.get("CIC_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        "api_key": os.environ.get("CIC_API_KEY", ""),
        "model": os.environ.get("CIC_MODEL", "openai/gpt-6-luna"),
        "temperature": os.environ.get("CIC_TEMPERATURE", "0.7"),
        "json_mode": os.environ.get("CIC_JSON_MODE", "0") == "1",
    }
