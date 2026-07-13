"""
Shared fixtures for UI validation tests.

Provides a temporary SQLite database pre-populated with realistic cycling
data so that Streamlit AppTest can exercise every page without hitting
Garmin, the network, or the real vault.
"""

import os
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _vault_dir(tmp_path_factory):
    """Create a temporary vault directory for all tests."""
    vault = tmp_path_factory.mktemp("vault")
    (vault / "data").mkdir(exist_ok=True)
    (vault / "raw").mkdir(exist_ok=True)
    return vault


@pytest.fixture
def db_path(_vault_dir):
    """Return a path to a fresh SQLite database inside the temp vault."""
    return str(_vault_dir / "data" / "cycling_agent.sqlite")


@pytest.fixture
def seed_db(db_path):
    """
    Create and seed a SQLite database with realistic cycling data.

    Returns the sqlite3.Connection for further mutations if needed.
    """
    conn = sqlite3.connect(db_path)

    # Wellness — 30 days of daily records
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS wellness (
            date TEXT PRIMARY KEY,
            weight REAL,
            resting_hr REAL,
            rmssd REAL,
            stress REAL,
            sleep_score REAL,
            sleep_hours REAL,
            steps INTEGER,
            spo2 REAL,
            body_battery_start REAL,
            body_battery_end REAL,
            calories REAL,
            active_calories REAL,
            distance_m REAL,
            min_hr REAL,
            max_hr REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    today = date.today()
    for i in range(30):
        d = (today - timedelta(days=29 - i)).isoformat()
        c.execute(
            "INSERT OR REPLACE INTO wellness (date, weight, resting_hr, rmssd, stress, sleep_score, sleep_hours, steps, spo2, body_battery_start, body_battery_end, calories, active_calories, distance_m, min_hr, max_hr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d,
                72.0 + (i % 5) * 0.1,
                52.0 + (i % 3),
                45.0 + (i % 7) * 2,
                30.0 + (i % 5) * 5,
                60.0 + (i % 10) * 3,
                6.5 + (i % 4) * 0.5,
                8000 + i * 200,
                96.0 + (i % 3) * 0.5,
                50.0 + (i % 8) * 5,
                60.0 + (i % 6) * 5,
                2500.0 + i * 10,
                400.0 + i * 5,
                5000.0 + i * 100,
                48.0 + (i % 4),
                110.0 + (i % 5),
            ),
        )

    # Activities — 10 rides with full metrics
    c.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY,
            start_date TEXT NOT NULL,
            activity_type TEXT,
            duration REAL,
            distance REAL,
            average_power REAL,
            max_power REAL,
            average_hr REAL,
            max_hr REAL,
            calories REAL,
            tss REAL,
            ifr REAL,
            normalized_power REAL,
            file_type TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    activities_data = [
        ("garmin_1001", (today - timedelta(days=1)).isoformat(), "Cycling", 3600000, 5000000, 180.0, 450.0, 145.0, 180.0, 500.0, 45.0, 0.95, 195.0, "fit"),
        ("garmin_1002", (today - timedelta(days=3)).isoformat(), "Cycling", 7200000, 8000000, 160.0, 380.0, 140.0, 175.0, 800.0, 65.0, 0.90, 175.0, "fit"),
        ("garmin_1003", (today - timedelta(days=5)).isoformat(), "Cycling", 5400000, 6000000, 200.0, 500.0, 150.0, 185.0, 700.0, 55.0, 1.05, 220.0, "fit"),
        ("garmin_1004", (today - timedelta(days=7)).isoformat(), "Cycling", 1800000, 2500000, 250.0, 600.0, 155.0, 190.0, 400.0, 40.0, 1.10, 270.0, "fit"),
        ("garmin_1005", (today - timedelta(days=10)).isoformat(), "Cycling", 10800000, 12000000, 140.0, 300.0, 130.0, 165.0, 1200.0, 80.0, 0.85, 150.0, "fit"),
        ("garmin_1006", (today - timedelta(days=14)).isoformat(), "Cycling", 4200000, 4500000, 190.0, 420.0, 148.0, 178.0, 550.0, 50.0, 0.92, 205.0, "fit"),
        ("garmin_1007", (today - timedelta(days=18)).isoformat(), "Cycling", 2700000, 3000000, 220.0, 550.0, 152.0, 188.0, 450.0, 48.0, 1.02, 240.0, "fit"),
        ("garmin_1008", (today - timedelta(days=22)).isoformat(), "Cycling", 9000000, 10000000, 150.0, 350.0, 135.0, 170.0, 1000.0, 70.0, 0.88, 165.0, "fit"),
        ("garmin_1009", (today - timedelta(days=26)).isoformat(), "Cycling", 6300000, 7000000, 170.0, 400.0, 142.0, 176.0, 750.0, 58.0, 0.93, 185.0, "fit"),
        ("garmin_1010", (today - timedelta(days=29)).isoformat(), "Cycling", 4800000, 5500000, 185.0, 480.0, 147.0, 182.0, 600.0, 52.0, 0.96, 200.0, "fit"),
    ]

    for act in activities_data:
        c.execute(
            "INSERT OR REPLACE INTO activities (id, start_date, activity_type, duration, distance, average_power, max_power, average_hr, max_hr, calories, tss, ifr, normalized_power, file_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            act,
        )

    # Activity streams — power + heart_rate for the most recent activity
    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id TEXT NOT NULL,
            elapsed REAL NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            FOREIGN KEY (activity_id) REFERENCES activities(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity ON activity_streams(activity_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_streams_metric ON activity_streams(metric)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity_metric ON activity_streams(activity_id, metric)")

    import numpy as np

    np.random.seed(42)
    duration_sec = 3600  # 1 hour
    for sec in range(0, duration_sec, 5):
        power = 180.0 + np.random.normal(0, 30) + 50.0 * np.sin(sec / 300)
        hr = 145.0 + np.random.normal(0, 5) + 15.0 * np.sin(sec / 400)
        c.execute(
            "INSERT INTO activity_streams (activity_id, elapsed, metric, value) VALUES (?, ?, 'power', ?)",
            ("garmin_1001", sec, max(0, power)),
        )
        c.execute(
            "INSERT INTO activity_streams (activity_id, elapsed, metric, value) VALUES (?, ?, 'heart_rate', ?)",
            ("garmin_1001", sec, max(0, hr)),
        )

    # Morning checkin
    c.execute("""
        CREATE TABLE IF NOT EXISTS morning_checkin (
            date TEXT PRIMARY KEY,
            soreness INTEGER,
            stress INTEGER,
            sleep_quality INTEGER,
            mood INTEGER,
            energy INTEGER,
            motivation INTEGER,
            caffeine INTEGER DEFAULT 0,
            alcohol INTEGER DEFAULT 0,
            late_meals INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Insert a couple of checkins
    for i in range(3):
        d = (today - timedelta(days=i)).isoformat()
        c.execute(
            "INSERT OR REPLACE INTO morning_checkin (date, soreness, stress, sleep_quality, mood, energy, motivation, caffeine, alcohol, late_meals) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d, 3, 2, 4, 4, 3, 4, 1, 0, 0),
        )

    # Sync state
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            source TEXT PRIMARY KEY,
            last_synced_at TEXT NOT NULL,
            details TEXT,
            resume_offset INTEGER DEFAULT 0
        )
    """)
    c.execute(
        "INSERT OR REPLACE INTO sync_state (source, last_synced_at, details) VALUES (?, ?, ?)",
        ("garmin_wellness", (today - timedelta(days=1)).isoformat(), "{}"),
    )
    c.execute(
        "INSERT OR REPLACE INTO sync_state (source, last_synced_at, details) VALUES (?, ?, ?)",
        ("garmin_activities", (today - timedelta(days=1)).isoformat(), "{}"),
    )

    # Activity metrics
    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_metrics (
            activity_id TEXT NOT NULL,
            ftp_used REAL,
            normalized_power REAL,
            intensity_factor REAL,
            tss REAL,
            variability_index REAL,
            w_prime_capacity REAL,
            w_prime_min_balance REAL,
            decoupling_drift REAL,
            duration_sec REAL,
            computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (activity_id)
        )
    """)

    for act in activities_data:
        aid = act[0]
        avg_p = act[5]
        c.execute(
            "INSERT OR REPLACE INTO activity_metrics (activity_id, ftp_used, normalized_power, intensity_factor, tss, variability_index, w_prime_capacity, w_prime_min_balance, decoupling_drift, duration_sec) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, 250.0, act[12], act[10], act[9], 1.05, 30.0, 15.0, 3.5, act[3] / 1000),
        )

    # Activity routes (for Map tab)
    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_routes (
            activity_id TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            sequence INTEGER NOT NULL,
            PRIMARY KEY (activity_id, sequence)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_routes_activity ON activity_routes(activity_id)")

    # Add some route points near Louisville, KY
    base_lat, base_lon = 38.2527, -85.7585
    for seq in range(50):
        c.execute(
            "INSERT OR REPLACE INTO activity_routes (activity_id, latitude, longitude, sequence) VALUES (?, ?, ?, ?)",
            ("garmin_1001", base_lat + seq * 0.001, base_lon + seq * 0.001, seq),
        )

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def env_vars(db_path, _vault_dir):
    """
    Set environment variables so that config.setup() and the visualize app
    point to the temp vault and database.
    """
    env = {
        "CYCLING_AGENT_VAULT": str(_vault_dir),
        "ATHLETE_NAME": "Test Rider",
        "WEIGHT_KG": "72",
        "HEIGHT_CM": "175",
        "DISCIPLINE": "road",
        "FTP_WATTS": "250",
        "MAX_HR": "190",
        "RESTING_HR": "52",
        "LT1_POWER": "150",
        "LT2_POWER": "220",
        "PRIMARY_GOAL": "Improve FTP",
        "SECONDARY_GOAL": "Race readiness",
        "TRAINING_DAYS": "Mon,Wed,Fri",
        "MAX_SESSION_DURATION": "2h",
        "TERRAIN": "Hilly",
        "BIKES": "Road bike",
        "POWER_METER": "PowerTap",
        "HR_MONITOR": "Garmin HRM",
        "GARMIN_EMAIL": "",
        "GARMIN_PASSWORD": "",
    }
    old = {}
    for k, v in env.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    yield env
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v