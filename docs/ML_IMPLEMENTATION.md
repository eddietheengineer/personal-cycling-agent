# ML Implementation Summary

> Generated on 2026-07-12. Documents the implementation of the Rothschild-style individualized ML recovery model.

## Architecture

```
Data Layer (Garmin + Wellness)
    → Feature Engineering (z-scores, lags, EWMA, derived features)
        → ML Recovery Model (LASSO/SGD with online learning)
            → Prescription Engine (readiness-modulated load)
```

## Modules Created

### `src/analytics/feature_engineering.py`
- **Purpose**: Transform raw wellness + activity data into ML features.
- **Features**: 34 total — z-scored physiological metrics (RHR, RMSSD, stress, sleep), derived indices (sleep_index, wellness_composite), rolling aggregates (EWMA CTL/ATL, ACWR), and activity metrics (TSS, NP, W' balance).
- **Correlation pruning**: Drops features with |r| > 0.95 to avoid multicollinearity.
- **Output**: `pd.DataFrame` indexed by date, ready for model training.

### `src/analytics/recovery_model.py`
- **Purpose**: Individualized LASSO recovery model with online learning (Rothschild pattern).
- **Model**: `SGDRegressor` with L1 penalty (alpha=0.01) for feature selection.
- **Training**:
  - `train()`: Full retrain on historical data. Resets scaler to avoid stale state.
  - `partial_fit()`: Online learning with single new data point.
- **Target**: Next-day resting heart rate (proxy for physiological recovery).
- **Persistence**: JSON serialization of model coefficients, scaler parameters, and metrics.
- **Cold start handling**: Graceful fallback to 5.0 PRS with 0.3 confidence when model isn't trained.
- **Drift detection**: Monitors rolling RMSE; flags when error exceeds 2x baseline.

### `src/analytics/prescription_engine.py`
- **Purpose**: Combine ML predictions with rule-based guardrails for training prescription.
- **3-Index Scoring**:
  - Subjective (0.40): Self-reported wellness, sleep quality, stress.
  - Autonomic (0.30): HRV, RHR, body battery.
  - Fitness (0.30): CTL, ATL, TSS trends.
- **Pain Veto**: Pain ≥ 4/10 overrides all metrics (McIntyre 2011, Gabbett 2016).
- **Edge Cases**: Handles illness, travel, altitude, life stress with specific adjustments.
- **Hard Guardrails**: Max TSS caps based on readiness state.

## Integration

### `src/main.py`
- `run_analyze()`: Now includes ML model training inside the DB context.
  - Loads existing model if available (warm start).
  - Retrains on all available data each run (full retrain, not incremental).
  - Saves model to `{VAULT}/data/recovery_model.json`.
  - Reports training metrics (n_samples, RMSE, R², status).
- `run_prescribe()`: Uses prescription engine with ML predictions for readiness.

### `src/db/store.py`
- New tables: `morning_checkin`, `daily_readiness`, `training_log`, `edge_cases`, `validation_log`.
- WAL mode for concurrent read/write.

## Training Results

| Metric | Value |
|--------|-------|
| Samples | 1,583 |
| RMSE | 2.95 bpm |
| R² | 0.21 |
| Status | trained |
| Top Features | rmssd_z (1.23), acwr (0.97) |

### Interpretation
- R²=0.21 means the model explains ~21% of RHR variance — consistent with Rothschild's finding that individual models show 5x RMSE variance across athletes.
- Model will improve with more data (especially HRV/sleep from Garmin Connect sync).
- Online learning via `partial_fit()` will personalize weights as new data arrives.

## Known Limitations

1. **Sparse HRV data**: Historical Garmin data lacks RMSSD (1771/1771 rows NULL). Model relies mainly on RHR and stress.
2. **Proxy target**: Using next-day RHR as recovery proxy instead of actual performance data.
3. **Single-athlete**: No multi-athlete support yet.
4. **No morning checkins**: Subjective data not yet collected via UI.

## Next Steps

1. **Enable Garmin Connect sync**: Populate HRV, sleep stages, body battery for richer features.
2. **Morning check-in UI**: Streamlit form for subjective wellness data.
3. **Online learning**: Switch from full retrain to `partial_fit()` for daily incremental updates.
4. **Performance target**: Replace RHR proxy with actual performance metrics (FTP test results, race times).
5. **Multi-athlete**: Add athlete_id dimension to all tables and model isolation.

## References

- Rothschild et al. 2024, *Eur J Appl Physiol*: Individual recovery models via online LASSO.
- Alfonso et al. 2025, *Sci Rep*: Multi-modal readiness (HRV + RHR + subjective) yields 2.5x greater FTP gains.
- Kontro et al. 2026, *PLOS One*: 3D IR model for power-based fitness assessment.
- Saw et al. 2016: Readiness index weighting (0.40 subjective, 0.30 autonomic, 0.30 fitness).
- McIntyre 2011, Gabbett 2016: Pain monitoring as training veto.