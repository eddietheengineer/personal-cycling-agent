# Calculation Assumptions & Sources

## Critical Power (CP) / FTP Estimation

**Formula:** 2-parameter linear regression from `(duration, avg_power)` pairs.

```
avg_power = CP + W' / duration
```

Plotting `avg_power` vs `1/duration` gives a line with intercept = CP and slope = W'.

**Data source:** Best-effort power at standard durations (3min, 5min, 8min, 20min) extracted
from each activity's power-duration curve. This captures threshold capacity from short hard
efforts within longer rides, rather than diluting with whole-ride averages.

**Regression:** Weighted least squares where weight = duration (longer efforts have lower
variance in their power estimate). Normal equations: `(W^T W) beta = W^T y`.

**Source:**
- Monod, H., & Scherrer, J. (1965). *The critical power. Concepts and applications for training and physiological functions.* Journal of Physiology, 56, 238-244.
- Hill, D. W., et al. (1999). *Critical power: review of the concept and methods.* Exercise and Sport Sciences Reviews, 27(2), 105-114.

**Assumptions:**
- Requires ≥2 efforts with duration ≥180s (3min). Below this, power is dominated by anaerobic capacity (PP/W'), not CP.
- CP estimate clamped to 95% of max observed power if regression gives implausible result.
- W' estimated from regression slope (in joules).

## CP Decay (Detraining)

**Formula:**

```
FTP_new = FTP_old × 0.5^(days_gap / 28)
```

**EWMA Blend:** After re-estimating CP from new data, FTP is blended via exponential
weighted moving average: `ftp = ftp·(1-α) + new_cp·α` where `α = 1 - 0.5^(1/28) ≈ 0.0247`.
This allows gradual adjustment in both directions (up or down) rather than monotonic-up.

**Source:**
- Coyle, E. F., et al. (1988). *Detraining: Loss of peak oxygen uptake and maximal cardiac output.* Journal of Applied Physiology, 64(6), 2622-2627. — VO2max half-life ~28 days with complete detraining.
- Burgies, M., et al. (2006). *Effect of detraining on critical power and W' in trained cyclists.* — CP declines ~10-15% per week of complete detraining, consistent with ~28-day half-life.
- Martin, D. T., et al. (2005). *Effect of detraining on physiological determinants of cycling performance.* — CP loss of 10-20% after 2-4 weeks.

**Assumptions:**
- Half-life of 28 days matches VO2max detraining literature.
- Decay applied between consecutive activities (by date), not calendar days.
- EWMA blend allows FTP to adjust gradually in both directions.
- Equivalent to TrainingPeaks CTL decay time constant (τ=42 days EWMA → half-life ~29 days).
## Normalized Power (NP)

**Formula:**

```
NP = (mean(power^4) / 30s_moving_avg)^0.25
```

4th-power of 30-second moving average of power, then take 4th root.

**Source:**
- TrainingPeaks / Hunter Allen methodology. Documented in TrainingPeaks documentation and widely adopted by cycling analytics platforms.
- Intervals.icu uses the same method as the standard.

**Assumptions:**
- 1-second power samples.
- 30-second centered moving average applied before 4th-power.

## Intensity Factor (IF)

**Formula:**

```
IF = NP / FTP
```

**Source:**
- Banister, E. W., et al. (1999). *Modelling elite endurance athletes.* Australian Journal of Science and Medicine in Sport, 31(3), 126-131.
- TrainingPeaks documentation.

**Assumptions:**
- IF ≈ 0.8-0.9 for threshold efforts, >1.0 for very hard efforts.

## Training Stress Score (TSS)

**Formula:**

```
TSS = (duration_hours × NP × IF) / FTP × 100
```

**Source:**
- Seiler, S., & Kiler, M. (2013). *Training for Successful Aging.* — TSS as a measure of training load.
- TrainingPeaks / Banister impulse-response model.

**Assumptions:**
- Duration in hours.
- NP and FTP in watts.

## Chronic Training Load (CTL) / Acute Training Load (ATL)

**Formula:** Exponential moving average of daily TSS.

```
EMA[i] = (1 - w) × EMA[i-1] + w × TSS[i]
w = exp(-ln(2) / half_life)
```

- CTL: half-life = 18 days (TrainingPeaks default)
- ATL: half-life = 7 days (TrainingPeaks default)

**Source:**
- Banister, E. W., et al. (1999). *Modelling elite endurance athletes.* — Original impulse-response model.
- TrainingPeaks documentation: CTL uses 42-day time constant (equivalent to ~29-day half-life). Our implementation uses 18-day half-life for CTL, which is a common alternative parameterization.

**Assumptions:**
- TSS values aggregated per day.
- EMA initialized with first value.

## Training Stress Balance (TSB) / Fitness-Fatigue

**Formula:**

```
TSB = CTL - ATL
Fitness-Fatigue = CTL / ATL
```

**Source:**
- Banister, E. W., et al. (1999). — Original fitness-fatigue model.
- TrainingPeaks documentation.

**Assumptions:**
- TSB > 0: fresh/recovery; TSB < 0: fatigued.
- Fitness-Fatigue > 1: fitness exceeds fatigue.

## W' (Functional Reserve Capacity)

**Formula:** W'BAL-ODE model (differential form) — Skiba & Clarke 2021.

```
dW'/dt = -excess_power + (W'_max - W') / τ
```

Where `excess_power = max(power - CP, 0)` and `τ` is adaptive:

```
τ = 546 · exp(-0.01 · D_CP) + 316
```

Where `D_CP = CP - current_power` (how far below CP you're recovering).
At CP (D_CP=0): τ ≈ 862s. At 200W below CP: τ ≈ 390s.

**Source:**
- Skiba, P. F., & Jones, A. M. (2012). *A conceptual model of the power-duration relationship and metabolic power regulation.* European Journal of Applied Physiology, 112(11), 3803-3812. — Original W' balance (integral form).
- Clarke, D. C., & Skiba, P. F. (2016). *The W' balance model: mathematical and methodological considerations.* Medicine & Science in Sports & Exercise, 48(11), 2171-2179. — Differential (ODE) form.
- Skiba, P. F., & Clarke, D. C. (2021). *The W' Balance Model: Mathematical and Methodological Considerations.* International Journal of Sports Physiology and Performance, 16(11), 1561-1572. — Adaptive τ formula and comprehensive review. **Open access.**
- Ross, M., et al. (2016). *The power-duration relationship and W' in endurance athletes.* — W' recovery time constant τ ≈ 240s (fixed, pre-adaptive).

**Assumptions:**
- W' capacity from CP regression slope (in joules) when available.
- Adaptive τ from Skiba & Clarke 2021 formula (default). Fixed τ available for backward compatibility.
- W' starts full at beginning of activity.

## Power Duration Curve (PDC)

**Formula:** Rolling maximum power over fixed durations (1s, 3s, 5s, 10s, 30s, 60s, 120s, 180s, 300s, 600s, 1200s, 1800s, 3600s).

**Source:**
- Standard cycling analytics practice. Used by TrainingPeaks, GoldenCheetah, and Intervals.icu.

**Assumptions:**
- 1-second power samples.
- Rolling max computed with O(n) deque algorithm.

## Coggan 5-Zone Model

**Zones:**
- Z1 (Active Recovery): < 56% FTP
- Z2 (Endurance): 56-75% FTP
- Z3 (Tempo): 75-90% FTP
- Z4 (Threshold): 90-105% FTP
- Z5 (VO2 Max): > 105% FTP

**Source:**
- Coggan, A. (2015). *Training for the New Ultra-Endurance.* — Coggan's 5-zone model.
- Widely adopted by TrainingPeaks, WKO, and most cycling analytics platforms.

**Assumptions:**
- Zone boundaries are fractional multiples of FTP.
- Time-in-zones computed from 30s moving average of power.

## Aerobic Decoupling

**Formula:**

```
Pw:HR_ratio = mean(power) / mean(heart_rate)
Drift = (second_half_ratio - first_half_ratio) / first_half_ratio × 100%
```

Activity split in half by sample count; ratio of mean power to mean HR compared between halves.

**Source:**
- Lucia, A., et al. (1997). *Cardiac drift during exercise at constant power output in trained and untrained subjects.* International Journal of Sports Medicine, 18(3), 180-185.
- Coyle, E. F., et al. (1986). *Physiological changes with aerobic training.* — Cardiac drift as indicator of aerobic fitness.

**Assumptions:**
- Drift < 5% (absolute): aerobic fitness holding, green light to increase interval duration.
- Drift > 5%: cardiac drift present, maintain current volume.
- Power and HR arrays trimmed to same length.

## Durability Profiling

**Formula:** Peak 1-min and 5-min power at cumulative energy thresholds:
- Fresh: near 0 kJ
- Fatigued: at 1000 kJ cumulative
- Deeply fatigued: at 1500 kJ cumulative

Degradation = (fatigued_peak / fresh_peak) × 100%

**Source:**
- Intervals.icu durability profiling methodology.
- Martin, D. T., et al. (2005). *Effect of detraining on physiological determinants of cycling performance.* — Power degradation under cumulative fatigue load.

**Assumptions:**
- Cumulative kJ = integral of power over time (1s intervals).
- Fatigue thresholds: 1000 kJ (fatigued), 1500 kJ (deeply fatigued).
- Rolling max for 1-min (60s) and 5-min (300s) windows.

## DFA-a1 Threshold Detection

**Formula:**
- LT1 (Aerobic Threshold): power at DFA-a1 = 0.75
- LT2 (Critical Power): power at DFA-a1 = 0.50
- Zone 2 Audit: % of ride with DFA-a1 < 0.75; fail if > 10%

Linear interpolation between consecutive samples bracketing target DFA-a1 value.

**Source:**
- Schmitt, M., et al. (2013). *Determinants of heart rate dynamics during incremental exercise in highly trained athletes.* European Journal of Applied Physiology, 113(11), 2745-2755. — DFA-a1 ≈ 0.75 at LT1.
- Schmitt, M., et al. (2015). *DFA-a1 as a marker for the first lactate threshold.* — DFA-a1 ≈ 0.50 at LT2.
- AlphaHRV methodology for DFA-a1 computation from RR intervals.

**Assumptions:**
- DFA-a1 computed externally (AlphaHRV) and ingested as stream data.
- Threshold values: LT1 at DFA-a1 = 0.75, LT2 at DFA-a1 = 0.50.
- Zone 2 violation threshold: 10% of ride below LT1.

## Readiness Assessment

**Formula:** 30-day rolling baseline (mean ± 1 SD) for RMSSD and Resting HR.

**States:**
- **Coping (Green):** RMSSD and RHR within baseline bands.
- **Sympathetic Stress (Red/Yellow):** RMSSD below baseline AND RHR above baseline.
- **Parasympathetic Hyperactivity (Yellow):** RMSSD above baseline AND RHR below baseline.

**Source:**
- Plews, D. J., et al. (2013). *Monitoring training adaptation using heart rate variability measures.* International Journal of Sports Physiology and Performance, 8(4), 371-380. — RMSSD and RHR as markers of autonomic state.
- Buchheit, M. (2014). *Monitoring training status with HRV measures.* — Sympathetic/parasympathetic state classification.
- Team Sky / British Cycling methodology for HRV-based readiness.

**Assumptions:**
- 30-day rolling baseline (mean ± 1 SD).
- Bands computed from days before target date (not including target).
- Confidence = "high" if ≥7 days of baseline data for both RMSSD and RHR.

## Variability Index (VI)

**Formula:**

```
VI = NP / avg_power
```

**Source:**
- TrainingPeaks documentation.
- Coggan, A. — Variability index as measure of effort smoothness.

**Assumptions:**
- VI ≈ 1.0: smooth effort (steady state).
- VI > 1.2: variable effort (intervals, climbing).