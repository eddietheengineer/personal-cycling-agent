"""Tests for Map page: mock geopy geocoding, enter city, verify no exception."""

import os
import sqlite3
import sys
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


# ---------------------------------------------------------------------------
# Map Page Tests
# ---------------------------------------------------------------------------


class TestMapPageGeocoding:
    """Geocoding on the Map page with mocked geopy."""

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_geocode_city_no_exception(self, mock_geocode, app):
        """Entering a city name and geocoding it raises no exception."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_geocode_success_shows_center_info(self, mock_geocode, app):
        """Successful geocode displays center coordinates in info."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

        infos = [i.value for i in app.info]
        assert any("Center:" in v for v in infos)

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_geocode_failure_shows_error(self, mock_geocode, app):
        """Failed geocode (None result) shows error message."""
        mock_geocode.return_value = None

        _go(app, "Map")
        assert not app.exception

        errors = [e.value for e in app.error]
        assert any("Could not geocode" in e for e in errors)

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_geocode_exception_handled(self, mock_geocode, app):
        """Geopy exception during geocoding is handled gracefully."""
        from geopy.exc import GeopyError
        mock_geocode.side_effect = GeopyError("timeout")

        _go(app, "Map")
        assert not app.exception

        errors = [e.value for e in app.error]
        assert any("Could not geocode" in e for e in errors)


class TestMapPageRouteData:
    """Map page behavior with and without route data."""

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_no_route_data_shows_info(self, mock_geocode, app):
        """Map page shows 'No route data' info when activity_routes is empty."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

        infos = [i.value for i in app.info]
        assert any("No route data" in v for v in infos)

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_route_data_within_radius_shows_map(self, mock_geocode, app):
        """With route data near the city, the map renders without error."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        # Insert a route point near Louisville
        from src.db.store import CyclingDB
        vault = os.environ["CYCLING_AGENT_VAULT"]
        data_dir = Path(vault) / "data"
        db = CyclingDB(str(data_dir / "cycling_agent.sqlite"))
        db.conn.execute(
            "INSERT INTO activity_routes (activity_id, latitude, longitude, sequence) VALUES (?, ?, ?, ?)",
            ("test-ride-1", 38.25, -85.75, 0),
        )
        db.conn.execute(
            "INSERT INTO activities (id, start_date) VALUES (?, ?)",
            ("test-ride-1", "2025-01-01"),
        )
        db.conn.commit()
        db.conn.close()

        # Need a fresh app to pick up the DB connection with data
        app2 = AppTest.from_file(VISUALIZE_PATH)
        _go(app2, "Map")
        assert not app2.exception


class TestMapPageInputs:
    """Map page input widgets."""

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_city_input_present(self, mock_geocode, app):
        """City text input is present on the Map page."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

        text_inputs = [t for t in app.text_input if t.label == "City"]
        assert len(text_inputs) >= 1

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_radius_slider_present(self, mock_geocode, app):
        """Radius slider is present on the Map page."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

        sliders = [s for s in app.slider if "Radius" in s.label]
        assert len(sliders) >= 1

    @patch("geopy.geocoders.Nominatim.geocode")
    def test_route_map_header_present(self, mock_geocode, app):
        """Route Map subheader is rendered."""
        mock_loc = MagicMock()
        mock_loc.latitude = 38.2527
        mock_loc.longitude = -85.7585
        mock_geocode.return_value = mock_loc

        _go(app, "Map")
        assert not app.exception

        subheaders = [s.value for s in app.subheader]
        assert "Route Map" in subheaders