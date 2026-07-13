"""
SQLite persistence layer for cycling-ai-agent.

Tables:
- wellness: daily wellness records (HRV/RMSSD, RHR, weight, stress, sleep).
- activities: completed activity summaries with key metrics.
- activity_streams: per-activity time-series data (power, HR, DFA-a1, etc.).
"""

import logging
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
                updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS morning_checkin (
                date            TEXT    PRIMARY KEY,
                soreness        INTEGER,
                stress          INTEGER,
                sleep_quality   INTEGER,
                mood            INTEGER,
                energy          INTEGER,
                motivation      INTEGER,
                caffeine        INTEGER DEFAULT 0,
                alcohol         INTEGER DEFAULT 0,
                late_meals      INTEGER DEFAULT 0,
                updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
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
                details         TEXT
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

        # --- New tables for ML model and prescription engine ---

        c.execute("""
            CREATE TABLE IF NOT EXISTS morning_checkin (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_id      TEXT    NOT NULL,
                date            TEXT    NOT NULL,
                perceived_readiness REAL,
                soreness        REAL,
                life_stress     REAL,
                sleep_quality   REAL,
                mood            REAL,
                energy          REAL,
                motivation      REAL,
                pain_score      REAL,
                pain_location   TEXT,
                notes           TEXT,
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
                life_stress     REAL,
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
                     distance_m, min_hr, max_hr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    max_hr = excluded.max_hr
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
                INSERT OR REPLACE INTO activities
                    (id, start_date, activity_type, duration, distance,
                     average_power, max_power, average_hr, max_hr,
                     calories, tss, ifr, normalized_power, file_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        """Store computed metrics for an activity (separate from raw data)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO activity_metrics "
            "(activity_id, ftp_used, normalized_power, intensity_factor, tss, variability_index, "
            " w_prime_capacity, w_prime_min_balance, decoupling_drift, duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity_id,
                metrics.get("cp_used"),
                metrics.get("normalized_power"),
                metrics.get("intensity_factor"),
                metrics.get("tss"),
                metrics.get("variability_index"),
                metrics.get("w_prime_capacity"),
                metrics.get("w_prime_min_balance"),
                metrics.get("decoupling_drift"),
                metrics.get("duration_sec"),
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

    # -- Morning Checkin --

    def insert_morning_checkin(self, record: dict[str, Any]) -> int:
        """Insert or replace a morning checkin record. Returns the row id."""
        self.conn.execute(
            "INSERT INTO morning_checkin "
            "(athlete_id, date, perceived_readiness, soreness, life_stress, "
            " sleep_quality, mood, energy, motivation, pain_score, pain_location, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(athlete_id, date) DO UPDATE SET "
            "perceived_readiness = excluded.perceived_readiness, "
            "soreness = excluded.soreness, "
            "life_stress = excluded.life_stress, "
            "sleep_quality = excluded.sleep_quality, "
            "mood = excluded.mood, "
            "energy = excluded.energy, "
            "motivation = excluded.motivation, "
            "pain_score = excluded.pain_score, "
            "pain_location = excluded.pain_location, "
            "notes = excluded.notes, "
            "recorded_at = datetime('now')",
            (
                record.get("athlete_id"),
                record.get("date"),
                record.get("perceived_readiness"),
                record.get("soreness"),
                record.get("life_stress"),
                record.get("sleep_quality"),
                record.get("mood"),
                record.get("energy"),
                record.get("motivation"),
                record.get("pain_score"),
                record.get("pain_location"),
                record.get("notes"),
            ),
        )
        self.conn.commit()
        return int(self.conn.execute(
            "SELECT id FROM morning_checkin WHERE athlete_id = ? AND date = ?",
            (record.get("athlete_id"), record.get("date")),
        ).fetchone()[0])

    def get_morning_checkin(self, athlete_id: str, date: str) -> dict[str, Any] | None:
        """Get a morning checkin for a specific athlete and date."""
        row = self.conn.execute(
            "SELECT * FROM morning_checkin WHERE athlete_id = ? AND date = ?",
            (athlete_id, date),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_morning_checkins(
        self, athlete_id: str, days: int = 30
    ) -> list[dict[str, Any]]:
        """Get recent morning checkins for an athlete, ordered by date desc."""
        rows = self.conn.execute(
            "SELECT * FROM morning_checkin "
            "WHERE athlete_id = ? AND date >= date('now', ?) "
            "ORDER BY date DESC",
            (athlete_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_morning_checkins(
        self, athlete_id: str, oldest: str | None = None, newest: str | None = None
    ) -> list[dict[str, Any]]:
        """Query morning checkins with optional date range."""
        query = "SELECT * FROM morning_checkin WHERE athlete_id = ?"
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

    # -- Daily Readiness --

    def insert_daily_readiness(self, record: dict[str, Any]) -> int:
        """Insert or replace a daily readiness record. Returns the row id."""
        self.conn.execute(
            "INSERT INTO daily_readiness "
            "(athlete_id, date, rmssd, resting_hr, rmssd_mean_30d, rmssd_std_30d, "
            " rhr_mean_30d, rhr_std_30d, sleep_hours, sleep_score, "
            " perceived_readiness, soreness, life_stress, mood, "
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
            "life_stress = excluded.life_stress, "
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
                record.get("life_stress"),
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
            "life_stress": data.get("stress"),  # map stress -> life_stress
            "sleep_quality": data.get("sleep_quality"),
            "mood": data.get("mood"),
            "energy": data.get("energy"),
            "motivation": data.get("motivation"),
            "caffeine": 1 if data.get("caffeine") else 0,
            "alcohol": 1 if data.get("alcohol") else 0,
            "late_meals": 1 if data.get("late_meals") else 0,
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