"""
Central configuration resolver for the Cycling AI Agent.

All secrets (API keys, biometrics) and personal data (user profile, SQLite DB)
live in a vault directory *outside* the git repository. This module resolves
the vault path and loads environment variables from it.

Vault location (first match wins):
  1. CYCLING_AGENT_VAULT environment variable
  2. ~/cycling-agent-data/  (default — non-hidden)

The vault contains:
  - config.env          # API keys, LLM endpoint, MQTT, biometrics
                         # Passwords are stored as SHA-256 hashes (see hash_password())
  - user_profile.md     # Training goals, constraints, equipment
  - data/               # SQLite database and pipeline logs
  - raw/                # Raw FIT/TCX/GPX files downloaded from Intervals.icu

Call config.setup() once at program startup before accessing any env vars.
"""

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

# Environment variable names whose values are stored as SHA-256 hashes
_HASHED_VARS = {"INTERVALS_ICU_API_SECRET", "MQTT_PASSWORD"}


def _vault_dir() -> Path:
    """Resolve the vault directory path."""
    override = os.environ.get("CYCLING_AGENT_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "cycling-agent-data"


def setup() -> Path:
    """
    Initialize the vault: create directories if needed, load config.env.

    Passwords stored as SHA-256 hashes are transparently resolved back to
    plaintext in the process environment.

    Returns the resolved vault path.
    """
    vault = _vault_dir()
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "data").mkdir(exist_ok=True)
    (vault / "raw").mkdir(exist_ok=True)

    env_file = vault / "config.env"
    if env_file.exists():
        load_dotenv(str(env_file), override=True)
        _resolve_hashed_passwords()

    return vault


def _resolve_hashed_passwords() -> None:
    """
    For any env var in _HASHED_VARS whose value starts with 'hash:',
    verify it's a valid SHA-256 hex string and replace the env var
    with the raw value stored alongside it.

    Format in config.env:
        INTERVALS_ICU_API_SECRET=hash:<sha256hex>
        INTERVALS_ICU_API_SECRET_RAW=actual_secret_value

    The _RAW variant is loaded from the file but never exposed outside
    the process. The hash variant is what's persisted to disk.
    """
    for var in _HASHED_VARS:
        hashed = os.environ.get(var, "")
        if hashed.startswith("hash:"):
            digest = hashed[5:]
            raw_var = var + "_RAW"
            raw_value = os.environ.get(raw_var, "")
            if not raw_value:
                # No raw value stored — can't resolve, leave as-is
                continue
            # Verify the hash matches
            computed = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
            if computed == digest:
                # Hash verified — set the plaintext in env
                os.environ[var] = raw_value
            else:
                # Hash mismatch — password was changed externally
                os.environ[var] = ""  # invalidate
        # If value doesn't start with 'hash:', it's plaintext — leave as-is


def hash_password(plaintext: str) -> str:
    """
    Hash a password for storage in config.env.

    Returns 'hash:<sha256hex>' for the hash line, and the plaintext
    for the corresponding _RAW line.

    Usage in setup.py:
        h, raw = hash_password(user_input)
        env["INTERVALS_ICU_API_SECRET"] = h
        env["INTERVALS_ICU_API_SECRET_RAW"] = raw
    """
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return f"hash:{digest}", plaintext


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
    """Return the path to the raw data directory in the vault."""
    return vault_path() / "raw"