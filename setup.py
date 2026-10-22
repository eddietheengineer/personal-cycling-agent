#!/usr/bin/env python3
"""
Interactive setup wizard for the Cycling AI Agent.

Guides the user through configuring .env, USER_PROFILE.md, and Ollama.
Accepts current values (if set) or prompts for new ones.

Usage:
    python setup.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = PROJECT_ROOT / ".venv"


def _ensure_venv() -> None:
    """Create and activate a virtual environment if it doesn't exist."""
    if not VENV_DIR.exists():
        print(f"  Creating virtual environment at {VENV_DIR}...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        print(f"  Installing dependencies...")
        subprocess.run([str(VENV_DIR / "bin" / "pip"), "install", "-r", "requirements.txt"], check=True)
    else:
        print(f"  Virtual environment found at {VENV_DIR}.")

    # Activate venv by prepending to PATH and updating sys.path
    venv_python = VENV_DIR / "bin" / "python"
    if venv_python.exists() and venv_python != Path(sys.executable):
        print(f"  Reloading with venv Python: {venv_python}")
        os.execv(str(venv_python), [str(venv_python), __file__])

# Run venv setup at module level, before any src/ imports
_ensure_venv()


def _vault_dir() -> Path:
    """Resolve the vault directory (outside the git repo)."""
    override = os.environ.get("CYCLING_AGENT_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "cycling-agent-data"


VAULT = _vault_dir()
ENV_PATH = VAULT / "config.env"
PROFILE_PATH = VAULT / "user_profile.md"
ENV_EXAMPLE = os.path.join(PROJECT_ROOT, ".env.example")
PROFILE_TEMPLATE = os.path.join(PROJECT_ROOT, "USER_PROFILE_TEMPLATE.md")


# ── Helpers ──────────────────────────────────────────────────────────

def banner(title: str) -> None:
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def prompt(label: str, default: str = "") -> str:
    if default:
        print(f"  {label} [{default}]: ", end="", flush=True)
    else:
        print(f"  {label}: ", end="", flush=True)
    value = input().strip()
    return value if value else default


try:
    from src.config import vault_path  # noqa: F401 — fails fast if deps missing
except ImportError as e:
    import sys
    missing = str(e).split("'")[1] if "'" in str(e) else str(e)
    print(f"Missing dependency: {missing}")
    print(f"Run: pip install -r requirements.txt")
    sys.exit(1)


def read_env() -> dict[str, str]:
    """Parse KEY=VALUE lines from .env (ignoring comments and blanks)."""
    env = {}
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def write_env(env: dict[str, str]) -> None:
    """Write .env from the dict, preserving order from .env.example."""
    # Read example to get canonical key order and comments
    lines = []
    if os.path.exists(ENV_EXAMPLE):
        with open(ENV_EXAMPLE, "r") as f:
            example_lines = f.readlines()
    else:
        example_lines = []

    seen = set()
    for line in example_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line.rstrip("\n"))
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in env:
                lines.append(f"{key}={env[key]}")
                seen.add(key)
            else:
                lines.append(line.rstrip("\n"))  # keep original (empty value)

    # Add any keys not in the example
    for key, value in env.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def copy_template(src: str, dst: str) -> bool:
    """Copy template if destination doesn't exist. Returns True if copied."""
    if os.path.exists(dst):
        return False
    if not os.path.exists(src):
        return False
    shutil.copy2(src, dst)
    return True


def check_ollama() -> tuple[bool, list[str]]:
    """Check if Ollama is running and return (running, available_models)."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            models = []
            for line in result.stdout.strip().split("\n")[1:]:  # skip header
                parts = line.split()
                if parts:
                    models.append(parts[0].split(":")[0])
            return True, models
        return False, []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, []


def _check_openai_compat(base_url: str) -> tuple[bool, list[str]]:
    """Check if an OpenAI-compatible endpoint is reachable and list models."""
    import requests as _req
    url = base_url.rstrip("/") + "/models"
    api_key = os.getenv("LLM_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = _req.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("id", m.get("name", "")) for m in data.get("data", [])]
            return True, models
    except Exception:
        pass
    return False, []

# ── Sections ─────────────────────────────────────────────────────────

def setup_env() -> None:
    banner("Garmin Connect (Personal API)")

    env = read_env()

    print("  Garmin Connect is the primary data source for this agent.")
    print("  It provides HRV/RMSSD, RHR, stress, sleep, weight, and activities.")
    print("  Create a Personal API key in the Garmin Connect web UI:")
    print("  Settings -> Developer Settings -> Personal API (API key).")
    print()

    env["GARMIN_PERSONAL_API_KEY"] = prompt(
        "  Garmin Personal API key", env.get("GARMIN_PERSONAL_API_KEY", "")
    )

    if not env["GARMIN_PERSONAL_API_KEY"]:
        print("\n  ⚠  Personal API key is required for Garmin sync. Set it later in config.env or re-run setup.")

    banner("LLM (OpenAI-compatible)")

    print("  Supports vLLM, Ollama (with OpenAI compat), LM Studio, LocalAI, etc.")
    print("  Set LLM_BASE_URL to the /v1 base URL (e.g. http://localhost:8010/v1)")
    print()

    env["LLM_BASE_URL"] = prompt(
        "  LLM Base URL (/v1)", env.get("LLM_BASE_URL", "http://localhost:11434/v1")
    )
    env["LLM_API_KEY"] = prompt(
        "  API Key (blank for local)", env.get("LLM_API_KEY", "")
    )

    # Try to discover available models
    if env["LLM_BASE_URL"]:
        running, models = _check_openai_compat(env["LLM_BASE_URL"])
        if running and models:
            print(f"  Found {len(models)} model(s):")
            for idx, m in enumerate(models, 1):
                print(f"    {idx}. {m}")
            if len(models) > 1:
                choice = prompt(f"\n  Select model (1-{len(models)}, or blank for default)", "")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(models):
                        env["LLM_MODEL"] = models[idx]
                        print(f"  → Selected: {models[idx]}")
                    else:
                        print("  Invalid selection, using default.")
                except ValueError:
                    pass
            elif len(models) == 1:
                env["LLM_MODEL"] = models[0]
                print(f"  → Auto-selected: {models[0]}")
        elif running and not models:
            print("  ⚠  Endpoint reachable but no models listed.")
        else:
            print("  ⚠  Cannot reach LLM endpoint. Make sure your server is running.")

    if not env.get("LLM_MODEL"):
        env["LLM_MODEL"] = prompt(
            "  LLM Model", env.get("LLM_MODEL", "llama3")
        )
    env["LLM_TIMEOUT"] = prompt(
        "  LLM Timeout (seconds)", env.get("LLM_TIMEOUT", "120")
    )

    banner("Rider Biometrics")

    env["RIDER_WEIGHT_KG"] = prompt(
        "  Weight (kg)", env.get("RIDER_WEIGHT_KG", "")
    )


    write_env(env)
    print(f"\n  ✓  Written to {ENV_PATH}")


def setup_profile() -> None:
    banner("User Profile")

    copied = copy_template(PROFILE_TEMPLATE, PROFILE_PATH)
    if copied:
        print(f"  Created {PROFILE_PATH} from template.")
    else:
        print(f"  {PROFILE_PATH} already exists.")

    print(f"\n  Edit {PROFILE_PATH} with your training goals and constraints.")
    print("  Or press Enter to skip for now.")
    prompt("  Press Enter to continue", "")


def setup_garmin() -> None:
    banner("Garmin Data (Optional)")
    print("  Two options for getting your Garmin data:")
    print("  1. Import a data export ZIP (historical activities + wellness)")
    print("  2. Sync from Garmin Connect API (includes HRV/RMSSD)")
    print()

    # Option 1: Data export
    answer = prompt("  Path to Garmin export ZIP (or Enter to skip)", "")
    if answer:
        path = os.path.expanduser(answer.strip())
        if not os.path.exists(path):
            print(f"  ⚠  File not found: {path}")
        else:
            print(f"  Importing {path}...")
            from src.ingestion.garmin_export import import_garmin_export
            counts = import_garmin_export(path)
            print(f"  ✓  Imported {counts['wellness_records']} wellness records, {counts['activity_records']} activities")
    else:
        print("  No export ZIP provided.")

    # Option 2: API sync
    print()
    api_key = os.getenv("GARMIN_PERSONAL_API_KEY", "")
    if api_key:
        answer = prompt("  Sync HRV data from the Garmin Personal API? [y/N]", "").lower()
        if answer == "y":
            try:
                from src.ingestion.garmin_personal import sync_garmin
                days = prompt("  Days to sync back", "90")
                counts = sync_garmin(days=int(days))
                print(f"  ✓  Synced {counts['wellness_records']} records ({counts['with_hrv']} with HRV)")
            except Exception as e:
                print(f"  ⚠  Sync failed: {e}")
        else:
            print("  Skipped API sync.")
    else:
        print("  Personal API key not set. Set GARMIN_PERSONAL_API_KEY in config.env for API sync.")
    print()

def setup_cron() -> None:
    banner("Daily Automation")

    print("  Install a cron job to run the pipeline at 05:00 each morning?")
    answer = prompt("  [y/N]", "").lower()

    if answer == "y":
        cron_script = os.path.join(PROJECT_ROOT, "setup_cron.sh")
        if os.path.exists(cron_script):
            subprocess.run(["bash", cron_script], check=False)
        else:
            print(f"  ⚠  {cron_script} not found. Install manually:")
            print(f"    crontab -e")
            print(f"    0 5 * * * cd {PROJECT_ROOT} && python -m src.main >> {VAULT}/data/pipeline.log 2>&1")
    else:
        print("  Skipped. Run 'bash setup_cron.sh' later to enable.")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:

    banner("🚴‍♂️ Cycling AI Agent — Setup Wizard")
    print(f"  Project: {PROJECT_ROOT}")
    print(f"  Vault:   {VAULT}")
    print()

    # Create vault directory
    VAULT.mkdir(parents=True, exist_ok=True)

    # Ensure config.env exists (copy from template if first run)
    if not ENV_PATH.exists():
        if os.path.exists(ENV_EXAMPLE):
            shutil.copy2(ENV_EXAMPLE, ENV_PATH)
            print(f"  Created {ENV_PATH} from template.")
        else:
            ENV_PATH.touch()

    setup_env()
    setup_profile()
    setup_garmin()

    banner("Setup Complete!")
    print()
    print(f"  Secrets stored in: {VAULT}")
    print(f"  - {ENV_PATH}       (API keys, LLM)")
    print(f"  - {PROFILE_PATH}  (training profile)")
    print(f"  - {VAULT}/data/   (SQLite DB, logs)")
    print()
    print("  Next steps:")
    print(f"  1. Edit {PROFILE_PATH} with your training goals")
    print("  2. Make sure Ollama is running:  ollama serve")
    print("  3. Test the pipeline:  python -m src.main")
    print()


if __name__ == "__main__":
    main()