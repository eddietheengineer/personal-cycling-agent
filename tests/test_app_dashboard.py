"""Tests for the Dashboard page: week strip, readiness card, check-in, coach chat."""

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
# Dashboard Tests
# ---------------------------------------------------------------------------


class TestDashboardWeekStrip:
    """Week strip: Sync / Rules / AI buttons."""

    def test_sync_button_present(self, app):
        """Sync button is rendered in the week strip header."""
        _go(app, "Dashboard")
        btn_keys = [b.key for b in app.button]
        assert "sync_dash" in btn_keys

    def test_rules_button_present(self, app):
        """Rules button is rendered in the week strip header."""
        _go(app, "Dashboard")
        btn_keys = [b.key for b in app.button]
        assert "gen_rules_dash" in btn_keys

    def test_ai_button_present(self, app):
        """AI button is rendered in the week strip header."""
        _go(app, "Dashboard")
        btn_keys = [b.key for b in app.button]
        assert "gen_ai_dash" in btn_keys

    def test_week_strip_header(self, app):
        """Week strip shows '7-Day Plan' heading."""
        _go(app, "Dashboard")
        # The heading is rendered as markdown bold text
        markdowns = [m.value for m in app.markdown]
        assert any("7-Day Plan" in m for m in markdowns)


class TestDashboardReadinessCard:
    """Readiness card renders without analysis data."""

    def test_readiness_info_message_when_no_analysis(self, app):
        """Without analysis data, readiness card shows info message."""
        _go(app, "Dashboard")
        infos = [i.value for i in app.info]
        assert any("analysis" in v.lower() for v in infos)


class TestDashboardCheckin:
    """Check-in section: 6 sliders, 3 checkboxes, form submission."""

    def test_six_sliders_present(self, app):
        """Check-in form has 6 select sliders."""
        _go(app, "Dashboard")
        expected = {"Soreness", "Life Stress", "Sleep Quality", "Mood", "Energy", "Motivation"}
        actual = {s.label for s in app.select_slider}
        assert expected.issubset(actual), f"Missing sliders: {expected - actual}"

    def test_slider_options_count(self, app):
        """Each check-in slider has 5 options."""
        _go(app, "Dashboard")
        for slider in app.select_slider:
            assert len(slider.options) == 5, f"{slider.label} has {len(slider.options)} options"

    def test_slider_defaults_middle(self, app):
        """Each check-in slider defaults to 3 (middle value)."""
        _go(app, "Dashboard")
        for slider in app.select_slider:
            assert slider.value == 3, f"{slider.label} defaults to {slider.value}, expected 3"

    def test_three_checkboxes_present(self, app):
        """Check-in form has 3 lifestyle checkboxes."""
        _go(app, "Dashboard")
        checkbox_labels = {c.label for c in app.checkbox}
        expected = {"☕ Caffeine", "🍺 Alcohol", "🌙 Late Meals"}
        assert expected.issubset(checkbox_labels), f"Missing checkboxes: {expected - checkbox_labels}"

    def test_checkboxes_default_false(self, app):
        """Check-in checkboxes default to unchecked."""
        _go(app, "Dashboard")
        for cb in app.checkbox:
            assert cb.value is False, f"{cb.label} defaults to True"

    def test_save_checkin_button_present(self, app):
        """Save Check-in button exists in the form."""
        _go(app, "Dashboard")
        btn_labels = [b.label for b in app.button]
        assert "Save Check-in" in btn_labels

    def test_checkin_notes_textarea(self, app):
        """Notes text area is present in check-in form."""
        _go(app, "Dashboard")
        text_areas = [t for t in app.text_area if t.label == "Notes"]
        assert len(text_areas) >= 1


class TestDashboardCoachChat:
    """Coach chat section: input, send/clear buttons."""

    def test_coach_section_header(self, app):
        """Coach section header is rendered."""
        _go(app, "Dashboard")
        markdowns = [m.value for m in app.markdown]
        assert any("Coach" in m for m in markdowns)

    def test_coach_text_input_present(self, app):
        """Coach text input field is present."""
        _go(app, "Dashboard")
        text_inputs = [t for t in app.text_input if "coach" in t.key]
        assert len(text_inputs) >= 1

    def test_coach_send_button_present(self, app):
        """Send button for coach chat exists."""
        _go(app, "Dashboard")
        btn_keys = [b.key for b in app.button]
        assert "dash_coach_send" in btn_keys

    def test_coach_clear_button_present(self, app):
        """Clear button for coach chat exists."""
        _go(app, "Dashboard")
        btn_keys = [b.key for b in app.button]
        assert "dash_coach_clear" in btn_keys

    def test_coach_messages_initialized(self, app):
        """Coach messages list is initialized in session state."""
        _go(app, "Dashboard")
        assert hasattr(app.session_state, "coach_messages")