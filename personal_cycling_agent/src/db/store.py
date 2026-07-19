"""
SQLite persistence layer for cycling-ai-agent.

Tables:
- wellness: daily wellness records (HRV/RMSSD, RHR, weight, stress, sleep).
- activities: completed activity summaries with key metrics.
- activity_streams: per-activity time-series data (power, HR, DFA-a1, etc.).
"""

import logging
import json
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class CyclingDB:
    """Thin wrapper around a local SQLite database for cycling telemetry."""

    def __init__(self, db_path: str = "data/cycling_agent.sqlite"):
        if not db_path:
            raise ValueError('db_path must not be empty')
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_tables()
    def _apply_pragmas(self):
        """Apply performance pragmas to the connection."""
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")

    def _migrate_wellness(self):
        """Add missing columns to the wellness table."""
        c = self.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wellness'")
        if c.fetchone() is None:
            return
        c.execute("PRAGMA table_info(wellness)")
        existing = {row[1] for row in c.fetchall()}

        columns = {
            "spo2": "REAL",
            "body_battery_start": "REAL",
            "body_battery_end": "REAL",
            "calories": "REAL",
            "active_calories": "REAL",
            "distance_m": "REAL",
            "min_hr": "REAL",
            "max_hr": "REAL",
            "respiration_rate": "REAL",
            "floors": "INTEGER",
            "hydration_ml": "REAL",
            "intensity_minutes": "REAL",
            "body_battery": "REAL",
            "training_readiness_score": "REAL",
            "endurance_score": "REAL",
            "hill_score": "REAL",
        }

        for col, typ in columns.items():
            if col not in existing:
                c.execute(f"ALTER TABLE wellness ADD COLUMN {col} {typ}")
                logger.info(f"Migrated: added column {col} to wellness")

        self.conn.commit()

    def _migrate_activity_metrics(self):
        """Add missing columns to the activity_metrics table."""
        c = self.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_metrics'")
        if c.fetchone() is None:
            return
        c.execute("PRAGMA table_info(activity_metrics)")
        existing = {row[1] for row in c.fetchall()}

        columns = {
            "cp_used": "REAL",
            "ride_cp": "REAL",
            "hr_tss": "REAL",
            "hr_trimp": "REAL",
        }

        for col, typ in columns.items():
            if col not in existing:
                c.execute(f"ALTER TABLE activity_metrics ADD COLUMN {col} {typ}")
                logger.info(f"Migrated: added column {col} to activity_metrics")

        self.conn.commit()

    def _migrate_sync_state(self):
        """Add missing columns to the sync_state table."""
        c = self.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sync_state'")
        if c.fetchone() is None:
            return
        c.execute("PRAGMA table_info(sync_state)")
        existing = {row[1] for row in c.fetchall()}

        columns = {
            "resume_offset": "INTEGER DEFAULT 0",
        }

        for col, typ in columns.items():
            if col not in existing:
                c.execute(f"ALTER TABLE sync_state ADD COLUMN {col} {typ}")
                logger.info(f"Migrated: added column {col} to sync_state")

        self.conn.commit()

    def _migrate_activity_api_fields(self):
        """Add API-only fields to activities table if they don't exist."""
        c = self.conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activities'")
        if c.fetchone() is None:
            return
        c.execute("PRAGMA table_info(activities)")
        existing_cols = {row[1] for row in c.fetchall()}

        new_cols = {
            "activity_name": "TEXT",
            "aerobic_training_effect": "REAL",
            "anaerobic_training_effect": "REAL",
            "vo2_max": "REAL",
            "elevation_gain": "REAL",
            "elevation_loss": "REAL",
            "min_elevation": "REAL",
            "max_elevation": "REAL",
            "min_temperature": "REAL",
            "max_temperature": "REAL",
            "avg_respiration_rate": "REAL",
            "max_respiration_rate": "REAL",
            "avg_left_balance": "REAL",
            "moving_duration": "REAL",
            "intensity_factor": "REAL",
            "hr_zone_1": "REAL",
            "hr_zone_2": "REAL",
            "hr_zone_3": "REAL",
            "hr_zone_4": "REAL",
            "hr_zone_5": "REAL",
            "power_zone_1": "REAL",
            "power_zone_2": "REAL",
            "power_zone_3": "REAL",
            "power_zone_4": "REAL",
            "power_zone_5": "REAL",
            "power_zone_6": "REAL",
            "power_zone_7": "REAL",
            "max_avg_power_1s": "REAL",
            "max_avg_power_2s": "REAL",
            "max_avg_power_5s": "REAL",
            "max_avg_power_10s": "REAL",
            "max_avg_power_20s": "REAL",
            "max_avg_power_30s": "REAL",
            "max_avg_power_60s": "REAL",
            "max_avg_power_120s": "REAL",
            "max_avg_power_300s": "REAL",
            "max_avg_power_600s": "REAL",
            "max_avg_power_1200s": "REAL",
            "max_avg_power_1800s": "REAL",
            "max_avg_power_3600s": "REAL",
            "source_duration": "TEXT",
            "source_distance": "TEXT",
            "source_power": "TEXT",
            "source_hr": "TEXT",
            "source_calories": "TEXT",
            "power_meter": "TEXT",
        }

        for col, col_type in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE activities ADD COLUMN {col} {col_type}")
                logger.info(f"Migrated: added column {col} to activities")

        self.conn.commit()

    def _migrate_raw_tables(self):
        """Backfill raw_activities from existing activities table data."""
        c = self.conn.cursor()

        # Check if activities table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activities'")
        if c.fetchone() is None:
            return  # no activities table to backfill from

        # Check if raw_activities table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_activities'")
        if c.fetchone() is None:
            return  # raw_activities not yet created

        # Check if we already backfilled (check for any garmin_ prefixed IDs)
        c.execute("SELECT COUNT(*) FROM raw_activities")
        existing_count = c.fetchone()[0]
        if existing_count > 0:
            return  # already has data, skip backfill

        # Backfill raw_activities from existing activities table
        rows = c.execute("""
            SELECT id, start_date, activity_type, duration, distance,
                   average_power, max_power, average_hr, max_hr,
                   calories, tss, normalized_power
            FROM activities
        """).fetchall()

        backfilled = 0
        for row in rows:
            garmin_id_str = row[0]  # "garmin_12345"
            if not garmin_id_str.startswith("garmin_"):
                continue
            try:
                garmin_id = int(garmin_id_str[len("garmin_"):])
            except (ValueError, TypeError):
                continue

            c.execute(
                """INSERT OR IGNORE INTO raw_activities
                    (garmin_id, start_time_local, activity_type_key,
                     duration_ms, distance_cm, avg_power, max_power,
                     avg_heart_rate, max_heart_rate, calories,
                     training_stress_score, norm_power, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    garmin_id,
                    row[1],  # start_date
                    row[2],  # activity_type
                    (row[3] * 1000) if row[3] else None,  # duration -> ms
                    (row[4] * 100) if row[4] else None,   # distance -> cm
                    row[5],  # avg_power
                    row[6],  # max_power
                    row[7],  # avg_hr
                    row[8],  # max_hr
                    row[9],  # calories
                    row[10], # tss
                    row[11], # normalized_power
                ),
            )
            backfilled += 1

        if backfilled:
            logger.info(f"Backfilled {backfilled} rows into raw_activities from existing activities")

        self.conn.commit()

    def _create_tables(self):
        self._migrate_wellness()
        self._migrate_activity_metrics()
        self._migrate_sync_state()
        c = self.conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS wellness (
                date              TEXT    PRIMARY KEY,
                weight            REAL,
                resting_hr        REAL,
                rmssd             REAL,
                stress            REAL,
                sleep_score       REAL,
                sleep_hours       REAL,
                steps             INTEGER,
                spo2              REAL,
                body_battery_start REAL,
                body_battery_end  REAL,
                calories          REAL,
                active_calories   REAL,
                distance_m        REAL,
                min_hr            REAL,
                max_hr            REAL,
                respiration_rate  REAL,
                floors            INTEGER,
                hydration_ml      REAL,
                intensity_minutes REAL,
                body_battery      REAL,
                training_readiness_score REAL,
                endurance_score   REAL,
                hill_score        REAL,
                updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)


        c.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id              TEXT    PRIMARY KEY,
                start_date      TEXT    NOT NULL,
                activity_type   TEXT,
                duration        REAL,
                distance        REAL,
                average_power   REAL,
                max_power       REAL,
                average_hr      REAL,
                max_hr          REAL,
                calories        REAL,
                tss             REAL,
                ifr             REAL,
                normalized_power REAL,
                file_type       TEXT,
                updated_at      TEXT   NOT NULL DEFAULT (datetime('now'))
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS activity_streams (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id     TEXT    NOT NULL,
                elapsed         REAL    NOT NULL,
                metric          TEXT    NOT NULL,
                value           REAL    NOT NULL,
                FOREIGN KEY (activity_id) REFERENCES activities(id)
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity ON activity_streams(activity_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_streams_metric ON activity_streams(metric)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_streams_activity_metric ON activity_streams(activity_id, metric)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                source          TEXT    PRIMARY KEY,
                last_synced_at  TEXT    NOT NULL,
                details         TEXT,
                resume_offset   INTEGER DEFAULT 0
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS activity_metrics (
                activity_id     TEXT    NOT NULL,
                ftp_used        REAL,
                normalized_power REAL,
                intensity_factor REAL,
                tss             REAL,
                variability_index REAL,
                w_prime_capacity REAL,
                w_prime_min_balance REAL,
                decoupling_drift REAL,
                duration_sec    REAL,
                computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (activity_id)
            )
        """)
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS hr_calibration (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                calibration_factor REAL,
                num_calibration_rides INTEGER,
                last_updated TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Raw data tables (immutable, append-only)
        c.execute("""
            CREATE TABLE IF NOT EXISTS raw_activities (
                garmin_id              INTEGER PRIMARY KEY,
                start_time_local       TEXT    NOT NULL,
                activity_type_key      TEXT,
                duration_ms            REAL,
                distance_cm            REAL,
                avg_power              REAL,
                max_power              REAL,
                avg_heart_rate         REAL,
                max_heart_rate         REAL,
                calories               REAL,
                training_stress_score  REAL,
                norm_power             REAL,
                moving_duration_ms     REAL,
                elapsed_duration_ms    REAL,
                elevation_gain         REAL,
                steps                  REAL,
                intensity              REAL,
                raw_json               TEXT,
                synced_at              TEXT   NOT NULL DEFAULT (datetime('now'))
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS raw_fit_sessions (
                garmin_id              INTEGER PRIMARY KEY,
                total_elapsed_time_ms  REAL,
                total_distance_m       REAL,
                sport                  TEXT,
                avg_heart_rate         REAL,
                max_heart_rate         REAL,
                total_calories         REAL,
                avg_cadence            REAL,
                max_cadence            REAL,
                avg_power              REAL,
                max_power              REAL,
                parsed_at              TEXT   NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Raw wellness data (immutable, append-only)
        c.execute("""
            CREATE TABLE IF NOT EXISTS raw_wellness (
                date              TEXT    NOT NULL,
                source            TEXT    NOT NULL,
                raw_json          TEXT,
                synced_at         TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (date, source)
            )
        """)

        # --- New tables for ML model and prescription engine ---

        c.execute("""
            CREATE TABLE IF NOT EXISTS morning_checkin (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                date            TEXT    NOT NULL,
                perceived_readiness REAL,
                soreness        REAL,
                stress          REAL,
                sleep_quality   REAL,
                mood            REAL,
                energy          REAL,
                motivation      REAL,
                pain_score      REAL,
                pain_location   TEXT,
                notes           TEXT,
                caffeine        INTEGER DEFAULT 0,
                alcohol         INTEGER DEFAULT 0,
                late_meals      INTEGER DEFAULT 0,
                recorded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(athlete_id, date)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_readiness (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                date            TEXT    NOT NULL,
                rmssd           REAL,
                resting_hr      REAL,
                rmssd_mean_30d  REAL,
                rmssd_std_30d   REAL,
                rhr_mean_30d    REAL,
                rhr_std_30d     REAL,
                sleep_hours     REAL,
                sleep_score     REAL,
                perceived_readiness REAL,
                soreness        REAL,
                stress          REAL,
                mood            REAL,
                readiness_score REAL,
                readiness_state TEXT,
                recommendation  TEXT,
                ctl             REAL,
                atl             REAL,
                tsb             REAL,
                acwr            REAL,
                computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(athlete_id, date)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS training_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                planned_date    TEXT    NOT NULL,
                workout_id      TEXT,
                planned_type    TEXT,
                planned_duration REAL,
                planned_tss     REAL,
                readiness_at_plan REAL,
                actual_activity_id TEXT,
                actual_duration  REAL,
                actual_tss      REAL,
                actual_np       REAL,
                actual_rpe      REAL,
                completed       INTEGER DEFAULT 0,
                modification_reason TEXT,
                post_ride_notes TEXT,
                decoupling_drift REAL,
                w_prime_min_balance REAL,
                planned_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_training_log_athlete ON training_log(athlete_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_training_log_planned_date ON training_log(planned_date)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS edge_cases (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                case_type       TEXT    NOT NULL,
                start_date      TEXT    NOT NULL,
                end_date        TEXT,
                description     TEXT,
                training_impact TEXT,
                resolution      TEXT,
                resolved        INTEGER DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_edge_cases_athlete ON edge_cases(athlete_id)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS validation_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                check_name      TEXT    NOT NULL,
                target_date     TEXT    NOT NULL,
                severity        TEXT    NOT NULL,
                message         TEXT,
                raw_value       REAL,
                expected_min    REAL,
                expected_max    REAL,
                action_taken    TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_validation_log_athlete ON validation_log(athlete_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_validation_log_date ON validation_log(target_date)")

        self.conn.commit()
        # Run raw tables migration after all tables exist
        self._migrate_raw_tables()
        self._migrate_activity_api_fields()

    # -- Wellness --

    def store_wellness(self, records: list[dict[str, Any]]) -> int:
        """
        Upsert wellness records. Uses INSERT ... ON CONFLICT(date) DO UPDATE SET
        to preserve the updated_at timestamp on existing rows.

        Returns the number of records processed.
        """
        c = self.conn.cursor()
        n = 0
        for rec in records:
            date = rec.get("date") or rec.get("id")
            if not date:
                continue

            c.execute(
                """
                INSERT INTO wellness
                    (date, weight, resting_hr, rmssd, stress, sleep_score, sleep_hours, steps,
                     spo2, body_battery_start, body_battery_end, calories, active_calories,
                     distance_m, min_hr, max_hr, respiration_rate, floors, hydration_ml,
                     intensity_minutes, body_battery, training_readiness_score,
                     endurance_score, hill_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    weight = excluded.weight,
                    resting_hr = excluded.resting_hr,
                    rmssd = excluded.rmssd,
                    stress = excluded.stress,
                    sleep_score = excluded.sleep_score,
                    sleep_hours = excluded.sleep_hours,
                    steps = excluded.steps,
                    spo2 = excluded.spo2,
                    body_battery_start = excluded.body_battery_start,
                    body_battery_end = excluded.body_battery_end,
                    calories = excluded.calories,
                    active_calories = excluded.active_calories,
                    distance_m = excluded.distance_m,
                    min_hr = excluded.min_hr,
                    max_hr = excluded.max_hr,
                    respiration_rate = excluded.respiration_rate,
                    floors = excluded.floors,
                    hydration_ml = excluded.hydration_ml,
                    intensity_minutes = excluded.intensity_minutes,
                    body_battery = excluded.body_battery,
                    training_readiness_score = excluded.training_readiness_score,
                    endurance_score = excluded.endurance_score,
                    hill_score = excluded.hill_score
                """,
                (
                    date,
                    rec.get("weight"),
                    rec.get("resting_hr"),
                    rec.get("rmssd"),
                    rec.get("stress"),
                    rec.get("sleep_score"),
                    rec.get("sleep_hours"),
                    rec.get("steps"),
                    rec.get("spo2"),
                    rec.get("body_battery_start"),
                    rec.get("body_battery_end"),
                    rec.get("calories"),
                    rec.get("active_calories"),
                    rec.get("distance_m"),
                    rec.get("min_hr"),
                    rec.get("max_hr"),
                    rec.get("respiration_rate"),
                    rec.get("floors"),
                    rec.get("hydration_ml"),
                    rec.get("intensity_minutes"),
                    rec.get("body_battery"),
                    rec.get("training_readiness_score"),
                    rec.get("endurance_score"),
                    rec.get("hill_score"),
                ),
            )
            n += 1

        self.conn.commit()
        logger.info(f"Stored {n} wellness records")
        return n

    def get_wellness(
        self,
        oldest: str | None = None,
        newest: str | None = None,
    ) -> list[sqlite3.Row]:
        """Query wellness records within a date range."""
        query = "SELECT * FROM wellness"
        params: list[Any] = []

        if oldest and newest:
            query += " WHERE date >= ? AND date <= ?"
            params = [oldest, newest]
        elif oldest:
            query += " WHERE date >= ?"
            params = [oldest]
        elif newest:
            query += " WHERE date <= ?"
            params = [newest]

        query += " ORDER BY date DESC"
        return self.conn.execute(query, params).fetchall()

    def get_latest_wellness(self) -> sqlite3.Row | None:
        """Get the most recent wellness record."""
        return self.conn.execute(
            "SELECT * FROM wellness ORDER BY date DESC LIMIT 1"
        ).fetchone()

    def get_wellness_dates(self, oldest: str | None = None, newest: str | None = None) -> set[str]:
        """Return the set of dates that have wellness records, optionally filtered."""
        query = "SELECT date FROM wellness"
        params: list[Any] = []

        if oldest and newest:
            query += " WHERE date >= ? AND date <= ?"
            params = [oldest, newest]
        elif oldest:
            query += " WHERE date >= ?"
            params = [oldest]
        elif newest:
            query += " WHERE date <= ?"
            params = [newest]

        rows = self.conn.execute(query, params).fetchall()
        return {row["date"] for row in rows}

    # -- Activities --

    def store_activities(self, records: list[dict[str, Any]]) -> int:
        """
        Upsert activity summaries keyed on Intervals.icu activity id.

        Returns the number of records processed.
        """
        c = self.conn.cursor()
        n = 0
        for rec in records:
            aid = rec.get("id")
            if not aid:
                continue

            c.execute(
                """
                INSERT INTO activities
                    (id, start_date, activity_type, duration, distance,
                     average_power, max_power, average_hr, max_hr,
                     calories, tss, ifr, normalized_power, file_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    start_date = excluded.start_date,
                    activity_type = excluded.activity_type,
                    duration = excluded.duration,
                    distance = excluded.distance,
                    average_power = excluded.average_power,
                    max_power = excluded.max_power,
                    average_hr = excluded.average_hr,
                    max_hr = excluded.max_hr,
                    calories = excluded.calories,
                    tss = excluded.tss,
                    ifr = excluded.ifr,
                    normalized_power = excluded.normalized_power,
                    file_type = excluded.file_type
                """,
                (
                    aid,
                    rec.get("start_date_local"),
                    rec.get("type"),
                    rec.get("duration"),
                    rec.get("distance"),
                    rec.get("average_power"),
                    rec.get("max_power"),
                    rec.get("average_hr"),
                    rec.get("max_hr"),
                    rec.get("calories"),
                    rec.get("tss"),
                    rec.get("ifr"),
                    rec.get("normalized_power"),
                    rec.get("file_type"),
                ),
            )
            n += 1

        self.conn.commit()
        logger.info(f"Stored {n} activity records")
        return n

    def store_raw_wellness(self, date: str, source: str, data: Any) -> None:
        """Store raw JSON response from a Garmin wellness API call."""
        c = self.conn.cursor()
        c.execute(
            """
            INSERT INTO raw_wellness (date, source, raw_json)
            VALUES (?, ?, ?)
            ON CONFLICT(date, source) DO UPDATE SET
                raw_json = excluded.raw_json,
                synced_at = datetime('now')
            """,
            (date, source, json.dumps(data) if not isinstance(data, str) else data),
        )
        self.conn.commit()

    def get_activities(
        self,
        oldest: str | None = None,
        newest: str | None = None,
        activity_type: str | None = None,
    ) -> list[sqlite3.Row]:
        """Query activity summaries with optional filters."""
        query = "SELECT * FROM activities"
        conditions = []
        params: list[Any] = []

        if oldest:
            conditions.append("start_date >= ?")
            params.append(oldest)
        if newest:
            conditions.append("start_date <= ?")
            params.append(newest)
        if activity_type:
            conditions.append("activity_type = ?")
            params.append(activity_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY start_date DESC"
        return self.conn.execute(query, params).fetchall()

    # -- Raw Data Store (immutable, append-only) --

    def store_raw_activity(self, garmin_id: int, data: dict[str, Any]) -> None:
        """Store a raw Garmin API activity summary. UPSERT on garmin_id."""
        import json
        self.conn.execute(
            """INSERT OR REPLACE INTO raw_activities
                (garmin_id, start_time_local, activity_type_key,
                 duration_ms, distance_cm, avg_power, max_power,
                 avg_heart_rate, max_heart_rate, calories,
                 training_stress_score, norm_power,
                 moving_duration_ms, elapsed_duration_ms,
                 elevation_gain, steps, intensity,
                 raw_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                garmin_id,
                data.get("startTimeLocal", ""),
                data.get("activityTypeKey"),
                data.get("duration"),
                data.get("distance"),
                data.get("avgPower"),
                data.get("maxPower"),
                data.get("avgHeartRate"),
                data.get("maxHeartRate"),
                data.get("calories"),
                data.get("trainingStressScore"),
                data.get("normPower"),
                data.get("movingDuration"),
                data.get("elapsedDuration"),
                data.get("elevationGain"),
                data.get("steps"),
                data.get("intensity"),
                json.dumps(data),
            ),
        )
        self.conn.commit()

    def store_raw_fit_session(self, garmin_id: int, data: dict[str, Any]) -> None:
        """Store raw FIT session metrics. UPSERT on garmin_id."""
        self.conn.execute(
            """INSERT OR REPLACE INTO raw_fit_sessions
                (garmin_id, total_elapsed_time_ms, total_distance_m,
                 sport, avg_heart_rate, max_heart_rate, total_calories,
                 avg_cadence, max_cadence, avg_power, max_power, parsed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                garmin_id,
                data.get("total_elapsed_time_ms"),
                data.get("total_distance_m"),
                data.get("sport"),
                data.get("avg_heart_rate"),
                data.get("max_heart_rate"),
                data.get("total_calories"),
                data.get("avg_cadence"),
                data.get("max_cadence"),
                data.get("avg_power"),
                data.get("max_power"),
            ),
        )
        self.conn.commit()

    def refresh_activities(self) -> int:
        """Rebuild the activities table from raw data.

        For each garmin_id in raw_activities:
        - Start with API values (duration_ms/1000, distance_m as-is)
        - Override with FIT session values if available and reasonable
        - Override duration with stream-derived duration_sec from activity_metrics
        - Extract API-only fields from raw_json
        - Compute source indicators (FIT vs API)
        - Store result in activities table as garmin_{garmin_id}

        Returns the number of activities refreshed.
        """
        rows = self.conn.execute(
            "SELECT * FROM raw_activities ORDER BY garmin_id"
        ).fetchall()

        # Pre-fetch all FIT session data
        fit_rows = self.conn.execute(
            "SELECT * FROM raw_fit_sessions"
        ).fetchall()
        fit_by_id: dict[int, dict] = {}
        for fr in fit_rows:
            fit_by_id[fr["garmin_id"]] = dict(fr)

        # Pre-fetch all activity metrics for duration_sec
        metrics_rows = self.conn.execute(
            "SELECT activity_id, duration_sec FROM activity_metrics"
        ).fetchall()
        metrics_by_id: dict[str, dict] = {}
        for mr in metrics_rows:
            metrics_by_id[mr["activity_id"]] = dict(mr)

        refreshed = 0
        for row in rows:
            garmin_id = row["garmin_id"]
            db_id = f"garmin_{garmin_id}"

            # Start with API values
            duration = (row["duration_ms"] or 0) / 1000.0
            distance = (row["distance_cm"] or 0)  # Column named cm but stores meters
            avg_power = row["avg_power"]
            max_power = row["max_power"]
            avg_hr = row["avg_heart_rate"]
            max_hr = row["max_heart_rate"]
            calories = row["calories"]
            tss = row["training_stress_score"]
            norm_power = row["norm_power"]
            activity_type = row["activity_type_key"]
            start_date = row["start_time_local"]

            # Override with FIT session values if available
            fit = fit_by_id.get(garmin_id)
            if fit:
                # FIT HR/power are more accurate than API
                if fit["avg_heart_rate"] is not None:
                    avg_hr = fit["avg_heart_rate"]
                if fit["max_heart_rate"] is not None:
                    max_hr = fit["max_heart_rate"]
                if fit["avg_power"] is not None:
                    avg_power = fit["avg_power"]
                if fit["max_power"] is not None:
                    max_power = fit["max_power"]
                if fit["total_calories"] is not None:
                    calories = fit["total_calories"]
                if fit["total_elapsed_time_ms"] is not None:
                    fit_dur = fit["total_elapsed_time_ms"] / 1000.0
                    if fit_dur > 0:
                        duration = fit_dur
                if fit["total_distance_m"] is not None:
                    if fit["total_distance_m"] > 0:
                        distance = fit["total_distance_m"]

            # Override duration with stream-derived duration_sec (most accurate)
            am = metrics_by_id.get(db_id)
            if am and am.get("duration_sec") is not None and am["duration_sec"] > 0:
                duration = am["duration_sec"]

            # Parse raw_json for API-only fields
            row_dict = dict(row)
            raw_json = row_dict.get("raw_json")
            api_data = json.loads(raw_json) if raw_json else {}

            # Activity name and type
            activity_name = api_data.get("activityName")
            activity_type = row_dict.get("activity_type_key") or (api_data.get("activityType", {}).get("typeKey") if isinstance(api_data.get("activityType"), dict) else None)

            # Training effects
            aerobic_te = api_data.get("aerobicTrainingEffect")
            anaerobic_te = api_data.get("anaerobicTrainingEffect")

            # VO2 Max
            vo2_max = api_data.get("vO2MaxValue")

            # Elevation
            elev_gain = api_data.get("elevationGain")
            elev_loss = api_data.get("elevationLoss")
            min_elev = api_data.get("minElevation")
            max_elev = api_data.get("maxElevation")

            # Temperature
            min_temp = api_data.get("minTemperature")
            max_temp = api_data.get("maxTemperature")

            # Respiration
            avg_resp = api_data.get("avgRespirationRate")
            max_resp = api_data.get("maxRespirationRate")

            # Balance
            avg_left_balance = api_data.get("avgLeftBalance")

            # Moving duration (seconds)
            moving_dur = (api_data.get("movingDuration") or 0) / 1000.0 if api_data.get("movingDuration") else None

            # Intensity factor
            intensity_factor = api_data.get("intensityFactor")

            # HR zones
            hr_z1 = api_data.get("hrTimeInZone_1")
            hr_z2 = api_data.get("hrTimeInZone_2")
            hr_z3 = api_data.get("hrTimeInZone_3")
            hr_z4 = api_data.get("hrTimeInZone_4")
            hr_z5 = api_data.get("hrTimeInZone_5")

            # Power zones
            pwr_z1 = api_data.get("powerTimeInZone_1")
            pwr_z2 = api_data.get("powerTimeInZone_2")
            pwr_z3 = api_data.get("powerTimeInZone_3")
            pwr_z4 = api_data.get("powerTimeInZone_4")
            pwr_z5 = api_data.get("powerTimeInZone_5")
            pwr_z6 = api_data.get("powerTimeInZone_6")
            pwr_z7 = api_data.get("powerTimeInZone_7")

            # Max avg power at various durations
            max_pwr_1s = api_data.get("maxAvgPower_1")
            max_pwr_2s = api_data.get("maxAvgPower_2")
            max_pwr_5s = api_data.get("maxAvgPower_5")
            max_pwr_10s = api_data.get("maxAvgPower_10")
            max_pwr_20s = api_data.get("maxAvgPower_20")
            max_pwr_30s = api_data.get("maxAvgPower_30")
            max_pwr_60s = api_data.get("maxAvgPower_60")
            max_pwr_120s = api_data.get("maxAvgPower_120")
            max_pwr_300s = api_data.get("maxAvgPower_300")
            max_pwr_600s = api_data.get("maxAvgPower_600")
            max_pwr_1200s = api_data.get("maxAvgPower_1200")
            max_pwr_1800s = api_data.get("maxAvgPower_1800")
            max_pwr_3600s = api_data.get("maxAvgPower_3600")

            # Source indicators
            source_duration = "FIT" if fit and fit["total_elapsed_time_ms"] else "API"
            source_distance = "FIT" if fit and fit["total_distance_m"] else "API"
            source_power = "FIT" if fit and fit["avg_power"] else "API"
            source_hr = "FIT" if fit and fit["avg_heart_rate"] else "API"
            source_calories = "FIT" if fit and fit["total_calories"] else "API"

            self.conn.execute(
                """INSERT OR REPLACE INTO activities
                    (id, start_date, activity_type, activity_name, duration, distance,
                     average_power, max_power, average_hr, max_hr,
                     calories, tss, normalized_power, intensity_factor,
                     aerobic_training_effect, anaerobic_training_effect, vo2_max,
                     elevation_gain, elevation_loss, min_elevation, max_elevation,
                     min_temperature, max_temperature,
                     avg_respiration_rate, max_respiration_rate, avg_left_balance,
                     moving_duration,
                     hr_zone_1, hr_zone_2, hr_zone_3, hr_zone_4, hr_zone_5,
                     power_zone_1, power_zone_2, power_zone_3, power_zone_4, power_zone_5, power_zone_6, power_zone_7,
                     max_avg_power_1s, max_avg_power_2s, max_avg_power_5s, max_avg_power_10s,
                     max_avg_power_20s, max_avg_power_30s, max_avg_power_60s, max_avg_power_120s,
                     max_avg_power_300s, max_avg_power_600s, max_avg_power_1200s, max_avg_power_1800s, max_avg_power_3600s,
                     source_duration, source_distance, source_power, source_hr, source_calories,
                     updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    db_id,
                    start_date,
                    activity_type,
                    activity_name,
                    duration,
                    distance,
                    avg_power,
                    max_power,
                    avg_hr,
                    max_hr,
                    calories,
                    tss,
                    norm_power,
                    intensity_factor,
                    aerobic_te,
                    anaerobic_te,
                    vo2_max,
                    elev_gain,
                    elev_loss,
                    min_elev,
                    max_elev,
                    min_temp,
                    max_temp,
                    avg_resp,
                    max_resp,
                    avg_left_balance,
                    moving_dur,
                    hr_z1,
                    hr_z2,
                    hr_z3,
                    hr_z4,
                    hr_z5,
                    pwr_z1,
                    pwr_z2,
                    pwr_z3,
                    pwr_z4,
                    pwr_z5,
                    pwr_z6,
                    pwr_z7,
                    max_pwr_1s,
                    max_pwr_2s,
                    max_pwr_5s,
                    max_pwr_10s,
                    max_pwr_20s,
                    max_pwr_30s,
                    max_pwr_60s,
                    max_pwr_120s,
                    max_pwr_300s,
                    max_pwr_600s,
                    max_pwr_1200s,
                    max_pwr_1800s,
                    max_pwr_3600s,
                    source_duration,
                    source_distance,
                    source_power,
                    source_hr,
                    source_calories,
                ),
            )
            refreshed += 1

        self.conn.commit()
        logger.info(f"Refreshed {refreshed} activities from raw data")
        return refreshed

    # -- Activity Streams --

    def store_activity_streams(
        self, activity_id: str, metric: str, values: list[tuple[float, float]]
    ) -> int:
        """
        Store a time-series metric for an activity.

        Args:
            activity_id: Intervals.icu activity ID.
            metric: metric name (e.g. "power", "heart_rate", "dfa_a1").
            values: list of (elapsed_seconds, value) tuples.

        Returns the number of rows inserted.
        """
        c = self.conn.cursor()
        rows = [(activity_id, elapsed, metric, val) for elapsed, val in values]
        c.executemany(
            "INSERT INTO activity_streams (activity_id, elapsed, metric, value) VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        logger.info(f"Stored {len(rows)} {metric} samples for {activity_id}")
        return len(rows)

    def get_activity_streams(
        self, activity_id: str, metric: str
    ) -> list[sqlite3.Row]:
        """Get all samples for a specific metric of an activity."""
        return self.conn.execute(
            "SELECT elapsed, value FROM activity_streams "
            "WHERE activity_id = ? AND metric = ? ORDER BY elapsed",
            (activity_id, metric),
        ).fetchall()

    # -- Sync State --

    def get_last_synced(self, source: str) -> str | None:
        """Return the last_synced_at timestamp for a source, or None."""
        row = self.conn.execute(
            "SELECT last_synced_at FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return row["last_synced_at"] if row else None

    def get_resume_offset(self, source: str) -> int:
        """Return the resume_offset for a source, or 0."""
        row = self.conn.execute(
            "SELECT resume_offset FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        offset = row["resume_offset"] if row else 0
        if offset is None:
            return 0
        return int(offset)

    def set_last_synced(self, source: str, ts: str, details: str | None = None, resume_offset: int = 0):
        """Record the last sync timestamp for a source."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_state (source, last_synced_at, details, resume_offset) "
            "VALUES (?, ?, ?, ?)",
            (source, ts, details, resume_offset),
        )
        self.conn.commit()

    # -- Activity Metrics (computed, separate from raw data) --

    def store_activity_metrics(self, activity_id: str, metrics: dict[str, Any]):
        """Store computed metrics for an activity (separate from raw data).

        Merges with existing row: columns not provided in `metrics` retain
        their previous values, preventing data loss on partial updates.
        """
        # Read existing row to preserve columns not in this update
        existing = self.conn.execute(
            "SELECT ftp_used, cp_used, ride_cp, normalized_power, intensity_factor, tss, "
            "variability_index, w_prime_capacity, w_prime_min_balance, "
            "decoupling_drift, duration_sec, hr_tss, hr_trimp "
            "FROM activity_metrics WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()

        if existing is not None:
            existing_dict = {
                "cp_used": existing[1],
                "ride_cp": existing[2],
                "normalized_power": existing[3],
                "intensity_factor": existing[4],
                "tss": existing[5],
                "variability_index": existing[6],
                "w_prime_capacity": existing[7],
                "w_prime_min_balance": existing[8],
                "decoupling_drift": existing[9],
                "duration_sec": existing[10],
                "hr_tss": existing[11],
                "hr_trimp": existing[12],
            }
            existing_dict.update(metrics)
            metrics = existing_dict

        self.conn.execute(
            "INSERT OR REPLACE INTO activity_metrics "
            "(activity_id, ftp_used, cp_used, ride_cp, normalized_power, intensity_factor, tss, "
            "variability_index, w_prime_capacity, w_prime_min_balance, "
            "decoupling_drift, duration_sec, hr_tss, hr_trimp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity_id,
                metrics.get("ftp_used"),
                metrics.get("cp_used"),
                metrics.get("ride_cp"),
                metrics.get("normalized_power"),
                metrics.get("intensity_factor"),
                metrics.get("tss"),
                metrics.get("variability_index"),
                metrics.get("w_prime_capacity"),
                metrics.get("w_prime_min_balance"),
                metrics.get("decoupling_drift"),
                metrics.get("duration_sec"),
                metrics.get("hr_tss"),
                metrics.get("hr_trimp"),
            ),
        )
        self.conn.commit()

    def get_activity_metrics(self, activity_id: str) -> dict[str, Any] | None:
        """Get computed metrics for an activity, or None if not computed."""
        row = self.conn.execute(
            "SELECT * FROM activity_metrics WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    def get_hr_calibration(self) -> dict[str, Any] | None:
        """Get stored HR calibration factor."""
        row = self.conn.execute(
            "SELECT calibration_factor, num_calibration_rides, last_updated "
            "FROM hr_calibration WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def store_hr_calibration(self, factor: float, num_rides: int) -> None:
        """Store or update HR calibration factor."""
        self.conn.execute(
            "INSERT OR REPLACE INTO hr_calibration "
            "(id, calibration_factor, num_calibration_rides, last_updated) "
            "VALUES (1, ?, ?, datetime('now'))",
            (factor, num_rides),
        )
        self.conn.commit()

    # -- Trend Queries --

    ALLOWED_TABLES = {"wellness", "activity_metrics", "activity_streams"}

    def get_trend_data(self, table: str, columns: list[str], oldest: str | None = None, newest: str | None = None) -> list[dict[str, Any]]:
        """Return rows for longitudinal plotting.

        Args:
            table: table name (e.g. "wellness", "activity_metrics").
            columns: column names to SELECT.
            oldest: inclusive start date (ISO format).
            newest: inclusive end date (ISO format).
        """
        if table not in self.ALLOWED_TABLES:
            raise ValueError(f"Table '{table}' is not allowed. Allowed: {self.ALLOWED_TABLES}")
        cols = ", ".join(columns)
        query = f"SELECT {cols} FROM {table}"
        params: list = []
        if oldest and newest:
            query += " WHERE date >= ? AND date <= ?"
            params = [oldest, newest]
        elif oldest:
            query += " WHERE date >= ?"
            params = [oldest]
        elif newest:
            query += " WHERE date <= ?"
            params = [newest]
        query += " ORDER BY date"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_activity_metrics_by_date(
        self, oldest: str | None = None, newest: str | None = None
    ) -> list[dict[str, Any]]:
        """Query activity_metrics joined with activities for date filtering."""
        query = """
            SELECT am.*, a.start_date
            FROM activity_metrics am
            JOIN activities a ON a.id = am.activity_id
        """
        params: list = []
        if oldest and newest:
            query += " WHERE a.start_date >= ? AND a.start_date <= ?"
            params = [oldest, newest]
        elif oldest:
            query += " WHERE a.start_date >= ?"
            params = [oldest]
        elif newest:
            query += " WHERE a.start_date <= ?"
            params = [newest]
        query += " ORDER BY a.start_date"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_activity_with_metrics(self, activity_id: str) -> dict[str, Any] | None:
        """Join activities and activity_metrics for a single activity."""
        act = self.conn.execute(
            "SELECT * FROM activities WHERE id = ?", (activity_id,)
        ).fetchone()
        if act is None:
            return None
        result = dict(act)
        metrics = self.get_activity_metrics(activity_id)
        if metrics:
            result.update(metrics)
        return result

    # -- Routes --

    def store_routes(self, activity_id: str, points: list[tuple[float, float]]) -> int:
        """Bulk-insert route points for an activity.

        Args:
            activity_id: the activity identifier.
            points: list of (latitude, longitude) tuples.

        Returns the number of points inserted. Skips if activity already has routes.
        """
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM activity_routes WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()[0]
        if existing > 0:
            logger.info(f"Activity {activity_id} already has routes; skipping")
            return 0

        c = self.conn.cursor()
        c.executemany(
            "INSERT INTO activity_routes (activity_id, latitude, longitude, sequence) VALUES (?, ?, ?, ?)",
            ((activity_id, lat, lon, seq) for seq, (lat, lon) in enumerate(points)),
        )
        self.conn.commit()
        logger.info(f"Stored {len(points)} route points for {activity_id}")
        return len(points)

    def get_all_routes(self) -> list[dict[str, Any]]:
        """Return all route points ordered by activity_id and sequence."""
        rows = self.conn.execute(
            "SELECT * FROM activity_routes ORDER BY activity_id, sequence"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_routes_for_activity(self, activity_id: str) -> list[dict[str, Any]]:
        """Return route points for a specific activity, ordered by sequence."""
        rows = self.conn.execute(
            "SELECT * FROM activity_routes WHERE activity_id = ? ORDER BY sequence",
            (activity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_route_count(self) -> int:
        """Return the number of distinct activities with route data."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT activity_id) FROM activity_routes"
        ).fetchone()
        return row[0]
    def get_route_count_for_activity(self, activity_id: str) -> int:
        """Return the number of route points for a specific activity."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM activity_routes WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
        return row[0]


    # -- Daily Readiness --

    def insert_daily_readiness(self, record: dict[str, Any]) -> int:
        """Insert or replace a daily readiness record. Returns the row id."""
        self.conn.execute(
            "INSERT INTO daily_readiness "
            "(athlete_id, date, rmssd, resting_hr, rmssd_mean_30d, rmssd_std_30d, "
            " rhr_mean_30d, rhr_std_30d, sleep_hours, sleep_score, "
            " perceived_readiness, soreness, stress, mood, "
            " readiness_score, readiness_state, recommendation, "
            " ctl, atl, tsb, acwr) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(athlete_id, date) DO UPDATE SET "
            "rmssd = excluded.rmssd, "
            "resting_hr = excluded.resting_hr, "
            "rmssd_mean_30d = excluded.rmssd_mean_30d, "
            "rmssd_std_30d = excluded.rmssd_std_30d, "
            "rhr_mean_30d = excluded.rhr_mean_30d, "
            "rhr_std_30d = excluded.rhr_std_30d, "
            "sleep_hours = excluded.sleep_hours, "
            "sleep_score = excluded.sleep_score, "
            "perceived_readiness = excluded.perceived_readiness, "
            "soreness = excluded.soreness, "
            "stress = excluded.stress, "
            "mood = excluded.mood, "
            "readiness_score = excluded.readiness_score, "
            "readiness_state = excluded.readiness_state, "
            "recommendation = excluded.recommendation, "
            "ctl = excluded.ctl, "
            "atl = excluded.atl, "
            "tsb = excluded.tsb, "
            "acwr = excluded.acwr, "
            "computed_at = datetime('now')",
            (
                record.get("athlete_id"),
                record.get("date"),
                record.get("rmssd"),
                record.get("resting_hr"),
                record.get("rmssd_mean_30d"),
                record.get("rmssd_std_30d"),
                record.get("rhr_mean_30d"),
                record.get("rhr_std_30d"),
                record.get("sleep_hours"),
                record.get("sleep_score"),
                record.get("perceived_readiness"),
                record.get("soreness"),
                record.get("stress"),
                record.get("mood"),
                record.get("readiness_score"),
                record.get("readiness_state"),
                record.get("recommendation"),
                record.get("ctl"),
                record.get("atl"),
                record.get("tsb"),
                record.get("acwr"),
            ),
        )
        self.conn.commit()
        return int(self.conn.execute(
            "SELECT id FROM daily_readiness WHERE athlete_id = ? AND date = ?",
            (record.get("athlete_id"), record.get("date")),
        ).fetchone()[0])

    def get_daily_readiness_by_date(self, athlete_id: str, date: str) -> dict[str, Any] | None:
        """Get daily readiness for a specific athlete and date."""
        row = self.conn.execute(
            "SELECT * FROM daily_readiness WHERE athlete_id = ? AND date = ?",
            (athlete_id, date),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_daily_readiness(
        self, athlete_id: str, days: int = 30
    ) -> list[dict[str, Any]]:
        """Get recent daily readiness records for an athlete."""
        rows = self.conn.execute(
            "SELECT * FROM daily_readiness "
            "WHERE athlete_id = ? AND date >= date('now', ?) "
            "ORDER BY date DESC",
            (athlete_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_readiness_by_state(
        self, athlete_id: str, readiness_state: str
    ) -> list[dict[str, Any]]:
        """Get readiness records filtered by readiness state."""
        rows = self.conn.execute(
            "SELECT * FROM daily_readiness "
            "WHERE athlete_id = ? AND readiness_state = ? "
            "ORDER BY date DESC",
            (athlete_id, readiness_state),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_readiness(
        self, athlete_id: str, oldest: str | None = None, newest: str | None = None
    ) -> list[dict[str, Any]]:
        """Query daily readiness with optional date range."""
        query = "SELECT * FROM daily_readiness WHERE athlete_id = ?"
        params: list[Any] = [athlete_id]

        if oldest:
            query += " AND date >= ?"
            params.append(oldest)
        if newest:
            query += " AND date <= ?"
            params.append(newest)

        query += " ORDER BY date DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- Training Log --

    def insert_training_log(self, record: dict[str, Any]) -> int:
        """Insert a training log entry. Returns the row id."""
        self.conn.execute(
            "INSERT INTO training_log "
            "(athlete_id, planned_date, workout_id, planned_type, planned_duration, "
            " planned_tss, readiness_at_plan, actual_activity_id, actual_duration, "
            " actual_tss, actual_np, actual_rpe, completed, modification_reason, "
            " post_ride_notes, decoupling_drift, w_prime_min_balance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("athlete_id"),
                record.get("planned_date"),
                record.get("workout_id"),
                record.get("planned_type"),
                record.get("planned_duration"),
                record.get("planned_tss"),
                record.get("readiness_at_plan"),
                record.get("actual_activity_id"),
                record.get("actual_duration"),
                record.get("actual_tss"),
                record.get("actual_np"),
                record.get("actual_rpe"),
                record.get("completed", 0),
                record.get("modification_reason"),
                record.get("post_ride_notes"),
                record.get("decoupling_drift"),
                record.get("w_prime_min_balance"),
            ),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def update_training_log(self, log_id: int, updates: dict[str, Any]) -> None:
        """Update fields on an existing training log entry."""
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(log_id)
        self.conn.execute(
            f"UPDATE training_log SET {set_clause} WHERE id = ?",
            values,
        )
        self.conn.commit()

    def get_training_log(
        self, athlete_id: str, oldest: str | None = None, newest: str | None = None
    ) -> list[dict[str, Any]]:
        """Query training log with optional date range."""
        query = "SELECT * FROM training_log WHERE athlete_id = ?"
        params: list[Any] = [athlete_id]

        if oldest:
            query += " AND planned_date >= ?"
            params.append(oldest)
        if newest:
            query += " AND planned_date <= ?"
            params.append(newest)

        query += " ORDER BY planned_date DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_planned_workouts(
        self, athlete_id: str, date: str
    ) -> list[dict[str, Any]]:
        """Get planned workouts for a specific date."""
        rows = self.conn.execute(
            "SELECT * FROM training_log WHERE athlete_id = ? AND planned_date = ?",
            (athlete_id, date),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_completed_training(
        self, athlete_id: str, days: int = 7
    ) -> list[dict[str, Any]]:
        """Get completed training entries for recent days."""
        rows = self.conn.execute(
            "SELECT * FROM training_log "
            "WHERE athlete_id = ? AND completed = 1 "
            "AND planned_date >= date('now', ?) "
            "ORDER BY planned_date DESC",
            (athlete_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Edge Cases --

    def insert_edge_case(self, record: dict[str, Any]) -> int:
        """Insert an edge case record. Returns the row id."""
        self.conn.execute(
            "INSERT INTO edge_cases "
            "(athlete_id, case_type, start_date, end_date, description, "
            " training_impact, resolution, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("athlete_id"),
                record.get("case_type"),
                record.get("start_date"),
                record.get("end_date"),
                record.get("description"),
                record.get("training_impact"),
                record.get("resolution"),
                record.get("resolved", 0),
            ),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def update_edge_case(self, case_id: int, updates: dict[str, Any]) -> None:
        """Update fields on an edge case record."""
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(case_id)
        self.conn.execute(
            f"UPDATE edge_cases SET {set_clause} WHERE id = ?",
            values,
        )
        self.conn.commit()

    def get_edge_cases(
        self, athlete_id: str, resolved: int | None = None
    ) -> list[dict[str, Any]]:
        """Get edge cases for an athlete, optionally filtered by resolved status."""
        query = "SELECT * FROM edge_cases WHERE athlete_id = ?"
        params: list[Any] = [athlete_id]

        if resolved is not None:
            query += " AND resolved = ?"
            params.append(resolved)

        query += " ORDER BY start_date DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_active_edge_cases(self, athlete_id: str) -> list[dict[str, Any]]:
        """Get unresolved edge cases for an athlete."""
        return self.get_edge_cases(athlete_id, resolved=0)

    # -- Validation Log --

    def insert_validation_log(self, record: dict[str, Any]) -> int:
        """Insert a validation log entry. Returns the row id."""
        self.conn.execute(
            "INSERT INTO validation_log "
            "(athlete_id, check_name, target_date, severity, message, "
            " raw_value, expected_min, expected_max, action_taken) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("athlete_id"),
                record.get("check_name"),
                record.get("target_date"),
                record.get("severity"),
                record.get("message"),
                record.get("raw_value"),
                record.get("expected_min"),
                record.get("expected_max"),
                record.get("action_taken"),
            ),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def get_validation_logs(
        self, athlete_id: str, oldest: str | None = None, newest: str | None = None,
        severity: str | None = None
    ) -> list[dict[str, Any]]:
        """Query validation logs with optional filters."""
        query = "SELECT * FROM validation_log WHERE athlete_id = ?"
        params: list[Any] = [athlete_id]

        if oldest:
            query += " AND target_date >= ?"
            params.append(oldest)
        if newest:
            query += " AND target_date <= ?"
            params.append(newest)
        if severity:
            query += " AND severity = ?"
            params.append(severity)

        query += " ORDER BY target_date DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_validation_errors(
        self, athlete_id: str, date: str
    ) -> list[dict[str, Any]]:
        """Get error-level validation entries for a specific date."""
        rows = self.conn.execute(
            "SELECT * FROM validation_log "
            "WHERE athlete_id = ? AND target_date = ? AND severity = 'error' "
            "ORDER BY created_at DESC",
            (athlete_id, date),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Morning Check-in ----

    def store_morning_checkin(self, data: dict[str, Any]) -> None:
        """Store or update a morning check-in record."""
        # Check if table exists and get columns
        try:
            cursor = self.conn.execute("PRAGMA table_info(morning_checkin)")
            columns = [row[1] for row in cursor.fetchall()]
        except Exception:
            columns = []
        
        # Map our simple fields to the actual schema
        # Use a default athlete_id for single-athlete use
        athlete_id = data.get("athlete_id", "default")
        
        values = {
            "athlete_id": athlete_id,
            "date": data["date"],
            "soreness": data.get("soreness"),
            "stress": data.get("stress"),
            "sleep_quality": data.get("sleep_quality"),
            "mood": data.get("mood"),
            "energy": data.get("energy"),
            "motivation": data.get("motivation"),
            "caffeine": 1 if data.get("caffeine") else 0,
            "alcohol": 1 if data.get("alcohol") else 0,
            "late_meals": 1 if data.get("late_meals") else 0,
            "notes": data.get("notes"),
        }
        
        # Only insert columns that exist in the table
        insert_cols = [col for col in values.keys() if col in columns]
        if not insert_cols:
            logger.warning("morning_checkin table has no matching columns")
            return
        
        placeholders = ", ".join(["?" for _ in insert_cols])
        col_list = ", ".join(insert_cols)
        param_list = [values[col] for col in insert_cols]
        
        self.conn.execute(
            f"INSERT OR REPLACE INTO morning_checkin ({col_list}) VALUES ({placeholders})",
            param_list,
        )
        self.conn.commit()

    def get_morning_checkin(self, date: str) -> dict[str, Any] | None:
        """Get a single morning check-in by date."""
        row = self.conn.execute(
            "SELECT * FROM morning_checkin WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None

    def get_morning_checkins(self, limit: int = 30) -> list[dict[str, Any]]:
        """Get recent morning check-ins."""
        rows = self.conn.execute(
            "SELECT * FROM morning_checkin ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    # -- Lifecycle --

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()