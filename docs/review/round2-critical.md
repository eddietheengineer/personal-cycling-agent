# Round 2 Review: Critical Issues

Second pass, focused on **critical** issues only — bugs that cause data loss, silent corruption, or broken behavior. Round 1 covered style, dead code, and architecture.

## CRITICAL: Stream keying fix creates a data-access gap

**Files:** `main.py:214-222`, `garmin_connect.py:638`, `garmin_export.py:313`

The round-1 fix changed `_parse_fit_file` and `sync_routes_from_fit` to use `garmin_{id}` keys. But `main.py`'s stream lookup **strips** the `garmin_` prefix:

```python
# main.py:219-222
sid = aid
if sid.startswith("garmin_"):
    sid = sid[len("garmin_"):]
stream_ids.append(sid)
```

**Impact:** After a reparse (`reparse_all_fit_files` → `_parse_fit_file` → `store_activity_streams(f"garmin_{id}", ...)`), streams are keyed `garmin_{id}`. But `main.py` queries with bare `{id}`. **All reparsed streams are invisible to the analytics pipeline.** NP, TSS, IF, decoupling, W′, durability — all computed from empty stream data.

The original code (bare `{id}` in `_parse_fit_file`) was actually *consistent* with `main.py`'s strip-prefix lookup. The "fix" broke it.

**Fix options:**
- (A) Revert `_parse_fit_file` to bare `{id}` (matches `main.py`'s lookup). Keep `garmin_{id}` in `_fetch_activity_streams` (original sync path). The two paths use different keys, but `reparse_all_fit_files` deletes all streams first, so there's no overlap.
- (B) Change `main.py` to query with `garmin_{id}` (no strip). This requires `_fetch_activity_streams` to also use `garmin_{id}` (it already does). But then the original sync path and reparse path both use `garmin_{id}`, and `main.py` must not strip.

**Option B is cleaner** — one key format everywhere. Requires changing `main.py:219-222` and `main.py:271-273` to NOT strip the prefix.

### Same issue for routes

`sync_routes_from_fit` now uses `garmin_{id}`. The Map page (`visualize.py:1556-1558`) queries `activity_routes` by `activity_id` — it uses whatever key is in the table, so it's consistent. But if any code queries routes with bare `{id}`, it won't find them. **Verify all route lookups use `garmin_{id}`.**

## CRITICAL: `store_activity_streams` has no dedup — reparse without delete duplicates

**File:** `store.py:1112-1133`

`store_activity_streams` does a plain `INSERT` — no `INSERT OR REPLACE`, no `DELETE` first. If called twice for the same `(activity_id, metric)`, rows are duplicated. `reparse_all_fit_files` does `DELETE FROM activity_streams` first (line 1782), so it's safe *today*. But any future code path that calls `store_activity_streams` without a prior delete will silently duplicate streams, doubling all computed metrics.

**Fix:** Add a `DELETE FROM activity_streams WHERE activity_id = ? AND metric = ?` at the start of `store_activity_streams`, or use `INSERT OR REPLACE` with a unique constraint on `(activity_id, metric, elapsed)`.

## CRITICAL: `store_routes` skips if any routes exist — partial GPS data is permanent

**File:** `store.py:1383-1391`

```python
existing = self.conn.execute(
    "SELECT COUNT(*) FROM activity_routes WHERE activity_id = ?",
    (activity_id,),
).fetchone()[0]
if existing > 0:
    return 0
```

If a FIT file has GPS for the first 10 minutes but loses signal for the rest, the first `sync_routes_from_fit` call stores 10 minutes of points. A later re-sync (after a firmware update or re-download) can never update the route — it sees `existing > 0` and skips. **Partial GPS data is permanent.**

**Fix:** Delete existing routes before inserting new ones (same pattern as the stream fix), or add a `force` parameter.

## HIGH: `run_analyze` returns `{}` on no wellness data — downstream code crashes

**File:** `main.py:144-146`

```python
if not wellness_records:
    logger.warning("No wellness data in DB; run --ingest first")
    return {}
```

Returns an empty dict. `run_prescribe` then does `analysis.get("readiness")` → `None`, `analysis.get("training_load")` → `None`. The prompt builder handles `None` readiness, but `run_prescribe` also does `analysis["ml_prediction"] = ml_prediction` (line 715) — writing to an empty dict works, but the prescription is generated with zero context. The user gets a generic LLM response with no data.

**Fix:** Return a dict with explicit `None` values for all expected keys, or raise a clear error.

## HIGH: `sync_garmin` early-return path doesn't reset rate limiter

**File:** `garmin_connect.py:1270-1273`

```python
if start > today:
    # Already up to date
    db.close()
    return {"wellness_records": 0, "with_hrv": 0}
```

This returns before `reset_rate_limiter()` (line 1282). The rate limiter retains its backoff state from the previous sync. If the previous sync hit a 429 and backed off to 300s, the next "already up to date" sync returns immediately (good), but the *following* sync (after new data arrives) starts with a 300s backoff instead of 1s. **Minor in practice** (the backoff decays), but inconsistent.

**Fix:** Call `reset_rate_limiter()` before the early return, or move it before the date logic.

## HIGH: `_deduplicate_samples` numpy version changes behavior for unsorted input

**File:** `main.py:112-115`

```python
elapsed = np.array([float(r["elapsed"]) for r in rows])
values = np.array([float(r["value"]) for r in rows])
_, unique_idx = np.unique(elapsed, return_index=True)
return values[np.sort(unique_idx)].tolist()
```

`np.unique` returns indices of the **first occurrence** in the *sorted* order, not the original order. If the input rows are not sorted by elapsed time (which they should be from the SQL `ORDER BY elapsed`, but the contract isn't enforced), the deduped output will be in sorted-elapsed order, not original order. The original Python version preserved original order.

In practice, the SQL query always orders by `elapsed`, so this is safe. But the function's contract ("keep only the first sample per second") is subtly different: the numpy version keeps the first sample in *sorted* order, the Python version kept the first in *input* order. If input is sorted, they're identical.

**Fix:** Add a comment noting the input must be sorted by elapsed, or use `pandas.drop_duplicates` which preserves order.

## MEDIUM: `visualize.py` `_load_analysis` cache is never invalidated on sync completion

**File:** `visualize.py:622-643`

The mtime-based cache works for file changes, but `run_analyze` (called from `background_sync` with `run_analyze_after=True`) writes `latest_analysis.json` in a background thread. The mtime check happens on the next render. If the user is on the Dashboard when the sync completes, the readiness card shows stale data until the next rerun (which happens on the next button click or navigation). **Not a bug** — Streamlit reruns on every interaction — but the "Sync complete" banner appears before the readiness card updates.

**Fix:** Invalidate the cache (`st.session_state.pop("_analysis_cache", None)`) in `_clear_sync_flags()`.

## MEDIUM: `garmin_connect.py` `_fetch_activity_streams` catches all exceptions silently

**File:** `garmin_connect.py:647-649`

```python
except Exception as e:
    logger.debug(f"Failed to fetch streams for activity {activity_id}: {type(e).__name__}: {e}")
    return 0
```

`logger.debug` — invisible at the default log level (INFO). If FIT download fails for every activity (e.g. network issue, expired tokens), the sync reports success with 0 streams stored, and the user has no idea. **Fix:** use `logger.warning`.

## MEDIUM: `main.py` `run_analyze` writes `latest_analysis.json` outside the DB context manager

**File:** `main.py:596-619`

The `with CyclingDB(DB_PATH) as db:` block ends at line ~590. The `result` dict is built and `latest_analysis.json` is written *after* the DB is closed. If the write fails (disk full), the DB is already closed and the function returns a result that was never persisted. The next `run_prescribe` call (which reads the file) gets stale data. **Low risk** (disk full is rare), but the ordering is fragile.

## Summary

| Severity | Issue | File | Fix |
|----------|-------|------|-----|
| **CRITICAL** | Stream keying mismatch after reparse | `main.py`, `garmin_connect.py` | Use `garmin_{id}` everywhere; stop stripping prefix in `main.py` |
| **CRITICAL** | `store_activity_streams` no dedup | `store.py` | Add DELETE-before-INSERT or unique constraint |
| **CRITICAL** | `store_routes` skips partial updates | `store.py` | Delete-before-insert or force parameter |
| HIGH | `run_analyze` returns `{}` on no data | `main.py` | Return explicit None keys or raise |
| HIGH | Rate limiter not reset on early return | `garmin_connect.py` | Move `reset_rate_limiter()` earlier |
| HIGH | `_deduplicate_samples` order contract | `main.py` | Document sorted-input requirement |
| MEDIUM | Analysis cache not invalidated on sync | `visualize.py` | Pop cache in `_clear_sync_flags` |
| MEDIUM | Silent stream fetch failures | `garmin_connect.py` | `logger.debug` → `logger.warning` |
| MEDIUM | Analysis JSON write after DB close | `main.py` | Write inside the `with` block |