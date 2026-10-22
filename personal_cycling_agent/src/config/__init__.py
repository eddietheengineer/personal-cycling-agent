"""
Central configuration resolver for the Cycling AI Agent.

All secrets (API keys, biometrics) and personal data (user profile, SQLite DB)
live in a vault directory *outside* the git repository. This module resolves
the vault path and loads environment variables from it.

Vault location (first match wins):
  1. CYCLING_AGENT_VAULT environment variable
  2. ~/cycling-agent-data/  (default — non-hidden)

The vault contains:
  - config.env          # API keys, LLM endpoint, biometrics
  - user_profile.md     # Training goals, constraints, equipment
  - data/               # SQLite database and pipeline logs
  - raw/                # Raw FIT/TCX/GPX files downloaded from Intervals.icu

Call config.setup() once at program startup before accessing any env vars.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _vault_dir() -> Path:
    """Resolve the vault directory path.

    Priority: CYCLING_AGENT_VAULT > DATA_DIR > ~/cycling-agent-data
    """
    for var in ("CYCLING_AGENT_VAULT", "DATA_DIR"):
        override = os.environ.get(var)
        if override:
            return Path(override).expanduser().resolve()
    return Path.home() / "cycling-agent-data"


def setup() -> Path:
    """
    Initialize the vault: create directories if needed, load config.env.

    Returns the resolved vault path.
    """
    vault = _vault_dir()
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "data").mkdir(exist_ok=True)
    (vault / "raw").mkdir(exist_ok=True)

    env_file = vault / "config.env"
    if env_file.exists():
        load_dotenv(str(env_file), override=True)

    return vault


def vault_path() -> Path:
    """Return the resolved vault path (call after setup())."""
    return _vault_dir()


def config_env_path() -> Path:
    """Return the path to config.env in the vault."""
    return vault_path() / "config.env"


def user_profile_path() -> Path:
    """Return the path to user_profile.md in the vault."""
    return vault_path() / "user_profile.md"


def db_path(name: str = "cycling_agent.sqlite") -> Path:
    """Return the path to a SQLite database in the vault data directory."""
    return vault_path() / "data" / name


def raw_dir() -> Path:
    """Return the path to raw data files in the vault."""
    return vault_path() / "raw"

# Re-export schedule convenience functions
from src.config.schedule import (
    DEFAULT_SCHEDULE,
    get_available_days,
    get_available_hours,
    load_schedule,
    save_schedule,
)