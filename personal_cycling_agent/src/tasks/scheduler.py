"""
Background sync scheduler — polls Garmin Connect on a configurable cadence.

Runs as a daemon thread that periodically calls sync_garmin(days=1) and
sync_activities(days=1) to keep data fresh without manual intervention.

Configuration is persisted in config.env:
    AUTO_SYNC_ENABLED=true
    AUTO_SYNC_ACTIVITY_HOURS=1
    AUTO_SYNC_WELLNESS_HOURS=6

Usage:
    from src.tasks.scheduler import get_scheduler
    scheduler = get_scheduler()
    scheduler.start()
    # ... later
    scheduler.stop()
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from src import config
from src.config.constants import DEFAULT_ACTIVITY_SYNC_HOURS, DEFAULT_WELLNESS_SYNC_HOURS
from src.tasks.worker import get_default_sync, get_default_task


# Module-level lock for config.env writes (prevents TOCTOU between threads)
_env_lock = threading.Lock()


def _write_env(key: str, value: str) -> None:
    """Write a KEY=VALUE pair to config.env, updating existing key if present."""
    env_path = config.config_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with _env_lock:
        if env_path.exists():
            lines = env_path.read_text().splitlines()
        else:
            lines = []
        if any(l.startswith(f"{key}=") for l in lines):
            lines = [f'{key}="{value}"' if l.startswith(f"{key}=") else l for l in lines]
        else:
            lines.append(f'{key}="{value}"')
        env_path.write_text("\n".join(lines) + "\n")

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_ACTIVITY_HOURS = DEFAULT_ACTIVITY_SYNC_HOURS
DEFAULT_WELLNESS_HOURS = DEFAULT_WELLNESS_SYNC_HOURS

# How long stop() waits for the loop thread before returning and letting it
# finish in the background (exposed so tests can shorten it).
STOP_JOIN_TIMEOUT_S = 10.0


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    return val in ("true", "1", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


# ── Scheduler ─────────────────────────────────────────────────────────────

class SyncScheduler:
    """Daemon thread that periodically polls Garmin for new data.

    Two independent cycles:
    - Activities: polls every N hours for new completed activities
    - Wellness: polls every N hours for new daily wellness data

    Skips a cycle if manual background work (a sync or a reparse task) is
    already running (avoids collisions).
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Last successful sync timestamps (in-memory)
        self._last_activity_sync: float = 0.0
        self._last_wellness_sync: float = 0.0

        # Stats
        self._stats: dict[str, Any] = {
            "total_activity_syncs": 0,
            "total_wellness_syncs": 0,
            "total_errors": 0,
            "last_activity_sync_time": None,
            "last_wellness_sync_time": None,
            "last_error": None,
            "last_error_time": None,
        }

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def enabled(self) -> bool:
        return _env_bool("AUTO_SYNC_ENABLED", False)

    @property
    def activity_interval_hours(self) -> int:
        return _env_int("AUTO_SYNC_ACTIVITY_HOURS", DEFAULT_ACTIVITY_HOURS)

    @property
    def wellness_interval_hours(self) -> int:
        return _env_int("AUTO_SYNC_WELLNESS_HOURS", DEFAULT_WELLNESS_HOURS)

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def start(self) -> None:
        """Start the scheduler daemon thread (idempotent)."""
        with self._lock:
            if self.is_running:
                logger.info("Sync scheduler already running")
                return
            if not self.enabled:
                logger.info("Sync scheduler not enabled — set AUTO_SYNC_ENABLED=true")
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="sync-scheduler",
            )
            self._thread.start()
            logger.info(
                f"Sync scheduler started: activities every "
                f"{self.activity_interval_hours}h, wellness every "
                f"{self.wellness_interval_hours}h"
            )

    def stop(self) -> None:
        """Stop the scheduler.

        Sets the stop event and joins the loop briefly. If a sync is still in
        flight, the loop thread exits on its own once it finishes; until then
        ``is_running`` stays True so a later ``start()`` cannot spawn a second
        concurrent loop.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.warning(
                    "Sync scheduler stop: loop still finishing a sync; "
                    "it will exit on its own"
                )
        logger.info("Sync scheduler stopped")

    def _is_background_work_running(self) -> bool:
        """Check if user-initiated background work is currently running.

        Covers both a manual sync (``BackgroundSync``) and a reparse or
        prescribe task (``BackgroundTask``) — all three write the same
        database and FIT store the auto-sync cycles write to.
        """
        bg_sync = get_default_sync()
        if bg_sync is not None and bg_sync.is_running:
            return True
        bg_task = get_default_task()
        return bg_task is not None and bg_task.is_running

    def _run(self) -> None:
        """Main scheduler loop.

        Intervals are re-read from the environment every cycle so that
        ``set_intervals`` takes effect without an app restart. A shortened
        interval also collapses any over-long pending wait (bounded by one
        new interval); a lengthened one applies from the next fire onward.
        """
        try:
            # On startup, check for missed data immediately (don't wait for first interval)
            next_activity_check = time.time()
            next_wellness_check = time.time()

            while not self._stop_event.is_set():
                now = time.time()

                # Re-read each cycle: interval changes must apply without a restart
                activity_interval = self.activity_interval_hours * 3600
                wellness_interval = self.wellness_interval_hours * 3600

                # Collapse pending waits that now exceed the (shortened) interval
                if next_activity_check > now + activity_interval:
                    next_activity_check = now + activity_interval
                if next_wellness_check > now + wellness_interval:
                    next_wellness_check = now + wellness_interval

                # ── Activity check ──
                if now >= next_activity_check:
                    if not self._is_background_work_running():
                        self._sync_activities()
                    next_activity_check = now + activity_interval

                # ── Wellness check ──
                if now >= next_wellness_check:
                    if not self._is_background_work_running():
                        self._sync_wellness()
                    next_wellness_check = now + wellness_interval

                # Sleep briefly and check stop event
                self._stop_event.wait(timeout=30)
        finally:
            # Self-clear so a later start() isn't blocked by a dead thread ref
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _sync_activities(self) -> None:
        """Poll for new activities."""
        try:
            from src.ingestion.garmin_personal import sync_activities

            logger.info("Auto-sync: checking for new activities")
            result = sync_activities(days=1)
            processed = result.get("activities_processed", 0)

            with self._lock:
                self._last_activity_sync = time.time()
                self._stats["total_activity_syncs"] += 1
                self._stats["last_activity_sync_time"] = datetime.now().isoformat()

            if processed > 0:
                logger.info(f"Auto-sync: {processed} new activities synced")
            else:
                logger.debug("Auto-sync: no new activities")

        except Exception as exc:
            logger.error(f"Auto-sync activities failed: {exc}", exc_info=True)
            with self._lock:
                self._stats["total_errors"] += 1
                self._stats["last_error"] = str(exc)
                self._stats["last_error_time"] = datetime.now().isoformat()

    def _sync_wellness(self) -> None:
        """Poll for new wellness data."""
        try:
            from src.ingestion.garmin_personal import sync_garmin

            logger.info("Auto-sync: checking for new wellness data")
            result = sync_garmin(days=1)
            records = result.get("wellness_records", 0)

            with self._lock:
                self._last_wellness_sync = time.time()
                self._stats["total_wellness_syncs"] += 1
                self._stats["last_wellness_sync_time"] = datetime.now().isoformat()

            if records > 0:
                logger.info(f"Auto-sync: {records} wellness records synced")
            else:
                logger.debug("Auto-sync: no new wellness data")

        except Exception as exc:
            logger.error(f"Auto-sync wellness failed: {exc}", exc_info=True)
            with self._lock:
                self._stats["total_errors"] += 1
                self._stats["last_error"] = str(exc)
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the scheduler, persisting to config.env."""
        os.environ["AUTO_SYNC_ENABLED"] = str(enabled).lower()
        _write_env("AUTO_SYNC_ENABLED", str(enabled).lower())

        if enabled:
            # start() is a no-op when already running, so repeated Save clicks
            # don't re-trigger syncs. A freshly started loop's first check
            # fires immediately, which serves as the initial sync.
            self.start()
        else:
            self.stop()

    def set_intervals(self, activity_hours: int, wellness_hours: int) -> None:
        """Update sync intervals and persist to config.env."""
        os.environ["AUTO_SYNC_ACTIVITY_HOURS"] = str(activity_hours)
        os.environ["AUTO_SYNC_WELLNESS_HOURS"] = str(wellness_hours)
        _write_env("AUTO_SYNC_ACTIVITY_HOURS", str(activity_hours))
        _write_env("AUTO_SYNC_WELLNESS_HOURS", str(wellness_hours))
        logger.info(
            f"Sync intervals updated: activities={activity_hours}h, "
            f"wellness={wellness_hours}h"
        )


# ── Singleton ─────────────────────────────────────────────────────────────

_scheduler: SyncScheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> SyncScheduler:
    """Get or create the global sync scheduler instance."""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = SyncScheduler()
    return _scheduler