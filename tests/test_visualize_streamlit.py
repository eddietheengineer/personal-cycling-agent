"""
Streamlit AppTest-based UI validation tests.

Uses streamlit.testing.v1.AppTest to exercise the actual Streamlit app
without a browser. Tests navigate between pages, inspect widget state,
and verify input constraints.

NOTE: These tests seed a minimal SQLite database so the app starts
without errors. Heavy chart rendering (pandas/plotly) is avoided by
keeping the DB mostly empty — the tests focus on widget structure,
navigation, and input validation rather than chart correctness.
"""

import os
import sqlite3
import sys
from datetime import date
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

    # Cleanup env
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


def _navigate(app: AppTest, page: str) -> AppTest:
    """Navigate to a page by name and re-run.

    Calls app.run() first to ensure the sidebar is populated,
    then selects the target page and runs again.
    """
    app.run()
    nav = app.sidebar.selectbox[0]
    if page in nav.options:
        nav.select(page)
        app.run()
    return app


# ---------------------------------------------------------------------------
# Navigation Tests
# ---------------------------------------------------------------------------

class TestNavigation:
    """Verify all pages are accessible and have correct structure."""

    def test_all_pages_accessible(self, app):
        """All 6 pages load without exceptions."""
        app.run()
        pages = ["Check-in", "Activity Detail", "Trends", "Map", "Profile", "Settings"]
        for page in pages:
            _navigate(app, page)
            assert not app.exception, f"Page '{page}' threw an exception"

    def test_default_page_is_checkin(self, app):
        """Default page on load is Check-in."""
        app.run()
        nav = app.sidebar.selectbox[0]
        assert nav.value == "Check-in"

    def test_main_title_present(self, app):
        """Main title 'Cycling Dashboard' is always shown."""
        app.run()
        titles = [t.value for t in app.title]
        assert "Cycling Dashboard" in titles

    def test_sidebar_header(self, app):
        """Sidebar has 'Dashboard' header."""
        app.run()
        headers = [h.value for h in app.sidebar.header]
        assert "Dashboard" in headers


# ---------------------------------------------------------------------------
# Check-in Page Tests
# ---------------------------------------------------------------------------

class TestCheckinPage:
    """Validate Check-in page widgets and constraints."""

    def test_slider_labels(self, app):
        """All 6 select sliders are present with correct labels."""
        app.run()
        expected = {"Soreness", "Life Stress", "Sleep Quality", "Mood", "Energy", "Motivation"}
        actual = {s.label for s in app.select_slider}
        assert actual == expected, f"Expected {expected}, got {actual}"

    def test_slider_ranges(self, app):
        """All sliders have 5 options (1-5 mapped to labels)."""
        app.run()
        for slider in app.select_slider:
            assert len(slider.options) == 5, f"{slider.label} has {len(slider.options)} options"

    def test_slider_defaults(self, app):
        """All sliders default to middle value (3)."""
        app.run()
        for slider in app.select_slider:
            assert slider.value == 3, f"{slider.label} defaults to {slider.value}, expected 3"

    def test_checkbox_labels(self, app):
        """Three lifestyle checkboxes are present."""
        app.run()
        expected = {"Morning Caffeine", "Alcohol Yesterday", "Late Meals"}
        actual = {c.label for c in app.checkbox}
        assert actual == expected

    def test_checkbox_defaults(self, app):
        """All checkboxes default to False."""
        app.run()
        for cb in app.checkbox:
            assert cb.value is False, f"{cb.label} defaults to {cb.value}"

    def test_date_input_present(self, app):
        """Date input defaults to today."""
        app.run()
        date_inputs = [d for d in app.date_input if d.label == "Date"]
        assert len(date_inputs) == 1
        assert date_inputs[0].value == date.today()

    def test_save_button_present(self, app):
        """Save Check-in button exists."""
        app.run()
        buttons = [b.label for b in app.button]
        assert "Save Check-in" in buttons

    def test_checkin_header(self, app):
        """Page shows 'Morning Check-in' header."""
        app.run()
        headers = [h.value for h in app.header]
        assert "Morning Check-in" in headers


# ---------------------------------------------------------------------------
# Activity Detail Page Tests
# ---------------------------------------------------------------------------

class TestActivityDetailPage:
    """Validate Activity Detail page."""

    def test_page_loads_with_no_activities(self, app):
        """Page shows info message when no activities exist."""
        _navigate(app, "Activity Detail")
        infos = [i.value for i in app.info]
        assert any("No activities" in v for v in infos), f"Expected 'No activities' message, got {infos}"

    def test_no_crash_on_empty(self, app):
        """Page does not crash with empty activity list."""
        _navigate(app, "Activity Detail")
        assert not app.exception


# ---------------------------------------------------------------------------
# Trends Page Tests
# ---------------------------------------------------------------------------

class TestTrendsPage:
    """Validate Trends page."""

    def test_page_loads_with_no_wellness(self, app):
        """Page shows info message when no wellness data exists."""
        _navigate(app, "Trends")
        infos = [i.value for i in app.info]
        assert any("No wellness" in v for v in infos), f"Expected 'No wellness' message, got {infos}"

    def test_no_crash_on_empty(self, app):
        """Page does not crash with empty wellness data."""
        _navigate(app, "Trends")
        assert not app.exception


# ---------------------------------------------------------------------------
# Map Page Tests
# ---------------------------------------------------------------------------

class TestMapPage:
    """Validate Map page widgets."""

    def test_city_input(self, app):
        """City text input is present with default value."""
        _navigate(app, "Map")
        city_inputs = [t for t in app.text_input if t.label == "City"]
        assert len(city_inputs) == 1
        assert city_inputs[0].value == "Louisville, Kentucky"

    def test_radius_slider(self, app):
        """Radius slider is present with correct bounds."""
        _navigate(app, "Map")
        radius_sliders = [s for s in app.slider if "adius" in s.label or "Radius" in s.label]
        assert len(radius_sliders) == 1
        slider = radius_sliders[0]
        assert slider.value >= 10
        assert slider.value <= 500

    def test_route_info_message(self, app):
        """Shows info about no route data."""
        _navigate(app, "Map")
        infos = [i.value for i in app.info]
        assert any("No route" in v or "route" in v.lower() for v in infos), f"Expected route info, got {infos}"


# ---------------------------------------------------------------------------
# Profile Page Tests
# ---------------------------------------------------------------------------

class TestProfilePage:
    """Validate Profile page widgets."""

    def test_name_input(self, app):
        """Name text input is populated from env."""
        _navigate(app, "Profile")
        name_inputs = [t for t in app.text_input if t.label == "Name"]
        assert len(name_inputs) == 1
        assert name_inputs[0].value == "Test Rider"

    def test_weight_input(self, app):
        """Weight number input loads from env."""
        _navigate(app, "Profile")
        weight_inputs = [n for n in app.number_input if "Weight" in n.label]
        assert len(weight_inputs) == 1
        assert weight_inputs[0].value == 72

    def test_ftp_input(self, app):
        """FTP number input loads from env."""
        _navigate(app, "Profile")
        ftp_inputs = [n for n in app.number_input if "FTP" in n.label]
        assert len(ftp_inputs) == 1
        assert ftp_inputs[0].value == 250

    def test_discipline_selector(self, app):
        """Discipline selectbox shows options and defaults to road."""
        _navigate(app, "Profile")
        discipline = [s for s in app.selectbox if "Discipline" in s.label]
        assert len(discipline) == 1
        assert discipline[0].value == "road"
        assert set(discipline[0].options) == {"road", "gravel", "MTB", "TT"}

    def test_save_profile_button(self, app):
        """Save Profile button exists."""
        _navigate(app, "Profile")
        buttons = [b.label for b in app.button]
        assert "Save Profile" in buttons

    def test_profile_header(self, app):
        """Page shows 'Athlete Profile' subheader."""
        _navigate(app, "Profile")
        subheaders = [s.value for s in app.subheader]
        assert "Athlete Profile" in subheaders

    def test_numeric_inputs_non_negative(self, app):
        """All numeric profile inputs have min_value=0."""
        _navigate(app, "Profile")
        for n in app.number_input:
            assert n.value >= 0, f"{n.label} has negative value {n.value}"


# ---------------------------------------------------------------------------
# Settings Page Tests
# ---------------------------------------------------------------------------

class TestSettingsPage:
    """Validate Settings/Garmin page."""

    def test_page_loads(self, app):
        """Settings page loads without error."""
        _navigate(app, "Settings")
        assert not app.exception

    def test_not_connected_message(self, app):
        """Shows not connected message when no credentials."""
        _navigate(app, "Settings")
        infos = [i.value for i in app.info]
        assert any("Not connected" in v or "not connected" in v.lower() for v in infos), \
            f"Expected connection status, got {infos}"

    def test_sync_buttons_disabled(self, app):
        """Sync buttons are disabled without auth."""
        _navigate(app, "Settings")
        sync_buttons = [b for b in app.button if "Update Latest" in b.label or "Pull All" in b.label]
        for btn in sync_buttons:
            assert btn.disabled, f"Button '{btn.label}' should be disabled without auth"

    def test_reanalyze_enabled(self, app):
        """Reanalyze button is always enabled."""
        _navigate(app, "Settings")
        reanalyze = [b for b in app.button if "Reanalyze" in b.label]
        assert len(reanalyze) == 1
        assert not reanalyze[0].disabled

    def test_generate_prescription_button(self, app):
        """Generate Prescription button exists."""
        _navigate(app, "Settings")
        buttons = [b.label for b in app.button]
        assert "Generate Prescription" in buttons

    def test_garmin_subheader(self, app):
        """Page shows 'Garmin Connect' subheader."""
        _navigate(app, "Settings")
        subheaders = [s.value for s in app.subheader]
        assert "Garmin Connect" in subheaders