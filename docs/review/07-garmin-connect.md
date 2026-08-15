# Review: `src/ingestion/garmin_connect.py`

1,986 lines — the largest module in the codebase. Garmin Connect API sync (wellness + activities), FIT file download/parsing, rate limiting, MFA auth.

## What it does

- `RateLimiter` + `_retry_on_rate_limit`: proactive 1s spacing + exponential backoff on 429.
- `_create_client` / `authenticate_garmin`: garmin-auth 0.3.0 login with cached tokens, MFA two-phase flow.
- `sync_garmin`: wellness sync — bulk fetch (weigh-ins, body comp, steps, endurance/hill scores) then per-day endpoints (HRV, sleep, stats, HR, respiration, SpO2, hydration, intensity minutes, body battery, floors, training readiness, user summary, lifestyle, morning readiness).
- `sync_activities` / `_sync_activities_batch`: activity discovery in batches of 100, raw storage, FIT download + parse, `refresh_activities()`.
- `reparse_all_fit_files`: delete all streams, re-parse local FIT files.
- `fetch_wellness_for_date`: single-day wellness (used by the `__main__` block).
- `extract_power_meters`: scan local FITs for power meter device info.

## Findings

### 1. `sync_garmin` makes ~15 API calls per day (lines 1437-1688)

Each day in `fetch_dates` hits: HRV, sleep, stats, heart_rates, respiration, SpO2, hydration, intensity_minutes, body_battery, floors, training_readiness, user_summary, lifestyle, morning_training_readiness — 14 endpoints, each with `rl.wait()` (1s) + a 0.5s `time.sleep` at the end. That's **~21 seconds per day**. A 10-year unbounded sync (3,650 days) would take ~21 hours and ~51,000 API calls. The bulk fetches (weigh-ins, body comp, steps, endurance, hill) help, but 9 of the 14 per-day endpoints have no bulk equivalent used here.

**Change:** (a) check which of these endpoints `garminconnect` supports as date-range calls and bulk them (e.g. `get_body_battery(start, end)` exists in the library); (b) make the per-day endpoint list configurable — a user who doesn't track SpO2/hydration/floors shouldn't pay 5 extra calls per day; (c) for the unbounded 10-year case, consider skipping per-day endpoints entirely and only fetching HRV+sleep+stats (the fields the readiness engine actually uses).

### 2. The `__main__` block (lines 1908-1986) is a third, divergent wellness sync

It does its own login (with interactive MFA prompt), its own date-range logic (going *backwards* from `last_date - 1`), and calls `fetch_wellness_for_date` (the 5-endpoint version) instead of `sync_garmin` (the 14-endpoint version). It also has a bug: `db.set_last_synced("garmin_wellness", target_str)` (line 1982) is called for *every* day including days with no data, and the dates go backwards, so `last_synced` ends up set to the *oldest* date — the next run will re-sync everything in between. **Change:** delete the `__main__` block (the CLI entry point is `main.py`) or make it a thin wrapper around `sync_garmin`.

### 3. `fetch_wellness_for_date` (lines 544-648) is only used by the `__main__` block

It fetches 5 endpoints (stats, HRV, heart_rates, body comp, weigh-ins, sleep) — a subset of `sync_garmin`'s 14. If finding 2 is fixed (delete `__main__`), this function is dead. **Change:** delete it.

### 4. Rate-limit handling closes the DB and returns mid-sync (lines 873-883, 983-992)

On a 429 during activity sync, the code saves `resume_offset`, calls `db.close()`, and returns. The caller (`sync_activities`, line 1100-1114) then tries `db._exec(...)` and `db.close()` again on the already-closed connection — guarded by try/except, but it's a fragile contract: `_sync_activities_batch` closing the DB is an undocumented side effect. **Change:** don't close the DB inside `_sync_activities_batch`; return a sentinel (e.g. `rate_limited=True` in the result) and let the caller close.

### 5. `db._exec` / `db._commit` are called directly from ingestion (lines 710-714, 1155-1183, 1238-1241, 1782-1783, 1105)

The ingestion module reaches into `CyclingDB`'s private methods for: power meter UPDATEs, `DELETE FROM wellness` (force resync), `DELETE FROM activity_streams` (reparse), `SELECT MAX(start_date)`. These bypass the store's API. **Change:** add proper store methods (`update_power_meter`, `clear_wellness`, `clear_activity_streams`, `get_max_activity_date`) and use them.

### 6. Stream dedup is O(n) with a set of floats (lines 748-755, 1890-1897)

`seen: set[float]` with `if t not in seen` — floating-point timestamps as set keys. FIT timestamps are millisecond-precision floats; two samples at the same ms are deduped, but float representation could in theory cause near-duplicates to slip through. In practice fine. The dedup logic is duplicated in `_fetch_activity_streams` and `_parse_fit_file` — **extract a `_deduplicate(values)` helper** (or do it in `store_activity_streams`).

### 7. `store_activity_streams` is called with inconsistent activity-id formats

- `_fetch_activity_streams` (line 756): `f"garmin_{activity_id}"`
- `_parse_fit_file` (line 1898): `str(activity_id)` — **no `garmin_` prefix**

`reparse_all_fit_files` calls `_parse_fit_file`, so after a reparse the streams are keyed by bare `activity_id` while the activities table uses `garmin_{id}`. Then `main.py:275-277` strips the `garmin_` prefix before looking up streams — so it finds the reparsed streams but *not* the originally-synced ones (which are keyed `garmin_{id}` in the streams table). **This is a real bug: after a reparse, the original stream rows are orphaned (wrong key) and the new rows use a different key.** Wait — `reparse_all_fit_files` does `DELETE FROM activity_streams` first (line 1782), so the old rows are gone. But the *key* changes from `garmin_{id}` to `{id}`, and `main.py` strips the prefix from the activity id before querying — so it queries `{id}` and finds the reparsed rows. OK, it works, but only because the delete happens first. If the delete ever fails or is skipped, you get duplicate streams under two keys. **Change:** use one key format everywhere (`garmin_{id}`) and add a note that streams must be deleted before re-keying.

### 8. `_garmin_activity_to_store_format` drops most API fields (lines 771-813)

It maps only 11 fields to the `store_activities` format. But `store_raw_activity` (called just before, line 946) stores the *entire* API dict as `raw_json`, and `refresh_activities` re-extracts ~40 fields from that JSON. So Phase 1.5's `store_activities` call is immediately overwritten by Phase 3's `refresh_activities`. The `store_activities` call at line 954 is **redundant** — it writes 11 columns that get replaced 200 lines later. **Change:** either skip Phase 1.5's `store_activities` (let `refresh_activities` do it) or make `refresh_activities` the only writer.

### 9. `sync_garmin`'s incremental date logic is confusing (lines 1257-1271)

```python
else:
    # Incremental: sync from day after last sync up to today
    ...
    gap = max(0, (today - last_date).days)
    days = min(days, gap)
    last_date = today
```
Then `sync_dates` is built *backwards from `last_date`* (now `today`) for `days` entries. So "incremental" actually means "the last N days ending today", not "days since last sync". If `last_synced` was 10 days ago and `days=1`, it syncs only today — the 9 days in between are never fetched. The `gap` computation is used to *cap* `days`, but the start point is always `today - days + 1`, not `last_synced + 1`. **Change:** start from `last_synced + 1` and go forward to `today`, capped by `days`.

### 10. `force_resync` deletes all wellness data (lines 1236-1241)

`DELETE FROM wellness` + `DELETE FROM raw_wellness` + clear sync state. This is a destructive operation triggered by a boolean flag — no confirmation, no log of how much data was deleted. **Change:** log the row count before deleting, and consider a `--force-resync-days N` variant.

### 11. Minor

- **Line 834:** `from datetime import date` inside `_sync_activities_batch` — `date` is already imported at module level (check top imports; the type hints use `"date | None"` as strings, suggesting it's *not* imported at top). Inconsistent.
- **Lines 683-684:** `import io` / `import zipfile` inside the function — move to module top.
- **Line 1133:** `import fitdecode` inside `extract_power_meters` — shadows the module-level `fitdecode = None` fallback. If the top-level import failed, this re-import will also fail and raise ImportError instead of the friendly message.
- **`_prompt_mfa_interactive` (lines 454-462) is dead code** — the MFA flow uses `authenticate_garmin` (UI) or raises in non-interactive contexts. Grep shows no callers.
- **`GarminAuthResult` (lines 465-471)** is a plain class with `__init__` — make it a dataclass for consistency with the rest of the codebase.

## Follow-ups for later reviews

- [ ] `garmin_export.py`: the Intervals.icu export path — does it have the same stream-keying issue (finding 7)?
- [ ] `main.py` / `visualize.py`: confirm which sync functions the UI calls (`sync_garmin` vs `sync_activities`) and whether `force_resync` is exposed.
- [ ] `tasks/worker.py`: the background sync wrapper — does it handle the mid-sync 429 return correctly (finding 4)?