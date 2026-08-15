# Review: `src/db/store.py` (CyclingDB)

1846 lines, single class `CyclingDB` — the entire SQLite persistence layer.

## What it does

- 15 tables: `wellness`, `activities`, `activity_streams`, `sync_state`, `activity_metrics`, `activity_routes`, `hr_calibration`, `raw_activities`, `raw_fit_sessions`, `raw_wellness`, `morning_checkin`, `daily_readiness`, `training_log`, `edge_cases`, `validation_log`, `post_ride_checkin`.
- Raw/derived split: `raw_*` tables are immutable Garmin API/FIT payloads; `activities` is rebuilt from raw by `refresh_activities()`; `activity_metrics` holds computed values (NP/IF/TSS/W′/decoupling/HR-TSS).
- Thread safety via a single `RLock` around `execute`/`commit`; WAL mode.

## Findings

### 1. Large block of dead code: ML/prescription tables have no callers

The tables `daily_readiness`, `training_log`, `edge_cases`, `validation_log` (schema at lines 438-552, "New tables for ML model and prescription engine") and their ~15 accessor methods (lines 1433-1749: `insert_daily_readiness`, `get_daily_readiness*`, `insert_training_log`, `get_training_log`, `get_planned_workouts`, `get_completed_training`, `insert_edge_case`, `get_edge_cases`, `get_active_edge_cases`, `insert_validation_log`, `get_validation_logs`, `get_validation_errors`) have **zero callers in `src/` and zero in `tests/`** (verified by grep).

This is ~320 lines of schema + accessors for a feature that was never wired up (see `docs/ML_IMPLEMENTATION.md` — likely the plan that produced it).

**Change:** either delete the tables + methods, or if the ML/prescription work is still planned, move the schema to a clearly-marked `store_ml.py` so the core store isn't carrying dead weight. Deleting is the boring option; the tables are empty in any fresh vault.

### 2. Misleading column names: `duration_ms` is seconds, `distance_cm` is meters

`raw_activities.duration_ms` actually stores **seconds** (Garmin API `duration`), and `distance_cm` stores **meters** (Garmin API `distance`). The code even acknowledges this at line 907-908 ("Column names are misleading — historical naming from Garmin API conversion"). Similarly `raw_fit_sessions.total_elapsed_time_ms` stores seconds (line 936: "Column name says ms but fitdecode returns seconds").

The backfill in `_migrate_raw_tables` (lines 250-251) makes it worse: it converts `activities.duration` (seconds) × 1000 into `duration_ms` and `distance` (km) × 100 into `distance_cm` — so the backfilled rows are in the *same* wrong units as the API rows, by accident.

**Change:** rename columns to `duration_s`, `distance_m`, `total_elapsed_time_s` via a migration (`ALTER TABLE ... RENAME COLUMN` is supported since SQLite 3.25). This is the single most confusing part of the file; every reader of `refresh_activities` has to trust a comment.

### 3. `store_activity_metrics` merge logic is fragile (lines 1177-1233)

The "merge with existing row" logic hand-maps 12 columns by **positional index** from the SELECT (lines 1193-1206: `existing[1]` = `cp_used`, etc.). If the SELECT column order ever changes, values silently land in the wrong columns. It also hardcodes the column list in three places (SELECT, dict, INSERT) that must stay in sync.

**Change:** use `dict(existing)` (row_factory is `sqlite3.Row`) and build the INSERT from the dict keys. One source of truth, no positional indexing.

### 4. `store_morning_checkin` uses `INSERT OR REPLACE` on a table with `UNIQUE(athlete_id, date)` (line 1793)

`INSERT OR REPLACE` deletes the conflicting row and inserts a new one — this **changes the rowid** and bypasses any `ON DELETE` behavior. The table also has `perceived_readiness`, `pain_score`, `pain_location` columns that the method never writes (they're not in the `values` dict at lines 1767-1780), so a re-check-in after a schema change would lose those fields. The method also does a `PRAGMA table_info` introspection on every call (line 1757) to build the column list — defensive code for a schema this module itself creates.

**Change:** use `INSERT ... ON CONFLICT(athlete_id, date) DO UPDATE SET ...` like every other upsert in the file. Drop the PRAGMA introspection; the schema is owned by `_create_tables`.

### 5. `store_routes` has a redundant commit and a race (lines 1374-1399)

Inside the lock it checks `COUNT(*)`, and if routes exist it calls `self.conn.commit()` (line 1389) — committing a no-op just to be safe. The check-then-insert is fine under the RLock, but the `commit()` on the skip path is noise. Also `garmin_export.py:316` does the same `get_route_count_for_activity` check *before* calling `store_routes`, so the check happens twice.

**Change:** drop the no-op commit; keep one check (either in the store or the caller, not both).

### 6. `get_trend_data` hardcodes `date` as the filter column (lines 1323-1332)

The method validates table/column names against allowlists (good — it's used with user-influenced input from the AI coach's `db_query` tool), but the WHERE clause always filters on `date`. `activity_metrics` has no `date` column (it has `computed_at`), so `get_trend_data("activity_metrics", [...], oldest=...)` would raise `sqlite3.OperationalError: no such column: date`. Callers currently avoid this by not passing dates for that table (visualize.py:1309, 1322 call it with no dates), but the API invites the bug.

**Change:** take a `date_column` parameter (default `"date"`), or split into per-table methods.

### 7. `refresh_activities` is a 240-line method (lines 869-1108)

It does: fetch all raw rows, fetch all FIT rows, fetch all metrics, then per-row: merge API/FIT/metrics values, parse `raw_json` for ~40 API-only fields, compute source indicators, and run a 50-column `INSERT OR REPLACE`. The `raw_json` field extraction (lines 955-1017) is a flat list of ~40 `api_data.get(...)` calls that map 1:1 to INSERT columns.

**Change:** extract the `raw_json` → column mapping into a dict (`{"activity_name": "activityName", "aerobic_te": "aerobicTrainingEffect", ...}`) and loop over it. Cuts ~60 lines and makes the mapping auditable. The FIT-override logic (lines 921-947) is also a good extraction candidate.

### 8. `_migrate_raw_tables` backfill is one-shot and unit-fragile (lines 201-266)

It backfills `raw_activities` from `activities` once (skips if any rows exist). It converts `duration` × 1000 and `distance` × 100 — but only for rows whose id starts with `garmin_`. If a user's DB has non-Garmin activities (Intervals.icu import via `garmin_export.py`), those are silently skipped, and the backfill is marked "done" anyway because the Garmin rows made the count > 0.

**Change:** if the raw-tables migration is already complete for all deployed vaults (likely, given the 3-week age), delete it. Otherwise, gate on a `schema_version` table rather than row count.

### 9. Minor

- **Line 52:** missing blank line between `_commit` and `_apply_pragmas`. Same at 1243-1244, 1421-1422, 1810-1811, 1836-1837.
- **Line 811:** `import json` inside `store_raw_activity` — `json` is already imported at module level (line 11).
- **Lines 26-29:** default `db_path="data/cycling_agent.sqlite"` is relative to CWD. Every real caller passes an explicit path (via `config.db_path()`), but the default is a footgun — a test or script that constructs `CyclingDB()` bare gets a DB in whatever directory it runs from. Consider making `db_path` required.
- **`get_activity_with_metrics` (1359-1370):** does two separate queries (activity, then metrics) instead of one JOIN. Fine at this scale, but it's the only "join" in the file that isn't a SQL join.
- **`ALLOWED_TABLES`/`ALLOWED_COLUMNS` (1266-1288):** good allowlist design for the AI coach's query tool. Note `activity_metrics` allowlist is missing `cp_used`, `ride_cp`, `hr_tss`, `hr_trimp` (added by `_migrate_activity_metrics`) — the coach can't query the newest columns.

## Follow-ups for later reviews

- [ ] `main.py`: verify the `refresh_activities` call at line 142 and the metrics pipeline (lines 312-490) — the merge semantics of `store_activity_metrics` matter there.
- [ ] `agent/db_query.py`: confirm it only uses `get_trend_data` with allowlisted tables (the `date`-column issue in finding 6).
- [ ] `ingestion/garmin_connect.py`: the two `store_activity_streams` call sites (756, 1897) — check for duplicate stream rows on re-sync (no dedup in `store_activity_streams`; `INSERT` without conflict handling).