"""
Central configuration resolver for the Cycling AI Agent.

All secrets (API keys, biometrics) and personal data (user profile, SQLite DB)
live in a vault directory *outside* the git repository. This module resolves
the vault path and loads environment variables from it.

Vault location (first match wins):
  1. CYCLING_AGENT_VAULT environment variable
  2. ~/cycling-agent-data/  (default — non-hidden)

The vault contains:
  - config.env          # API keys, LLM endpoint, credentials, biometrics
                         # Passwords are stored as PBKDF2 hashes (see hash_password())
  - user_profile.md     # Training goals, constraints, equipment
  - data/               # SQLite database and pipeline logs
  - raw/                # Raw FIT/TCX/GPX files downloaded from Intervals.icu

Call config.setup() once at program startup before accessing any env vars.
"""

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

# Environment variable names whose values are stored as pbkdf2 hashes
_HASHED_VARS = {"GARMIN_PASSWORD"}


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

    Passwords stored as pbkdf2 hashes are transparently resolved back to
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
    For any env var in _HASHED_VARS whose value starts with 'pbkdf2:',
    verify it against the _RAW value stored alongside it and store
    the raw plaintext in _resolved_credentials dict.

    Format in config.env:
        GARMIN_PASSWORD=pbkdf2:<salt_hex>:<hash_hex>
        GARMIN_PASSWORD_RAW=actual_secret_value

    Resolved passwords are stored in _resolved_credentials (not os.environ)
    to minimize exposure in /proc/<pid>/environ and child processes.
    """
    for var in _HASHED_VARS:
        hashed = os.environ.get(var, "")
        raw_var = var + "_RAW"
        raw_value = ""
        if hashed.startswith("pbkdf2:"):
            parts = hashed[9:].split(":", 1)
            if len(parts) != 2:
                logger.warning(f"Malformed pbkdf2 hash for {var}, skipping resolution")
                continue
            salt_hex, hash_hex = parts
            raw_value = os.environ.get(raw_var, "")
            if not raw_value:
                continue
            try:
                salt = bytes.fromhex(salt_hex)
            except ValueError:
                logger.warning(f"Invalid salt hex for {var}, skipping resolution")
                continue
            computed = hashlib.pbkdf2_hmac('sha256', raw_value.encode(), salt, 600000).hex()
            if computed == hash_hex:
                _resolved_credentials[var] = raw_value
                os.environ[var] = ""
            else:
                logger.warning(f"Hash mismatch for {var}, credential not resolved")
        elif hashed.startswith("hash:"):
            # Legacy SHA-256 format — resolve for backward compatibility
            digest = hashed[5:]
            raw_value = os.environ.get(raw_var, "")
            if not raw_value:
                continue
            computed = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
            if computed == digest:
                _resolved_credentials[var] = raw_value
                os.environ[var] = ""
            else:
                logger.warning(f"Legacy hash mismatch for {var}, credential not resolved")
        # If value doesn't start with 'pbkdf2:' or 'hash:', it's plaintext — leave as-is

        # Clear _RAW env var after resolution (success or failure)
        os.environ.pop(raw_var, None)


# Module-level store for resolved credentials (not in os.environ)
_resolved_credentials: dict[str, str] = {}


def get_resolved_credential(var: str) -> str:
    """Get a resolved credential, checking _resolved_credentials first, then os.environ."""
    if var in _resolved_credentials:
        return _resolved_credentials[var]
    return os.environ.get(var, "")


def hash_password(plaintext: str) -> tuple[str, str]:
    """
    Hash a password for storage in config.env using pbkdf2.

    Returns 'pbkdf2:<salt_hex>:<hash_hex>' for the hash line.
    No _RAW line is needed — garminconnect caches auth tokens,
    so passwords are only used for initial authentication.

    Returns (hash_line, plaintext) for compatibility with callers
    that still need the raw value at runtime.
    """
    salt = os.urandom(16)
    hash_hex = hashlib.pbkdf2_hmac('sha256', plaintext.encode(), salt, 600000).hex()
    return f"pbkdf2:{salt.hex()}:{hash_hex}", plaintext


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