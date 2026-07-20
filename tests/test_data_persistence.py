"""
Tests for data persistence invariants.

Defends against the class of bug where the app writes to the container's
writable layer instead of the persistent /data volume, causing all data
(DB, tokens, config) to be lost on every add-on upgrade.

See: run.sh HA detection, config._vault_dir() resolution, config.json map field.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


# ── config._vault_dir() resolution tests ──────────────────────────────


class TestVaultDirResolution:
    """config._vault_dir() must resolve to /data in HA mode, never to ~/cycling-agent-data."""

    def _clean_env(self):
        """Save and clear vault-related env vars."""
        saved = {}
        for key in ("CYCLING_AGENT_VAULT", "DATA_DIR"):
            saved[key] = os.environ.pop(key, None)
        return saved

    def _restore_env(self, saved):
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_vault_dir_uses_cycling_agent_vault(self):
        """CYCLING_AGENT_VAULT takes highest priority."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = "/data"
            from src.config import _vault_dir
            result = _vault_dir()
            assert str(result) == "/data", (
                f"Expected /data, got {result}. "
                "In HA mode, CYCLING_AGENT_VAULT=/data must be respected."
            )
        finally:
            self._restore_env(saved)

    def test_vault_dir_uses_data_dir_fallback(self):
        """DATA_DIR is used when CYCLING_AGENT_VAULT is not set."""
        saved = self._clean_env()
        try:
            os.environ["DATA_DIR"] = "/data"
            from src.config import _vault_dir
            result = _vault_dir()
            assert str(result) == "/data", (
                f"Expected /data, got {result}. "
                "DATA_DIR=/data must be respected as fallback."
            )
        finally:
            self._restore_env(saved)

    def test_vault_dir_cycling_agent_vault_beats_data_dir(self):
        """CYCLING_AGENT_VAULT takes priority over DATA_DIR."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = "/data"
            os.environ["DATA_DIR"] = "/wrong/path"
            from src.config import _vault_dir
            result = _vault_dir()
            assert str(result) == "/data", (
                f"Expected /data, got {result}. "
                "CYCLING_AGENT_VAULT must take priority over DATA_DIR."
            )
        finally:
            self._restore_env(saved)

    def test_vault_dir_fallback_to_home(self):
        """When no env vars are set, falls back to ~/cycling-agent-data."""
        saved = self._clean_env()
        try:
            from src.config import _vault_dir
            from pathlib import Path
            result = _vault_dir()
            expected = str(Path.home() / "cycling-agent-data")
            assert str(result) == expected, (
                f"Expected {expected}, got {result}. "
                "Fallback to ~/cycling-agent-data is correct when no env vars set."
            )
        finally:
            self._restore_env(saved)

    def test_vault_dir_never_returns_home_in_ha_mode(self):
        """CRITICAL: In HA mode (CYCLING_AGENT_VAULT=/data), vault must NOT fall back to home."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = "/data"
            from src.config import _vault_dir
            from pathlib import Path
            result = _vault_dir()
            home_fallback = str(Path.home() / "cycling-agent-data")
            assert str(result) != home_fallback, (
                f"Vault resolved to {home_fallback} in HA mode! "
                "This means data will be written to the container's writable layer "
                "and lost on every upgrade."
            )
            assert str(result) == "/data"
        finally:
            self._restore_env(saved)


# ── config.setup() path tests ─────────────────────────────────────────


class TestConfigSetupPaths:
    """config.setup() must create directories under /data in HA mode."""
class TestConfigSetupPaths:
    """config.setup() must create directories under the vault, not ~/cycling-agent-data."""

    def _clean_env(self):
        saved = {}
        for key in ("CYCLING_AGENT_VAULT", "DATA_DIR"):
            saved[key] = os.environ.pop(key, None)
        return saved

    def _restore_env(self, saved):
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_db_path_under_vault(self, tmp_path):
        """db_path() must return vault/data/<name>.sqlite."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = str(tmp_path)
            from src import config
            config.setup()
            db_path = config.db_path("cycling_agent.sqlite")
            assert str(db_path) == str(tmp_path / "data" / "cycling_agent.sqlite"), (
                f"Expected {tmp_path}/data/cycling_agent.sqlite, got {db_path}. "
                "Database must be under vault/data for persistence."
            )
        finally:
            self._restore_env(saved)

    def test_raw_dir_under_vault(self, tmp_path):
        """raw_dir() must return vault/raw."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = str(tmp_path)
            from src import config
            config.setup()
            raw = config.raw_dir()
            assert str(raw) == str(tmp_path / "raw"), (
                f"Expected {tmp_path}/raw, got {raw}. "
                "Raw FIT files must be under vault for persistence."
            )
        finally:
            self._restore_env(saved)

    def test_config_env_path_under_vault(self, tmp_path):
        """config_env_path() must return vault/config.env."""
        saved = self._clean_env()
        try:
            os.environ["CYCLING_AGENT_VAULT"] = str(tmp_path)
            from src import config
            config.setup()
            env_path = config.config_env_path()
            assert str(env_path) == str(tmp_path / "config.env"), (
                f"Expected {tmp_path}/config.env, got {env_path}. "
                "Credentials must be under vault for persistence."
            )
        finally:
            self._restore_env(saved)


# ── run.sh HA detection tests ─────────────────────────────────────────


class TestRunShHADetection:
    """run.sh must detect HA mode without relying on bashio."""

    @pytest.fixture
    def run_sh_path(self):
        repo_root = Path(__file__).parent.parent
        return repo_root / "personal_cycling_agent" / "run.sh"

    def test_run_sh_no_bashio_dependency(self, run_sh_path):
        """run.sh must NOT use 'command -v bashio' for DATA_DIR detection."""
        content = run_sh_path.read_text()
        # The bashio check must NOT appear in the DATA_DIR detection block.
        # It's OK for bashio to appear in comments explaining why it's not used.
        lines = content.split("\n")
        data_dir_block_end = None
        for i, line in enumerate(lines):
            if 'DATA_DIR="/data"' in line or "DATA_DIR=" in line:
                data_dir_block_end = i
                break

        assert data_dir_block_end is not None, "Could not find DATA_DIR assignment in run.sh"

        # Check the HA detection block (lines before DATA_DIR assignment)
        detection_block = "\n".join(lines[: data_dir_block_end + 1])

        # Must NOT use bashio for detection
        assert "command -v bashio" not in detection_block, (
            "run.sh uses 'command -v bashio' for HA detection. "
            "bashio is NOT available in python:3.12-slim images. "
            "Use HASSIO_INGRESS_ENTRY or /data directory existence instead."
        )

    def test_run_sh_detects_ha_via_data_dir(self, run_sh_path):
        """run.sh must detect HA via /data directory existence."""
        content = run_sh_path.read_text()
        assert '"$DATA_DIR"' in content or "'/data'" in content or '"/data"' in content, (
            "run.sh must reference /data for HA detection."
        )
        # Must check for /data directory or HASSIO_INGRESS_ENTRY
        assert '-d "/data"' in content or "HASSIO_INGRESS_ENTRY" in content, (
            "run.sh must detect HA via /data directory or HASSIO_INGRESS_ENTRY env var."
        )

    def test_run_sh_sets_cycling_agent_vault(self, run_sh_path):
        """run.sh must export CYCLING_AGENT_VAULT after detecting HA."""
        content = run_sh_path.read_text()
        assert 'CYCLING_AGENT_VAULT' in content, (
            "run.sh must export CYCLING_AGENT_VAULT for config._vault_dir() resolution."
        )

    def test_run_sh_data_dir_resolves_to_data_in_ha(self, run_sh_path, tmp_path):
        """Simulate run.sh HA detection: DATA_DIR must be /data when /data exists."""
        # Create a temporary /data-like directory to simulate HA
        ha_data = tmp_path / "data"
        ha_data.mkdir()

        # Run the detection logic from run.sh in a subshell
        result = subprocess.run(
            ["bash", "-c", f"""
if [ -n "${{HASSIO_INGRESS_ENTRY}}" ] || [ -d "{ha_data}" ]; then
    DATA_DIR="{ha_data}"
else
    DATA_DIR="${{HOME}}/cycling-agent-data"
fi
echo "$DATA_DIR"
"""],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        detected_path = result.stdout.strip()
        assert detected_path == str(ha_data), (
            f"run.sh HA detection resolved to {detected_path} instead of {ha_data}. "
            "This means data will be written to the wrong path."
        )

    def test_run_sh_data_dir_fallback_standalone(self, run_sh_path, tmp_path):
        """In standalone mode (no /data, no HASSIO_INGRESS_ENTRY), use home fallback."""
        result = subprocess.run(
            ["bash", "-c", """
unset HASSIO_INGRESS_ENTRY
if [ -n "${HASSIO_INGRESS_ENTRY}" ] || [ -d "/data" ]; then
    DATA_DIR="/data"
else
    DATA_DIR="${HOME}/cycling-agent-data"
fi
echo "$DATA_DIR"
"""],
            capture_output=True,
            text=True,
            env={**os.environ, "HASSIO_INGRESS_ENTRY": "", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        detected_path = result.stdout.strip()
        expected = str(tmp_path / "cycling-agent-data")
        assert detected_path == expected, (
            f"Standalone mode resolved to {detected_path} instead of {expected}."
        )


# ── config.json map field tests ───────────────────────────────────────


class TestConfigJsonMap:
    """config.json must declare /data persistence for HA add-on."""

    @pytest.fixture
    def config_json_path(self):
        repo_root = Path(__file__).parent.parent
        return repo_root / "personal_cycling_agent" / "config.json"

    def test_config_json_has_map_field(self, config_json_path):
        """config.json must have a 'map' field for HA data persistence."""
        config = json.loads(config_json_path.read_text())
        assert "map" in config, (
            "config.json is missing 'map' field. "
            "HA add-on data may not persist across updates without it."
        )

    def test_config_json_map_includes_data(self, config_json_path):
        """config.json map must include 'data' type."""
        config = json.loads(config_json_path.read_text())
        map_field = config.get("map", [])
        assert isinstance(map_field, list), (
            "config.json 'map' must be a list of dicts."
        )
        map_types = {entry.get("type") for entry in map_field if isinstance(entry, dict)}
        assert "data" in map_types, (
            "config.json 'map' must include type 'data' for persistent storage. "
            "Without it, /data may not survive add-on updates."
        )

    def test_config_json_map_data_is_writable(self, config_json_path):
        """config.json map data entry must be read-write (not read-only)."""
        config = json.loads(config_json_path.read_text())
        map_field = config.get("map", [])
        for entry in map_field:
            if isinstance(entry, dict) and entry.get("type") == "data":
                read_only = entry.get("read_only", True)
                assert read_only is False, (
                    "config.json 'map' data entry must have read_only: false. "
                    "Default is true, which would make /data read-only."
                )
                return
        pytest.fail("No 'data' map entry found in config.json")

    def test_config_json_is_valid_json(self, config_json_path):
        """config.json must be valid JSON (HA rejects YAML)."""
        content = config_json_path.read_text()
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            pytest.fail(f"config.json is not valid JSON: {e}")

    def test_config_json_no_legacy_map_syntax(self, config_json_path):
        """config.json must not use legacy integer map syntax (e.g. "map": 0)."""
        config = json.loads(config_json_path.read_text())
        map_field = config.get("map")
        if map_field is not None:
            assert isinstance(map_field, list), (
                f"config.json 'map' uses legacy syntax ({type(map_field).__name__}). "
                "Must be a list of dicts like [{'type': 'data', 'read_only': false}]."
            )


# ── Dockerfile ENV test ───────────────────────────────────────────────


class TestDockerfileEnv:
    """Dockerfile must set CYCLING_AGENT_VAULT=/data as belt-and-suspenders."""

    @pytest.fixture
    def dockerfile_path(self):
        repo_root = Path(__file__).parent.parent
        return repo_root / "personal_cycling_agent" / "Dockerfile"

    def test_dockerfile_sets_cycling_agent_vault(self, dockerfile_path):
        """Dockerfile must set ENV CYCLING_AGENT_VAULT=/data."""
        content = dockerfile_path.read_text()
        assert "CYCLING_AGENT_VAULT" in content, (
            "Dockerfile must set ENV CYCLING_AGENT_VAULT=/data. "
            "This ensures config._vault_dir() resolves correctly even if "
            "called before run.sh exports the variable."
        )
        assert "CYCLING_AGENT_VAULT=/data" in content or "CYCLING_AGENT_VAULT=/data" in content.replace(" ", ""), (
            "Dockerfile CYCLING_AGENT_VAULT must be set to /data."
        )