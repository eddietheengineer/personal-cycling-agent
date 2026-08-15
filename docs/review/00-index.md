# Code Review Index

Review of `personal-cycling-agent` — 15 documents covering all source modules.

## Documents

| # | File | Module | Lines | Key Findings |
|---|------|--------|-------|-------------|
| 01 | [config](01-config.md) | `src/config/` | ~600 | Dead code, PBKDF2 stores plaintext, `setup()` not idempotent |
| 02 | [db/store](02-db-store.md) | `src/db/store.py` | 1,846 | ~320 lines dead (ML tables), misleading column names, 240-line `refresh_activities` |
| 03 | [analytics-core](03-analytics-core.md) | `power_metrics`, `training_load`, `decoupling`, `w_prime`, `threshold` | ~1,200 | Python loops over numpy, dead functions, docstring/constant mismatch |
| 04 | [readiness-recovery](04-readiness-recovery.md) | `readiness`, `recovery_model`, `individual_model` | ~900 | Near-duplicate models, ACWR fallback wrong, ML prediction never reaches LLM |
| 05 | [weekly-planner](05-weekly-planner-prescription.md) | `weekly_planner`, `prescription_engine` | ~1,300 | Dead functions, blocking weather call, 3 prescription paths |
| 06 | [analytics-remaining](06-analytics-remaining.md) | `strain_score`, `hr_training_load`, `durability`, `three_dim_ir`, `feedback_loop`, `feature_engineering` | ~1,100 | Third NP copy, 3D IR never persisted, feedback loop uses fictional plan |
| 07 | [garmin-connect](07-garmin-connect.md) | `src/ingestion/garmin_connect.py` | 1,986 | 15 API calls/day, divergent `__main__`, stream keying bug, incremental date logic wrong |
| 08 | [garmin-export](08-garmin-export.md) | `src/ingestion/garmin_export.py` | 392 | Wrong docstring, route keying mismatch, sleep data not extracted |
| 09 | [agent](09-agent.md) | `src/agent/` | ~700 | `ALLOWED_TABLES` not enforced, `build_json_context` dead, `WEIGHT_KG` stale |
| 10 | [weather-journal](10-weather-journal.md) | `src/services/weather.py`, `src/memory/journal.py` | ~420 | Weather fetched every render, journal grows unbounded, LLM call in UI thread |
| 11 | [tasks](11-tasks.md) | `src/tasks/` | ~660 | `_env_lock` never acquired, `BackgroundSync`/`BackgroundTask` 90% identical, worker imports UI |
| 12 | [wiki](12-wiki.md) | `src/wiki/` | ~1,500 | Ingest overwrites pages, 2 blocking LLM calls in UI thread, `get_context_for_coach` dead |
| 13 | [main](13-main.md) | `src/main.py` | 810 | 490-line `run_analyze`, `--prescribe` flag never acted on, ML prediction never reaches LLM |
| 14 | [ui-helpers](14-ui-helpers.md) | `src/ui_helpers.py` | 294 | Zone range gaps, `_get_units_system` re-reads file per call, `_HR_RANGES` imported by analytics |
| 15 | [visualize](15-visualize.md) | `src/visualize.py` | 3,271 | 3,271-line file, `latest_analysis.json` read every render, 3 DB connections in Settings |

## Cross-Cutting Themes

### 1. Multiple overlapping systems that were never consolidated

- **Readiness:** `readiness.py` (rule-based), `recovery_model.py` (ML), `individual_model.py` (Rothschild), `three_dim_ir.py` (3D impulse response), `prescription_engine.py` (3-index scoring). Five systems, three produce scores, none are clearly "the" readiness score.
- **Prescription:** `weekly_planner.py` (rules), `weekly_planner.py` (AI/LLM), `prescription_engine.py` (3-index), `main.py:run_prescribe` (LLM). Four paths, the LLM sees almost none of the computed results.
- **NP computation:** `power_metrics.py`, `strain_score.py`, and the PDC in `main.py` each compute normalized power independently.

### 2. Computed results that never reach the user

- ML prediction (`recovery_model.py`) → computed in `run_prescribe` → stored in `analysis["ml_prediction"]` → **not passed to `build_system_prompt`** → invisible to the LLM.
- Prescription engine output → computed in `run_prescribe` → stored in `analysis["prescription_engine"]` → **not passed to `build_system_prompt`** → invisible to the LLM.
- Feedback loop → computed in `run_analyze` → stored in `analysis["feedback"]` → passed to prompt builder via `analysis` param in the coach chat, but the coach chat is the only path; `run_prescribe` doesn't pass it.
- 3D IR model → rebuilt from zero every sync (never persisted) → shown in Trends but not used by any decision.

### 3. Dead code

- ~320 lines in `db/store.py` (ML/prescription tables + accessors with zero callers).
- `estimate_critical_power` (dead in production), `analyze_batch`, `rolling_validation`, `POPULATION_PRIORS`, `_project_ctl_atl`, `_select_session_type`, `build_json_context`, `get_context_for_coach`, `fetch_wellness_for_date`, `_prompt_mfa_interactive`, `DEFAULT_SCHEDULE`, `_SLOT_TO_WINDOW`.
- `try/except ImportError` dual-import pattern in 8+ analytics modules (the fallback branch is never hit).
- `garmin_connect.py` `__main__` block (divergent third wellness sync).

### 4. Performance

- Python loops over numpy arrays where vectorization is straightforward (`power_metrics.py`, `training_load.py`, `w_prime.py`).
- `search_pages` reads every wiki page's full content on every search.
- `get_weekly_forecast` (blocking HTTP) called on every Dashboard render.
- `_get_units_system` re-reads and re-parses the profile file on every call.
- `latest_analysis.json` opened and parsed on every Dashboard render.
- `sync_garmin` makes ~15 API calls per day (~21 seconds/day).

### 5. Layering violations

- `main.py` (UI/CLI) imports from `tasks/worker.py` which imports from `src.main` (circular).
- `analytics/hr_training_load.py` imports `_HR_RANGES` from `ui_helpers.py` (analytics → UI).
- `tasks/worker.py` imports `run_analyze`/`run_prescribe` from `src.main` (tasks → UI).
- `garmin_connect.py` calls `db._exec`/`db._commit` directly (ingestion → DB internals).
- `visualize.py` opens its own DB connections instead of using the session-state one.

### 6. Data integrity

- Stream keying: `garmin_{id}` in activities, bare `id` in streams after reparse. Works only because `reparse_all_fit_files` deletes all streams first.
- `sync_garmin` incremental mode syncs "last N days ending today" instead of "days since last sync" — gaps are never filled.
- `force_resync` deletes all wellness data with no confirmation or row-count log.
- `excluded_power_meters.json` is written by the UI on every render (even when unchanged).

## Suggested Priority Order

1. **Fix `run_prescribe` to pass the full `analysis` dict to `build_system_prompt`** (13-main #2) — one-line change, makes all analytics visible to the LLM.
2. **Fix `main()` to actually call `run_prescribe`** (13-main #10) — the `--prescribe` flag is parsed but never used.
3. **Fix `current_w_prime` to carry forward** (13-main #7) — W′ estimation is currently stateless.
4. **Consolidate readiness systems** (04, 05, 06) — pick one, delete the rest, or clearly document which is authoritative.
5. **Fix `sync_garmin` incremental date logic** (07-garmin-connect #9) — data gaps.
6. **Cache weather + analysis JSON** (10-weather #2, 15-visualize #2) — remove per-render HTTP/file I/O.
7. **Split `visualize.py`** (15-visualize #1) — 3,271 lines is unmaintainable.
8. **Delete dead code** (all docs) — ~500+ lines across the codebase.
9. **Fix stream keying** (07-garmin-connect #7, 08-garmin-export #2) — latent data corruption.
10. **Vectorize numpy loops** (03-analytics-core) — performance.