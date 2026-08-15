# Review: `src/main.py`

810 lines. The CLI pipeline orchestrator: ingest → analyze → prescribe. Also the `--visualize` launcher for the Streamlit dashboard.

## Findings

### 1. `run_analyze` is a 490-line function (lines 135-623)

It does: refresh activities, assess readiness, batch-fetch all streams, walk activities chronologically computing PDC/CP/power metrics/HR load/durability/decoupling/thresholds/W′/strain/Pmax/3D-IR, store metrics, calibrate HR, compute training load, train two ML models, run the feedback loop, and write `latest_analysis.json`. **Change:** split into focused functions: `_compute_activity_metrics(db, activities, streams)`, `_train_models(db, features)`, `_run_feedback_loop(...)`. The per-activity loop (lines 270-471) should be its own function.

### 2. `run_prescribe` passes almost nothing to `build_system_prompt` (lines 719-722)

```python
prompt = build_system_prompt(
    readiness=analysis.get("readiness"),
    recent_activities=analysis.get("recent_activities"),
)
```
The `analysis` dict contains `training_load`, `power_metrics`, `w_prime`, `durability`, `decoupling`, `thresholds`, `strain_scores`, `pmax_estimates`, `three_dim_ir`, `feedback`, `ml_prediction`, `prescription_engine` — but **none of them are passed to the prompt builder**. The LLM only sees readiness + 14 recent activities. The entire analytics pipeline's output is invisible to the prescription. **Change:** pass the full `analysis` dict as the `analysis` parameter (the prompt builder already handles it, lines 154-250 of `prompt_builder.py`).

### 3. ML prediction is computed but never reaches the LLM (lines 639-683, 715)

`ml_prediction` is computed (predicted PRS, confidence, limiting factor) and stored in `analysis["ml_prediction"]` (line 715) — but `build_system_prompt` is called at line 719 with only `readiness` and `recent_activities`. The `analysis` parameter is not passed, so `ml_prediction` is never in the prompt. **Change:** fixed by finding 2 (pass `analysis` to the prompt builder).

### 4. Prescription engine result is computed but never reaches the LLM (lines 685-710, 716)

Same issue: `prescription_engine_result` (readiness assessment, daily plan, safety notes) is stored in `analysis["prescription_engine"]` but never passed to the prompt. The LLM generates the prescription *without* seeing the rule-based engine's output. **Change:** fixed by finding 2.

### 5. `planned_tss=80.0` is hardcoded in the prescription engine input (line 701)

`PrescriptionInput(..., planned_tss=80.0, # default)` — the prescription engine's readiness index uses `planned_tss` to compute the load component. A hardcoded 80 means the engine always evaluates against a 80-TSS plan, regardless of what the user actually planned. **Change:** read the planned TSS from the weekly plan (if one exists) or the user profile.

### 6. Feedback loop compares against a fictional plan (lines 566-593)

```python
planned_tss = 100.0  # Default planned TSS
planned_zones = {"Z1": 20.0, "Z2": 50.0, "Z3": 15.0, "Z4": 10.0, "Z5": 5.0}
planned_intensity = 0.7
```
The feedback loop compares the latest ride against a hardcoded "plan" that doesn't exist. The result (`feedback_result`) is stored in the analysis dict but (per finding 2) never reaches the LLM. **Change:** either wire it to the actual weekly plan or delete it until it's connected to real plan data.

### 7. `current_w_prime` is initialized to 0.0 and never updated (line 203)

`current_w_prime = 0.0` — then at line 419: `wp_cap = current_w_prime / 1000.0 if current_w_prime > 0 else None`. Since `current_w_prime` is always 0, `wp_cap` is always `None`. The W′ estimation never receives a prior capacity estimate — it starts from scratch every run. **Change:** carry forward the previous activity's `w_prime_capacity` (from `wp_result`) into `current_w_prime` at the end of each loop iteration.

### 8. `estimate_critical_power` is imported but only `estimate_ride_cp` is used (line 44)

`from src.analytics.power_metrics import (..., estimate_critical_power, estimate_ride_cp, ...)` — `estimate_critical_power` is imported but never called in `main.py` (confirmed by grep: it's dead in production per `03-analytics-core.md`). **Change:** remove from the import.

### 9. `run_prescribe` builds ML features from a single synthetic row (lines 652-669)

```python
wellness_for_ml = [{
    "date": readiness.get("date", ""),
    "rmssd": readiness.get("rmssd"),
    ...
}]
activity_for_ml = [{
    "start_date": readiness.get("date", ""),
    "tss": training_load.get("atl", 0),
    "np": 0, "ifr": 0,
    "w_prime_min_balance": 50,
    "decoupling_drift": 0,
}]
```
This constructs a single-row DataFrame with placeholder values (`np: 0`, `ifr: 0`, `w_prime_min_balance: 50`, `decoupling_drift: 0`) and calls `compute_features` on it. The features will be mostly NaN or zero — the ML prediction is based on garbage input. **Change:** use the actual latest wellness + activity data from the DB, not synthetic placeholders.

### 10. `main()` doesn't call `run_prescribe` (lines 798-804)

```python
if run_all or args.ingest:
    run_ingest()
if run_all or args.analyze:
    analysis = run_analyze()
else:
    analysis = None
```
There's no `if run_all or args.prescribe: run_prescribe(analysis)`. The `--prescribe` flag is parsed (line 749) but **never acted upon**. Running `python -m src.main` (full pipeline) does ingest + analyze but **not** prescribe. **Change:** add the prescribe step.

### 11. `--visualize` launches Streamlit as a subprocess (lines 757-782)

`subprocess.run([sys.executable, "-m", "streamlit", "run", ...])` — blocks the parent process. The LAN IP detection (lines 762-774) runs `hostname -I` which fails on macOS (no `-I` flag). **Change:** use `subprocess.Popen` (non-blocking) and handle the macOS case.

### 12. `config.setup()` is called at module import time (line 32)

`config.setup()` runs on every import of `main.py`, including when `visualize.py` imports from it. This creates directories and reads env files on import — a side effect that should be explicit. **Change:** move `config.setup()` into `main()` and into the Streamlit entry point.

### 13. `import bisect` inside `run_analyze` (line 262)

Move to module top.

### 14. `import traceback` inside the ML training except block (line 539)

Move to module top (or use `logger.warning(..., exc_info=True)`).

### 15. `import socket, subprocess` and `import ipaddress` inside `main()` (lines 758-760)

`socket` is imported but never used. Move `subprocess`/`ipaddress` to module top.

## Cross-cutting

- `main.py` is the integration point for all analytics. The per-activity loop (lines 270-471) is where every metric is computed and stored. It's the most important code in the codebase and the least testable (490-line function, no dependency injection).
- The `latest_analysis.json` file (line 616-619) is the bridge between the CLI pipeline and the Streamlit UI. The UI reads it to populate the dashboard. This is a fragile contract — a schema change in `run_analyze`'s result dict silently breaks the UI.