"""
Regression tests for the sync scheduler (src/tasks/scheduler.py).

Covers review finding A5:
- interval changes apply without an app restart (loop re-reads each cycle)
- enabling the scheduler runs exactly one initial sync (no double-sync when
  the Settings Save button calls set_enabled on every click)
- stop() cannot orphan a mid-sync loop into a second concurrent loop
- auto-sync skips firing while a reparse BackgroundTask is in flight
"""

import threading
import time

import pytest

import src.tasks.scheduler as scheduler_module
from src import config
from src.tasks.scheduler import SyncScheduler


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll predicate until true or timeout; returns the final result."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class SyncRecorder:
    """Stands in for the real Garmin syncs; counts calls and concurrency."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.activity_calls = 0
        self.wellness_calls = 0
        self._concurrent = 0
        self.max_concurrent = 0
        self.activity_entered = threading.Event()
        self.activity_blocker: threading.Event | None = None

    def attach(self, sched: SyncScheduler) -> None:
        """Route the scheduler's sync calls to this recorder."""
        sched._sync_activities = self._activity
        sched._sync_wellness = self._wellness

    def _activity(self) -> None:
        with self.lock:
            self.activity_calls += 1
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            if self.activity_blocker is not None:
                self.activity_entered.set()
                self.activity_blocker.wait(timeout=5.0)
        finally:
            with self.lock:
                self._concurrent -= 1

    def _wellness(self) -> None:
        with self.lock:
            self.wellness_calls += 1


@pytest.fixture
def sched(env_vars, monkeypatch):
    """Fresh (non-singleton) scheduler with a fast loop tick.

    env_vars points the vault at a tmp dir, so set_enabled/set_intervals
    persist to an isolated config.env. monkeypatch restores any AUTO_SYNC_*
    keys the tests overwrite directly.
    """
    monkeypatch.setenv("AUTO_SYNC_ENABLED", "true")
    monkeypatch.setenv("AUTO_SYNC_ACTIVITY_HOURS", "1")
    monkeypatch.setenv("AUTO_SYNC_WELLNESS_HOURS", "1")
    s = SyncScheduler()
    # Shrink the 30s loop tick to 20ms so tests observe multiple cycles fast.
    monkeypatch.setattr(
        s._stop_event, "wait", lambda timeout=None: time.sleep(0.02)
    )
    return s


def test_shortened_interval_applies_without_restart(sched, monkeypatch):
    """Changing the interval in Settings must take effect on the running loop."""
    rec = SyncRecorder()
    rec.attach(sched)
    monkeypatch.setenv("AUTO_SYNC_ACTIVITY_HOURS", "48")
    monkeypatch.setenv("AUTO_SYNC_WELLNESS_HOURS", "48")

    sched.start()
    assert _wait_for(lambda: rec.activity_calls >= 1), "initial activity sync never fired"
    assert _wait_for(lambda: rec.wellness_calls >= 1), "initial wellness sync never fired"

    # User shortens the activity interval (0h = fire every tick)
    monkeypatch.setenv("AUTO_SYNC_ACTIVITY_HOURS", "0")

    assert _wait_for(lambda: rec.activity_calls >= 2, timeout=5.0), (
        "interval change never applied — loop still using the interval "
        "captured at loop start"
    )
    sched.stop()


def test_set_intervals_persists_to_config_env(sched):
    """set_intervals updates the live properties and config.env."""
    sched.set_intervals(3, 7)

    assert sched.activity_interval_hours == 3
    assert sched.wellness_interval_hours == 7
    text = config.config_env_path().read_text()
    assert 'AUTO_SYNC_ACTIVITY_HOURS="3"' in text
    assert 'AUTO_SYNC_WELLNESS_HOURS="7"' in text


def test_enable_runs_single_initial_sync(sched):
    """Enabling runs one activity + one wellness sync, not two of each."""
    rec = SyncRecorder()
    rec.attach(sched)

    sched.set_enabled(True)
    assert _wait_for(lambda: rec.activity_calls >= 1 and rec.wellness_calls >= 1)
    time.sleep(0.3)  # settle — a duplicate initial sync would land here

    assert rec.activity_calls == 1
    assert rec.wellness_calls == 1
    assert sched.is_running
    sched.stop()


def test_repeated_save_click_does_not_resync(sched):
    """The Save button calls set_enabled unconditionally; that must be a no-op
    while the scheduler is already running."""
    rec = SyncRecorder()
    rec.attach(sched)

    sched.set_enabled(True)
    assert _wait_for(lambda: rec.activity_calls >= 1)

    sched.set_enabled(True)
    sched.set_enabled(True)
    time.sleep(0.3)

    assert rec.activity_calls == 1
    assert rec.wellness_calls == 1
    sched.stop()


def test_stop_does_not_spawn_second_loop_mid_sync(sched, monkeypatch):
    """stop() during an in-flight sync must not let start() spawn a second
    concurrent loop."""
    monkeypatch.setattr(scheduler_module, "STOP_JOIN_TIMEOUT_S", 0.2)
    rec = SyncRecorder()
    rec.attach(sched)
    blocker = threading.Event()
    rec.activity_blocker = blocker

    sched.start()
    assert rec.activity_entered.wait(timeout=2.0), "activity sync never started"

    # stop() returns while the sync is still in flight
    sched.stop()
    assert sched.is_running, "stop() orphaned the running loop"

    # Must be a no-op: the loop thread is still finishing its sync
    sched.start()

    blocker.set()
    # The loop exits on its own and clears its own thread reference
    assert _wait_for(lambda: not sched.is_running, timeout=5.0)

    # A fresh start() then works and fires again
    sched.start()
    assert _wait_for(lambda: rec.activity_calls >= 2, timeout=5.0)
    time.sleep(0.2)

    assert rec.max_concurrent == 1, "two scheduler loops ran concurrent syncs"
    sched.stop()


def test_stop_without_start_is_safe(sched):
    sched.stop()
    assert not sched.is_running


def test_start_respects_disabled_flag(sched, monkeypatch):
    monkeypatch.setenv("AUTO_SYNC_ENABLED", "false")
    rec = SyncRecorder()
    rec.attach(sched)

    sched.start()
    time.sleep(0.1)

    assert not sched.is_running
    assert rec.activity_calls == 0


def test_auto_sync_skips_while_reparse_task_running(sched, monkeypatch):
    """A BackgroundTask (Reparse FIT) in flight must block auto-sync fires —
    both write the same database and FIT store. Once the task finishes,
    syncs fire again."""
    rec = SyncRecorder()
    rec.attach(sched)

    task_running = True

    class FakeTask:
        @property
        def is_running(self) -> bool:
            return task_running

    monkeypatch.setattr(scheduler_module, "get_default_task", lambda: FakeTask())
    # 0h = fire on every 20ms tick, so the post-reparse resume is observable
    monkeypatch.setenv("AUTO_SYNC_ACTIVITY_HOURS", "0")
    monkeypatch.setenv("AUTO_SYNC_WELLNESS_HOURS", "0")

    sched.start()
    time.sleep(0.3)  # ~15 loop ticks — an unguarded fire would land here

    assert rec.activity_calls == 0, "auto-sync fired while a reparse task was running"
    assert rec.wellness_calls == 0, "auto-sync fired while a reparse task was running"

    task_running = False
    assert _wait_for(lambda: rec.activity_calls >= 1, timeout=5.0), (
        "auto-sync never resumed after the reparse task finished"
    )
    sched.stop()
