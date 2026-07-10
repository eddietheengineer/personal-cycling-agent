# 🚴‍♂️ Personal Cycling Agent: System Architecture & Implementation Plan

## 1. Project Overview
This repository implements a privacy-first, locally hosted AI cycling coach. The system bypasses proprietary, black-box algorithms (e.g., Garmin Training Readiness) by directly ingesting raw telemetry (HRV, RHR, Power, DFA-a1) to calculate a rider's true physiological state. An LLM then uses these statistical models, cross-referenced with a private user profile, to dynamically prescribe daily training.

The agent is available in two deployment modes:

| Mode | Description |
|---|---|
| **Home Assistant Add-on** | Recommended. Automatic Garmin sync, analytics, and a live dashboard embedded in HA via Ingress. Configured through the HA add-on UI. |
| **Standalone CLI** | Full control. Run the pipeline locally with `python -m src.main`, configure via `setup.py`, and launch the Streamlit dashboard on any machine. |

## 2. Core Scientific Modules (The Analytics Engine)

The system relies on five distinct mathematical models to evaluate readiness and drive exercise prescription. The agent must implement these strictly using Python (`pandas`, `numpy`, `scipy`).

### A. Two-Factor Autonomous Readiness Engine
Evaluates daily autonomic nervous system stress against a rolling statistical baseline.
* **Baseline Calibration:** Calculate a 30-day rolling mean ($\mu$) and standard deviation ($\sigma$) for Overnight RMSSD (HRV) and Resting Heart Rate (RHR).
* **Normal Band Formula:** Define homeostasis as $\mu \pm (0.75 \times \sigma)$.
* **State Machine:**
    * **Coping (Green):** HRV and RHR within normal bands. Proceed with planned intensity.
    * **Sympathetic Stress (Red/Yellow):** HRV below normal band AND RHR above normal band. Enforce complete rest or strict Zone 1 recovery.
    * **Parasympathetic Hyperactivity (Yellow):** HRV abnormally high AND RHR abnormally low. Indicates deep systemic exhaustion. Cap intensity; permit steady endurance only.

### B. AlphaHRV (DFA-a1) Threshold Modeler
Analyzes fractal heart rate correlation to map metabolic thresholds without formal testing.
* **LT1 (Aerobic Threshold):** Identify the exact power output where real-time DFA-a1 intersects **0.75**.
* **LT2 (Critical Power):** Identify the power output where DFA-a1 intersects **0.50**.
* **Zone 2 Audit:** Flag any prescribed endurance ride where DFA-a1 drops below 0.75 for $>10\%$ of the ride. Downgrade target watts for the next session.

### C. Dynamic FRC ($W'$) Tracking
* **Anaerobic Battery:** Model the kilojoule drawdown and reconstitution of Functional Reserve Capacity during high-intensity micro-intervals.
* **Progression Trigger:** If minimum $W'$ balance during a sprint session stays above $40\%$, increase the wattage or rep count for the next session.

### D. Durability Profiling
* Calculate multi-state Power-Duration Curves (PDC).
* Track 1-minute and 5-minute peak power at **0 kJ (Fresh)**, **1,000 kJ (Fatigued)**, and **1,000 kJ (Deeply Fatigued)** to quantify structural endurance degradation.

### E. Aerobic Decoupling ($Pw:HR$)
* Track cardiac drift (Power-to-Heart-Rate ratio) between the first and second halves of steady-state aerobic rides.
* **Trigger:** Drift $> 5\%$ = Maintain current volume. Drift $< 5\%$ = Green light to increase interval duration.

---

## 3. Data Architecture & Ingestion

The system ingests raw telemetry directly from Garmin Connect, processed locally.

| Source | Telemetry | Ingestion Method |
| :--- | :--- | :--- |
| **Garmin Connect API** | Activities, HRV/RMSSD, RHR, Stress, Sleep | `src/ingestion/garmin_connect.py` with `garminconnect` + `garmin-auth` |
| **Garmin Export ZIP** | Historical activities, wellness (no HRV) | `src/ingestion/garmin_export.py` |

All processed data is stored in a local SQLite database (`cycling_agent.sqlite`). Raw FIT files are archived for offline reference.

**Storage locations:**

| Mode | Vault Path |
| :--- | :--- |
| **Home Assistant Add-on** | `/data/` (persistent volume managed by HA Supervisor) |
| **Standalone CLI** | `~/cycling-agent-data/` (override with `CYCLING_AGENT_VAULT` env var) |

---

## 4. Repository Structure & Privacy Fencing

To ensure the repository remains open-source and generic while protecting user data, the architecture separates the *engine* from the *state*.

```text
personal-cycling-agent/
├── .gitignore                  # Blocks .env, user_profile.md, *.sqlite
├── README.md                   # Public project description
├── USER_PROFILE_TEMPLATE.md    # Blank template for athlete goals/constraints
├── requirements.txt            # Python dependencies
├── repository.yaml             # HA add-on repository manifest
├── setup.py                    # Interactive setup wizard (standalone)
├── setup_cron.sh               # Daily cron automation (standalone)
├── addon/                      # Home Assistant add-on
│   ├── config.yaml             # Add-on manifest (ingress, options, schema)
│   ├── Dockerfile              # Container build from HA base image
│   ├── run.sh                  # Entrypoint: config → sync → Streamlit
│   ├── DOCS.md                 # Add-on user documentation
│   └── translations/           # Localized option descriptions
├── docs/                       # Technical documentation
│   ├── PLAN.md                 # This file
│   └── calculations.md         # Calculation assumptions & sources
└── src/
    ├── config.py                # Vault path resolver & env loader
    ├── main.py                  # Pipeline orchestrator
    ├── visualize.py             # Streamlit dashboard (ingress-compatible)
    ├── ingestion/               # API scripts (garmin_connect.py, garmin_export.py)
    ├── analytics/               # Math models (readiness, threshold, w_prime, etc.)
    ├── agent/                   # LLM integration (prompt_builder, llm_client, mqtt)
    └── db/                      # SQLite persistence (store.py)
```

**Privacy Rules:**

1. All API keys, credentials, and biometrics live exclusively in the vault directory (`config.env`).
2. All subjective goals, training history, and schedule topologies live exclusively in `user_profile.md`.
3. Neither file is ever committed to version control.
4. In add-on mode, credentials are configured through the HA add-on UI and stored in `/data/options.json`.
---

## 5. Current State & Roadmap

### Implemented

* **Data ingestion:** Garmin Connect API (`garminconnect` + `garmin-auth`) and Garmin Export ZIP importer.
* **Analytics engine:** Readiness, DFA-a1 thresholds, W' tracking, durability profiling, aerobic decoupling, training load (CTL/ATL/TSB), power zones.
* **Dashboard:** Streamlit visualization with route heatmap, power/HR charts, training load trends, and profile editing.
* **Home Assistant add-on:** Full add-on with Ingress dashboard, automatic sync on start, HA config UI for athlete profile, and persistent token storage.
* **Profile management:** Athlete profile editable through the dashboard **Profile** tab or the add-on configuration UI — no manual file editing required.
* **Standalone CLI:** `setup.py` wizard, cron automation, MQTT prescription output.

### Future Work

* **AI insights integration:** Wire OpenAI/Anthropic API keys (configured in add-on or `config.env`) to generate daily training recommendations from analytics output.
* **MQTT integration for add-on:** Publish readiness, training load, and prescriptions to MQTT for Home Assistant sensors.
* **Scheduled sync in add-on:** Add configurable sync interval (currently syncs on every container start) to avoid Garmin rate limiting.
* **MFA support for add-on:** Pre-auth flow for Garmin accounts with MFA enabled (currently requires manual tokenstore copy).