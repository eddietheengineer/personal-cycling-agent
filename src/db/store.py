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
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        c = self.conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS wellness (
                date        TEXT    PRIMARY KEY,
                weight      REAL,
                resting_hr  REAL,
                rmssd       REAL,
                stress      REAL,
                sleep_score REAL,
                sleep_hours REAL,
                steps       INTEGER,
                updated_at  TEXT   NOT NULL DEFAULT (datetime('now'))
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

        self.conn.commit()

    # -- Wellness --

    def store_wellness(self, records: list[dict[str, Any]]) -> int:
        """
        Upsert wellness records. Uses INSERT OR REPLACE keyed on date.

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
                INSERT OR REPLACE INTO wellness
                    (date, weight, resting_hr, rmssd, stress, sleep_score, sleep_hours, steps)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

    # -- Lifecycle --

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()