# Review: remaining `src/analytics/` — strain_score, hr_training_load, durability, three_dim_ir, feedback_loop, feature_engineering

## strain_score.py (204 ln)

Pmax estimation + Strain Score (SS) decomposition into aerobic/glycolytic/alactic components (Kontro et al. 2026).

### Findings

1. **`_compute_normalized_power` is a third copy of NP (lines 177-184).** `power_metrics.py` has `_compute_normalized_power` (with the 30s MA), and this module has its own. This one is shorter — verify it also does the 30s moving average (the elided lines 178-183 suggest `ma = ...` then `mean(ma**4)**0.25`). If it skips the MA, the TSS-equivalent at line 164 is computed differently than `power_metrics.py`'s TSS. **Change:** import from `power_metrics` (make it public) instead of duplicating.

2. **`estimate_pmax`'s fallback `cp + w_prime/10` (line 89) is a 10-second burst assumption.** W′ spread over 10s — arbitrary. The docstring doesn't say why 10s. Also the clamp `max(cp * 2.0, ...)` (line 94) means Pmax is *never* below 2×CP — a rider whose best 5s power is 1.8×CP gets Pmax silently raised to 2×CP, which then inflates every Strain Score downstream. **Change:** document or remove the floor.

3. **`compute_strain_score`'s `mpa = cp` simplification (line 139).** "Simplified: MPA ≈ CP" — MPA (maximal aerobic power) is typically 10-20% above CP. Using CP deflates the numerator (`pmax - mpa + cp`) and inflates the denominator for low powers, biasing SS downward for aerobic work. Acceptable as a v1, but the docstring should say the SS values are not comparable to the paper's.

4. **`w_prime` parameter is unused in `compute_strain_score` (line 103).** It's in the signature but never referenced in the body (only `cp`, `pmax`, `power`, `ftp` are used). **Change:** drop the parameter.

## hr_training_load.py (266 ln)

Banister dTRIMP from per-second HR, normalized to a TSS-like scale, with athlete-specific calibration.

### Findings

1. **`_banister_trimp` is a Python loop with per-sample `math.exp` (lines 112-119).** 7,200 iterations per ride. Vectorize: `hr_r = np.clip((hr - resting_hr) / hr_reserve, 0, 1); trimp = np.sum(hr_r * 0.64 * np.exp(b * hr_r)) / 60`. Same for `_compute_hr_time_in_zones` (lines 162-168) — `np.searchsorted` over the zone boundaries.

2. **`from src.ui_helpers import _HR_RANGES, _zone_for_value` (line 32).** An *analytics* module imports private helpers from a *UI* module. This inverts the dependency direction (analytics should not depend on UI). The HR zone boundaries (58/75/90/95% max HR) are a domain constant, not a UI concern. **Change:** move `_HR_RANGES` to `config/constants.py` (or a `zones.py`) and have both `ui_helpers` and `hr_training_load` import from there.

3. **Calibration factor is recomputed from scratch every sync (main.py:476-494).** The median of *all* dual-sensor ride ratios is recomputed on every `--analyze` and stored. This is fine statistically (median is robust), but it means the calibration factor can change retroactively — a ride's `hr_tss` stored last week was calibrated with last week's factor, but the factor is now different. The stored `hr_tss` values in `activity_metrics` are not recomputed. Minor inconsistency; document it or recompute.

4. **`_THRESHOLD_HR_FRACTION = THRESHOLD_HR_FRACTION` (line 66) is a pointless alias.** Same for `_MIN_HR_SAMPLES = MIN_VALID_HR_SAMPLES`, `_CAL_MIN`, `_CAL_MAX`, `_MIN_CAL_RIDES` (lines 69-76). The constants are already imported; the re-aliasing adds nothing. **Change:** use the imported names directly.

## durability.py (161 ln)

Peak 1-min/5-min power at cumulative kJ thresholds (fresh/fatigued/deeply fatigued).

### Findings

1. **`rolling_max` is a third copy** (lines 87-99, defined *inside* the function) of the same deque algorithm in `power_metrics.py:_rolling_max`. **Change:** import from `power_metrics`.

2. **`peak_at_load` with `FRESH_KJ + 1` (line 117) is a hack.** "Fresh" means "at the start of the ride", but the code searches for where cumulative kJ crosses 1 kJ — which for a 200W rider is ~5 seconds in, before the 60s rolling window has any value (all NaN). The test file (`test_durability.py`) confirms this: nearly every test asserts `peak_1min_fresh is None` because the 1 kJ crossing happens before index 60. **The "fresh" peak is effectively never computed for normal rides.** **Change:** define "fresh" as the first valid rolling-window value (index `window-1`), not a kJ threshold.

3. **`durations` parameter is unused (line 47).** In the signature, never in the body. **Change:** drop it.

4. **Degradation ratio only uses the "fatigued" state (lines 126-127).** `degradation_1min = p1_fatigued / p1_fresh` — but `p1_fresh` is almost always None (finding 2), so degradation is almost always None. The "deeply fatigued" peaks are computed but never used in any ratio. **Change:** fix finding 2 first; then decide whether degradation should be fatigued/fresh or deeply_fatigued/fresh.

## three_dim_ir.py (287 ln)

3D impulse-response model: fitness/fatigue dynamics per energy system.

### Findings

1. **The model is rebuilt from scratch every sync and never persisted (main.py:213, 444-451).** `ThreeDIMModel()` is instantiated fresh in `run_analyze`, updated once per activity in the loop, and `to_dict()` is called at the end (main.py:610) — but the state is discarded. Next sync starts from zero fitness/fatigue. The "fitness trend" (1-day change) is therefore always measured against a zero baseline, not yesterday's actual state. **Change:** persist the model state (JSON in vault, like the recovery models) and load it at the start of `run_analyze`.

2. **`SYSTEM_PARAMETERS` are "population priors" with no individual fitting (lines 25-49).** The comment says "individual fitting improves accuracy" but no fitting code exists. The k1/k2 values (0.001/0.0005 for CP) mean performance is in arbitrary units — `predicted_cp` is *not* watts (the comment at line 29 claims "watts for CP" but k1×SS with k1=0.001 and SS~200 gives 0.2, not 250W). **The `predicted_cp`/`predicted_wp`/`predicted_pmax` fields are meaningless as absolute values** — only the trends and the fitness/fatigue ratio are interpretable. The prompt builder uses `readiness_from_fitness` (prompt_builder.py:238), which is the logistic-mapped ratio — that part is fine. **Change:** either fit the parameters or relabel the outputs as "relative fitness index" and stop calling them `predicted_cp`.

3. **`get_readiness_from_fitness` (lines 222-239) is a third readiness score.** After `readiness.py` (composite 0-100) and `prescription_engine.py` (3-index 0-1), this is a third independent "readiness" number. It's the only one that reaches the LLM prompt (prompt_builder.py:238). The other two don't (see earlier reviews). **Change:** consolidate — pick one readiness number for the prompt.

4. **`days = (current_date - last_date).days or 1` (line 163).** If two activities are on the same day, `days = 0` → `or 1` makes it 1 day of decay. Same-day double sessions get a full day of fatigue decay between them. Minor, but wrong.

## feedback_loop.py (140 ln)

Post-ride plan mutation rules.

### Findings

1. **The "plan" it compares against is hardcoded (main.py:572-577).** `planned_tss = 100.0`, `planned_zones = {"Z1": 20, "Z2": 50, "Z3": 15, "Z4": 10, "Z5": 5}`, `planned_intensity = 0.7` — constants, not the actual plan. The weekly planner produces real plans (saved to `latest_weekly_plan.json`), but the feedback loop never reads them. So "TSS overshoot (130% of plan)" is measured against a fictional 100-TSS plan. **Change:** load the actual planned session for the ride's date from the weekly plan (or the training_log table), or delete the feedback loop until plans are real.

2. **`ftp_drift` is never passed (main.py:579-588).** The parameter exists but the call site doesn't provide it, so Rule 3 is dead.

3. **The result is computed but not applied.** `feedback_result` goes into `analysis["feedback"]` (main.py:612) — and like `ml_prediction`/`prescription_engine`, it's unclear whether it reaches the LLM prompt (check prompt_builder). The `next_day_tss_adjustment` multiplier is never consumed by the weekly planner. **Change:** wire it into the planner or delete it.

## feature_engineering.py (174 ln)

Feature pipeline for the ML recovery models.

### Findings

1. **`get_feature_names` (lines 156-174) is a hardcoded list that must match `compute_features`'s output.** It lists 15 names, but `compute_features` produces many more (all the `_lag1/2/3/7` columns, `sleep_index`, `wb_score`, `wb_composite`, `ctl`, `atl`, `acwr`, `tss_7d`, `decoupling_trend`, `w_prime_trend`, plus the raw wellness columns). The model's `feature_names` is used to *select* columns (`features.columns.intersection(self.feature_names)`), so the lag features are silently dropped. That may be intentional (lags leak future info into a next-day prediction? No — lag1 is yesterday, which is fine for predicting today). **Change:** derive the list from the pipeline, or document why lags are excluded.

2. **`_prune_correlated_features` drops columns non-deterministically (lines 138-153).** It drops any column with |r| > 0.85 with *any* other column, iterating in column order. If A correlates with B and C, the drop set depends on iteration order. More importantly: pruning is done on the *training* data's correlation structure, but the pruned column set is not saved — the next sync re-prunes, potentially dropping different columns. The model's `feature_names` (saved in the model JSON) may then not match the pruned frame. **Change:** persist the pruned feature list in the model file, or skip pruning (L1 regularization already handles collinearity).

3. **`df.ffill(limit=2)` (line 73) forward-fills *all* columns, including `tss`.** A rest day after a 150-TSS ride gets tss=150 for up to 2 days, inflating CTL/ATL/ACWR. TSS should be 0 on days with no activity, not forward-filled. **Change:** ffill only the wellness columns (rmssd, resting_hr, stress, sleep), and fill `tss` with 0.

4. **`main.py` calls `compute_features(wellness_dicts, activity_metrics_dicts, morning_checkins=None)` (main.py:514-516).** Morning check-ins are in the DB (`morning_checkin` table) but never passed — so `wb_score`/`wb_composite` (the subjective features) are always NaN → dropped by `dropna(thresh=...)`. The subjective index in the ML model is never actually trained. **Change:** load morning check-ins from the DB and pass them in.

## Cross-cutting (this batch)

- **`main.py`'s per-activity loop (lines 270-471) is the integration point for all of these.** It computes: ride CP → rolling CP → power metrics → HR load → durability → decoupling → thresholds → W′ → strain/Pmax → 3D IR → store metrics. Each is wrapped in its own try/except with a warning log. The order matters (CP before power metrics, W′ before strain), but there's no documentation of the dependency graph. A one-paragraph comment at the top of the loop would help.
- **`current_w_prime` (main.py:203) is initialized to 0.0 and never updated** — so `wp_cap` at line 419 is always None, and W′ capacity is always re-estimated from the current ride's peak 30s excess (see w_prime.py review). The "carry forward the previous ride's W′" intent is dead code.

## Follow-ups for later reviews

- [ ] `agent/prompt_builder.py`: confirm which of `feedback`, `ml_prediction`, `prescription_engine`, `three_dim_ir` actually reach the LLM prompt.
- [ ] `visualize.py`: which of these results are rendered in the UI (durability? strain score? 3D IR?) — dead results that are computed but never shown are candidates for removal.