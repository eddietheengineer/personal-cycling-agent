# Review: `src/ingestion/garmin_export.py`

392 lines. Imports a Garmin Connect "Download your data" ZIP (historical baseline) and extracts GPS routes from local FIT files.

## What it does

- `import_garmin_export`: parses `UDSFile_*.json` (wellness) and `*_summarizedActivities.json` (activities) from the export ZIP, stores via `db.store_wellness` / `db.store_activities`, archives the ZIP to the vault.
- `sync_routes_from_fit`: scans `raw_dir` for `.fit` files, extracts `position_lat`/`position_long` from record messages, stores via `db.store_routes`.
- `__main__`: CLI entry for the ZIP import.

## Findings

### 1. Module docstring is factually wrong (lines 8-10)

> "That data is only available via the Garmin Connect API (ingested through Intervals.icu)."

There is no Intervals.icu integration in this codebase. HRV/RMSSD is ingested directly via the Garmin Connect API in `garmin_connect.py` (`client.get_hrv_data`). **Change:** fix the docstring to reference `garmin_connect.py`.

### 2. `sync_routes_from_fit` uses the bare activity id, not `garmin_{id}` (line 313, 369)

`activity_id = fit_path.stem` — the bare numeric id. But the activities table uses `garmin_{id}` (set by `garmin_connect.py` and by `_extract_activity` line 201). `db.store_routes(activity_id, points)` and `db.get_route_count_for_activity(activity_id)` therefore key routes by the *bare* id while activities are keyed by `garmin_{id}`. The Map page (`main.py`) looks up routes by activity id — if it passes `garmin_{id}`, it finds no routes. **Change:** use `f"garmin_{fit_path.stem}"` consistently, or make `store_routes`/`get_route_count_for_activity` normalize the key. (Same keying inconsistency flagged in `07-garmin-connect.md` finding 7.)

### 3. `_get_sleep_hours` is a stub that always returns None (lines 121-125)

The comment says "We'd need the sleep-specific JSON files for this" — the Garmin export *does* include `DI-Connect-Sleep/` JSON files with actual sleep data. This is a missed data source: sleep hours (and sleep score) are available in the export but not extracted. **Change:** either parse the sleep JSON files or delete the stub and the `sleep_hours` key (it's always None).

### 4. `_extract_wellness` hardcodes `rmssd: None` and `sleep_score: None` (lines 93, 95)

Same issue — the export has sleep data; RMSSD genuinely isn't in the UDS files, but sleep score is in the sleep JSON. The hardcoded Nones are fine for RMSSD but wrong for sleep_score if the sleep files are parsed.

### 5. `import_garmin_export` doesn't deduplicate activities (line 266)

`_parse_uds_files` dedupes wellness by date (line 77-80), but `_parse_activity_files` returns all activities with no dedup. If the export ZIP contains overlapping date ranges (multiple `summarizedActivities` files), the same activity appears twice. `db.store_activities` uses `INSERT OR REPLACE` (per `02-db-store.md`), so duplicates are harmless but wasteful. **Change:** dedupe by `activityId` in `_parse_activity_files` for symmetry.

### 6. `import shutil` inside the function (line 259)

Move to module top.

### 7. `sync_routes_from_fit` reads the entire FIT file into a list (lines 321-325)

`records = [f for f in fit if ...]` materializes all record frames in memory. For a 3-hour ride at 1Hz that's ~10,800 frames — fine. But the GPS extraction loop (lines 350-362) then iterates again. A single pass would halve the work. Minor.

### 8. No rate limiting or progress callback in `sync_routes_from_fit`

It's a local file operation (no API), so no rate limiting needed. But for a vault with thousands of FIT files there's no progress reporting. The UI's "Reparse FIT" button calls `reparse_all_fit_files` (which has progress), but `sync_routes_from_fit` is called from `main.py`'s sync flow — check whether it's wrapped in a progress callback there. **Follow-up for main.py review.**

## Follow-ups for later reviews

- [ ] `main.py`: confirm the activity-id format passed to `get_route_count_for_activity` / route lookups (finding 2).
- [ ] `db/store.py`: `store_routes` and `get_route_count_for_activity` — do they normalize the activity id? (Already reviewed; they don't.)