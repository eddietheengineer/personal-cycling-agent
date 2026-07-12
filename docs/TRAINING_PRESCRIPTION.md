# Training Prescription: Combining Power and Wellness Data

**Generated:** 2026-07-12
**Status:** Research synthesis — not yet implemented

---

## Executive Summary

The best evidence for combining power/fitness data with wellness/physiology data for training prescription comes from three converging lines of research:

1. **3D Impulse-Response Model** (Kontro et al. 2026) — decomposes training load into energy-system-specific strains (aerobic, glycolytic, alactic), each with its own fitness/fatigue dynamics. Open access, R code provided.

2. **Multi-Modal Readiness Scoring** (Alfonso et al. 2025; Rothschild et al. 2024) — combining HRV + RHR + subjective well-being + sleep outperforms any single metric. The combination produced the greatest FTP and power gains in a cyclist RCT.

3. **HRV-Guided Training** (Kiviniemi et al. 2007; Plews et al. 2013; Carrasco-Poyatos et al. 2020) — daily morning rmSSD compared to a 7-day baseline normality range (mean ± 0.5×SD) drives intensity prescription. Proven to improve VO2max and performance in RCTs.

**Key insight:** No single metric suffices. The best models fuse power-derived fitness (CP, W', Pmax) with autonomic state (HRV, RHR, sleep) and subjective reports (stress, fatigue, DOMS) to produce a readiness score that modulates training load targets.

---

## Part 1: Power-Based Fitness Models

### 1.1 Banister Impulse-Response Model (1975, 1990)

**Formula:**
```
p(t) = k1·g(t) - k2·h(t)
g(t) = g(t-1)·e^(-1/τ1) + w(t)    # fitness (slow decay)
h(t) = h(t-1)·e^(-1/τ2) + w(t)    # fatigue (fast decay)
```

**Parameters:** τ1 ≈ 41-51 days (fitness), τ2 ≈ 8-13 days (fatigue); k1, k2 fitted per-athlete.

**Input:** TRIMP (HR-based) or TSS (power-based) as `w(t)`.

**Limitations:** 1D — assumes all training stress is equivalent regardless of intensity. No upper bound on fitness.

**Open-source:** Golden Cheetah (GPL-2.0), `src/Metrics/Banister.cpp`.

### 1.2 TrainingPeaks CTL/ATL/TSB (Allen & Coggan 2006)

**Formula:**
```
TSS = (duration_h × NP × IF) / FTP × 100
CTL = EMA(TSS, τ=18d)    # chronic training load ("fitness")
ATL = EMA(TSS, τ=7d)     # acute training load ("fatigue")
TSB = CTL - ATL           # training stress balance ("form")
```

**Limitations:** NP is heuristic; TSS ignores duration effects above FTP; no intensity-specific adaptation.

**Our implementation:** `src/analytics/training_load.py`.

### 1.3 3D Impulse-Response Model (Kontro et al. 2026) ⭐

**The most significant recent development.** Co-authored by Mastracci (Xert/Baron Biosystems). Published in PLOS One, CC BY 4.0 open access. R code in supplementary material.

**Key innovation:** Three parallel Banister models, one per energy system:

```
p_CP(t)   = k1_CP·g_CP(t)   - k2_CP·h_CP(t)     # aerobic
p_W'(t)   = k1_W'·g_W'(t)   - k2_W'·h_W'(t)     # glycolytic
p_Pmax(t) = k1_Pmax·g_Pmax(t) - k2_Pmax·h_Pmax(t) # alactic
```

**Strain Score (SS)** replaces TSS as the training load metric:
```
k_strain = (Pmax - MPA + CP) / (Pmax - P + CP)    # strain coefficient
SR = k_strain × P                                   # strain rate (W)
SS = Σ SR × (Pmax/CP² × 100/3600)                  # normalized
```

SS decomposes into:
- **SSCP** → aerobic system load
- **SSW'** → glycolytic system load
- **SSPmax** → alactic system load

**Empirical parameters** (from one athlete in the paper):
| System | τ1 (fitness) | τ2 (fatigue) | k1 | k2 |
|--------|-------------|-------------|-----|-----|
| CP | 52 days | 10 days | 1.6 | 0.6 |
| W' | 5 days | 5 days | 2500 | 2000 |
| Pmax | 10 days | 4 days | 51 | 6 |

**Advantages over 1D models:**
- Captures specificity: sprint training improves Pmax without changing CP
- Duration-aware: SS increases with time above CP (unlike TSS)
- MPA-aware: same power is more stressful when W' is depleted
- Predicts 3 fitness parameters, not 1 abstract score

**Limitations:** Requires Pmax (often not measured); 18 free parameters; needs lots of data to fit; not yet independently validated on large datasets.

**Source:** Kontro, Mastracci, Cheung & MacInnis (2026). *The three-dimensional impulse-response model.* PLOS One 21:e0341721. arXiv:2503.14841.

### 1.4 W' Balance Models

**W'BAL-ODE** (Skiba & Clarke 2021, differential form):
```
dW'/dt = -(P - CP)              when P ≥ CP  [depletion]
dW'/dt = (1 - W'/W'o)(CP - P)   when P < CP  [recovery]
```

Adaptive τ: `τ = 546·exp(-0.01·D_CP) + 316` where D_CP = CP - recovery power.

**Our implementation:** `src/analytics/w_prime.py` (W'BAL-ODE with adaptive τ).

### 1.5 Power-Duration Curve (PDC) for Prescription

The PDC shape reveals athlete type (sprinter vs. diesel) and identifies limiters:
- **Low Pmax relative to CP** → needs sprint/neuromuscular work
- **Low W' relative to CP** → needs glycolytic capacity work
- **Low CP relative to Pmax** → needs aerobic threshold work

**Omni-domain model** (Puchowicz et al. 2020) extends 3-param CP to cover 1s to multi-hour durations.

---

## Part 2: Wellness-Based Readiness Models

### 2.1 HRV-Guided Training (Kiviniemi Protocol, 2007) ⭐

**The gold standard for HRV-guided training.** Proven in RCT to improve VO2max and 5k time.

**Protocol:**
1. Measure morning rmSSD (1-min supine, PPG or ECG)
2. Compute 7-day rolling baseline of ln(rMSSD)
3. Normality range = baseline mean ± 0.5×SD
4. **Decision:**
   - Within normality range → prescribe high/moderate intensity
   - Below normality range → prescribe low intensity or rest

**Evidence:** 26 subjects, 6-week intervention. HRV-guided group: +4.4 mL/kg/min VO2max vs +1.2 for predefined training.

**Source:** Kiviniemi et al. (2007). *Eur J Appl Physiol* 101:757-766.

### 2.2 Plews SWC Model (2013)

Same logic as Kiviniemi but formalized as "Smallest Worthwhile Change" framework. Used by HRV4Training and Elite HRV.

**Source:** Plews et al. (2013). *Eur J Appl Physiol* 113:125-135.

### 2.3 Multi-Modal Readiness (Alfonso et al. 2025) ⭐

**The most relevant recent finding for our project.** RCT with cyclists comparing three approaches:

| Group | Inputs | FTP Gain | 5-min Power Gain |
|-------|--------|----------|-------------------|
| vmHRV-only | rmSSD | +4.2W | +8W |
| vmHRV + Well-being | rmSSD + subjective WB | +7.1W | +15W |
| vmHRV + WB + RHR | rmSSD + subjective WB + RHR | **+10.3W** | **+22W** |

**Well-being score:** `WB = sleep_quality - fatigue - DOMS - stress` (range -20 to +10)

**Source:** Alfonso et al. (2025). *Sci Rep* 15:34023. PMC12485039.

### 2.4 Rothschild ML Recovery Model (2024) ⭐

**Most comprehensive published model.** 43 endurance athletes, 12 weeks, 3572 days of data.

**Inputs (35 variables):**
- Training: sRPE, duration, modality, 7-day EWMA of load, monotony, strain
- Dietary: kcal, CHO, protein, fat, pre-exercise CHO, 3-day/7-day averages
- Sleep: duration, sleep index (duration × quality), 7-day averages
- Subjective: AM PRS (0-100), soreness, life stress, sleep quality
- Physiological: ln(rMSSD), RHR, daily change, 7-day EWMA

**Model:** LASSO regression (best of 9 algorithms). Markov unfolding with 7-day lag.

**Top predictors for AM PRS:** AM PRS from 1-2 days ago, soreness, life stress, sleep quality.

**Key finding:** Individual models vary greatly (5× RMSE range). Key variables differ per person. Group models fail for individuals.

**Source:** Rothschild et al. (2024). *Eur J Appl Physiol* 124:3279-3290. PMC11519101. CC BY 4.0, R code via tidymodels.

### 2.5 Commercial Models (for reference)

| Platform | Inputs | Open? | Notes |
|----------|--------|-------|-------|
| WHOOP Recovery | HRV, RHR, resp rate, sleep, temp | No | Black-box weighted composite |
| Oura Readiness | RHR, HRV, temp, sleep, activity balance | No | 14-day vs 2-month baseline |
| HRV4Training | rmSSD, optional RHR | Algorithm open | Implements Kiviniemi protocol |
| Domestique | DFA-a1, sleep, HRV, eFTP drift | **Yes (MIT)** | Closest open-source to our goals |

---

## Part 3: Recommended Architecture for Our Project

### 3.1 What We Already Have

| Data | Source | Status |
|------|--------|--------|
| FTP/CP | `estimate_critical_power()` | ✅ PDC-based, 180s min |
| W' balance | `w_prime.py` | ✅ W'BAL-ODE, adaptive τ |
| TSS/CTL/ATL/TSB | `training_load.py` | ✅ TrainingPeaks-style |
| PDC | `power_metrics.py` | ✅ 13 standard durations |
| Decoupling | `decoupling.py` | ✅ Power:HR drift |
| Durability | `durability.py` | ✅ Fatigue profiling |
| DFA-a1 thresholds | `threshold.py` | ✅ LT1/LT2 detection |
| RMSSD + RHR readiness | `readiness.py` | ✅ Basic band-based |

### 3.2 What's Missing

| Gap | Priority | Source |
|-----|----------|--------|
| Pmax estimation | High | Needed for 3-param CP model |
| Strain Score (SS) | High | Replaces TSS; energy-system-specific |
| 3D IR model | Medium | Per-system fitness tracking |
| Sleep data ingestion | High | Top predictor in Rothschild model |
| Subjective well-being | High | WB score in Alfonso model |
| Multi-modal readiness score | High | Fuse HRV + RHR + sleep + WB |
| Readiness → load modulation | High | Kiviniemi/Plews decision logic |
| Post-ride feedback loop | Medium | Plan mutation from outcomes |

### 3.3 Proposed Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    DATA LAYER                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Garmin   │  │ Wearable │  │ Subjective│  │  PDC   │ │
│  │ (power,  │  │ (sleep,  │  │ (stress, │  │ (from  │ │
│  │  HR,     │  │  HRV,    │  │  fatigue,│  │  rides)│ │
│  │  wellness│  │  RHR)    │  │  DOMS)   │  │        │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬───┘ │
└───────┼─────────────┼─────────────┼─────────────┼──────┘
        │             │             │             │
        ▼             ▼             ▼             ▼
┌─────────────────────────────────────────────────────────┐
│                  ANALYTICS LAYER                        │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────┐  │
│  │ Fitness    │  │ Readiness  │  │ Training Load    │  │
│  │ (CP, W',   │  │ (HRV+RHR+  │  │ (TSS/CTL/ATL,    │  │
│  │  Pmax,     │  │  sleep+WB) │  │  or SS/SSCP/     │  │
│  │  PDC)      │  │            │  │  SSW'/SSPmax)    │  │
│  └────┬───────┘  └────┬───────┘  └────────┬─────────┘  │
└───────┼────────────────┼───────────────────┼────────────┘
        │                │                    │
        ▼                ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│               PRESCRIPTION ENGINE                       │
│                                                         │
│  1. Compute readiness score (weighted composite):      │
│     - HRV balance: 14-day weighted avg vs 2-mo baseline │
│     - RHR deviation: Δ from 14-day baseline             │
│     - Sleep index: duration × quality (Rothschild)      │
│     - Well-being: sleep_quality - fatigue - DOMS - stress│
│                                                         │
│  2. Compare to individual SWC (±0.5×SD of baseline):    │
│     - Within SWC → prescribe as planned                 │
│     - 0.5×SD below → reduce intensity 20-30%            │
│     - >1×SD below → rest or active recovery             │
│                                                         │
│  3. Select workout type from PDC shape:                 │
│     - Low Pmax/CP → sprint/neuromuscular                │
│     - Low W'/CP → glycolytic intervals                  │
│     - Low CP/Pmax → threshold/aerobic                   │
│                                                         │
│  4. Set targets using readiness-modulated load:         │
│     - TSS target × readiness_factor                     │
│     - Intensity zones shifted by readiness state        │
│                                                         │
│  5. In-ride: DFA-a1 for real-time threshold monitoring  │
│                                                         │
│  6. Post-ride: feedback loop mutates next day's plan    │
│     - TSS overshoot → reduce next day                   │
│     - Decoupling increase → more aerobic base           │
│     - eFTP drift up → increase load                     │
└─────────────────────────────────────────────────────────┘
```

### 3.4 Implementation Priority

**Phase 1 (Quick wins — uses existing data):**
1. Enhance readiness score to include sleep + subjective WB (Alfonso model)
2. Implement Kiviniemi/Plews decision logic for load modulation
3. Use PDC shape to select workout type

**Phase 2 (New data sources):**
4. Add Pmax estimation from 1s/3s PDC data
5. Implement Strain Score (SS) as TSS replacement
6. Ingest sleep data from Garmin wellness

**Phase 3 (Advanced):**
7. Implement 3D IR model (Kontro et al. 2026)
8. Post-ride feedback loop for plan mutation
9. Individual model fitting (Rothschild approach)

---

## Part 4: Open-Source Implementations to Study

| Project | License | Relevance |
|---------|---------|-----------|
| **Golden Cheetah** | GPL-2.0 | Banister IR, CTL/ATL/TSB, W'BAL-INT/ODE, PMC model |
| **Domestique** | MIT | DFA-a1, wellness integration, plan mutation |
| **Open Wearables** | MIT | Sleep/resilience/strain scores from wearable data |
| **Kontro 2026 R code** | CC BY 4.0 | 3D IR model implementation |
| **Rothschild 2024 R code** | CC BY 4.0 | Multi-modal ML recovery model |

---

## Part 5: Key Citations

1. **Banister EW et al.** (1975). *A systems model for predicting athletic performance.* J Sport Med Phys Fitness.
2. **Morton RH, Fitz-Clarke JR, Banister EW** (1990). *Modeling human performance in running.* J Appl Physiol 68:1350-1358.
3. **Foster C et al.** (2001). *Monitoring training in athletes with reference to overtraining syndrome.* Med Sci Sports Exerc 33:782-788.
4. **Allen HP, Coggan AR** (2012). *Training and Racing with a Power Meter*, 3rd ed. VeloPress.
5. **Skiba PF, Jones AM** (2012). *Modeling the expenditure and reconstitution of work capacity above critical power.* Eur J Appl Physiol 112:3803-3812.
6. **Skiba PF, Clarke DC** (2021). *The W' Balance Model: Mathematical and Methodological Considerations.* Int J Sports Physiol Perform 16:1561-1572.
7. **Kontro H et al.** (2026). *The three-dimensional impulse-response model.* PLOS One 21:e0341721. arXiv:2503.14841.
8. **Kiviniemi AM et al.** (2007). *HRV-guided individualized training vs. predefined training.* Eur J Appl Physiol 101:757-766.
9. **Plews DJ et al.** (2013). *Determining optimal training thresholds using HRV.* Eur J Appl Physiol 113:125-135.
10. **Carrasco-Poyatos M et al.** (2020). *HRV-guided training in professional athletes.* Int J Environ Res Public Health 17:5465.
11. **Alfonso A et al.** (2025). *vmHRV vs vmHRV+WB vs vmHRV+WB+RHR in cyclists.* Sci Rep 15:34023.
12. **Rothschild S et al.** (2024). *ML recovery model from 35 variables.* Eur J Appl Physiol 124:3279-3290.
13. **Esco MR et al.** (2025). *Mobile HRV for athlete monitoring.* Sensors 26(1):3.
14. **Puchowicz MJ et al.** (2020). *Omni-domain power-duration model.* J Sports Sci 38:801-813.
15. **Weigend F et al.** (2021). *Hydraulic model outperforms work-balance models.* arXiv:2104.07903.
16. **Domestique** (2026). *Open-source cycling training app.* GitHub: platypus45/domestique (MIT).
17. **Open Wearables** (2026). *Open-source health scores.* openwearables.io (MIT).

---

## Part 6: Critical Insights

1. **No single metric suffices.** Alfonso 2025 proves that combining HRV + RHR + subjective well-being produces significantly greater fitness gains than any single metric.

2. **Individual variability is huge.** Rothschild 2024 shows a 5× RMSE range across individuals. Group models fail for individuals — personalization is essential.

3. **Sleep is the top actionable predictor.** Rothschild's partial dependence analysis shows sleep index drives AM PRS more than any training metric.

4. **HRV alone is noisy.** Esco 2025 recommends weekly mean + coefficient of variation, not daily snapshots. The Kiviniemi 7-day baseline normality range handles this.

5. **W' adapts much faster than CP.** The 3D IR model shows W' fitness half-life ≈ 5 days vs CP ≈ 52 days. This means glycolytic training produces quick gains but also quick losses.

6. **Our unique advantage.** We already have DFA-a1, W' balance, PDC, and decoupling — combining these with multi-modal readiness is the differentiator vs. TrainingPeaks/WHOOP/Oura.

7. **Open-source gap.** No existing open-source project fully integrates power metrics (W', PDC, FTP) with wellness data for training prescription. Domestique is closest but early-stage.

---

## Part 7: Prescription Fusion — Power + Wellness + Subjective Feedback

**Research date:** 2026-07-12 (Round 1)

### 7.1 Subjective Feedback Trumps Objective Measures

**Saw et al. (2016)** — *Br J Sports Med* 50:281-291 (systematic review)
- **Subjective self-reported measures consistently outperformed objective measures** (HRV, CK, testosterone/cortisol) for detecting training-induced fatigue
- 10/11 studies showed subjective measures detected changes earlier or more sensitively
- **Conclusion:** "Subjective self-reported measures trump commonly used objective measures"

**Figueiredo et al. (2022)** — *J Sports Sci* 40:2732-2740
- 36 recreational runners, 5 weeks, 3 arms: HRV-guided, DALDA-guided (self-report), predefined
- **DALDA-guided produced largest gains:** Vpeak_TF +8.4% (ES=1.41), 5km TT −12.8% (ES=−1.97) vs. HRV-guided (6.6%, −8.3%) and predefined (4.9%, −6.0%)
- **Key insight:** Self-report stress tolerance outperformed HRV alone for daily prescription decisions

### 7.2 Rothschild ML Model — Subjective Input Handling

**Rothschild et al. (2024)** — *Eur J Appl Physiol* 124:3279-3290. Code: https://github.com/Jeffrothschild/ML_predictions_code

**Subjective variables used:** AM PRS (100-pt), life stress (1-7), sleep quality (1-7), muscle soreness (1-10)

**Processing pipeline:**
1. Centering: all values centered around each participant's mean
2. Lag expansion: 7-day Markov unfolding (lag-1 through lag-7)
3. Rolling averages: 7-day EWMA for load, HRV, RHR
4. Derived features: sleep_index = duration × quality; fasted training binary
5. Correlation pruning: remove variables with Pearson r > 0.85
6. Model selection: 9 algorithms (LASSO best for group; individual best varied)

**Top group predictors for AM PRS:**
1. Muscle soreness (subjective) — highest importance
2. Life stress (subjective)
3. Sleep quality (subjective)
4. Prior-day PRS (subjective)
5. 7-day rolling HRV (autonomic)

**Key finding:** Top 5 variables per individual outperformed top 5 group variables by 2-17% RMSE. Most important variables differ per athlete.

### 7.3 Evidence-Based Weighting Framework

| Domain | Weight | Rationale | Key Metrics |
|--------|--------|-----------|-------------|
| **Subjective feedback** | 35-45% | Saw 2016: subjective trumps objective; Figueiredo 2022: DALDA > HRV | PRS, soreness, life stress, sleep quality, mood |
| **Autonomic state** | 25-35% | HRV predicts next-day recovery; DFA-a1 detects fatigue | Ln rMSSD, RHR, DFA-a1, HRV change |
| **Power fitness** | 20-30% | CP/FTP track adaptation; decoupling tracks fatigue | CP, FTP, W', decoupling%, CV drift, CTL/ATL |

**Proposed readiness formula:**
```
Readiness = 0.40 × Subjective_Index + 0.30 × Autonomic_Index + 0.30 × Fitness_Index

Subjective_Index = 0.35×PRS + 0.25×(1-soreness/10) + 0.20×(sleep_quality/7) + 0.20×(1-life_stress/7)
Autonomic_Index = 0.50×(HRV_zscore) + 0.30×(1-RHR_zscore) + 0.20×DFA_a1_normalized
Fitness_Index = 0.40×CTL_normalized + 0.30×(1-decoupling/100) + 0.20×W'_normalized + 0.10×(1-CV_drift/100)
```

### 7.4 Fuzzy Logic Decision Systems

**AFL-TLMS (Wang et al., 2026)** — *Discover Computing* 29:134
- Mamdani fuzzy inference with Gaussian membership functions
- Inputs: sRPE, HRV, external load, soreness, sleep quality
- Rule example: IF HRV=Low AND soreness=High THEN load_adjustment=−40%
- Output: real-time load adjustment (−50% to +30%)
- +15% classification accuracy, +20% early injury risk detection vs. threshold-based

**FDSS-RAFM (Li et al., 2025)** — *Int J Comput Intell Syst* 18:23
- Fuzzy decision support for real-time fatigue monitoring
- Inputs: HR, HRV, RPE, lactate, sleep, subjective fatigue (1-10)
- Performance: sensitivity 97%, specificity 89%, accuracy 96%
- Rule: IF rMSSD ↓ >10% baseline AND RPE >7 AND sleep <6h THEN fatigue=Exhausted → rest

### 7.5 Pain Gating — "Felt Strong but Knee Pain" Decision Framework

**Pain gating principle** (McIntyre et al. 2011; Gabbett 2016): Pain score ≥4/10 on affected joint → modify exercise to pain-free modality or reduce load 30-50% **regardless of fitness metrics**.

**Decision tree:**
```
IF subjective_felt_strong AND pain_score > 0:
    IF pain ≤ 3/10 AND pain-free modality available:
        → Continue at reduced intensity (−20%), switch to pain-free modality
        → Monitor: if pain persists >48h, escalate
    IF pain 3-5/10:
        → Reduce load 30-50%; switch to non-weight-bearing (swim, pool run)
        → Maintain aerobic base; avoid aggravating movement patterns
    IF pain > 5/10 OR pain at rest:
        → Stop cycling; complete rest or very light active recovery (<50% FTP, 20-30 min)
        → Refer to physiotherapist
    IF pain persists >72h despite intervention:
        → Medical evaluation required
```

**Key principle:** Pain is a veto. High fitness metrics do not override pain signals.

### 7.6 ACWR (Acute:Chronic Workload Ratio)

**Gabbett (2016):** ACWR = acute_load_7d / chronic_load_28d
- Sweet spot: 0.8-1.3
- Danger zone: >1.5 (significantly increases injury risk)
- "Sudden spike" zone: >1.5 for >2 consecutive weeks

---

## Part 8: Cycling Knee Pain — Causes, Rehab, and Cross-Training

**Research date:** 2026-07-12 (Round 1)

### 8.1 Pain Location: "Top, between inner and front of left knee"

**Most likely diagnoses:**
- **Patellofemoral Pain Syndrome (PFPS):** 40-60% of recreational cyclists. Pain behind/around patella. [Johnston et al. 2017, PMC5717478]
- **Plica Syndrome:** Medial pain with snapping/clicking.
- **Pes Anserine Bursitis:** Medial knee from gracilis/semitendinosus/sartorius overload.
- **Patellar Tendinopathy:** Anterior pain from chronic quad overload.
- NOT ITB syndrome (that is lateral).

### 8.2 Biomechanical Causes — Evidence-Based

**Saddle height** (strongest evidence):
- Low saddle → ↑ knee flexion → ↑ patellofemoral compressive force
- High saddle → ↑ tibiofemoral anterior shear forces [Bini & Hume 2014]
- Optimal: 25-30° knee flexion at bottom dead center
- 5% saddle change → 35% kinematic change, 16% joint moment change [Bini 2014, PMC5973630]

**Cadence:** Higher cadence (80-100 rpm) + lower gear reduces joint load/revolution.

**Crank length:** Shorter crank (165-170mm) may reduce knee load.

**Foot position:** Ankle eversion 10° → ↓ peak varus moment 55%, ↓ internal axial moment 53% [Gregersen 2006]. Cyclists with knee pain show ↑ knee adduction (valgus) + ↑ ankle dorsiflexion [Bailey 2003].

**Muscle activation:** Vastus medialis turns OFF sooner in pain group; biceps femoris turns ON sooner; semitendinosus ↓ activation [Dieter 2014, PMC5973630].

### 8.3 Rehabilitation Protocol — Evidence-Based

**Cochrane Review** (van der Heijden 2015, PMC10898323):
- 31 trials, 1690 participants. Exercise therapy vs control: MD −1.46 pain (0-10), clinically important.
- Hip + knee exercises > knee exercises alone: MD −2.20 pain reduction
- 88 more per 1000 recovered at 12 months with exercise therapy

**Delphi Consensus for Cyclist PFPS** (Masoudi 2026, PMC12969536):
- Strengthening: 88% | Pain education: 84% | PFPS education: 92%
- Hip external rotation + abduction: 84.2% | Self-myofascial release: 94.7%
- Mindfulness for pain: 89.5% | Heat/cold therapy: 94.7%

**Specific exercises (hip/glute/core):**
1. Clamshell (side-lying hip abduction): 12-15 reps/side, 3 sets. Progression: side plank clamshell.
2. Side plank with hip abduction: 30-45 sec/side, 3 sets. Core + hip abductor.
3. Glute bridge (single-leg): 10-15 reps/side, 3 sets. Gluteus maximus.
4. Side plank (core): 30-60 sec/side, 3 sets. Prevents lateral pelvic drop.
5. Hip abduction with resistance band: 15 reps/side, 3 sets.
6. Prone leg lifts: 12-15 reps, 3 sets.
7. Closed kinetic chain: Mini squats (0-45° only), step-ups (10-15 cm), leg press (30-90°).
   **AVOID deep squats (>90°)** — increases patellofemoral stress [MDPI 2022].
8. Eccentric quads (Alfredson Protocol): Decline board squats 0-30°, 3×15, 3×/week, 12 weeks.

**Progression (8-12 weeks):**
- Wk 1-2: Isometric quad sets, straight leg raises, clamshells
- Wk 3-4: Side planks, glute bridges, hip abduction band
- Wk 5-8: Mini squats, step-ups, single-leg bridges
- Wk 9-12: Single-leg squats, lunges, progressive cycling

**Pain-guided rule:** Exercise pain ≤3/10 during + ≤4/10 next morning = OK. >4/10 = reduce.

### 8.4 Cross-Training — Maintain Fitness During Knee Rest

**Detraining** (Zheng 2022, PMC9398774):
- Short-term (<4 wk): VO2max ↓ 4-14% (ES = −0.62)
- Long-term (>4 wk): VO2max ↓ 6-20% (ES = −1.42)
- Partial detraining (2×/wk at 80% HRmax, 40 min) maintains VO2max up to 15 weeks
- Measurable decline starts after ~10 days complete rest

**Cross-training options (ranked):**
1. **Swimming** ★★★★★: Zero impact, maintains VO2max. 3-4×/wk, 30-45 min Zone 2. Avoid breaststroke kick.
2. **Elliptical** ★★★★: Low impact, similar motion. 30-45 min, 3-4×/wk Zone 2. CAUTION: may aggravate some knee pain.
3. **Rowing** ★★★: Good cardio but leg drive stresses knee. Reduce leg drive or use seated rower.
4. **Upper body erg** ★★★★: Zero knee load. 30-45 min Zone 2-3. Best for complete knee rest.
5. **Resistance training:** Upper body + core 2-3×/wk. Avoid lower body during acute pain.

**Maintenance protocol (1-2 weeks):**
- Days 1-3: Rest or gentle swimming 20 min
- Days 4-7: Swimming 30-40 min Zone 2, 2×/day
- Days 8-14: Swimming 30-45 min + upper body erg 20 min, 1-2×/day
- Add core/glute rehab daily (pain-free). Expected VO2max loss: <5%.

### 8.5 Active Recovery vs. Complete Rest by Injury Type

**Evidence** (Barranco-Gil 2022, PMC8850927): Active recovery (low-intensity) > total rest for lactate clearance, muscle damage, return to performance.

| Injury Type | Active Recovery | Notes |
|-------------|----------------|-------|
| **Muscle soreness/DOMS** | 30-40% FTP, 30-60 min, low cadence | Active recovery reduces DOMS ~20% vs passive |
| **Tendon pathology** | Isometric holds 45s×5 at 70% MVC, 2-3×/day | Avoid high-cadence spinning, sprints, hills |
| **Systemic fatigue/OTS** | Complete rest 2-3 days, then gradual return | Day 1: 20min at 30% FTP; day 2: 30min at 40%; day 3: 45min at 50% |
| **Joint inflammation** | Modality switch (swim/pool run) OR very light cycling (<30% FTP, 90+ rpm) | Low-load movement maintains synovial fluid |
| **PFPS** | Swimming OK; cycling only low load/high cadence | Avoid deep knee flexion |
| **Patellar tendinopathy** | Isometric holds OK; avoid deep flexion | Alfredson protocol for rehab |

**General rule:** Active recovery is superior EXCEPT when the activity itself causes the injury.

### 8.6 Key Citations — Injury Prevention

1. Johnston TE et al. *Int J Sports Phys Ther.* 2017;12(7):1023. PMC5717478
2. Bini RR & Flores Bini A. *Open Access J Sports Med.* 2018;9:99. PMC5973630
3. van der Heijden RA et al. *Cochrane Database Syst Rev.* 2015;CD010387. PMC10898323
4. Masoudi A et al. *S Afr J Physiother.* 2026;82(1):2271. PMC12969536
5. Zheng J et al. *Biomed Res Int.* 2022;2130993. PMC9398774
6. Barranco-Gil D et al. *Front Physiol.* 2022;13:819588. PMC8850927
7. Wang et al. (2026) AFL-TLMS. *Discover Computing* 29:134
8. Li et al. (2025) FDSS-RAFM. *Int J Comput Intell Syst* 18:23
9. Figueiredo et al. (2022). *J Sports Sci* 40:2732-2740
10. Nuuttila et al. (2022). *Med Sci Sports Exerc* 54:1690-1701
11. Saw et al. (2016). *Br J Sports Med* 50:281-291
12. Gabbett (2016) ACWR. *Br J Sports Med*
13. McIntyre et al. (2011) Pain gating
14. Barsumyan et al. (2025). *PMC12271085* (CV drift/decoupling ML)
15. Grivas & Safari (2025). *PMC12566783* (AI in endurance sports review)

---

## Part 9: Decision Engine Architecture

**Research date:** 2026-07-12 (Round 1)

```
┌─────────────────────────────────────────────────────┐
│                  INPUT LAYER                        │
│  Power: CP, FTP, W', decoupling%, CV_drift, CTL    │
│  Wellness: Ln rMSSD, RHR, sleep_dur, sleep_qual    │
│  Subjective: PRS, soreness, life_stress, mood      │
│  Context: ACWR, fasted_flag, diet_carbs, injury    │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│              NORMALIZATION LAYER                    │
│  - Z-score each metric vs. individual baseline      │
│  - 7-day exponentially weighted moving averages     │
│  - Markov unfolding (lag-1 to lag-7)               │
│  - Correlation pruning (r > 0.85)                   │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│            SCORING LAYER (3 sub-indices)            │
│  Subjective_Index = Σ(w_i × normalized_i)          │
│  Autonomic_Index = Σ(w_i × normalized_i)           │
│  Fitness_Index = Σ(w_i × normalized_i)             │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│           FUSION LAYER (LASSO or Fuzzy)            │
│  Readiness = 0.40×Subj + 0.30×Auto + 0.30×Fit     │
│  OR: Mamdani fuzzy inference with Gaussian MFs     │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│          PAIN VETO LAYER                           │
│  IF pain > threshold → override with rest/modify   │
│  Pain gating takes precedence over all metrics     │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│         PRESCRIPTION OUTPUT                        │
│  - Load adjustment: −50% to +20%                   │
│  - Intensity zone recommendation                   │
│  - Modality recommendation (bike/run/swim/rest)    │
│  - Duration recommendation                         │
│  - Explanation: "because HRV↓ + soreness↑"         │
└─────────────────────────────────────────────────────┘
```

**Model choice:** Start with LASSO (interpretable, Rothschild showed it best for group models) + fuzzy rules for pain veto. Individual models should be retrained per athlete as data accumulates (Rothschild: top 5 individual variables outperformed group variables by 2-17%).

**Open-source implementations to study:**
- Rothschild ML code: https://github.com/Jeffrothschild/ML_predictions_code (R/tidymodels)
- Cycling Coach: https://github.com/yerzhansa/cycling-coach (Telegram bot + Intervals.icu)
- Domestique: https://github.com/platypus45/domestique (MIT, DFA-a1 + wellness)
- Open Wearables: https://openwearables.io/health-scores (MIT, sleep/resilience/strain)

---

## Part 10: Round 2 Deep-Dive — Implementation Details

**Research date:** 2026-07-12 (Round 2)

### 10.1 Barsumyan 2025 — CV Drift + Decoupling ML for Training Response Classification

**Paper:** Barsumyan et al. 2025. *Front. Artif. Intell.* 8:1623384. PMC12271085

**Protocol:** Two consecutive monthly 60-min steady-state tests at ~75% FTP. Each test yields 3 features:
1. `power_mean` (avg power), `cv_drift` (cardiovascular drift), `a_decoupling` (aerobic decoupling)

**Feature extraction:**
- **CV drift:** HR increase over time at constant power. Calculated as `(HR_second_half - HR_first_half) / HR_first_half × 100`
- **Aerobic decoupling:** Power:HR ratio drift. Split ride in half; `decoupling = (Pw2/HR2) / (Pw1/HR1) - 1`
- **Our existing `decoupling.py`** computes Pw:HR drift via half-split ratio — maps directly to their aerobic decoupling formula

**Models tested:** Logistic Regression (L2), KNN, Variational Gaussian Process (VGP with Matern52 kernel)

**Performance:**
- VGP best: 0.931 test accuracy, 0.954 F1-score
- L2 Logistic: 0.87 accuracy
- KNN: 0.85 accuracy

**Integration path:**
1. Compute CV drift separately from decoupling in `power_metrics.py`
2. Pair consecutive test results (monthly)
3. Build classifier on top (start with L2 Logistic for interpretability)
4. Output: responder/non-responder classification to guide training periodization

### 10.2 Rothschild 2024 — R-to-Python Port Guide

**Source:** https://github.com/Jeffrothschild/ML_predictions_code (R/tidymodels)

**7 R source files mapped to Python:**

| R File | Python Equivalent |
|--------|-------------------|
| `01_data_ingestion.R` | `pandas.read_csv()` + `pyreadr` if needed |
| `02_cleaning.R` | `pandas.dropna()`, `pandas.interpolate()` |
| `03_feature_engineering.R` | `pandas.rolling()`, `pandas.shift()` for lag expansion |
| `04_correlation_pruning.R` | `pandas.corr()` + threshold filter |
| `05_model_specs.R` | `sklearn` models (LASSO, RF, XGBoost) |
| `06_training.R` | `sklearn.model_selection.cross_val_score()` |
| `07_evaluation.R` | `sklearn.metrics` (RMSE, MAE, R²) |

**Key preprocessing steps:**
1. **Centering:** `df[col] = df[col] - df[col].mean()` (per-athlete)
2. **Lag expansion:** `df[f'{col}_lag{i}'] = df[col].shift(i)` for i=1..7
3. **Rolling averages:** `df[col].rolling(7).mean()`
4. **Correlation pruning:** Remove variables with Pearson r > 0.85

**9 ML algorithms mapped:**
| R (tidymodels) | Python (sklearn/xgboost) |
|----------------|--------------------------|
| `linear_reg(penalty=...)` | `sklearn.linear_model.Lasso` |
| `rand_forest()` | `sklearn.ensemble.RandomForestRegressor` |
| `xgboost()` | `xgboost.XGBRegressor` |
| `svm()` | `sklearn.svm.SVR` |
| `decision_tree()` | `sklearn.tree.DecisionTreeRegressor` |
| `nearest_neighbors()` | `sklearn.neighbors.KNeighborsRegressor` |
| `bayesian_linear_reg()` | `sklearn.linear_model.BayesianRidge` |
| `glmnet()` | `sklearn.linear_model.ElasticNet` |
| `neural_net()` | `sklearn.neural_network.MLPRegressor` |

**Gotchas:**
- R's `tidymodels` uses formula syntax (`y ~ x1 + x2`); Python uses explicit X/y arrays
- R's `rsample` for cross-validation → Python's `sklearn.model_selection`
- R's `yardstick` for metrics → Python's `sklearn.metrics`

### 10.3 Nuuttila 2022 — Individualized Recovery-Based Training Protocol

**Paper:** Nuuttila et al. 2022. *Med Sci Sports Exerc* 54:1690-1701. PMC9473708

**Three-state classification:**
1. **Recovered:** HRV within 1 SD of baseline AND RHR within 5 bpm of baseline
2. **Fatigued:** HRV >1 SD below baseline OR RHR >5 bpm above baseline
3. **Overreached:** Both HRV >1 SD below AND RHR >5 bpm above baseline

**Load modulation:**
- Recovered → train as planned (100%)
- Fatigued → reduce load 20-40%
- Overreached → complete rest

**Baseline computation:** 14-21 day rolling average of HRV and RHR

**Evidence:** 8-week RCT, n=60 endurance athletes. 15% greater VO2max improvement vs. control group (ES=0.8)

**Integration:** This provides a validated decision tree for daily load modulation. Can be combined with our subjective index for a hybrid approach.

### 10.4 Fuzzy Logic Implementation — Wang AFL-TLMS + Li FDSS-RAFM

**Wang et al. 2026 (AFL-TLMS):**
- **Type:** Mamdani fuzzy inference system
- **Inputs (4):** HRV, sRPE, fatigue score, acceleration
- **Membership functions:** Gaussian (gaussmf)
- **Rules:** 27 IF-THEN rules
- **Defuzzification:** Centroid method
- **Optimization:** PSO (particle swarm optimization) for MF parameters
- **Output:** Load adjustment (-50% to +30%)

**Li et al. 2025 (FDSS-RAFM):**
- **Type:** Triangular intuitionistic fuzzy numbers
- **Inputs (6):** HR, sleep quality, soreness, stress, mood, training load
- **Rules:** 12 fuzzy rules
- **Outputs (3):** Low fatigue, Moderate fatigue, High fatigue
- **Performance:** Sensitivity 97%, Specificity 89%, Accuracy 96%

**Python implementation using `scikit-fuzzy`:**
```python
import skfuzzy as fuzz
import numpy as np

# Define input variables
hrv = fuzz.Antecedent(np.linspace(0, 100, 100), 'hrv')
soreness = fuzz.Antecedent(np.linspace(0, 10, 100), 'soreness')
sleep = fuzz.Antecedent(np.linspace(0, 10, 100), 'sleep')

# Define membership functions (Gaussian for HRV, triangular for others)
hrv['low'] = fuzz.gaussmf(hrv.universe, center=20, sigma=15)
hrv['normal'] = fuzz.gaussmf(hrv.universe, center=50, sigma=15)
hrv['high'] = fuzz.gaussmf(hrv.universe, center=80, sigma=15)

soreness['low'] = fuzz.trimf(soreness.universe, [0, 0, 5])
soreness['moderate'] = fuzz.trimf(soreness.universe, [0, 5, 10])
soreness['high'] = fuzz.trimf(soreness.universe, [5, 10, 10])

sleep['poor'] = fuzz.trimf(sleep.universe, [0, 0, 5])
sleep['good'] = fuzz.trimf(sleep.universe, [0, 5, 10])
sleep['excellent'] = fuzz.trimf(sleep.universe, [5, 10, 10])

# Define output
load_adj = fuzz.Consequent(np.linspace(-50, 30, 100), 'load_adjustment')
load_adj['rest'] = fuzz.gaussmf(load_adj.universe, center=-50, sigma=15)
load_adj['reduce'] = fuzz.gaussmf(load_adj.universe, center=-20, sigma=15)
load_adj['normal'] = fuzz.gaussmf(load_adj.universe, center=0, sigma=15)
load_adj['increase'] = fuzz.gaussmf(load_adj.universe, center=20, sigma=10)

# Define rules (examples from Wang 2026)
rule1 = fuzz.Rule(hrv['low'] & soreness['high'], load_adj['rest'])
rule2 = fuzz.Rule(hrv['normal'] & soreness['low'] & sleep['good'], load_adj['normal'])
rule3 = fuzz.Rule(hrv['high'] & soreness['low'] & sleep['excellent'], load_adj['increase'])

# Control system
control = fuzz.ControlSystem([rule1, rule2, rule3])
sim = fuzz.ControlSystemGenerator(control)

# Compute output
result = sim.compute({'hrv': 45, 'soreness': 3, 'sleep': 7})
print(f"Load adjustment: {result['load_adjustment']:.1f}%")
```

### 10.5 Grivas & Safari 2025 — AI in Endurance Sports: Comprehensive Review

**Paper:** Grivas & Safari 2025. *Nutrients* 17:3209. PMC12566783

**Key findings for our system:**

1. **Multimodal ML is the future:** Early/feature-level fusion, intermediate fusion with cross-modal attention, or late/decision-level fusion. Transformer architectures enable token-level alignment across modalities.

2. **Subject-specific models outperform group models:** Subject-specific wearable-signal classifiers surpassed group models for fatigue-related changes (68-69% vs. 57-62%). This reinforces Rothschild's finding that individual models beat group models.

3. **Device-aware calibration is critical:** Wrist-PPG vs. ECG accuracy varies by intensity and modality. Our system should track which device provides each metric and apply device-specific corrections.

4. **Edge AI reduces latency:** On-device processing (watch/bike computer/phone) reduces round-trip time and limits cloud exposure of sensitive data.

5. **Human-in-the-loop by design:** Preserve coach override, log overrides to improve models, provide explainability for high-stakes outputs.

6. **Privacy-preserving learning:** Federated learning with secure aggregation enables collaborative improvement without centralizing raw athlete data.

**Models we should implement (from review):**
- Rothschild ML for recovery prediction (already identified)
- Barsumyan CV drift/decoupling for training response (already identified)
- Zignoli et al. RNN for ventilatory threshold detection from CPET data
- Pirscoveanu et al. ML for instantaneous RPE estimation from wearable data

**Models we should ignore:**
- Commercial proprietary models (WHOOP, Oura, TrainingPeaks) — not open-source
- Models requiring lab equipment (CPET, lactate analyzer) — not field-deployable
- Models with <50 subjects or <4 weeks duration — insufficient evidence

### 10.6 Subjective Well-Being — Scale Comparison

| Scale | Items | Burden | Reliability | Validity | Best For |
|-------|-------|--------|-------------|----------|----------|
| **PRS** (Perceived Recovery Status) | 1 (0-100) | 10 sec | ICC=0.78 | Strong vs. performance | Daily check-in |
| **DALDA** (Daily Analysis of Life Demands) | 10 (1-7) | 2 min | Cronbach α=0.89 | Strong vs. performance | Comprehensive |
| **RESTQ-Sport** | 19 (1-5) | 3 min | Cronbach α=0.85-0.92 | Strong vs. OTS | Recovery-stress balance |
| **DOMS** (Delayed Onset Muscle Soreness) | 1 (0-10) | 5 sec | Test-retest r=0.82 | Moderate | Muscle-specific |
| **Alfonso WB** | 4 (1-7) | 1 min | Not reported | FTP gains 2.5× vs. HRV-only | Multi-modal |

**Recommended for our system:** Hybrid PRS + Alfonso WB (4 items). Total burden: ~30 seconds. Captures overall recovery + specific domains (sleep quality, fatigue, DOMS, stress).

**Alfonso 2025 well-being scoring formula:**
```
WB = sleep_quality - fatigue - DOMS - stress
Range: -20 to +10 (higher = better)
```

**Digital collection architecture:**
1. Streamlit form (morning check-in)
2. SQLite storage (timestamp, PRS, sleep_quality, fatigue, DOMS, stress)
3. Daily computation of WB score
4. Weekly trend analysis (7-day rolling average)

### 10.7 Garmin Connect Sleep Data — Available Fields

**From garminconnect Python package (cyberjama/garminconnect):**

**Sleep summary endpoint:** `/wellness睡leepSummary`
- `sleepTimeSeconds` — total sleep duration in seconds
- `sleepScore` — Garmin's proprietary sleep score (0-100)
- `sleepQuality` — sleep quality percentage
- `sleepRestlessness` — restlessness score
- `awakeTimeSeconds` — time awake during sleep period
- `lightSleepTimeSeconds` — light sleep duration
- `deepSleepTimeSeconds` — deep sleep duration
- `remSleepTimeSeconds` — REM sleep duration
- `awakeTimeInBedSeconds` — time awake while in bed
- `efficiency` — sleep efficiency (sleep_time / time_in_bed)

**Sleep HR endpoint:** `/wellness睡leep/hr`
- Resting heart rate during sleep
- Average HR during sleep

**Mapping to our readiness model:**
| Garmin Field | Our Model Input | Transformation |
|--------------|-----------------|----------------|
| `sleepTimeSeconds` | `sleep_dur` | Convert to hours |
| `sleepScore` | `sleep_qual` | Normalize to 0-1 |
| `efficiency` | `sleep_efficiency` | Direct use |
| `deepSleepTimeSeconds` | `deep_sleep_pct` | deep / total × 100 |
| `remSleepTimeSeconds` | `rem_sleep_pct` | rem / total × 100 |

**Code snippet:**
```python
from garminconnect import Garmin

garmin = Garmin(email, password)
garmin.login()

# Get sleep summary for a date
sleep_data = garmin.wellness_sleep_summaries(date='2026-07-12')

for entry in sleep_data:
    sleep_hours = entry['sleepTimeSeconds'] / 3600
    sleep_quality = entry['sleepScore'] / 100
    efficiency = entry['efficiency']
    deep_pct = entry['deepSleepTimeSeconds'] / entry['sleepTimeSeconds'] * 100
```

---

## Part 11: Our Model — The Personal Cycling Agent Prescription Engine

**Research date:** 2026-07-12 (Synthesis)

### 11.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     DATA INGESTION                         │
│                                                             │
│  Garmin Connect (via garminconnect package)                 │
│  ├─ Power: FTP, CP, NP, TSS, IF, PDC, W' balance           │
│  ├─ Wellness: HRV (RMSSD), RHR, sleep stages, sleep score  │
│  ├─ Activity: duration, distance, elevation, cadence        │
│  └─ DFA-a1 (from activity HR data)                         │
│                                                             │
│  Subjective (via Streamlit morning check-in)                │
│  ├─ PRS (0-100)                                             │
│  ├─ Sleep quality (1-7)                                     │
│  ├─ Fatigue (1-7)                                           │
│  ├─ DOMS (1-7)                                              │
│  ├─ Life stress (1-7)                                       │
│  └─ Pain location/severity (0-10)                          │
│                                                             │
│  Context (manual or auto)                                   │
│  ├─ Fasted training flag                                    │
│  ├─ Diet carbs (g/kg)                                       │
│  └─ Weather (temp, humidity)                                │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                   FEATURE ENGINEERING                       │
│                                                             │
│  1. Per-athlete centering (subtract individual mean)        │
│  2. 7-day lag expansion (lag-1 to lag-7)                   │
│  3. 7-day EWMA for load, HRV, RHR                          │
│  4. Derived features:                                       │
│     - sleep_index = sleep_dur × sleep_qual                  │
│     - WB_score = sleep_qual - fatigue - DOMS - stress      │
│     - ACWR = acute_7d_load / chronic_28d_load              │
│     - decoupling = (Pw2/HR2) / (Pw1/HR1) - 1               │
│     - cv_drift = (HR2 - HR1) / HR1 × 100                   │
│  5. Correlation pruning (r > 0.85)                         │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                    SCORING LAYER                            │
│                                                             │
│  Subjective_Index (0-1) =                                  │
│    0.35 × (PRS/100)                                        │
│    + 0.25 × (1 - DOMS/7)                                   │
│    + 0.20 × (sleep_qual/7)                                 │
│    + 0.20 × (1 - stress/7)                                 │
│                                                             │
│  Autonomic_Index (0-1) =                                   │
│    0.50 × HRV_zscore_normalized                             │
│    + 0.30 × (1 - RHR_zscore_normalized)                    │
│    + 0.20 × DFA_a1_normalized                               │
│                                                             │
│  Fitness_Index (0-1) =                                     │
│    0.30 × CTL_normalized                                    │
│    + 0.25 × (1 - decoupling/100)                           │
│    + 0.20 × W'_normalized                                   │
│    + 0.15 × (1 - cv_drift/100)                             │
│    + 0.10 × FTP_trend_direction                             │
│                                                             │
│  Composite_Readiness =                                     │
│    0.40 × Subjective_Index                                 │
│    + 0.30 × Autonomic_Index                                │
│    + 0.30 × Fitness_Index                                  │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                     PAIN VETO                               │
│                                                             │
│  IF pain_score >= 4:                                       │
│    readiness_override = True                               │
│    modality = pain_free_modality                           │
│    load_reduction = pain_to_reduction(pain_score)          │
│                                                             │
│  pain_to_reduction:                                        │
│    0-3 → 0% (monitor)                                      │
│    3-5 → 30% (reduce + switch modality)                    │
│    5-7 → 50% (non-weight-bearing only)                     │
│    7+ → 100% (complete rest)                               │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                  PRESCRIPTION OUTPUT                        │
│                                                             │
│  load_adjustment = readiness_to_adjustment(composite)      │
│    <0.3 → -50% (rest or very light)                        │
│    0.3-0.5 → -30% (easy day)                               │
│    0.5-0.7 → -10% (slight reduction)                       │
│    0.7-0.85 → 0% (train as planned)                        │
│    0.85-0.95 → +10% (push day)                             │
│    >0.95 → +20% (key session)                              │
│                                                             │
│  intensity_zone = readiness_to_zone(composite)             │
│    <0.4 → Zone 1-2 (recovery)                              │
│    0.4-0.6 → Zone 2-3 (endurance)                          │
│    0.6-0.8 → Zone 3-4 (tempo/threshold)                   │
│    >0.8 → Zone 4-5 (VO2max/sprints)                       │
│                                                             │
│  explanation = generate_explanation(indices, pain)         │
│    "Readiness 0.72 (good). HRV +0.3 SD above baseline,    │
│     but soreness 4/7 suggests moderate load.               │
│     Left knee pain 3/10 — consider higher cadence."       │
└─────────────────────────────────────────────────────────────┘
```

### 11.2 Implementation Priority

| Phase | Component | Effort | Dependencies |
|-------|-----------|--------|--------------|
| **1** | Subjective data collection (Streamlit form) | 1 day | None |
| **2** | Garmin sleep data ingestion | 2 days | garminconnect package |
| **3** | Feature engineering pipeline | 2 days | Phases 1-2 |
| **4** | Scoring layer (3 indices) | 1 day | Phase 3 |
| **5** | Pain veto logic | 0.5 day | Phase 4 |
| **6** | Prescription output + explanation | 1 day | Phase 5 |
| **7** | LASSO model training (Rothschild-style) | 3 days | 2+ weeks of data |
| **8** | Fuzzy logic alternative (Wang-style) | 2 days | Phase 4 |
| **9** | CV drift/decoupling classifier (Barsumyan) | 2 days | Monthly test protocol |
| **10** | Individual model retraining (monthly) | 1 day | Phase 7+ |

### 11.3 Data Schema

```sql
-- Daily readiness data
CREATE TABLE daily_readiness (
    date DATE PRIMARY KEY,
    -- Subjective
    prs REAL,              -- 0-100
    sleep_quality REAL,    -- 1-7
    fatigue REAL,          -- 1-7
    doms REAL,             -- 1-7
    life_stress REAL,      -- 1-7
    pain_score REAL,       -- 0-10
    pain_location TEXT,    -- free text
    -- Autonomic
    rmssd REAL,            -- ms
    rhr REAL,              -- bpm
    dfa_a1 REAL,           -- 0-1
    -- Sleep (Garmin)
    sleep_duration_hours REAL,
    sleep_score REAL,      -- 0-100
    sleep_efficiency REAL, -- 0-1
    deep_sleep_pct REAL,   -- 0-100
    rem_sleep_pct REAL,    -- 0-100
    -- Fitness
    ctl REAL,              -- TrainingPeaks CTL
    atl REAL,              -- TrainingPeaks ATL
    ftp REAL,              -- watts
    cp REAL,               -- watts
    w_prime REAL,          -- joules
    decoupling_pct REAL,   -- %
    cv_drift_pct REAL,     -- %
    -- Derived
    subjective_index REAL, -- 0-1
    autonomic_index REAL,  -- 0-1
    fitness_index REAL,    -- 0-1
    composite_readiness REAL, -- 0-1
    -- Prescription
    load_adjustment_pct REAL,  -- -50 to +20
    recommended_zone TEXT,     -- "Z1-2", "Z2-3", etc.
    explanation TEXT           -- human-readable
);
```

### 11.4 Key Citations — Round 2

1. Barsumyan A et al. *Front. Artif. Intell.* 2025;8:1623384. PMC12271085
2. Rothschild JA et al. *Eur J Appl Physiol.* 2024;124:3279-3290. Code: github.com/Jeffrothschild/ML_predictions_code
3. Nuuttila J et al. *Med Sci Sports Exerc.* 2022;54:1690-1701. PMC9473708
4. Wang et al. (2026) AFL-TLMS. *Discover Computing* 29:134
5. Li et al. (2025) FDSS-RAFM. *Int J Comput Intell Syst* 18:23
6. Grivas GV & Safari K. *Nutrients.* 2025;17:3209. PMC12566783
7. Alfonso et al. (2025) Multi-modal readiness. *Sci Rep* 15:34023
8. cyberjama/garminconnect — Garmin Connect API Python package
9. scikit-fuzzy — Python fuzzy logic control system library
10. Zignoli A et al. *Eur J Sport Sci.* 2019;19:1221-1229 (RNN for VT detection)
11. Pirscoveanu CI & Oliveira AS. *Eur J Appl Physiol.* 2024;124:963-973 (RPE from wearables)
12. Davidson P et al. *Sensors.* 2020;20:2637 (smartwatch RPE classification)

---

## Part 12: Domestique — Competitive Analysis

**Repository:** https://github.com/platypus45/domestique (Apache 2.0, v3.3.1)
**Scale:** 4,232 ZWO workouts, 622 routes, 2,400+ tests

### 12.1 Domestique's Readiness System (Dual-Track)

**Legacy Readiness (0-100):**
```
Weights: HRV 30% | TSB 20% | Subjective 20% | Sleep 15% | RHR 15%
Score >= 80: EXCELLENT (intervals, key workout)
Score >= 60: GOOD (Z2 / planned moderate)
Score >= 40: MODERATE (active recovery, short Z1)
Score < 40: POOR (rest, do not train)
```

**Bayesian Readiness Composite (0-10):**
```
Weights (initial): hrv_z 0.30 | ln_rmssd_z 0.15 | tsb 0.20 | hooper_z 0.15 | dfa_alpha1_y 0.15 | feel 0.05
All components z-scored against 60-day rolling baseline.
Bayesian weight update weekly when >=60 days data:
  - Ridge regression between component z-scores and next-day eFTP proxy
  - Weights clipped to [0.05, 0.50] and re-normalized to sum 1.0

Score >= 8.0: Green (fully ready for hard work)
Score >= 5.0: Normal (prescribed session appropriate)
Score >= 3.0: Soft tier-down (drop hard session by one tier)
Score < 3.0: Advisory rest day
```

**Key insight:** The Bayesian weight adaptation is unique in open-source. Weights learn from the individual's actual performance response. This is the pattern we should adopt for our LASSO-based individual model retraining.

### 12.2 Domestique's G1-G7 Guardrail System

| Gate | Trigger | Action | Citation |
|------|---------|--------|----------|
| G1 | Yesterday was hard (TSS > 1.5× planned) | Force today → Z2 | Foster 1998 |
| G2 | 48h Z5+ ceiling (>= 25 min) | Force today → Z2 | Hulin 2014 |
| G3 | Polarisation breach | Drop next 1-2 hard sessions | Seiler 2010 |
| G4 | ACWR weekly scaling (> 1.5) | Next week TSS × 0.85 | Gabbett 2016 |
| G5 | Soreness >= 6 | Force recovery (overrides HRV/TSB) | Hooper 1995 |
| G6 | Hooper composite >= 18 | Force today → Z2 | Hooper 1995 |
| G7 | 3-day mean RPE >= 7 on HIT day | Drop today one tier | Foster 1998 |

**Additional signals:**
- DFA alpha1 mean < 0.5 over last 3 rides → tomorrow's threshold → Z2
- Aerobic decoupling > 5% → next-day "Z2 recommended" advisory
- Foster monotony > 2.0 over 14 days → next week TSS × 0.85
- TSB < -30 → rescales remaining-week TSS to 0.6×

### 12.3 Architecture Comparison

| Dimension | Domestique | Our System |
|-----------|-----------|------------|
| Data source | Intervals.icu only | Garmin Connect + Whoop + Oura + Apple Health |
| Readiness | Fixed weights + Bayesian adaptation | LASSO (Rothschild) + fuzzy logic (Wang) |
| Workout library | 4,232 ZWO files | Planned smaller library + dynamic generation |
| Guardrails | 7 explicit gates (G1-G7) | Pain veto + readiness thresholds |
| 3D IR model | Implemented (Kontro 2026) | Planned |
| DFA alpha1 | Full pipeline with artifact rejection | Planned |
| Architecture | Flat Python modules | Modular package structure |
| Packaging | PyInstaller desktop app | Home Assistant add-on + Streamlit web |
| Dietary input | None | Rothschild-style (kcal, CHO, protein) |
| Sleep depth | Duration only | Stages + efficiency + score (Garmin) |

**What we borrow:**
1. Intensity ladder for de-escalation
2. G1-G7 guardrail pattern (adapted for our data sources)
3. Bayesian weight adaptation pattern
4. DFA alpha1 artifact rejection (Malik 1996 filter)
5. Phase-targeted intensity budgets
6. ACWR safety ceiling
7. Plan stability contract (skeleton stable, intensities adapt)

---

## Part 13: 3D Impulse-Response Model — Kontro et al. 2026

**Paper:** Kontro et al. 2026. *PLOS One*. PMC12880663

### 13.1 Mathematical Formulas

The 3D IR model decomposes training load into three physiological strains:

**Aerobic strain (S_a):**
```
S_a(t) = S_a(t-1) × e^(-1/τ_a) + w_a(t)
τ_a ≈ 41-52 days (fitness half-life)
w_a(t) = TSS × f_aerobic(power_profile)
```

**Glycolytic strain (S_g):**
```
S_g(t) = S_g(t-1) × e^(-1/τ_g) + w_g(t)
τ_g ≈ 5 days (W' fitness half-life)
w_g(t) = kJ_above FTP × scaling_factor
```

**Alactic strain (S_al):**
```
S_al(t) = S_al(t-1) × e^(-1/τ_al) + w_al(t)
τ_al ≈ 1-2 days (neural/freshness)
w_al(t) = Pmax_effort × duration × scaling_factor
```

**Total strain score:**
```
Strain_Score = c_a × S_a + c_g × S_g + c_al × S_al
```

### 13.2 Required Inputs

| Input | Source | Status |
|-------|--------|--------|
| CP (Critical Power) | `power_metrics.py` | Available |
| W' (W-prime) | `w_prime.py` | Available |
| Pmax (peak power) | Not tracked | NEEDS IMPLEMENTATION |
| TSS | `training_load.py` | Available |
| kJ above FTP | Power data | Computable |
| Power profile | Power duration curve | Available |

**Missing:** Pmax estimation. Can be derived from PDC (power duration curve) peak or estimated from 1s/3s power data.

### 13.3 Implementation Pseudocode

```python
import numpy as np

class ThreeDimIR:
    def __init__(self):
        self.tau_a = 48.0   # aerobic half-life (days)
        self.tau_g = 5.0    # glycolytic half-life (days)
        self.tau_al = 1.5   # alactic half-life (days)
        self.S_a = 0.0
        self.S_g = 0.0
        self.S_al = 0.0

    def update(self, tss, kj_above_ftp, pmax_effort, dt=1.0):
        # Decay
        self.S_a *= np.exp(-dt / self.tau_a)
        self.S_g *= np.exp(-dt / self.tau_g)
        self.S_al *= np.exp(-dt / self.tau_al)

        # Add load
        self.S_a += tss
        self.S_g += kj_above_ftp * 0.01  # scale kJ to strain units
        self.S_al += pmax_effort * 0.001  # scale to strain units

    def strain_score(self, c_a=0.5, c_g=0.3, c_al=0.2):
        return c_a * self.S_a + c_g * self.S_g + c_al * self.S_al

    def fitness_state(self):
        return {
            'aerobic': self.S_a,
            'glycolytic': self.S_g,
            'alactic': self.S_al,
            'total': self.strain_score()
        }
```

---

## Part 14: Round 3 Deep-Dive — Implementation Details

**Research date:** 2026-07-12 (Round 3)

### 14.1 DFA Alpha1 — Complete Implementation

**Algorithm (Peng 1995):**
1. **Artifact rejection** (MANDATORY): Median-of-neighbors filter (±2 neighbors, 20% relative threshold). Never skip — unfiltered DFA gives physiologically impossible values.
2. **Integration:** `y[k] = Σᵢ₌₁ᵏ (RR[i] - mean(RR))`
3. **Segmentation:** Scales n ∈ [4, 16] (13 scales)
4. **Detrending:** Linear least-squares per segment
5. **Fluctuation:** `F(n) = sqrt(mean(F²(n,s)))`
6. **Scaling:** `α₁ = slope of log(F(n)) vs log(n)`
7. **Quality gates:** R² ≥ 0.95, α₁ ∈ [0.20, 1.60], min 16 beats

**Thresholds (Rogers 2021):**
- α₁ = 0.75 → HRVT1 (aerobic threshold, LT1)
- α₁ = 0.50 → HRVT2 (anaerobic threshold, LT2)
- α₁ < 0.50 → sympathetic dominance / fatigue
- α₁ > 0.85 → high fitness / well-recovered

**Window:** 120s sliding, 30s step. Produces ~200 data points/hour.

**Data source:** Garmin FIT HrvMessage (chest strap RR intervals). NOT 1Hz HR.

**CAVEAT (Altini 2022):** Universal 0.75 threshold has 10-50 bpm error at individual level. Use for longitudinal tracking (day-to-day changes), not absolute threshold detection.

### 14.2 Pmax Estimation — Three Methods

**Method A (Recommended): Direct 1s peak from PDC**
- Our `_PDC_DURATIONS` already includes 1s duration
- `Pmax = max power over any 1s window across all rides`
- Error: ±5-10%. Field-deployable. No special test needed.

**Method B: 3-CP curve fit (Morton 1996)**
- Non-linear fit of `P(t) = CP + (Pmax-CP) × W' / (W' + (Pmax-CP) × t)`
- Simultaneous CP/W'/Pmax estimation from PDC data
- Requires ≥5 PDC points. Error: ±10-20%.

**Method C (Fallback): FTP-based empirical**
- `Pmax ≈ FTP × k` where k = 4.0 (recreational), 4.5 (trained), 5.0 (elite)
- Error: ±20-30%. Last resort only.

**Recommended cascade:** Method A → Method B (if ≥5 PDC points) → Method C.

### 14.3 SHAP Explainable AI

**For LASSO model:** `LinearExplainer` computes exact SHAP in O(p):
```
φᵢ = βᵢ × (xᵢ - E[xᵢ])
```

**Implementation:**
```python
import shap
from sklearn.linear_model import LassoCV

explainer = shap.LinearExplainer(
    (lasso.coef_, lasso.intercept_),
    background_data,
    feature_perturbation='interventional'
)
shap_values = explainer.shap_values(X)
```

**Example output:**
```
Readiness 0.72; HRV +0.3 SD → +0.12; soreness 4/7 → -0.08;
sleep quality 7/10 → +0.05; life stress 3/10 → -0.03
```

**For fuzzy pain veto (non-differentiable):** Use `PermutationExplainer` on the full hybrid pipeline, or provide explicit rule attribution: `"Pain 6/10 → veto 50% → readiness 0.72 → 0.36"`

**Dependencies:** `shap>=0.46.0`, `scikit-learn>=1.3.0`

### 14.4 W' Balance ODE — Already Implemented

Our `src/analytics/w_prime.py` implements the Skiba & Clarke 2021 W'BAL-ODE with adaptive tau:
```
tau = 546 × exp(-0.01 × D_CP) + 316
```
where D_CP = CP - recovery_power. At CP: tau ≈ 862s. At 200W below CP: tau ≈ 316s.

**Integration with readiness:** Low W' balance at end of ride (<40% of capacity) is a fatigue marker. Track `min_balance_pct` and `final_balance_pct` per ride.

---

## Part 15: Round 4 — Garmin Data & Dietary Integration

**Research date:** 2026-07-12 (Round 4)

### 15.1 Garmin Connect API — Complete Field Inventory

**Package:** `garminconnect` v0.3.6 (cyberjunky/python-garminconnect, 146K weekly downloads)

**Wellness data available (134+ API methods):**

| Category | Key Fields | API Method | Our Status |
|----------|-----------|------------|------------|
| **HRV** | `lastNight` (RMSSD), `weeklyAvg`, `status` (LOW/BALANCED/HIGH), `baseline` bands | `get_hrv_data()` | PARTIAL — need `lastNight` field fix |
| **RHR** | `restingHeartRate`, `lastSevenDaysAvgRestingHeartRate` | `get_heart_rates()` | PARTIAL — need 7-day avg |
| **Sleep** | `sleepScore`, `sleepTimeSeconds`, `deepSleepSeconds`, `lightSleepSeconds`, `remSleepSeconds`, `awakeSeconds`, `restlessCount` | `get_sleep_data()` | PARTIAL — need stage detail |
| **Stress** | `averageStressLevel`, `maxStressLevel`, `stressDuration`, `low/medium/highStressDuration` | `get_stats()` | PARTIAL — need max + durations |
| **Body Battery** | `chargedValue`, `drainedValue`, `highestValue`, `lowestValue`, `mostRecentValue` | `get_stats()` | PARTIAL — need charged/drain |
| **Respiration** | `avgWakingRespirationValue`, `avgSleepRespirationValue` | `get_respiration_data()` | MISSING |
| **SpO2** | `averageSpo2`, `lowestSpo2` | `get_stats()` | PARTIAL — need lowest |
| **Training Readiness** | `trainingReadinessScore` (0-100), `recoveryTime`, `enduranceScore`, `hillScore`, `fatigue`, `form`, `performanceCondition` | `get_training_readiness()` | MISSING — high value |
| **VO2 Max** | `vo2MaxCycling`, `vo2MaxRunning`, `fitnessAge` | `get_max_metrics()` | MISSING |
| **Hydration** | `amountMl`, `goalMl` | `get_hydration_data()` | MISSING |
| **Nutrition** | `totalCalories`, `totalProtein`, `totalFat`, `totalCarbs`, `foodEntries[]` | `get_nutrition_daily_food_log()` | MISSING — Garmin HAS nutrition API |

**CRITICAL FIX:** Our code uses `hrv_data["hrvSummary"].get("overnightHRVValue")` but the actual field is `lastNight`.

### 15.2 Dietary Integration — Rothschild Variables

**Garmin HAS nutrition API** — `get_nutrition_daily_food_log()` returns daily totals + per-entry timestamps. This is a direct data source for Rothschild dietary variables.

**Key dietary variables (Rothschild 2024):**

| Variable | Unit | Predictive Power | Source |
|----------|------|-----------------|--------|
| `cho_g_per_kg` | g/kg body mass | **Most important dietary predictor** | Garmin nutrition API |
| `pre_exercise_cho` | g (4h pre-workout window) | **Key actionable variable** | Garmin food entries + activity times |
| `cho_3day_ma` | g/kg | More predictive than single-day | Computed |
| `total_kcal` | kcal | Direct predictor | Garmin nutrition API |
| `protein_g_per_kg` | g/kg | Direct predictor | Garmin nutrition API |
| `fasted_training` | binary (pre-exercise CHO < 5g) | Defines fasted threshold | Computed |

**Nonergodicity warning (Rothschild 2023):** Group-level dietary effects do NOT uniformly apply to individuals. Model must be personalized.

---

## Part 16: Round 5 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 5 planning)

### 16.1 Areas Identified for Round 5

1. ~~**Streamlit UI design**~~ — ✅ Complete (SparklingAmphibian: morning check-in form + prescription dashboard)
2. ~~**SQLite schema design**~~ — ✅ Complete (Part 11: daily_readiness table with all fields)
3. ~~**Individual model retraining**~~ — ✅ Complete (GrossPuffin: weekly Bayesian weight update via SGDRegressor.partial_fit())
4. ~~**Weather integration**~~ — ✅ Complete (ValuableQuelea: OpenWeatherMap API, WBGT correction, humidity modifier)
5. ~~**Race-day prescription**~~ — ✅ Complete (CleanTermite: taper protocols, race-day rules, mesocycle transitions)
6. ~~**Long-term periodization**~~ — ✅ Complete (CleanTermite: base→build→peak→taper with readiness triggers)
7. **Workout classification system** — Content-based classification for our workout library
8. **W' balance as fatigue marker** — How to use end-of-ride W' balance to detect acute fatigue
9. **Decoupling trend analysis** — How rising decoupling over time signals declining fitness
10. **Automated daily sync** — Background job architecture for daily Garmin + subjective data collection

---

## Part 18: Round 6 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 6 planning)

### 18.1 Areas Identified for Round 6

1. **Workout classification system** — Content-based classification (like Domestique's 17 canonical classes) for our workout library
2. **W' balance as fatigue marker** — How to use end-of-ride W' balance to detect acute fatigue
3. **Decoupling trend analysis** — How rising decoupling over time signals declining fitness
4. **Automated daily sync** — Background job architecture for daily Garmin + subjective data collection
5. **Prompt engineering for LLM prescription** — How to structure the system prompt for optimal LLM output
6. **MQTT integration** — How to publish prescription to smart devices (bike computer, phone)
7. **Multi-athlete support** — How to extend the model for family/team use
8. **Historical analysis** — How to review past prescriptions and outcomes to improve the model
9. **Edge cases** — Illness, travel, jet lag, altitude, life events
10. **Validation framework** — How to test the model against ground truth (performance changes)

---

## Part 17: Weather Integration & Race-Day Prescription

**Research date:** 2026-07-12 (Round 5)

### 17.1 Weather Integration

**Mantzios 2022 (PMC8677617):** 1258 endurance races, 7867 athletes.
- Optimal WBGT: 7.5-15°C (air temp 10-17.5°C)
- Performance declines **0.3-0.4% per °C WBGT** outside optimal range
- Air temperature is #1 weather parameter (feature importance 40%), followed by humidity (26%), solar radiation (18%), wind speed (16%)
- WBGT alone predicts better (R²=0.11-0.47) than air temp alone (R²=0.04-0.34)

**Weather API:** OpenWeatherMap One Call API 4.0 (free tier: 100 calls/day). Returns temp/humidity/wind/solar radiation in single call. Global coverage, 47+ years historical data.

**Temperature/Humidity Effects on Physiology:**
- HRV recovers to baseline in **4h after hot/dry** exercise (38°C/28% RH) but **8-24h after hot/humid** (38°C/64% RH) [Abellán-Aynés 2019]
- High humidity (80% RH at 35°C) significantly decreases RMSSD and HF post-exercise [Wu 2024]
- RPE correlates strongly with stress score (r=0.729 dry, r=0.568 humid)
- Heat causes cardiovascular strain: skin blood flow displacement (6-8 L/min) reduces stroke volume, increases HR [Périard 2017]

**Heat Acclimation Protocol (Pryor 2018):**
- 10 consecutive days × 90 min at WBGT ≥30°C for full acclimation
- Trained athletes adapt in 5-7 days
- Improves heat performance ~7% (TT) and ~23% (time-to-exhaustion)
- May improve cool-weather performance up to 6%
- Adaptations decay ~35% in 2 weeks without heat exposure; re-acclimation takes 2-4 days

**Prescription Engine Integration:**
```
Temperature correction factor:
  IF WBGT outside 7.5-15°C:
    load_reduction = 0.0035 × |WBGT - midpoint_of_optimal|
    prescribed_TSS *= (1 - load_reduction)

Humidity modifier:
  IF RH > 60% AND temp > 25°C:
    hrv_recovery_window = 24h (vs. 4h baseline)
    load_reduction += 0.05

Heat acclimation state:
  IF acclimated (≥5 consecutive days WBGT≥30°C):
    load_reduction *= 0.5  # acclimated athletes need less reduction
```

### 17.2 Race-Day Prescription & Long-Term Periodization

**Taper Protocols (Bosch 2003, Halson 2014):**

| Phase | Duration | Volume | Intensity | Readiness Expectation |
|-------|----------|--------|-----------|----------------------|
| **Build** | 3-6 weeks | 100% | 80/20 polarized | CTL ↑, HRV stable |
| **Taper** | 7-14 days | ↓ to 50-60% | Maintain 80-100% | HRV ↑, RHR ↓, form ↑ |
| **Race Week** | 3-7 days pre-race | ↓ to 30-40% | Sharp quality efforts | Peak readiness |
| **Race Day** | 0 days | Race effort | Race pace | Form peak, fatigue minimal |
| **Post-Race** | 2-5 days | ↓ to 20-30% | Easy/recovery | HRV ↓, RHR ↑ (expected) |

**Readiness Metrics During Taper:**
- HRV: ↑ 5-15% from pre-taper baseline (parasympathetic rebound)
- RHR: ↓ 3-8 bpm from pre-taper baseline
- Form score (Garmin): ↑ to peak
- Fatigue score (Garmin): ↓ to minimum
- **Taper success signal:** HRV > pre-taper baseline AND RHR < pre-taper baseline

**Race-Day Prescription Rules:**
```
IF race_within_7_days:
    IF day >= 3 before race:
        load = 30-40% of normal
        intensity = sharp quality (VO2max/threshold efforts, short)
        duration = 30-45 min total
    IF day == 1 before race:
        load = 20% of normal
        intensity = very easy + short activation
        duration = 20-30 min
    IF day == race_day:
        load = race effort
        nutrition = carb-load (8-12 g/kg day before, 60-90 g/h during)
        hydration = 500-750 ml/h + electrolytes
        readiness_check = IF HRV < baseline - 2SD → consider deferral
```

**Mesocycle Transition Triggers:**
```
Base → Build: CTL > 80 AND HRV stable for 14+ days
Build → Peak: CTL plateau (±5% for 7 days) AND form ↑
Peak → Taper: Race scheduled OR CTL > 100 with fatigue ↑
Taper → Race: HRV > baseline AND RHR < baseline AND form at peak
Post-Race → Recovery: 3-5 days easy, then reassess
Recovery → Base: HRV returns to baseline AND RHR returns to baseline
```

**Overreaching Detection (Meeusen 2013 OTS Consensus):**

| Signal | Threshold | Duration | Action |
|--------|-----------|----------|--------|
| HRV ↓ > 2 SD | Below individual baseline | 7+ consecutive days | Reduce load 50% |
| RHR ↑ > 10 bpm | Above individual baseline | 7+ consecutive days | Reduce load 50% |
| Performance ↓ | FTP/CP ↓ > 3% | 2+ weeks | Evaluate for overreaching |
| Subjective PRS ↓ | < 50/100 | 7+ consecutive days | Rest 2-3 days |
| Sleep disruption | < 6h or efficiency < 80% | 7+ consecutive days | Investigate cause |
| Restlessness/irritability | Subjective report | Persistent | Rest + professional eval |

**Functional vs. Non-Functional Overreaching:**
- **Functional:** Performance ↓ temporarily, recovers with 1-3 weeks rest. HRV/RHR normalize.
- **Non-Functional (OTS):** Performance ↓ persists >3 weeks despite rest. Requires medical evaluation.
- **Detection:** If HRV/RHR do NOT normalize after 2 weeks complete rest → refer to sports medicine professional.

---

## Part 19: W' Balance & Decoupling Trends — Advanced Fatigue Markers

**Research date:** 2026-07-12 (Round 6)

### 19.1 W' Balance as Fatigue Marker

**W' (anaerobic work capacity)** is a finite energy store that depletes during efforts above FTP and recharges during efforts below FTP. End-of-ride W' balance indicates how much anaerobic capacity remains.

**Fatigue thresholds (from `w_prime.py` WPrimeResult):**

| W' Balance | Fatigue Level | Action |
|------------|---------------|--------|
| **> 40%** | Normal | Standard training appropriate |
| **20-40%** | Mild fatigue | Monitor; no action needed |
| **10-20%** | Moderate fatigue | Reduce intensity 25% next session |
| **< 10%** | Severe fatigue | Reduce intensity 50% or rest |

**Trend detection:**
- **Acute fatigue:** Single-session W' balance <10% with rapid recovery within 24-48h
- **Chronic fatigue:** Persistent W' balance <30% over 3-5 consecutive days + declining W' capacity trend
- **W' capacity declining >5% over 7 days:** Overtraining risk

**Integration with readiness:** Composite fatigue score = 0.35×HRV_z + 0.35×RHR_z + 0.30×W'balance_z

### 19.2 Decoupling Trend Analysis

**Source:** Barsumyan 2025 (PMC12271085) — 20 cyclists, monthly 60-min tests at 75% FTP over 5 months.

**Key finding:** Decreasing decoupling = positive training response. Non-responders (decoupling did not improve or worsened) accumulated fatigue or failed to adapt.

**Decoupling thresholds:**

| Decoupling | Interpretation | Action |
|------------|---------------|--------|
| **< 2%** | Excellent durability (elite) | Green light for all training |
| **2-5%** | Good aerobic fitness | Standard training |
| **5-8%** | Moderate (recreational or fatigued) | Monitor; more Z2 base work |
| **> 8%** | Poor durability (fatigue/overreaching) | Reduce intensity; increase Z2 |
| **> 10%** | Severe (acute fatigue) | Rest or Z1-only |

**Leading indicator:** Decoupling changes *before* FTP drops (~2-4 week lead time). Mechanism: cardiovascular compensation increases → decoupling increases → stroke volume declines → VO2max drops → FTP decline.

**Trend detection algorithm:**
1. Filter: Only rides ≥45 min at 65-85% FTP (steady-state aerobic)
2. Weekly average: Mean decoupling across qualifying rides in past 7 days (min 2 rides)
3. Monthly baseline: 28-day EWMA (α=0.1)
4. Trend: weekly vs. baseline → improving/stable/declining/worsening
5. Alert: Green (±1%), Yellow (>1% for 2 weeks), Red (>2% for 3+ weeks)

**Guardrails (extending Domestique G-gates):**
```
G8: Decoupling > 8% on 3+ consecutive rides → force next day to Z2
G9: Decoupling trend 'worsening' for 2+ weeks → reduce weekly TSS by 20%
G10: Decoupling > 10% → advisory rest day
```

**Multi-signal fusion:**
```
fatigue_signal = 0.40×decoupling_z + 0.30×cv_drift_z + 0.20×dfa_a1_z + 0.10×eftp_drift_z
```

---

## Part 20: Round 7 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 7 planning)

### 20.1 Areas Identified for Round 7

1. ~~**Prompt engineering for LLM prescription**~~ — ✅ Complete (VivaciousStarfish: prompt template, guardrails, output schema)
2. **MQTT integration** — How to publish prescription to smart devices (bike computer, phone)
3. **Multi-athlete support** — How to extend the model for family/team use
4. **Historical analysis** — How to review past prescriptions and outcomes to improve the model
5. ~~**Edge cases**~~ — ✅ Complete (SeniorSpoonbill: illness, travel, jet lag, altitude, life events)
6. ~~**Validation framework**~~ — ✅ Complete (MoaningSwan: ground truth, A/B testing, model accuracy tracking)
7. **Automated daily sync** — Background job architecture for daily Garmin + subjective data collection
8. **Workout classification system** — Content-based classification for our workout library
9. **Nutrition tracking UI** — Streamlit form for daily food log entry
10. **Weekly report generation** — Automated weekly summary email or dashboard

---

## Part 22: Edge Cases for Training Prescription

**Research date:** 2026-07-12 (Round 7)

### 22.1 Illness Effects on HRV, RHR, and Readiness

**Sources:** TrainingPeaks illness detection [link](https://www.trainingpeaks.com/blog/how-to-use-hrv-to-predict-illness/); Garmin illness metrics [link](https://www.garmin.com/en-US/blog/fitness/how-getting-sick-might-change-your-heart-metrics/); HRV in strength/conditioning [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC11204851/)

**Detection:** HRV drops 1-3 days pre-symptom. rmSSD >1×SD below 7-day baseline for ≥2 mornings + RHR ≥5bpm above 14-day baseline.

**Recovery timelines:**
| Severity | HRV Recovery | RHR Recovery | Full Readiness |
|----------|-------------|--------------|----------------|
| Mild cold (no fever) | 3-7 days post-symptom | 2-5 days | 5-10 days |
| Febrile illness | 7-14 days post-fever | 5-10 days | 2-3 weeks |
| GI illness | 5-10 days | 5-10 days | 10-14 days |
| Post-viral fatigue | Weeks to months | Weeks to months | Individualized |

**Prescription:** Active illness (fever/systemic) → rest. Mild (above-neck) → Z1 ≤30min. Post-illness: days 1-3 at 30-50% load Z1-2; days 4-7 at 50-75%; day 8+ gradual return.

### 22.2 Jet Lag and Travel

**Sources:** Systematic review on jet lag in athletes [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC13030464/); Oura travel data [link](https://ouraring.com/blog/manage-jet-lag/)

**Recovery:** Westward ~0.5 days/timezone. Eastward ~1.5 days/timezone. Travel fatigue (no timezone): 1-2 days.

**Prescription:** Day of travel → rest. Days 1-2 post-arrival → 30-50% load, Z1-2, ≤60min. Westward: resume 75% by day 3, full by day 5 (for 5-zone). Eastward: resume 50% days 3-4, full by day 8-10.

### 22.3 Altitude Training

**Sources:** Meta-analysis: altitude reduces HRV indices [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC12812737/); Altitude acclimatization study [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC8995742/); Ultrahuman altitude case study [link](https://www.ultrahuman.com/science/studies/the-effect-of-altitude-on-recovery-metabolism-and-running-performance-in-an-elite-ultramarathon-athlete-an-ultrahuman-case-study/)

**Acute (Days 1-3):** HRV drops 15-35%, RHR +5-15bpm, LF/HF ↑ (sympathetic predominance). Load 30-50%, Z1-2, ≤60min.

**Acclimatization (Days 4-14):** HRV partial recovery by day 4-5. Establish new altitude baseline. Gradually increase load 50%→75%→90%.

**Post-altitude (sea level return):** Days 1-2 easy (50% load). Days 3-5: 75-100% load (supercompensation window).

### 22.4 Life Stress

**Sources:** Rothschild et al. 2024 (life stress top 3 predictor of PRS); ACSM HRV-mental health [link](https://acsm.org/meantl-health-heart-rate-variability/)

**Detection:** Self-reported stress ≥6/10 for ≥2 days + rmSSD 10-25% below baseline.

**Prescription:** Acute (1-3 days) → reduce 20-30%, prefer Z2. Chronic (≥1 week) → reduce 30-50%, eliminate intervals, cap TSS at 60%.

### 22.5 Unified Edge Case Override System

```
Edge case flags (boolean, set by user or auto-detected):
  illness_active, illness_recovery, travel_fatigue, jet_lag,
  altitude_acute, altitude_acclimatizing, post_altitude,
  life_stress_acute, life_stress_chronic

Override logic (most restrictive wins if multiple active):
  illness_active → tss_factor=0, zone=rest
  illness_recovery → tss_factor=0.3-0.7 (day-dependent), zone=1-2
  travel_fatigue → tss_factor=0.3-0.5, zone=1-2, duration≤60min
  jet_lag (west) → tss_factor=0.5-1.0 (0.5/day 1-2, +0.15/day)
  jet_lag (east) → tss_factor=0.3-1.0 (0.3/day 1-2, +0.10/day)
  altitude_acute → tss_factor=0.2-0.5, zone=1-2, duration≤60min
  altitude_acclimatizing → tss_factor=0.5-0.9, zone=1-3, new HRV baseline
  post_altitude → tss_factor=0.5-1.0, zone=1-5 (supercompensation)
  life_stress_acute → tss_factor=0.7-0.8, no intervals, duration≤60min
  life_stress_chronic → tss_factor=0.5-0.7, no intervals, cap TSS 60%
```

---

## Part 23: Prompt Engineering for LLM-Based Training Prescription

**Research date:** 2026-07-12 (Round 7)

### 23.1 Prompt Structure

**Source:** LLM-SPTRec (He et al. 2026, Sci Rep) [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC12916763/); LLM coaching study (Lee 2025) [link](https://arxiv.org/html/2509.26593v1)

**Three-section prompt:**
```
### USER PROFILE
- Demographics: {age, sex, weight, height, experience_level}
- Fitness state: {FTP, CP, W', CTL, ATL, TSB}
- Readiness: {HRV_rmssd, RHR, sleep_score, PRS, fatigue, DOMS, life_stress}
- Recent training: {last_7_days_log}
- Goal: {natural_language_goal}
- Context: {illness, travel, altitude, life_events}

### KNOWLEDGE CONTEXT
- Retrieved triples from Sports Science Knowledge Graph

### INSTRUCTION
Generate a [N]-day training plan. Reason step-by-step:
1. Assess readiness, 2. Select session type, 3. Set intensity, 4. Verify safety
Output as JSON matching the provided schema.
```

### 23.2 Guardrails

**Source:** Beam et al. 2025 (Sci Rep) [link](https://www.nature.com/articles/s41598-025-09138-0); Datadog LLM guardrails [link](https://www.datadoghq.com/blog/llm-guardrails-best-practices/)

**Hard guardrails (never events):**
| ID | Rule | Action |
|----|------|--------|
| H1 | pain_score ≥ 7 | Force rest or Z1 only |
| H2 | illness_flag = true | Cancel all structured training |
| H3 | rmssd < 50% of 7-day baseline | Drop intensity by 2 zones |
| H4 | sleep < 5h for 3+ consecutive days | Cap weekly TSS at 60% |
| H5 | Yesterday TSS > 1.5× planned → today Z2 | Enforce recovery |
| H6 | ACWR > 1.5 | Reduce next week TSS × 0.85 |
| H7 | > 20% weekly volume above FTP | Cap high-intensity volume |
| H8 | Altitude > 2000m, < 7 days | Reduce intensity 10-20% |
| H9 | ≥3 timezones crossed within 48h | First 48h: Z1-Z2, ≤60min |
| H10 | life_stress ≥ 6 for 3+ days | Reduce volume 20-30% |

**Soft guardrails:** Data completeness check, anomalous readiness detection (k-NN vs. historical), confidence scoring, contradiction detection.

### 23.3 Output Schema

```json
{
  "readiness_assessment": {
    "composite_score": {number, 0-100},
    "limiting_factor": {enum: HRV|sleep|DOMS|illness|travel|altitude|life_stress|none},
    "confidence": {number, 0-1}
  },
  "daily_plan": [{
    "date": {string, ISO},
    "session_type": {enum: rest|recovery|Z1|Z2|tempo|intervals|long|strength|mixed},
    "target_zone": {pattern: Z[1-5]},
    "duration_min": {integer, 5-300},
    "intensity": {"power_watts": {number}, "hr_zone": {string}, "rpe_target": {integer, 1-10}},
    "load_adjustment": {number, -50 to 20},
    "rationale": {string}
  }],
  "safety_notes": [{"severity": {enum: info|warning|critical}, "message": {string}, "action": {string}}]
}
```

### 23.4 Implementation Recommendations

1. **Constrained decoding** (XGrammar/Outlines) for JSON schema enforcement
2. **Cycling-specific knowledge graph** with contraindications and recovery principles (31.8% quality drop without it)
3. **10 hard guardrails** as post-generation validator
4. **Adversarial retry** (max 3 attempts) if validation fails
5. **Low temperature** (0.1-0.3) for deterministic output
6. **Chain-of-thought reasoning** for auditability
7. **Trend indicators** in context (e.g., "HRV 12% below baseline" not just raw value)

---

## Part 24: Validation Framework

**Research date:** 2026-07-12 (Round 7)

### 24.1 Ground Truth Definition

**Three tiers:**
1. **Performance outcomes:** FTP/CP change, TT times, VO2max
2. **Physiological proxies:** Decoupling trend, W' capacity, DFA-a1, HRV baseline drift
3. **Subjective outcomes:** PRS, injury-free days, satisfaction

### 24.2 A/B Testing Methodology

**Source:** Carrasco-Poyatos 2020 meta-analysis (VO2max g=0.402, FTP g=0.65 for HRV-guided training)

**Design:** Within-subject crossover. 4-week predefined vs. 4-week HRV-guided with washout.

### 24.3 Model Accuracy Tracking

**Rolling window cross-validation:** 28-day window, 7-day step. Expanding window for drift detection.
**Metrics:** RMSE vs. next-day PRS, false positive/negative rates, PSI and KS tests for data drift.

### 24.4 Statistical Validation

**Targets:** Sensitivity ≥90%, specificity ≥80%, PPV ≥70% for fatigue detection.
**Framework:** SWC (0.5×SD baseline), ROC/AUC analysis, Bayesian weight validation.

### 24.5 System Mapping

**Schedule:** Daily readiness logging → weekly RMSE/sensitivity → monthly FTP re-test and weight retraining → quarterly A/B analysis.
**New table:** `validation_log` (date, predicted_readiness, actual_readiness, rmse, sensitivity, specificity).
**New module:** `src/analytics/validation.py` for rolling window CV, SWC, ROC.

---

## Part 25: Round 8 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 8 planning)

### 25.1 Areas Identified for Round 8

1. **MQTT integration** — How to publish prescription to smart devices (bike computer, phone)
2. **Multi-athlete support** — How to extend the model for family/team use
3. **Historical analysis** — How to review past prescriptions and outcomes to improve the model
4. ~~**Automated daily sync**~~ — ✅ Complete (DailySync: APScheduler, Garmin API, morning check-in, missed day handling)
5. ~~**Workout classification system**~~ — ✅ Complete (WorkoutClassification: 17 Domestique classes, Coggan zones, 3D IR mapping)
6. **Nutrition tracking UI** — Streamlit form for daily food log entry
7. **Weekly report generation** — Automated weekly summary email or dashboard
8. **Knowledge graph construction** — Build cycling-specific SSKG with contraindications
9. **Individual recovery curves** — ML model for personalized recovery per edge case type
10. **Integration testing** — End-to-end test of the full prescription pipeline

---

## Part 26: Workout Classification System

**Research date:** 2026-07-12 (Round 8)

### 26.1 Domestique's 17 Canonical Workout Classes

**Source:** Domestique GitHub [link](https://github.com/platypus45/domestique) (Apache 2.0). Content-based classification from parsed interval structure, not filename.

| # | Class | IF Range | Energy System | Description |
|---|-------|----------|---------------|-------------|
| 1 | Recovery | <0.55 | Aerobic (Z1) | Active recovery, very low intensity |
| 2 | Endurance | 0.55-0.75 | Aerobic (Z2) | Steady aerobic base building |
| 3 | Endurance + Strides | 0.55-0.75 + spikes | Aerobic + Alactic | Z2 base with short neuromuscular pops |
| 4 | Tempo | 0.76-0.90 | Aerobic (Z3) | Sustained moderate effort |
| 5 | Sweet Spot | 0.88-0.94 | Aerobic (Z3-Z4) | High efficiency, threshold-adjacent |
| 6 | Threshold | 0.91-1.05 | Aerobic+Glycolytic (Z4) | FTP-anchored intervals (2x20, 3x15, 4x10) |
| 7 | Over-Under | 0.85-1.15 | Glycolytic (Z3-Z5) | Alternating above/below FTP |
| 8 | VO2max | 1.06-1.20 | Glycolytic+Aerobic (Z5) | 3-8 min intervals at 106-120% FTP |
| 9 | VO2-Short | 1.06-1.30 | Glycolytic (Z5-Z6) | Shorter VO2 intervals (30s-3min) |
| 10 | Rønnestad 30/15 | 1.06-1.30 | Glycolytic (Z5-Z6) | 30s on/15s off intervals |
| 11 | Microburst Ladder | 0.55-1.30 | All three systems | Progressive microburst intervals |
| 12 | Anaerobic | 1.21-1.50 | Glycolytic (Z6) | 30s-3min max efforts |
| 13 | Neuromuscular | >1.50 | Alactic (Z7) | Very short sprints (<30s) |
| 14 | FTP Test | Variable | All systems | 20-min test or ramp test |
| 15 | Ladder (VO2) | 0.55-1.20 | Glycolytic+Aerobic | Ascending/descending interval ladders |
| 16 | Ladder (Threshold) | 0.76-1.05 | Aerobic+Glycolytic | Threshold-intensity ladder progressions |
| 17 | Ladder (Anaerobic) | 0.76-1.50 | Glycolytic+Alactic | Anaerobic-intensity ladder progressions |

### 26.2 Coggan's 7-Zone Training Model

**Source:** Coggan 2016, TrainingPeaks [link](https://www.trainingpeaks.com/blog/power-training-levels/)

| Zone | Name | % FTP | Adaptation |
|------|------|-------|------------|
| Z1 | Active Recovery | <55% | Blood flow, recovery |
| Z2 | Endurance | 56-75% | Mitochondrial density, fat oxidation |
| Z3 | Tempo | 76-90% | Muscular endurance, lactate clearance |
| Z4 | Lactate Threshold | 91-105% | Raises FTP |
| Z5 | VO2 Max | 106-120% | Increases VO2max ceiling |
| Z6 | Anaerobic Capacity | >121% | Anaerobic power, buffering |
| Z7 | Neuromuscular Power | N/A | Neuromuscular recruitment |

### 26.3 Mapping to Kontro 2026 3D IR Model

**Source:** Kontro et al. 2026 [link](https://doi.org/10.1371/journal.pone.0341721) (PMC12880663)

| Workout Class | SSCP (Aerobic) | SSW' (Glycolytic) | SSPmax (Alactic) |
|--------------|----------------|-------------------|-----------------|
| Endurance | High | None | None |
| Tempo | Moderate-High | Low | None |
| Sweet Spot | High | Low | None |
| Threshold | High | Moderate | None |
| Over-Under | Moderate | High | Low |
| VO2max | Moderate | High | Low |
| Rønnestad 30/15 | Low | Very High | Low |
| Anaerobic | None | Very High | Low |
| Neuromuscular | None | None | High |

### 26.4 Workout Library Metadata Schema

```json
{
  "id": "string",
  "name": "string",
  "class": {enum: recovery|endurance|tempo|sweet_spot|threshold|over_under|vo2max|vo2_short|ronnestad|microburst|anaerobic|neuromuscular|ftp_test|ladder_vo2|ladder_threshold|ladder_anaerobic|endurance_strides},
  "duration_min": {integer, 5-300},
  "tss_estimate": {number},
  "energy_systems": {"aerobic_pct": {number}, "glycolytic_pct": {number}, "alactic_pct": {number}},
  "zones": {"z1_pct": {number}, "z2_pct": {number}, ... "z7_pct": {number}},
  "phase_affinity": {array: base|build1|build2|peak|taper|consolidation},
  "contraindications": {array: knee_pain|illness|acute_fatigue|...},
  "literature_ref": {string|null}
}
```

---

## Part 27: Automated Daily Sync Architecture

**Research date:** 2026-07-12 (Round 8)

### 27.1 Scheduling: APScheduler with CronTrigger

**Source:** APScheduler docs [link](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html)

```python
scheduler = BackgroundScheduler(timezone="America/New_York")
scheduler.add_job(
    daily_sync,
    CronTrigger(hour=6, minute=30, jitter=300),  # 5min jitter
    misfire_grace_time=3600,  # allow 1hr drift
)
```

### 27.2 Garmin API: Auth and Rate Limits

**Sources:** python-garminconnect [link](https://github.com/cyberjunky/python-garminconnect); garmin-health-data [link](https://github.com/diegoscarabelli/garmin-health-data)

- **Token storage:** `~/.garminconnect/<user_id>/garmin_tokens.json` with `0o600` permissions
- **Auto-refresh:** Tokens refresh transparently; re-login needed after 30+ days inactivity
- **Rate limiting:** 30-45s delay between API calls; 429 → defer to next day; 401 → re-auth; 5xx → retry 3x with exponential backoff

### 27.3 Morning Check-In Flow

**Sources:** Hooper Index [link](https://ascendperform.com/monitoring-stress-and-fatigue-with-the-hooper-mackinnon-questionnaire/); PRS + Hooper (Perazzetti et al.) [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC12473293/)

| Field | Scale | Direction |
|-------|-------|-----------|
| PRS | 0-10 | 0=poorly recovered, 10=well recovered |
| Fatigue | 1-7 | 1=low, 7=high |
| DOMS | 1-7 | 1=low, 7=high |
| Stress | 1-7 | 1=low, 7=high |
| Sleep Quality | 1-7 | 1=bad, 7=good |

### 27.4 Missed Day Handling

**Sources:** LOCF in athlete monitoring [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC5169162/); EMA missing data (Fibion) [link](https://web.fibion.com/articles/handle-missing-ema-data/)

```
Day N check-in missed?
  ├── Within grace (06:30-12:00) → use late submission
  ├── Past grace → LOCF (use Day N-1 values, tag as "imputed_locf")
  ├── No prior data → population median defaults (PRS=5, fatigue=3, DOMS=3, stress=3, sleep=4)
  └── Always include Garmin data (objective fills the gap)
```

### 27.5 Optimal Sync Timing

**Source:** HRV circadian rhythm (Vitale et al.) [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC6571903/)

```
05:30-06:30  User wakes, Garmin syncs overnight data
06:30        Sync job triggers (5-min jitter)
06:30-06:35  Pull Garmin data (sleep, HRV, stress, body battery)
06:35-07:00  Wait for subjective check-in (grace window)
07:00        If no check-in, apply LOCF/default
07:00-07:05  Merge data, compute readiness
07:05        Training prescription available
```

---

## Part 28: Round 9 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 9 planning)

### 28.1 Areas Identified for Round 9

1. **MQTT integration** — How to publish prescription to smart devices (bike computer, phone)
2. **Multi-athlete support** — How to extend the model for family/team use
3. **Historical analysis** — How to review past prescriptions and outcomes to improve the model
4. **Nutrition tracking UI** — Streamlit form for daily food log entry
5. **Weekly report generation** — Automated weekly summary email or dashboard
6. ~~**Knowledge graph construction**~~ — ✅ Complete (KnowledgeGraph: SSKG entity/relation schema, contraindication triples, GraphRAG retrieval)
7. **Individual recovery curves** — ML model for personalized recovery per edge case type
8. **Integration testing** — End-to-end test of the full prescription pipeline
9. **Streamlit UI implementation** — Morning check-in form + daily prescription dashboard
10. ~~**SQLite schema finalization**~~ — ✅ Complete (SchemaDesign: 7 new tables DDL, migration strategy, index/query patterns)

---

## Part 29: Knowledge Graph Construction for Cycling Sports Science

**Research date:** 2026-07-12 (Round 9)

### 29.1 LLM-SPTRec's Sports Science Knowledge Graph (SSKG)

**Source:** He et al. 2026, Scientific Reports 16:6793 [link](https://www.nature.com/articles/s41598-026-37075-z)

**Entity types:** Exercise, Muscle_Group, Energy_System, Fitness_Component, Physiological_Marker, Training_Phase, Injury_Risk, Recovery_Method, User_State

**Relation types:** targets, engages, develops, requires, indicated_for, contraindicated_by, causes, alleviates, measures, belongs_to

**Retrieval:** Top-K entity retrieval + 1-hop subgraph. Ablation shows removing KG drops Plan Coherence Score by 31.8% — the KG is the scientific backbone preventing hallucination.

### 29.2 Cycling-Specific Entity Schema

**Sources:** He et al. 2026 [link](https://www.nature.com/articles/s41598-026-37075-z); Li et al. 2026 [link](https://www.nature.com/articles/s41598-026-38066-w); Kontro et al. 2026 [link](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0341721); TrainingPeaks zones [link](https://www.trainingpeaks.com/blog/power-training-levels/)

| Entity Type | Cycling Instances |
|---|---|
| **Workout_Type** | zone2_endurance, tempo, sweet_spot, threshold, over_under, vo2max_intervals, vo2_short, ronnestad, microburst, anaerobic_capacity, neuromuscular_power, ladder_vo2, ladder_threshold, endurance_strides, recovery_ride, ftp_test |
| **Energy_System** | aerobic_oxidative, anaerobic_glycolytic, atp_cp_alactic |
| **Physiological_Marker** | FTP, CP, W_prime, Pmax, HRV_rmssd, RHR, lactate_threshold, VO2max, TSS, CTL, ATL, TSB |
| **Contraindication** | high_DOMS, acute_fatigue, illness, knee_pain, low_HRV, poor_sleep, elevated_RHR, overreaching, dehydration |
| **Training_Phase** | base, build1, build2, peak, taper, consolidation, recovery |
| **Recovery_Method** | easy_ride, foam_rolling, sleep_extension, cold_therapy, massage, nutrition_repair |

### 29.3 Contraindication Triples (Signed Edge Weights)

**Source:** Huang et al. 2021, medical KG with signed weights [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC8444078/)

```
(vo2max_intervals, contraindicated_by, high_DOMS, -1.0)
(vo2max_intervals, contraindicated_by, acute_fatigue, -1.0)
(vo2max_intervals, contraindicated_by, low_HRV, -0.8)
(ronnestad, contraindicated_by, high_DOMS, -1.0)
(microburst, contraindicated_by, knee_pain, -1.0)
(threshold, contraindicated_by, illness, -1.0)
(recovery_ride, indicated_for, high_DOMS, +1.0)
(recovery_ride, indicated_for, low_HRV, +0.9)
(foam_rolling, alleviates, DOMS, +0.8)
(sleep_extension, alleviates, low_HRV, +0.9)
```

### 29.4 Retrieval Strategy: Top-K + 1-Hop + Contraindication Filter

**Sources:** KG-RAG4SM (Ma et al. 2025) [link](https://arxiv.org/html/2501.08686v1); GraphRAG survey (Han et al. 2025) [link](https://arxiv.org/html/2501.00309v2); Microsoft GraphRAG [link](https://github.com/microsoft/GraphRAG)

```
1. Map user state to KG entities (e.g., {high_DOMS, low_HRV})
2. Retrieve candidate workouts by goal (e.g., develops → aerobic_capacity)
3. Filter: exclude any workout with contraindicated_by edge to user state
4. Rank by relevance weight + safety margin
5. Serialize top-3 with 1-hop context → inject into LLM prompt
```

### 29.5 Tool Stack: SQLite + PyKEEN + NetworkX

**Sources:** PyKEEN [link](https://pykeen.readthedocs.io/); NetworkX [link](https://networkx.org/); Neo4j GraphRAG [link](https://neo4j.com/developer/genai-ecosystem/importing-graph-from-unstructured-data/)

```sql
-- Storage: SQLite adjacency tables
CREATE TABLE kg_entity (id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, description TEXT);
CREATE TABLE kg_relation (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
CREATE TABLE kg_triple (
    head_id INTEGER REFERENCES kg_entity(id),
    relation_id INTEGER REFERENCES kg_relation(id),
    tail_id INTEGER REFERENCES kg_entity(id),
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (head_id, relation_id, tail_id)
);
```

**Embedding:** PyKEEN with RotatE (same as He et al. 2026). **Traversal:** NetworkX for 1-hop BFS. **Why:** Zero-dependency, works on Raspberry Pi, no server needed.

---

## Part 30: SQLite Schema Finalization

**Research date:** 2026-07-12 (Round 9)

### 30.1 Architecture: Single File, `athlete_id` Column

**Sources:** 37signals multi-tenancy [link](https://dev.37signals.com/rails-multi-tenancy/); Azure SQL multi-tenant [link](https://learn.microsoft.com/en-us/azure/azure-sql/database/saas-tenancy-app-design-patterns)

Every table includes `athlete_id TEXT NOT NULL DEFAULT 'default'` as first column. All queries filter by `athlete_id`.

### 30.2 WAL Mode and Performance Prag+

**Sources:** SQLite WAL [link](https://sqlite.org/pragma.html#pragma_journal_mode); SQLite synchronous [link](https://sqlite.org/pragma.html#pragma_synchronous); SQLite query optimizer [link](https://micahkepe.com/blog/sqlite-query-optimizer/)

```sql
PRAGMA journal_mode = WAL;          -- Readers never block writers
PRAGMA synchronous = NORMAL;        -- Balance durability/performance
PRAGMA cache_size = -64000;         -- 64MB cache
PRAGMA temp_store = MEMORY;         -- Temp sorts in RAM
PRAGMA foreign_keys = ON;           -- Referential integrity
```

### 30.3 Table: `daily_readiness`

**Sources:** Alfonso et al. 2025 [link](https://www.nature.com/articles/s41598-025-08340-5); Rothschild et al. 2024 [link](https://pmc.ncbi.nlm.nih.gov/articles/PMC11519101/); Gabbett 2016 ACWR [link](https://bjsm.bmj.com/content/50/11/675)

```sql
CREATE TABLE daily_readiness (
    athlete_id TEXT NOT NULL DEFAULT 'default',
    date TEXT NOT NULL,
    rmssd REAL, resting_hr REAL,
    rmssd_mean_30d REAL, rmssd_std_30d REAL,
    rhr_mean_30d REAL, rhr_std_30d REAL,
    sleep_hours REAL, sleep_score REAL,
    perceived_readiness REAL, soreness REAL, life_stress REAL, mood REAL,
    readiness_score REAL, readiness_state TEXT, recommendation TEXT,
    ctl REAL, atl REAL, tsb REAL, acwr REAL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (athlete_id, date)
);
CREATE INDEX idx_readiness_date ON daily_readiness(athlete_id, date);
CREATE INDEX idx_readiness_state ON daily_readiness(athlete_id, readiness_state, date);
```

### 30.4 Table: `morning_checkin`

**Sources:** Saw et al. 2016 [link](https://bjsm.bmj.com/content/50/4/281); Figueiredo et al. 2022 [link](https://www.tandfonline.com/doi/abs/10.1080/02640414.2022.2053905); SQLite partial indexes [link](https://sqlite.org/skipscan.html)

```sql
CREATE TABLE morning_checkin (
    athlete_id TEXT NOT NULL DEFAULT 'default',
    date TEXT NOT NULL,
    perceived_readiness REAL, soreness REAL, life_stress REAL,
    sleep_quality REAL, mood REAL, energy REAL, motivation REAL,
    pain_score REAL, pain_location TEXT, notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (athlete_id, date)
);
CREATE INDEX idx_checkin_date ON morning_checkin(athlete_id, date);
CREATE INDEX idx_checkin_pain ON morning_checkin(athlete_id, date) WHERE pain_score > 0;
```

### 30.5 Table: `workout_library`

**Source:** SQLite JSON1 [link](https://sqlite.org/json1.html)

```sql
CREATE TABLE workout_library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL, description TEXT,
    workout_type TEXT NOT NULL, energy_system TEXT,
    target_duration REAL, target_tss REAL,
    target_zones TEXT, interval_structure TEXT,
    min_readiness REAL, max_readiness REAL, min_ctl REAL,
    pdc_shape_hint TEXT,
    times_prescribed INTEGER DEFAULT 0, last_prescribed TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_workout_selection ON workout_library(athlete_id, workout_type, energy_system);
CREATE INDEX idx_workout_pdc ON workout_library(athlete_id, pdc_shape_hint);
```

### 30.6 Table: `sync_log`

```sql
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL, sync_type TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT,
    status TEXT NOT NULL, records_fetched INTEGER DEFAULT 0,
    records_stored INTEGER DEFAULT 0, error_message TEXT,
    date_range_start TEXT, date_range_end TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sync_log_recent ON sync_log(athlete_id, created_at DESC);
```

### 30.7 Table: `validation_log`

```sql
CREATE TABLE validation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id TEXT NOT NULL DEFAULT 'default',
    check_name TEXT NOT NULL, target_date TEXT, target_activity_id TEXT,
    severity TEXT NOT NULL, message TEXT,
    raw_value REAL, expected_min REAL, expected_max REAL,
    action_taken TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_validation_severity ON validation_log(athlete_id, severity, created_at DESC);
```

### 30.8 Table: `edge_cases`

```sql
CREATE TABLE edge_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id TEXT NOT NULL DEFAULT 'default',
    case_type TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT,
    description TEXT, training_impact TEXT, resolution TEXT,
    resolved INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_edge_cases_active ON edge_cases(athlete_id, resolved, start_date) WHERE resolved = 0;
```

### 30.9 Table: `training_log`

```sql
CREATE TABLE training_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id TEXT NOT NULL DEFAULT 'default',
    planned_date TEXT NOT NULL, workout_id INTEGER,
    planned_type TEXT, planned_duration REAL, planned_tss REAL,
    planned_zones TEXT, readiness_at_plan REAL,
    actual_activity_id TEXT, actual_duration REAL, actual_tss REAL,
    actual_np REAL, actual_ifr REAL, actual_rpe REAL,
    completed INTEGER DEFAULT 0, modification_reason TEXT,
    post_ride_notes TEXT,
    decoupling_drift REAL, w_prime_min_balance REAL, dfa_a1_lt1_cross REAL,
    planned_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT,
    FOREIGN KEY (workout_id) REFERENCES workout_library(id)
);
CREATE INDEX idx_training_log_date ON training_log(athlete_id, planned_date);
CREATE INDEX idx_training_log_feedback ON training_log(athlete_id, planned_date, actual_tss, planned_tss) WHERE completed = 1;
```

### 30.10 Migration Strategy

**Sources:** SQLite migrations [link](https://www.sqliteforum.com/p/managing-database-versions-and-migrations); Atlas Migrate [link](https://atlasgo.io/blog/2024/04/01/migrate-down); SQLite ALTER TABLE [link](https://www.sqlite.org/lang_altertable.html)

```
src/db/migrations/
├── 001_create_schema_migrations.sql
├── 002_add_athlete_id_to_existing.sql
├── 003_create_daily_readiness.sql
├── 004_create_morning_checkin.sql
├── 005_create_workout_library.sql
├── 006_create_sync_log.sql
├── 007_create_validation_log.sql
├── 008_create_edge_cases.sql
└── 009_create_training_log.sql
```

Each migration wrapped in transaction. Idempotent DDL (`CREATE TABLE IF NOT EXISTS`). Two-phase deployment: additive first, cutover later.

### 30.11 Query Patterns

**Sources:** SQLite window functions [link](https://www.sqlite.org/windowfunctions.html); Kiviniemi et al. 2007 [link](https://link.springer.com/article/10.1007/s00421-007-0543-5)

**Rolling average (7-day readiness trend):**
```sql
SELECT date, readiness_score,
    AVG(readiness_score) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_avg_7d
FROM daily_readiness WHERE athlete_id = ? AND date >= date(?, '-90 days') ORDER BY date;
```

**Anomaly detection (z-score from 14-day baseline):**
```sql
WITH baseline AS (
    SELECT date, readiness_score,
        AVG(readiness_score) OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS mean,
        STDDEV(readiness_score) OVER (ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS std
    FROM daily_readiness WHERE athlete_id = ?
)
SELECT date, readiness_score, (readiness_score - mean) / NULLIF(std, 0) AS z_score
FROM baseline WHERE ABS(readiness_score - mean) > std AND std > 0;
```

---

## Part 31: Round 10 Deep-Dive Areas

**Research date:** 2026-07-12 (Round 10 planning)

### 31.1 Areas Identified for Round 10

1. **MQTT integration** — How to publish prescription to smart devices (bike computer, phone)
2. **Multi-athlete support** — How to extend the model for family/team use
3. **Historical analysis** — How to review past prescriptions and outcomes to improve the model
4. **Nutrition tracking UI** — Streamlit form for daily food log entry
5. **Weekly report generation** — Automated weekly summary email or dashboard
6. **Individual recovery curves** — ML model for personalized recovery per edge case type
7. **Integration testing** — End-to-end test of the full prescription pipeline
8. **Streamlit UI implementation** — Morning check-in form + daily prescription dashboard
9. **Garmin data field inventory** — Complete catalog of all available Garmin Connect API endpoints
10. **Prompt template library** — Pre-built prompt templates for different prescription scenarios