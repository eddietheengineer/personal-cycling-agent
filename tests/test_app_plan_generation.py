"""Tests for plan generation: mock generate_weekly_plan()/generate_ai_plan(), click Rules/AI buttons, verify plan generated."""

import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

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


def _find_button(app: AppTest, key: str):
    """Return the first button widget whose key matches."""
    for btn in app.button:
        if btn.key == key:
            return btn
    raise AssertionError(f"No button with key={key!r} found")


def _make_mock_plan():
    """Build a minimal mock WeeklyPlan object with 7 days."""
    plan = MagicMock()
    plan.week_start = date.today().isoformat()
    plan.readiness_summary = "Good readiness"
    plan.ctl_series = None
    plan.atl_series = None
    plan.tsb_series = None

    days = []
    for i in range(7):
        day_date = date.today() + timedelta(days=i)
        day = MagicMock()
        day.date = day_date.isoformat()
        day.weekday = day_date.weekday()
        day.session_type = "endurance" if i % 2 == 0 else "recovery"
        day.rest_day = (i == 6)
        day.indoor = False
        day.duration_min = 60
        day.target_tss = 30.0
        day.description = "Test session"
        day.weather_condition = None
        day.weather_temp_max = None
        day.weather_temp_min = None
        day.weather_precip = None
        days.append(day)
    plan.days = days
    return plan


# ---------------------------------------------------------------------------
# Rules-based Plan Generation
# ---------------------------------------------------------------------------


class TestRulesPlanGeneration:
    """Click Rules button to generate a rules-based weekly plan."""

    def test_rules_button_calls_generate_and_save(self, app):
        _go(app, "Dashboard")
        assert not app.exception
        with patch("src.analytics.weekly_planner.generate_weekly_plan", return_value=_make_mock_plan()), \
             patch("src.analytics.weekly_planner.save_weekly_plan") as mock_save:
            _find_button(app, "gen_rules_dash").click().run()
            mock_save.assert_called_once()


    def test_rules_success_shows_plan_generated_message(self, app):
        _go(app, "Dashboard")
        with patch("src.analytics.weekly_planner.generate_weekly_plan", return_value=_make_mock_plan()), \
             patch("src.analytics.weekly_planner.save_weekly_plan"):
            _find_button(app, "gen_rules_dash").click().run()
        successes = [s.value for s in app.success]
        assert any("Plan generated" in s for s in successes)

    def test_rules_failure_shows_error(self, app):
        _go(app, "Dashboard")
        with patch("src.analytics.weekly_planner.generate_weekly_plan", side_effect=Exception("No FTP set")):
            _find_button(app, "gen_rules_dash").click().run()
        errors = [e.value for e in app.error]
        assert any("Failed" in e for e in errors)


class TestAIPlanGeneration:
    """Click AI button to generate an AI-based weekly plan."""

    def test_ai_button_calls_generate_ai_and_save(self, app):
        _go(app, "Dashboard")
        assert not app.exception
        with patch("src.analytics.weekly_planner.generate_ai_plan", return_value=_make_mock_plan()), \
             patch("src.analytics.weekly_planner.save_weekly_plan") as mock_save:
            _find_button(app, "gen_ai_dash").click().run()
            mock_save.assert_called_once()


    def test_ai_success_shows_plan_generated_message(self, app):
        _go(app, "Dashboard")
        with patch("src.analytics.weekly_planner.generate_ai_plan", return_value=_make_mock_plan()), \
             patch("src.analytics.weekly_planner.save_weekly_plan"):
            _find_button(app, "gen_ai_dash").click().run()
        successes = [s.value for s in app.success]
        assert any("Plan generated" in s for s in successes)

    def test_ai_failure_shows_error(self, app):
        _go(app, "Dashboard")
        with patch("src.analytics.weekly_planner.generate_ai_plan", side_effect=Exception("LLM unavailable")):
            _find_button(app, "gen_ai_dash").click().run()
        errors = [e.value for e in app.error]
        assert any("AI failed" in e for e in errors)


class TestPlanDisplayAfterGeneration:
    """Verify the plan is displayed after successful generation."""

    def test_plan_displayed_after_rules_generation(self, app):
        mock_plan = _make_mock_plan()
        _go(app, "Dashboard")
        assert not app.exception
        with patch("src.analytics.weekly_planner.generate_weekly_plan", return_value=mock_plan), \
             patch("src.analytics.weekly_planner.save_weekly_plan"), \
             patch("src.analytics.weekly_planner.load_weekly_plan", return_value=mock_plan):
            _find_button(app, "gen_rules_dash").click().run()
        markdowns = [m.value for m in app.markdown]
        assert any("Mon" in m for m in markdowns)

    def test_plan_displayed_after_ai_generation(self, app):
        mock_plan = _make_mock_plan()
        _go(app, "Dashboard")
        assert not app.exception
        with patch("src.analytics.weekly_planner.generate_ai_plan", return_value=mock_plan), \
             patch("src.analytics.weekly_planner.save_weekly_plan"), \
             patch("src.analytics.weekly_planner.load_weekly_plan", return_value=mock_plan):
            _find_button(app, "gen_ai_dash").click().run()
        markdowns = [m.value for m in app.markdown]
        assert any("Mon" in m for m in markdowns)