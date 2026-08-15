# Review: `src/analytics/readiness.py` + `recovery_model.py` (+ `individual_model.py`)

## readiness.py (600 ln)

Multi-modal readiness: autonomic (HRV/RHR z-scores) 40% + stress 25% + load (ACWR) 35%, with Kiviniemi-style state classification and load-modulation factor.

### Findings

1. **CTL/ATL are recomputed locally, duplicating `training_load.py` (lines 443-460).** `assess_readiness` builds its own TSS series and runs `pd.Series(...).ewm(alpha=...)` with hardcoded half-lives (18, 7) — the same computation `training_load.py:_ema` does with the shared constants. The pandas EWM here is also seeded differently (`min_periods=1`, first value = first TSS) than `training_load.py`'s manual EMA. Two implementations, two seed behaviors, and the half-life values are hardcoded here instead of using `CTL_HALFLIFE_DAYS`/`ATL_HALFLIFE_DAYS`. **Change:** call `compute_training_load_history` (or share `_ema`) and pass the resulting CTL/ATL in.

2. **`_load_score`'s ACWR fallback is wrong (lines 185-190).** When `atl` is None but `recent_tss` (7-day *total* TSS) and `ctl` are available, it computes `acwr = recent_tss / ctl` — a 7-day sum divided by a chronic average. That's not a ratio of comparable quantities; ACWR is ATL/CTL where ATL is a *daily-average* acute load. A week with 700 TSS and CTL 100 gives "ACWR" 7.0 → "injury_risk" — nonsense. **Change:** either require `atl` (return neutral 50 when absent) or divide `recent_tss` by 7 first.

3. **`_autonomic_score` double-counts when both metrics deviate (lines 127-137).** RMSSD z=-1 and RHR z=+1 gives 50 - 15 - 15 = 20. The two signals are physiologically correlated (sympathetic activation raises RHR *and* lowers HRV), so a single stressor moves both and the score drops 30 points for one cause. The Kiviniemi decision below handles the joint case, but the composite score doesn't know about it. Consider capping the combined autonomic adjustment (e.g. max ±20 total) or using the max of the two z-deviations rather than the sum.

4. **Baseline window includes the target date's neighbors but not the target (lines 370-376) — correct, but `window` defaults to 30 while the docstring says "rolling baseline".** With only 30 days of data, the baseline is the *entire* history including the current training block — a rider mid-block will have an elevated-RHR baseline and miss the fatigue signal. Standard practice is a 28-90 day baseline *excluding* the current block, or at minimum documenting the tradeoff. The `window` parameter exists but no caller passes it (main.py uses default).

5. **`assess_all_dates` is O(n²) (lines 556-570).** For each of n dates it calls `assess_readiness`, which re-sorts all records and re-filters the baseline window. For 10 years of daily wellness data (~3,650 dates) that's ~13M date comparisons. It's only called from tests, but if it's ever wired to the UI's trends page it will be slow. **Change:** compute baselines incrementally (rolling window) or cache the sorted records.

6. **`pandas` import (line 22) is used only for the EWM in finding 1.** If finding 1 is fixed, the pandas dependency drops out of this module.

7. **Confidence scoring (lines 514-526) counts `recent_tss` as a data point but not `ctl`/`atl`.** A day with RMSSD+RHR+stress+7dTSS gets "high" confidence even if the load sub-score is the neutral 50 from missing CTL/ATL. Minor, but the confidence label overstates what the load component actually knows.

## recovery_model.py (380 ln) + individual_model.py (330 ln)

**These two modules are near-duplicates.** Both implement: `POPULATION_PRIORS` (identical 11-feature dict), `SGDRegressor(penalty="l1")` with `eta0=0.01`, `StandardScaler`, `train`/`partial_fit`/`predict`/`evaluate`/`save`/`load` with the same JSON format, the same cold-start/warming/trained status thresholds (7/28 samples), and the same confidence formula (`0.3 + 0.05*n` warming, `0.7 + 0.01*(n-28)` trained).

Differences:
- `individual_model.py` adds convergence tracking (`_stable_rmse_count`, `convergence_day`) and returns `individual_weights` in predictions.
- `recovery_model.py` adds `rolling_validation` and `check_drift` (EWMA residual).
- `recovery_model.py` clips predictions to 0-120 ("covers PRS or RHR"); `individual_model.py` clips to 0-10.

### Findings

1. **Consolidate into one module.** `main.py` trains *both* on the *same* features and the *same* target (`resting_hr.shift(-1)`, main.py:528 and 557) and saves them to two files. They will produce nearly identical coefficients. Pick one (recommend `individual_model.py` — it has the convergence tracking and is the newer Rothschild framing) and delete the other. This removes ~380 lines and a duplicate model file in the vault.

2. **The prediction path in `run_prescribe` is largely cosmetic (main.py:639-683).** It builds a *single-row* feature frame from the latest readiness dict, with hardcoded filler values (`"np": 0, "ifr": 0, "w_prime_min_balance": 50, "decoupling_drift": 0` at lines 664-666), calls `model.predict`, and logs the result. The `ml_prediction` dict is stuffed into `analysis["ml_prediction"]` (line 715) — but `build_system_prompt` (line 719) only takes `readiness` and `recent_activities`. **The ML prediction never reaches the LLM prompt.** Either wire `ml_prediction` into the prompt or delete the prediction block. (Verify in the agent/prompt_builder review.)

3. **`train()` refits on the full history every sync (main.py:529).** Every `--analyze` run loads the model, then calls `train()` on *all* features, which resets the scaler and refits from scratch. The `partial_fit` online-learning path (the whole point of the Rothschild approach) is never used in production. The model file is written every sync. This works, but it means the "online learning" framing in the docstrings is aspirational — it's actually batch retraining. Either use `partial_fit` for new days only, or rename the methods/docstrings to say "batch retrain".

4. **Target is `resting_hr.shift(-1)` — predicting tomorrow's RHR from today's features (main.py:528).** The docstrings say "next-day PRS prediction" (PRS = perceived readiness score, 0-10) but the actual target is RHR (30-120 bpm). That's why `recovery_model.py:205` clips to 0-120 with the comment "Model may be trained on PRS (0-10) or RHR (30-120)". The `predicted_prs` field name is a lie — it's a predicted RHR. **Change:** rename the field to `predicted_value` or actually predict a readiness score.

5. **`check_drift` compares the residual to the EWMA *after* updating it (lines 314-318).** `self._ewma_residual = alpha*residual + (1-alpha)*ewma` then `abs(residual - self._ewma_residual) > 2.0`. After the update, the EWMA is *pulled toward* the residual, so the difference is always ≤ `(1-alpha) * |residual - old_ewma|` — the test is weaker than intended. Compare against the *pre-update* EWMA. Also the threshold is a raw 2.0 (bpm if target is RHR) with no normalization — drift detection is scale-dependent.

6. **`load()` restores `coef_`/`intercept_`/scaler state but not `partial_fit` compatibility (lines 359-380).** After `load()`, calling `partial_fit` works (SGDRegressor with manually-set `coef_` accepts `partial_fit`), but `train()` resets the scaler and refits — so the loaded state is discarded on the next sync anyway (see finding 3). The save/load cycle is currently only useful for the prediction path.

7. **`rolling_validation` (lines 265-302) is never called** (verified by grep — no callers in src or tests). Dead code.

8. **`POPULATION_PRIORS` is dead weight in both modules.** It's used only as a fallback in `predict` when the model has no coefficients (cold start) and in `get_feature_importance`'s empty case. But the cold-start prediction returns `predicted_prs=5.0` with `limiting="cold_start"` — the priors dict is returned as `individual_weights` but nothing consumes it. If the priors are meant to drive cold-start predictions, the prediction should be a weighted sum of the features using these weights; as written they're just a label.

## Follow-ups for later reviews

- [ ] `agent/prompt_builder.py`: confirm `ml_prediction` and `prescription_engine` results are (or aren't) included in the LLM prompt.
- [ ] `feature_engineering.py`: check what `compute_features` actually produces — the hardcoded fillers in `run_prescribe` (np=0, ifr=0) suggest the feature frame shape is fragile.
- [ ] `prescription_engine.py`: it takes `acwr` from `training_load.get("acwr")` (main.py:700) — but `training_load_to_dict` (training_load.py:183-191) returns `fb` (fitness-fatigue), not `acwr`. Verify the key mismatch.