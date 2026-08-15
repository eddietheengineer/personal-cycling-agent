# Review: `src/analytics/` core — power_metrics, training_load, decoupling, w_prime, threshold

Five modules, ~1,200 lines total. These are the per-activity computation primitives called from `main.py`'s post-sync pipeline.

## power_metrics.py (461 ln)

NP/IF/TSS/VI, Coggan zones, power-duration curve (PDC), and CP/W′ estimation via FastFitness.Tips (FTT) ratios.

### Findings

1. **`_moving_average` is a Python loop over numpy (lines 82-96).** It builds a cumsum (numpy) then loops `for i in range(n)` doing per-element cumsum lookups — this is O(n) Python-level work on what is otherwise a numpy pipeline. For a 2-hour ride at 1 Hz that's 7,200 iterations; fine, but the cumsum trick buys nothing here since each iteration is a scalar op. Either use `np.convolve(arr, np.ones(window)/window, "same")` (true vectorized) or drop the cumsum and keep the loop simple. Note the edge behavior ("edges use whatever is available") differs from `mode="same"` — verify against `test_power_metrics.py` before swapping.

2. **`_compute_time_in_zones` uses a Python loop over fractions (lines 166-176).** Same issue: `np.searchsorted(_ZONE_BOUNDARIES, fractions, side="right") - 1` does this in one vectorized call. The loop is 7,200 iterations per ride.

3. **`estimate_critical_power` is dead in production (lines 354-425).** Imported by `main.py:44` but never called there (verified by grep — only `estimate_ride_cp` is called, at line 311). It's exercised only by `tests/test_power_metrics.py`. It also carries a "legacy fallback" branch (lines 394-405) for whole-ride `avg_power` that nothing in the current pipeline produces. **Change:** either wire it up (it's the multi-ride CP estimator that `main.py`'s rolling-CP logic seems to want) or delete it and its tests.

4. **`_ftt_ratio` clamps at table bounds (lines 296-299).** For `dur < 60s` it returns the 60s ratio (1.808); for `dur > 3600s` it returns 1.0. Callers filter to 180-3600s so the clamps are never hit in practice — but the function's contract ("interpolate") silently becomes "clamp" at the edges. Document it or raise.

5. **W′ from a single effort is a rough model (lines 346-349).** `W' = (power - CP) * duration` for the best effort only. This is the standard 2-point approximation degenerated to 1 point; it's labeled as an estimate and the docstring is honest about it. No change needed, but note that `w_prime.py` has a *separate* W′ capacity estimator (peak 30s excess) — the two modules estimate W′ differently and both results get stored (`w_prime_capacity` from w_prime.py, W′ from power_metrics.py's CP estimation). Verify in the main.py review which one the UI shows.

6. **`_PDC_DURATIONS` includes 1s/3s/5s/10s (line 63).** These are computed for every ride and stored, but CP estimation ignores <180s and the UI (per AGENTS.md) shows max powers from the Garmin API (`max_avg_power_1s` etc. columns), not the computed PDC. Check whether the UI ever renders the short-duration PDC entries; if not, drop them from the default set.

## training_load.py (191 ln)

CTL/ATL/TSB/FB via half-life EMAs over zero-filled daily TSS.

### Findings

1. **Docstring says "30-day EMA" / "7-day EMA" (lines 8-9) but the constants are half-lives of 18/7 days.** The module docstring at line 8 says "CTL: 30-day EMA of TSS, half-life 18 days" — conflating the TrainingPeaks 35-day rolling average with the half-life model. The `_ema` docstring (line 42) says "For half_life=42 (CTL)" — **42, not 18**. The constant is `CTL_HALFLIFE_DAYS = 18.0`. Stale docstrings in three places describing three different values. **Change:** fix all three to say half-life 18 days.

2. **`compute_training_load` and `compute_training_load_history` duplicate the zero-fill loop (lines 83-100 vs 145-158).** Identical 16-line block. Extract a `_daily_tss_series(tss_records) -> tuple[list[str], list[float]]` helper.

3. **`ftp` parameter is unused (line 60).** `compute_training_load(tss_records, ftp)` takes FTP "for context" per the docstring but never uses it. Callers pass `current_cp` (main.py:501). **Change:** drop the parameter or use it (e.g. validate TSS magnitude).

4. **EMA seed bias.** `_ema` seeds with the first value (line 51). If the first day of a 10-year sync has TSS=0 (common — sync starts mid-history), CTL starts at 0 and takes ~40 days to recover. TrainingPeaks-style implementations seed with the first *non-zero* value or with the mean of the first N days. Worth checking against `test_training_load.py` expectations before changing, but the bias is real for long histories.

## decoupling.py (132 ln)

Pw:HR drift between first/second halves.

### Findings

1. **`abs(drift_pct) < threshold` is the wrong direction (line 107).** Decoupling is *negative* drift (HR rises relative to power). A ride where HR *drops* at constant power (positive drift, e.g. warm-up effects, caffeine) also gets `increase_duration_recommended=True`. The docstring (line 8) says "Drift < 5%: green light" — meaning magnitude — but the standard interpretation is one-directional: drift ≤ 5% (i.e. `drift_pct > -5`) means good. As written, a -20% drift (severe decoupling) returns `abs(-20) < 5` = False (correct), but a +10% drift returns True (arguably wrong — HR dropping isn't a fitness signal). **Change:** use `drift_pct > -drift_threshold` (one-sided) or document that magnitude is intentional.

2. **Redundant empty check (lines 69-76).** After `min_len < DECOUPLING_MIN_SAMPLES` returns and both lists are sliced to `min_len`, `if not power_samples or not hr_samples` can never be true (min_len ≥ 10 implies both non-empty). Dead branch.

3. **No warm-up trim.** The first half includes the ride's first minutes, when HR is still rising from rest — this inflates the first-half HR and *deflates* apparent drift, biasing the result toward "good". Standard implementations (TrainingPeaks) use a 30-60 min window or trim the first 10%. The module docstring says "steady-state aerobic rides" but nothing enforces or trims for steady state. **Change:** add an optional warm-up trim parameter, or at least document the assumption loudly.

## w_prime.py (167 ln)

W'BAL-ODE tracking with adaptive tau (Skiba & Clarke 2021).

### Findings

1. **W′ capacity estimator is a peak, not an integral (lines 91-98).** `max(30s rolling mean of excess power) * 30` estimates W′ as the energy of the single hardest 30s. Real W′ is the *integral* of all excess power over the effort (or a fit to the power-duration curve). A ride with one 30s sprint at 1200W (CP 250) gives W′ ≈ 28.5 kJ — plausible by luck, but a ride with five 30s sprints gives the same capacity estimate while the balance model only tracks the first. The docstring says "rough W' estimate" — honest, but this value is stored in `activity_metrics.w_prime_capacity` and presumably shown in the UI. **Change:** either integrate excess over the whole ride (capped) or label the UI field "peak 30s excess" so it isn't read as W′.

2. **`tau` parameter is deprecated but still functional (line 52, 68-69, 116-126).** "Deprecated. If provided, used as fixed tau (for backward compatibility)." No caller passes it (verified — main.py calls with defaults). **Change:** delete the parameter and the `adaptive` branch.

3. **`from .power_metrics import _compute_normalized_power` (line 22).** Cross-module import of a *private* function. It works, but it couples w_prime to power_metrics' internals. If NP computation changes, w_prime silently changes too. Either make `compute_normalized_power` public (drop the underscore) or pass CP in (callers already have it — main.py computes CP before calling this).

4. **Balance loop is pure Python over every second (lines 118-136).** 7,200 iterations with per-iteration `np.exp` (via `_compute_tau`) — `np.exp` on a scalar is ~100x slower than `math.exp`. Use `math.exp` in `_compute_tau`. For a 4-hour ride this is ~29k scalar `np.exp` calls.

## threshold.py (170 ln)

DFA-a1 threshold detection (LT1 at 0.75, LT2 at 0.50) + Zone 2 audit.

### Findings

1. **`analyze_batch` is dead in production (lines 141-159).** Only called from `tests/test_threshold.py`. `main.py` imports `analyze_thresholds` (singular) only. **Change:** delete or wire up.

2. **Crossing interpolation averages *all* crossings (lines 59-76).** During a ride, DFA-a1 oscillates around 0.75 many times (every hill, every surge). Averaging all crossing powers gives the mean power at which the signal crosses — reasonable, but a single long sustained segment at threshold dominates less than many brief crossings. Alternative: weight by segment duration, or take the median. Not a bug, but worth a comment on why mean is chosen.

3. **Zone 2 audit counts *all* samples below 0.75 (line 121), including the warm-up.** Same warm-up bias as decoupling: the first 10 minutes of a ride have low DFA-a1 (high parasympathetic tone) and count as "violations" even though the rider was in Z2. The 10% pass threshold (line 124) may be systematically failed by rides with long easy warm-ups. **Change:** trim warm-up before the audit, or document it.

4. **`DFA_ZONE2_VIOLATION_THRESHOLD = 0.75` in constants.py (line 79) is named like a DFA value but is a *violation rate* used nowhere.** The actual violation threshold is `DFA_ZONE2_AUDIT_PASS_THRESHOLD = 0.10` (used at threshold.py:85). `DFA_ZONE2_VIOLATION_THRESHOLD` is imported by threshold.py (lines 17-22) — verify which one is actually used. From the signature at line 85, the default is `DFA_ZONE2_AUDIT_PASS_THRESHOLD`; the other constant appears to be dead. **Change:** delete `DFA_ZONE2_VIOLATION_THRESHOLD` if unused.

## Cross-cutting

- **`try: from src.config.constants import ... except ImportError: from ..config.constants import ...`** appears in every analytics module (power_metrics, training_load, decoupling, w_prime via power_metrics, threshold, durability, feature_engineering, hr_training_load, strain_score). This dual-import pattern exists to support both `src.` absolute and relative imports — but the package is always imported as `src.*` (verified: every consumer uses `from src.analytics...`). The `except ImportError` branch is dead. **Change:** pick one import style and delete the try/except in all 8+ modules.
- **Rounding at the boundary.** Every module rounds its results (`round(x, 2)` / `round(x, 4)`) before returning. This is fine for storage, but it means downstream consumers (e.g. `main.py` storing to DB, then re-reading for charts) work with rounded values. Consistent, so no change — just noting it's a deliberate convention.

## Follow-ups for later reviews

- [ ] `main.py`: which W′ value reaches the UI — `w_prime.py`'s `w_prime_capacity` or `power_metrics.py`'s CP-estimation W′? (finding: two different estimators, both stored)
- [ ] `main.py`: confirm `estimate_critical_power` truly has no call site (only the import).
- [ ] `visualize.py`: does the UI render short-duration PDC entries (1s-10s)?
- [ ] `tests/test_training_load.py`: check whether tests pin the EMA seed behavior (first-value seed) before changing it.