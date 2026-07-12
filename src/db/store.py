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
        self._create_tables()

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
            "ftp_used": "REAL",
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
        self.conn.commit()
        self.conn.execute("PRAGMA journal_mode=WAL")

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
                metrics.get("ftp_used"),
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

    # -- Lifecycle --

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()