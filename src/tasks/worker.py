"""Background sync worker using threading for non-blocking operations.

Provides a simple threading-based background task system that works
within Streamlit's single-process model. No external dependencies (Redis, Celery).
"""

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskResult:
    """Result of a background task."""
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0  # 0-100
    stage: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, progress: int = -1, stage: str = "", result: dict | None = None, error: str | None = None):
        with self._lock:
            if progress >= 0:
                self.progress = min(progress, 100)
            if stage:
                self.stage = stage
            if result is not None:
                self.result = result
            if error is not None:
                self.error = error

    def mark_running(self, stage: str = "Starting..."):
        with self._lock:
            self.status = TaskStatus.RUNNING
            self.progress = 0
            self.stage = stage

    def mark_completed(self, result: dict, stage: str = "Complete"):
        with self._lock:
            self.status = TaskStatus.COMPLETED
            self.progress = 100
            self.stage = stage
            self.result = result

    def mark_failed(self, error: str, stage: str = "Failed"):
        with self._lock:
            self.status = TaskStatus.FAILED
            self.stage = stage
            self.error = str(error)

    def snapshot(self) -> dict[str, Any]:
        """Thread-safe snapshot of current state."""
        with self._lock:
            return {
                "status": self.status.value,
                "progress": self.progress,
                "stage": self.stage,
                "result": self.result,
                "error": self.error,
            }


class BackgroundSync:
    """Manages background sync tasks with progress tracking.

    Usage:
        sync = BackgroundSync()
        sync.start(days=7, progress_callback=on_progress)
        # Later, check status:
        status = sync.snapshot()
    """

    def __init__(self):
        self._result: TaskResult = TaskResult()
        self._thread: threading.Thread | None = None
        self._progress_callback: Callable[[int, str], None] | None = None
        self._lock: threading.Lock = threading.Lock()
        self._cancelled: bool = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._result.status == TaskStatus.RUNNING

    def start(
        self,
        days: int = 1,
        db_path: str | None = None,
        tokenstore: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
        unbounded: bool = False,
        run_analyze_after: bool = False,
    ) -> None:
        """Start a background sync task.

        Args:
            days: Number of days of data to sync.
            db_path: Optional database path override.
            tokenstore: Optional token store path override.
            progress_callback: Optional callback(progress_percent, stage_text).
            unbounded: If True, sync all historical data until rate-limited.
            run_analyze_after: If True, run analytics after sync completes.
        """
        with self._lock:
            if self._result.status == TaskStatus.RUNNING:
                logger.warning("Sync already running, ignoring new request")
                return
            self._cancelled = False
            self._progress_callback = progress_callback
            self._result = TaskResult()

            self._thread = threading.Thread(
                target=self._run_sync,
                args=(days, db_path, tokenstore, unbounded, run_analyze_after),
                daemon=True,
                name="bg-sync",
            )
            self._thread.start()
    def _run_sync(self, days: int, db_path: str | None, tokenstore: str | None, unbounded: bool = False, run_analyze_after: bool = False) -> None:
        """Worker thread that runs the actual sync."""
        from src.ingestion.garmin_connect import sync_garmin, sync_activities

        self._result.mark_running("Starting sync...")
        self._notify(0, "Starting sync...")

        try:
            # Phase 1: Wellness data
            self._result.update(progress=10, stage="Fetching wellness data...")
            self._notify(10, "Fetching wellness data...")
            if self._cancelled:
                return
            wellness_counts = sync_garmin(
                days=1,
                db_path=db_path,
                tokenstore=tokenstore,
                unbounded=unbounded,
                progress_callback=lambda p, s: self._result.update(progress=p, stage=s),
            )
            self._result.update(progress=40, stage="Wellness sync complete")
            self._notify(40, "Wellness sync complete")
            if self._cancelled:
                return

            # Phase 2: Activity streams
            self._result.update(progress=50, stage="Fetching activity streams...")
            self._notify(50, "Fetching activity streams...")
            if self._cancelled:
                return
            activity_counts = sync_activities(
                days=days,
                db_path=db_path,
                tokenstore=tokenstore,
                unbounded=unbounded,
                progress_callback=lambda p, s: self._result.update(progress=p, stage=s),
            )
            self._result.update(progress=90, stage="Activity sync complete")
            self._notify(90, "Activity sync complete")

            result = {
                "wellness": wellness_counts,
                "activities": activity_counts,
            }

            # Phase 3: Run analytics if requested
            if run_analyze_after:
                self._result.update(progress=92, stage="Running analytics...")
                self._notify(92, "Running analytics...")
                try:
                    from src.main import run_analyze
                    analyze_result = run_analyze()
                    result["analysis"] = {
                        "ftp": analyze_result.get("ftp"),
                        "readiness": analyze_result.get("readiness"),
                        "training_load": analyze_result.get("training_load"),
                    }
                    logger.info("Analytics complete after sync")
                except Exception as e:
                    logger.warning(f"Analytics failed after sync: {e}")
                    result["analysis_error"] = str(e)

            self._result.mark_completed(result, "Sync and analytics complete!")
            self._notify(100, "Sync and analytics complete!")

        except Exception as exc:
            logger.error(f"Background sync failed: {exc}", exc_info=True)
            self._result.mark_failed(str(exc), f"Sync failed: {exc}")
            self._notify(100, f"Sync failed: {exc}")

    def _notify(self, progress: int, stage: str) -> None:
        if self._progress_callback is not None:
            try:
                self._progress_callback(progress, stage)
            except Exception as e:
                logger.warning(f"Progress callback error (non-fatal): {e}")

    def snapshot(self) -> dict[str, Any]:
        """Get a thread-safe snapshot of the current task state."""
        return self._result.snapshot()

    def cancel(self) -> bool:
        """Attempt to cancel a running sync. Returns True if it was running."""
        with self._lock:
            if self._result.status != TaskStatus.RUNNING:
                return False
            self._cancelled = True
            self._result.mark_failed("Cancelled by user", "Sync cancelled")
            return True


# Module-level singleton for use in Streamlit session state
_default_sync: BackgroundSync | None = None


def background_sync(days: int = 1, db_path: str | None = None, progress_callback: Callable[[int, str], None] | None = None) -> BackgroundSync:
    """Start or get the default background sync instance.

    Convenience function for use in Streamlit pages.
    """
    global _default_sync
    if _default_sync is None or not _default_sync.is_running:
        _default_sync = BackgroundSync()

    if not _default_sync.is_running:
        _default_sync.start(
            days=days,
            db_path=db_path,
            progress_callback=progress_callback,
        )

    return _default_sync