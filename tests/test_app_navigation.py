"""Tests for sidebar navigation: all 6 buttons, default page, page loading."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

VISUALIZE_PATH = str(Path(__file__).resolve().parent.parent / "src" / "visualize.py")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _minimal_vault(tmp_path):
    """Create a vault with a minimal SQLite DB for AppTest."""
    vault = tmp_path / "vault"
    data_dir = vault / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "cycling_agent.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS wellness (date TEXT PRIMARY KEY, weight REAL, resting_hr REAL, rmssd REAL, stress REAL, sleep_score REAL, sleep_hours REAL, steps INTEGER, spo2 REAL, body_battery_start REAL, body_battery_end REAL, calories REAL, active_calories REAL, distance_m REAL, min_hr REAL, max_hr REAL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("CREATE TABLE IF NOT EXISTS activities (id TEXT PRIMARY KEY, start_date TEXT NOT NULL, activity_type TEXT, duration REAL, distance REAL, average_power REAL, max_power REAL, average_hr REAL, max_hr REAL, calories REAL, tss REAL, ifr REAL, normalized_power REAL, file_type TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("CREATE TABLE IF NOT EXISTS morning_checkin (date TEXT PRIMARY KEY, soreness INTEGER, stress INTEGER, sleep_quality INTEGER, mood INTEGER, energy INTEGER, motivation INTEGER, caffeine INTEGER DEFAULT 0, alcohol INTEGER DEFAULT 0, late_meals INTEGER DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("CREATE TABLE IF NOT EXISTS sync_state (source TEXT PRIMARY KEY, last_synced_at TEXT NOT NULL, details TEXT, resume_offset INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS activity_metrics (activity_id TEXT NOT NULL, ftp_used REAL, normalized_power REAL, intensity_factor REAL, tss REAL, variability_index REAL, w_prime_capacity REAL, w_prime_min_balance REAL, decoupling_drift REAL, duration_sec REAL, computed_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (activity_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS activity_streams (id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id TEXT NOT NULL, elapsed REAL NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity ON activity_streams(activity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_metric ON activity_streams(metric)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity_metric ON activity_streams(activity_id, metric)")
    conn.execute("CREATE TABLE IF NOT EXISTS activity_routes (activity_id TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY (activity_id, sequence))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routes_activity ON activity_routes(activity_id)")
    conn.commit()
    conn.close()

    os.environ["CYCLING_AGENT_VAULT"] = str(vault)
    os.environ["PANDAS_PYARROW_TO_PYTHON"] = "deprecated"
    os.environ["ATHLETE_NAME"] = "Test Rider"
    os.environ["WEIGHT_KG"] = "72"
    os.environ["HEIGHT_CM"] = "175"
    os.environ["DISCIPLINE"] = "road"
    os.environ["FTP_WATTS"] = "250"
    os.environ["MAX_HR"] = "190"
    os.environ["RESTING_HR"] = "52"
    os.environ["LT1_POWER"] = "150"
    os.environ["LT2_POWER"] = "220"
    os.environ["PRIMARY_GOAL"] = "Improve FTP"
    os.environ["SECONDARY_GOAL"] = "Race readiness"
    os.environ["TRAINING_DAYS"] = "Mon,Wed,Fri"
    os.environ["MAX_SESSION_DURATION"] = "2h"
    os.environ["TERRAIN"] = "Hilly"
    os.environ["BIKES"] = "Road bike"
    os.environ["POWER_METER"] = "PowerTap"
    os.environ["HR_MONITOR"] = "Garmin HRM"
    os.environ["GARMIN_EMAIL"] = ""
    os.environ["GARMIN_PASSWORD"] = ""

    yield str(vault)

    for k in list(os.environ.keys()):
        if k in ("CYCLING_AGENT_VAULT", "PANDAS_PYARROW_TO_PYTHON",
                  "ATHLETE_NAME", "WEIGHT_KG", "HEIGHT_CM", "DISCIPLINE",
                  "FTP_WATTS", "MAX_HR", "RESTING_HR", "LT1_POWER", "LT2_POWER",
                  "PRIMARY_GOAL", "SECONDARY_GOAL", "TRAINING_DAYS",
                  "MAX_SESSION_DURATION", "TERRAIN", "BIKES", "POWER_METER",
                  "HR_MONITOR", "GARMIN_EMAIL", "GARMIN_PASSWORD"):
            del os.environ[k]


@pytest.fixture
def app(_minimal_vault) -> AppTest:
    """Create a fresh AppTest for the visualize script."""
    return AppTest.from_file(VISUALIZE_PATH)


def _go(app: AppTest, page: str) -> AppTest:
    """Navigate to *page* by setting session state directly, then re-run."""
    app.session_state.nav_page = page
    app.run()
    return app


# ---------------------------------------------------------------------------
# Navigation Tests
# ---------------------------------------------------------------------------


class TestNavigation:
    """Verify all 6 sidebar nav buttons, default page, and page loading."""

    def test_default_page_is_dashboard(self, app):
        """Default page on load is Dashboard."""
        app.run()
        assert app.session_state.nav_page == "Dashboard"

    def test_dashboard_button_exists(self, app):
        """Sidebar contains the Dashboard nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Dashboard" in btn_keys

    def test_activity_detail_button_exists(self, app):
        """Sidebar contains the Activities nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Activity Detail" in btn_keys

    def test_trends_button_exists(self, app):
        """Sidebar contains the Trends nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Trends" in btn_keys

    def test_map_button_exists(self, app):
        """Sidebar contains the Map nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Map" in btn_keys

    def test_profile_button_exists(self, app):
        """Sidebar contains the Profile nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Profile" in btn_keys

    def test_settings_button_exists(self, app):
        """Sidebar contains the Settings nav button."""
        app.run()
        btn_keys = [b.key for b in app.sidebar.button]
        assert "nav_Settings" in btn_keys

    def test_dashboard_loads_without_exception(self, app):
        """Dashboard page renders without raising."""
        _go(app, "Dashboard")
        assert not app.exception

    def test_activity_detail_loads_without_exception(self, app):
        """Activity Detail page renders without raising."""
        _go(app, "Activity Detail")
        assert not app.exception

    def test_trends_loads_without_exception(self, app):
        """Trends page renders without raising."""
        _go(app, "Trends")
        assert not app.exception

    def test_map_loads_without_exception(self, app):
        """Map page renders without raising."""
        _go(app, "Map")
        assert not app.exception

    def test_profile_loads_without_exception(self, app):
        """Profile page renders without raising."""
        _go(app, "Profile")
        assert not app.exception

    def test_settings_loads_without_exception(self, app):
        """Settings page renders without raising."""
        _go(app, "Settings")
        assert not app.exception