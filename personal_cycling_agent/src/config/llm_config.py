"""
LLM endpoint configuration persistence.

Stores OpenAI-compatible API settings in the vault so they survive
container restarts.  Falls back to environment variables (config.env)
when no vault config exists.
"""

import json
import os
from pathlib import Path

from src.config import vault_path

DEFAULT_CONFIG = {
    "base_url": "http://localhost:11434/v1",
    "api_key": "",
    "model": "",
    "timeout": 120,
}

_ENV_MAP = {
    "base_url": "LLM_BASE_URL",
    "api_key": "LLM_API_KEY",
    "model": "LLM_MODEL",
    "timeout": "LLM_TIMEOUT",
}


def _config_file() -> Path:
    return vault_path() / "llm_config.json"


def load_llm_config() -> dict:
    """Load LLM config from vault. Returns defaults if file missing."""
    path = _config_file()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(path) as f:
            data = json.load(f)
        result = dict(DEFAULT_CONFIG)
        for key in DEFAULT_CONFIG:
            if key in data:
                result[key] = data[key]
        return result
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_llm_config(config: dict) -> None:
    """Persist LLM config to vault."""
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def get_llm_base_url() -> str:
    """Resolve base URL: vault config > env var > default."""
    cfg = load_llm_config()
    val = cfg.get("base_url", "") or os.getenv("LLM_BASE_URL", "")
    return val or DEFAULT_CONFIG["base_url"]


def get_llm_api_key() -> str:
    """Resolve API key: vault config > env var > default."""
    cfg = load_llm_config()
    val = cfg.get("api_key", "") or os.getenv("LLM_API_KEY", "")
    return val or DEFAULT_CONFIG["api_key"]


def get_llm_model() -> str:
    """Resolve model name: vault config > env var > default (empty = auto-detect)."""
    cfg = load_llm_config()
    val = cfg.get("model", "") or os.getenv("LLM_MODEL", "")
    return val or DEFAULT_CONFIG["model"]


def get_llm_timeout() -> int:
    """Resolve timeout: vault config > env var > default."""
    cfg = load_llm_config()
    val = cfg.get("timeout")
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    env_val = os.getenv("LLM_TIMEOUT")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return DEFAULT_CONFIG["timeout"]