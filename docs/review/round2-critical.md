# Round 2 Review: Critical Issues

Second pass, focused on **critical** issues only — bugs that cause data loss, silent corruption, or broken behavior. Round 1 covered style, dead code, and architecture.

## CRITICAL: Stream keying mismatch after reparse — FIXED

**Files:** `main.py:214-222`, `garmin_connect.py:638`, `garmin_export.py:313`, `ui_helpers.py:89-93`

The round-1 fix changed `_parse_fit_file` and `sync_routes_from_fit` to use `garmin_{id}` keys. But `main.py`'s stream lookup **stripped** the `garmin_` prefix, and `ui_helpers.py:_stream_id()` also stripped it. After a reparse, all streams were invisible to the analytics pipeline and the Activities page stream charts.

**Fix applied (commit 59f0496):**
- `main.py`: removed prefix stripping in both the batch-fetch (line 214-220) and per-activity lookup (line 269).
- `ui_helpers.py`: `_stream_id()` now returns the activity id unchanged.
- All stream and route keys are now consistently `garmin_{id}`.

## CRITICAL: `store_activity_streams` has no dedup — FIXED

**File:** `store.py:1112-1139`

`store_activity_streams` did a plain `INSERT` — no `DELETE` first. If called twice for the same `(activity_id, metric)`, rows were duplicated.

**Fix applied (commit 59f0496):** Added `DELETE FROM activity_streams WHERE activity_id = ? AND metric = ?` before the `INSERT`.

## CRITICAL: `store_routes` skips if any routes exist — FIXED

**File:** `store.py:1380-1401`, `garmin_export.py:315-318`

`store_routes` checked `existing > 0` and skipped. Partial GPS data from a previous sync was permanent. `sync_routes_from_fit` also had a skip-if-routes-exist check.

**Fix applied (commit 59f0496):**
- `store_routes`: removed the skip check, added `DELETE FROM activity_routes WHERE activity_id = ?` before insert.
- `garmin_export.py`: removed the skip-if-routes-exist logic in `sync_routes_from_fit`.

## CRITICAL: `_stream_id()` in ui_helpers.py strips prefix — FIXED

**File:** `ui_helpers.py:89-93`

Found during re-review after the main.py fix. The Activities page (`visualize.py:1159`) uses `_stream_id(selected_id)` to look up streams. After the keying fix, streams are keyed `garmin_{id}`, but `_stream_id` returned bare `{id}`. **Stream charts on the Activities page would show no data.**

**Fix applied (commit 59f0496):** `_stream_id()` now returns the activity id unchanged.

## CRITICAL: `db_query.py` missing `import re` — FIXED

**File:** `agent/db_query.py:83`

`query_db()` calls `re.search()` at line 83 but `re` was never imported. **Every coach AI database query would crash with `NameError: name 're' is not defined`.**

**Fix applied (commit 59f0496):** Added `import re`.

## HIGH: `run_analyze` returns `{}` on no wellness data — FIXED

**File:** `main.py:144-163`

Returned an empty dict. `run_prescribe` then generated a prescription with zero context.

**Fix applied (commit b44411c):** Returns a dict with explicit `None`/empty values for all expected keys.

## HIGH: `_deduplicate_samples` order contract undocumented — FIXED

**File:** `main.py:101-119`

`np.unique` returns indices of the first occurrence in *sorted* order. The input must be sorted by elapsed time (guaranteed by SQL `ORDER BY elapsed`), but the contract was undocumented.

**Fix applied (commit b44411c):** Added docstring noting the sorted-input requirement.

## HIGH: `sync_garmin` early-return path doesn't reset rate limiter — NOT FIXED (low risk)

**File:** `garmin_connect.py:1152-1155`

The early return (already up to date) skips `reset_rate_limiter()`. The rate limiter retains backoff state from the previous sync. **Low risk** — the backoff decays, and the early return means no API calls are made. Left as-is.

## MEDIUM: `visualize.py` `_load_analysis` cache not invalidated on sync — FIXED

**File:** `visualize.py:204-211`

**Fix applied (commit 59f0496):** `_clear_sync_flags()` now pops `_analysis_cache` from session state.

## MEDIUM: Silent stream fetch failures — FIXED

**File:** `garmin_connect.py:648`

`_fetch_activity_streams` caught all exceptions at `logger.debug` — invisible at default log level.

**Fix applied (commit 59f0496):** Changed to `logger.warning`.

## MEDIUM: `extract_power_meters` silent failures — FIXED

**File:** `garmin_connect.py:1069`

FIT parse failures in `extract_power_meters` logged at `logger.debug`.

**Fix applied (commit b44411c):** Changed to `logger.warning`.

## MEDIUM: `last_synced` update failure silent — FIXED

**File:** `garmin_connect.py:991`

If `set_last_synced` fails, the failure was logged at `logger.debug`. The next sync would re-download all activities (duplicate work, rate limit risk).

**Fix applied (commit b44411c):** Changed to `logger.warning`.

## MEDIUM: `run_analyze` writes `latest_analysis.json` outside DB context — NOT FIXED (low risk)

**File:** `main.py:592-615`

The `result` dict is built and `latest_analysis.json` is written after the `with CyclingDB(...)` block closes. If the write fails (disk full), the DB is already closed. **Low risk** — disk full is rare, and the function still returns the result dict to the caller.

## Summary

| Severity | Issue | File | Status |
|----------|-------|------|--------|
| **CRITICAL** | Stream keying mismatch after reparse | `main.py`, `ui_helpers.py` | ✅ Fixed (59f0496) |
| **CRITICAL** | `store_activity_streams` no dedup | `store.py` | ✅ Fixed (59f0496) |
| **CRITICAL** | `store_routes` skips partial updates | `store.py`, `garmin_export.py` | ✅ Fixed (59f0496) |
| **CRITICAL** | `_stream_id()` strips prefix | `ui_helpers.py` | ✅ Fixed (59f0496) |
| **CRITICAL** | `db_query.py` missing `import re` | `agent/db_query.py` | ✅ Fixed (59f0496) |
| HIGH | `run_analyze` returns `{}` on no data | `main.py` | ✅ Fixed (b44411c) |
| HIGH | `_deduplicate_samples` order contract | `main.py` | ✅ Fixed (b44411c) |
| HIGH | Rate limiter not reset on early return | `garmin_connect.py` | ⏭️ Skipped (low risk) |
| MEDIUM | Analysis cache not invalidated on sync | `visualize.py` | ✅ Fixed (59f0496) |
| MEDIUM | Silent stream fetch failures | `garmin_connect.py` | ✅ Fixed (59f0496) |
| MEDIUM | `extract_power_meters` silent failures | `garmin_connect.py` | ✅ Fixed (b44411c) |
| MEDIUM | `last_synced` update failure silent | `garmin_connect.py` | ✅ Fixed (b44411c) |
| MEDIUM | Analysis JSON write after DB close | `main.py` | ⏭️ Skipped (low risk) |