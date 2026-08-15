# Review: `src/tasks/` package

worker.py: 370 lines. scheduler.py: 287 lines. Background sync infrastructure for Streamlit.

## `worker.py`

### 1. `background_sync` singleton is never reset after completion (lines 351-370)

The logic: if `_default_sync` exists and is not running, replace it **only if** the snapshot status is not "completed"/"failed". But a completed sync *has* status "completed" — so the condition `snap["status"] not in ("completed", "failed")` is False, and the old instance is **kept**. Then `if not _default_sync.is_running: _default_sync.start(...)` starts a *new* sync on the *same* instance. `BackgroundSync.start` resets `self._result = TaskResult()` (line 125), so the old result is discarded. This works, but the "preserve completed result" comment (line 348-349) is misleading — the result is preserved only until the next `background_sync()` call, which immediately overwrites it. **Change:** simplify — always create a new `BackgroundSync` when the old one is not running; the "preserve" logic is dead weight.

### 2. `background_task` has a race condition (lines 314-321)

```python
if _default_task is None or not _default_task.is_running:
    _default_task = BackgroundTask()
if not _default_task.is_running:
    _default_task.start(target, result_key)
```
Two Streamlit reruns hitting this simultaneously: both see `_default_task.is_running == False`, both create a new `BackgroundTask`, both call `start`. The second `start` is ignored (line 263-265: "Task already running"), but the first task's `target` is lost. **Change:** guard with a lock, or use a single `BackgroundTask` instance for the lifetime of the process.

### 3. `BackgroundSync._notify` puts to a queue that nobody reads (lines 216-221)

`self._progress_queue.put((progress, stage))` — but `_progress_queue` is never consumed. The actual progress updates go through `self._result.update(progress, stage)` (called directly in `_run_sync`). The queue is dead code. **Change:** delete `_progress_queue` and `_notify`; call `self._result.update` directly.

### 4. `cancel()` marks the task as failed immediately (lines 227-234)

`self._result.mark_failed("Cancelled by user", ...)` — but the worker thread is still running. It will continue executing `sync_garmin`/`sync_activities` until it hits the next `if self._cancelled: return` check (lines 145, 156, 162). The UI shows "Failed: Cancelled by user" while the sync is still in progress. **Change:** add a `CANCELLED` status to `TaskStatus`, or set a flag and let the worker thread mark the final state.

### 5. `run_analyze_after` imports from `src.main` (line 184)

`from src.main import run_analyze` — the worker (a background thread) imports the Streamlit UI module. `src.main` imports `streamlit` at module level, so this pulls Streamlit into the worker thread's context. It works (Streamlit is already loaded), but it's a layering violation: the tasks package should not depend on the UI package. **Change:** move `run_analyze`/`run_prescribe` to a non-UI module (e.g. `src/services/analysis.py`) and import from there.

### 6. `TaskResult._lock` is a dataclass field with `default_factory` (line 32)

`_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)` — this works, but if `TaskResult` is ever copied (e.g. `dataclasses.replace`), the lock is shared between copies. Unlikely in practice, but a `__post_init__` would be safer.

## `scheduler.py`

### 7. `_write_env` rewrites the entire config.env file (lines 38-51)

Reads all lines, replaces the matching key, writes back. If two threads call `_write_env` simultaneously (e.g. `set_enabled` + `set_intervals` from the UI), they can interleave and lose writes. The `_env_lock` (line 35) exists but is **never acquired** in `_write_env`. **Change:** acquire `_env_lock` in `_write_env`.

### 8. `set_enabled` writes to `os.environ` and `config.env` separately (lines 245-246)

`os.environ["AUTO_SYNC_ENABLED"] = ...` then `_write_env(...)`. If the process restarts, `os.environ` is repopulated from `config.env` by `config.setup()`. But if `config.env` write fails (disk full, permissions), the in-memory env var is set but not persisted — the scheduler runs until restart, then stops. **Change:** write to file first, then update `os.environ` (or read back from file to confirm).

### 9. Scheduler loop checks both cycles on the same 30s tick (lines 175-191)

`self._stop_event.wait(timeout=30)` — the loop wakes every 30s and checks both `next_activity_check` and `next_wellness_check`. If the activity interval is 5 minutes and wellness is 6 hours, the wellness check is evaluated every 30s (and skipped 718 times out of 720). Trivial CPU waste, but the pattern is slightly odd. **Change:** compute `next_wake = min(next_activity_check, next_wellness_check)` and wait until then.

### 10. `_sync_activities` / `_sync_wellness` call `sync_activities(days=1)` / `sync_garmin(days=1)` (lines 199, 225)

These create a fresh `CyclingDB` connection, authenticate with Garmin (token cache), and run the full sync pipeline — every 5 minutes for activities. The auth step (`_create_client`) is the expensive part (token validation, potential MFA). **Change:** cache the Garmin client across scheduler ticks (the tokens are valid for hours), or increase the default activity interval.

### 11. `_initial_sync` runs activities then wellness sequentially in a new thread (lines 255-260)

If the user enables auto-sync from the UI, this thread starts immediately. It can collide with a manual sync that's in progress (the `_is_manual_sync_running` check is only in the main loop, not in `_initial_sync`). **Change:** add the same guard.

## Cross-cutting

- Both `BackgroundSync` and `BackgroundTask` are ~90% identical (thread management, progress tracking, cancel). **Change:** extract a base class `_BackgroundWorker` with the shared logic; `BackgroundSync` adds the Garmin-specific `_run_sync`.