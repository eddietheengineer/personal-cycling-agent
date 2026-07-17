"""Tests for sync progress, sync errors, and sync results display."""

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
# Sync Progress Dialog Tests
# ---------------------------------------------------------------------------


class TestSyncProgressHidden:
    """Progress dialog is hidden when no sync is running."""

    def test_no_progress_dialog_on_dashboard(self, app):
        """Dashboard shows no sync progress dialog when idle."""
        _go(app, "Dashboard")
        # When no sync is running, _render_sync_progress returns early.
        # There should be no status elements and no sync-related errors.
        assert not app.exception
        # No sync_error or syncing flags set
        assert not getattr(app.session_state, "syncing", False)
        assert not getattr(app.session_state, "rearsing", False)

    def test_no_progress_dialog_on_settings(self, app):
        """Settings page shows no sync progress dialog when idle."""
        _go(app, "Settings")
        assert not app.exception
        assert not getattr(app.session_state, "syncing", False)
        assert not getattr(app.session_state, "rearsing", False)

    def test_sync_progress_defaults_zero(self, app):
        """Sync progress is 0 when no sync is active."""
        _go(app, "Dashboard")
        # sync_progress may or may not be set yet; if set it should be 0
        progress = getattr(app.session_state, "sync_progress", None)
        if progress is not None:
            assert progress == 0

    def test_sync_log_empty_when_idle(self, app):
        """Sync log is empty or absent when no sync has run."""
        _go(app, "Dashboard")
        sync_log = getattr(app.session_state, "sync_log", None)
        if sync_log is not None:
            assert len(sync_log) == 0

    def test_no_sync_error_displayed_when_idle(self, app):
        """No error messages displayed when sync is idle."""
        _go(app, "Dashboard")
        errors = [e.value for e in app.error]
        assert not any("Sync failed" in e for e in errors)

    def test_no_sync_result_displayed_when_idle(self, app):
        """No sync success messages displayed when idle."""
        _go(app, "Dashboard")
        successes = [s.value for s in app.success]
        assert not any("Synced" in s for s in successes)


# ---------------------------------------------------------------------------
# Sync Error Display Tests
# ---------------------------------------------------------------------------


class TestSyncErrorDisplay:
    """Sync error is displayed when sync_error is set in session state."""

    def test_sync_error_shown_on_dashboard(self, app):
        """Dashboard displays sync error message when sync_error is set."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_error = "Connection refused"
        app.run()
        assert not app.exception
        errors = [e.value for e in app.error]
        assert any("Sync failed" in e for e in errors)

    def test_sync_error_shown_on_settings(self, app):
        """Settings page displays sync error message when sync_error is set."""
        app.session_state.nav_page = "Settings"
        app.session_state.sync_error = "MFA required"
        app.run()
        assert not app.exception
        errors = [e.value for e in app.error]
        assert any("Sync failed" in e for e in errors)

    def test_mfa_error_shows_hint(self, app):
        """MFA-related sync errors show additional info hint."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_error = "MFA verification failed"
        app.run()
        assert not app.exception
        infos = [i.value for i in app.info]
        assert any("sign in" in v.lower() for v in infos)

    def test_sync_error_contains_message(self, app):
        """Sync error display includes the original error message."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_error = "Test error message"
        app.run()
        assert not app.exception
        errors = [e.value for e in app.error]
        assert any("Test error message" in e for e in errors)


# ---------------------------------------------------------------------------
# Sync Result Display Tests
# ---------------------------------------------------------------------------


class TestSyncResultDisplay:
    """Sync result is displayed when sync_result is set in session state."""

    def test_sync_result_shown_on_dashboard(self, app):
        """Dashboard displays sync success when sync_result is set."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_result = {"activities": {"activities_processed": 5}}
        app.run()
        assert not app.exception
        successes = [s.value for s in app.success]
        assert any("Synced" in s for s in successes)

    def test_sync_result_shows_activity_count(self, app):
        """Sync result message includes the number of activities synced."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_result = {"activities": {"activities_processed": 3}}
        app.run()
        assert not app.exception
        successes = [s.value for s in app.success]
        assert any("3" in s for s in successes)

    def test_sync_result_on_settings_shows_expander(self, app):
        """Settings page shows sync results in an expander."""
        app.session_state.nav_page = "Settings"
        app.session_state.sync_result = {
            "wellness": {"wellness_records": 10, "with_hrv": 8},
            "activities": {"activities_processed": 5, "stream_records": 1000},
        }
        app.run()
        assert not app.exception
        expanders = [e.label for e in app.expander]
        assert "Sync Results" in expanders

    def test_sync_result_dismiss_button_on_settings(self, app):
        """Settings page shows Done button to dismiss sync results."""
        app.session_state.nav_page = "Settings"
        app.session_state.sync_result = {"activities": {"activities_processed": 1}}
        app.run()
        assert not app.exception
        btn_labels = [b.label for b in app.button]
        assert "Done" in btn_labels

    def test_empty_sync_result_shows_zero(self, app):
        """Sync result with zero activities shows 'Synced 0'."""
        app.session_state.nav_page = "Dashboard"
        app.session_state.sync_result = {"activities": {"activities_processed": 0}}
        app.run()
        assert not app.exception
        successes = [s.value for s in app.success]
        assert any("Synced 0" in s for s in successes)