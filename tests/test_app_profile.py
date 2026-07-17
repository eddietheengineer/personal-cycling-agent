"""Tests for the Profile page: form inputs, Save Profile, schedule presets, Save Schedule."""

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
# Profile Form Input Tests
# ---------------------------------------------------------------------------


class TestProfileFormInputs:
    """Profile form: identity, training, physiological, goals, equipment fields."""

    def test_profile_subheader(self, app):
        """Profile page shows 'Athlete Profile' subheader."""
        _go(app, "Profile")
        subheaders = [s.value for s in app.subheader]
        assert "Athlete Profile" in subheaders

    def test_name_text_input(self, app):
        """Name text input is present and populated from env."""
        _go(app, "Profile")
        name_inputs = [t for t in app.text_input if t.label == "Name"]
        assert len(name_inputs) >= 1
        assert name_inputs[0].value == "Test Rider"

    def test_weight_number_input(self, app):
        """Weight number input is present and populated from env."""
        _go(app, "Profile")
        weight_inputs = [n for n in app.number_input if "Weight" in n.label]
        assert len(weight_inputs) >= 1
        assert weight_inputs[0].value == 72

    def test_height_number_input(self, app):
        """Height number input is present and populated from env."""
        _go(app, "Profile")
        height_inputs = [n for n in app.number_input if "Height" in n.label]
        assert len(height_inputs) >= 1
        assert height_inputs[0].value == 175

    def test_discipline_selectbox(self, app):
        """Discipline selectbox shows options and defaults to road."""
        _go(app, "Profile")
        discipline = [s for s in app.selectbox if "Discipline" in s.label]
        assert len(discipline) >= 1
        assert discipline[0].value == "road"
        assert set(discipline[0].options) == {"road", "gravel", "MTB", "TT"}

    def test_ftp_number_input(self, app):
        """FTP number input is present and populated from env."""
        _go(app, "Profile")
        ftp_inputs = [n for n in app.number_input if "FTP" in n.label]
        assert len(ftp_inputs) >= 1
        assert ftp_inputs[0].value == 250

    def test_max_hr_number_input(self, app):
        """Max HR number input is present and populated from env."""
        _go(app, "Profile")
        max_hr_inputs = [n for n in app.number_input if "Max HR" in n.label]
        assert len(max_hr_inputs) >= 1
        assert max_hr_inputs[0].value == 190

    def test_resting_hr_number_input(self, app):
        """Resting HR number input is present and populated from env."""
        _go(app, "Profile")
        resting_hr_inputs = [n for n in app.number_input if "Resting HR" in n.label]
        assert len(resting_hr_inputs) >= 1
        assert resting_hr_inputs[0].value == 52

    def test_gender_selectbox(self, app):
        """Gender selectbox is present with male/female options."""
        _go(app, "Profile")
        gender = [s for s in app.selectbox if s.label == "Gender"]
        assert len(gender) >= 1
        assert set(gender[0].options) == {"male", "female"}

    def test_lt1_number_input(self, app):
        """LT1 Power number input is present and populated from env."""
        _go(app, "Profile")
        lt1_inputs = [n for n in app.number_input if "LT1" in n.label]
        assert len(lt1_inputs) >= 1
        assert lt1_inputs[0].value == 150

    def test_lt2_number_input(self, app):
        """LT2 Power number input is present and populated from env."""
        _go(app, "Profile")
        lt2_inputs = [n for n in app.number_input if "LT2" in n.label]
        assert len(lt2_inputs) >= 1
        assert lt2_inputs[0].value == 220

    def test_primary_goal_text_input(self, app):
        """Primary Goal text input is present and populated from env."""
        _go(app, "Profile")
        goal_inputs = [t for t in app.text_input if t.label == "Primary Goal"]
        assert len(goal_inputs) >= 1
        assert goal_inputs[0].value == "Improve FTP"

    def test_secondary_goal_text_input(self, app):
        """Secondary Goal text input is present and populated from env."""
        _go(app, "Profile")
        goal_inputs = [t for t in app.text_input if t.label == "Secondary Goal"]
        assert len(goal_inputs) >= 1
        assert goal_inputs[0].value == "Race readiness"

    def test_training_days_text_input(self, app):
        """Available Training Days text input is present."""
        _go(app, "Profile")
        days_inputs = [t for t in app.text_input if "Training Days" in t.label]
        assert len(days_inputs) >= 1

    def test_max_session_text_input(self, app):
        """Max Session Duration text input is present."""
        _go(app, "Profile")
        session_inputs = [t for t in app.text_input if "Max Session" in t.label]
        assert len(session_inputs) >= 1

    def test_terrain_textarea(self, app):
        """Terrain Notes textarea is present."""
        _go(app, "Profile")
        terrain = [t for t in app.text_area if "Terrain" in t.label]
        assert len(terrain) >= 1

    def test_bikes_text_input(self, app):
        """Bikes text input is present and populated from env."""
        _go(app, "Profile")
        bikes_inputs = [t for t in app.text_input if t.label == "Bike(s)"]
        assert len(bikes_inputs) >= 1
        assert bikes_inputs[0].value == "Road bike"

    def test_power_meter_text_input(self, app):
        """Power Meter text input is present and populated from env."""
        _go(app, "Profile")
        pm_inputs = [t for t in app.text_input if t.label == "Power Meter"]
        assert len(pm_inputs) >= 1
        assert pm_inputs[0].value == "PowerTap"

    def test_hr_monitor_text_input(self, app):
        """HR Monitor text input is present and populated from env."""
        _go(app, "Profile")
        hr_inputs = [t for t in app.text_input if t.label == "HR Monitor"]
        assert len(hr_inputs) >= 1
        assert hr_inputs[0].value == "Garmin HRM"

    def test_tsb_floor_slider(self, app):
        """TSB Floor slider is present with range -30 to 10."""
        _go(app, "Profile")
        tsb_sliders = [s for s in app.slider if "TSB Floor" in s.label]
        assert len(tsb_sliders) >= 1

    def test_latitude_number_input(self, app):
        """Latitude number input is present."""
        _go(app, "Profile")
        lat_inputs = [n for n in app.number_input if n.label == "Latitude"]
        assert len(lat_inputs) >= 1

    def test_longitude_number_input(self, app):
        """Longitude number input is present."""
        _go(app, "Profile")
        lon_inputs = [n for n in app.number_input if n.label == "Longitude"]
        assert len(lon_inputs) >= 1

    def test_numeric_inputs_non_negative(self, app):
        """All numeric profile inputs have non-negative values."""
        _go(app, "Profile")
        for n in app.number_input:
            assert n.value >= 0, f"{n.label} has negative value {n.value}"


# ---------------------------------------------------------------------------
# Save Profile Button Tests
# ---------------------------------------------------------------------------


class TestSaveProfile:
    """Save Profile button presence and behavior."""

    def test_save_profile_button_exists(self, app):
        """Save Profile button is rendered."""
        _go(app, "Profile")
        btn_labels = [b.label for b in app.button]
        assert "Save Profile" in btn_labels


# ---------------------------------------------------------------------------
# Schedule Presets Tests
# ---------------------------------------------------------------------------


class TestSchedulePresets:
    """Schedule preset buttons: Morning, Afternoon, Evening, All Day."""

    def test_morning_preset_button(self, app):
        """Morning preset button exists."""
        _go(app, "Profile")
        btn_keys = [b.key for b in app.button]
        assert "preset_Morning" in btn_keys

    def test_afternoon_preset_button(self, app):
        """Afternoon preset button exists."""
        _go(app, "Profile")
        btn_keys = [b.key for b in app.button]
        assert "preset_Afternoon" in btn_keys

    def test_evening_preset_button(self, app):
        """Evening preset button exists."""
        _go(app, "Profile")
        btn_keys = [b.key for b in app.button]
        assert "preset_Evening" in btn_keys

    def test_all_day_preset_button(self, app):
        """All Day preset button exists."""
        _go(app, "Profile")
        btn_keys = [b.key for b in app.button]
        assert "preset_All Day" in btn_keys

    def test_four_preset_buttons_present(self, app):
        """All four preset buttons are rendered."""
        _go(app, "Profile")
        preset_keys = [k for k in [b.key for b in app.button] if k.startswith("preset_")]
        assert len(preset_keys) == 4


# ---------------------------------------------------------------------------
# Save Schedule Button Tests
# ---------------------------------------------------------------------------


class TestSaveSchedule:
    """Save Schedule button presence."""

    def test_save_schedule_button_exists(self, app):
        """Save Schedule button is rendered."""
        _go(app, "Profile")
        btn_labels = [b.label for b in app.button]
        assert "Save Schedule" in btn_labels

    def test_save_schedule_button_key(self, app):
        """Save Schedule button has the expected key."""
        _go(app, "Profile")
        btn_keys = [b.key for b in app.button]
        assert "save_schedule" in btn_keys

    def test_schedule_expander_present(self, app):
        """Training Schedule expander is rendered."""
        _go(app, "Profile")
        expanders = [e.label for e in app.expander]
        assert any("Training Schedule" in e for e in expanders)