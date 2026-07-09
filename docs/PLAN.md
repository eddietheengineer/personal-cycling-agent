# 🚴‍♂️ Autonomous Local Cycling Agent: System Architecture & Implementation Plan

## 1. Project Overview
This repository contains the architecture and implementation plan for a privacy-first, locally hosted AI cycling coach. The system bypasses proprietary, black-box algorithms (e.g., Garmin Training Readiness) by directly ingesting raw telemetry (HRV, RHR, Power, DFA-a1) to calculate a rider's true physiological state. A local Large Language Model (LLM) then uses these statistical models, cross-referenced with a private user profile, to dynamically prescribe daily training.

---

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

The system prioritizes clean, pre-parsed data from cloud APIs, processed locally.

| Source | Telemetry | Ingestion Method |
| :--- | :--- | :--- |
| **Garmin Connect** | Overnight HRV, RHR, Daily Stress | Intervals.icu Wellness API (`/wellness`) |
| **Polar H10** | High-fidelity RR intervals | BLE broadcast to head unit |
| **AlphaHRV** | DFA-a1 values | Parsed from FIT developer fields via Intervals.icu |
| **Intervals.icu** | Power/HR arrays, $W'$ balance, TSS | Python `requests` script to `/activities` |

---

## 4. Repository Structure & Privacy Fencing

To ensure the repository remains open-source and generic while protecting user data, the architecture separates the *engine* from the *state*.

```text
cycling-ai-agent/
├── .gitignore                  # MUST block .env, USER_PROFILE.md, and *.sqlite
├── README.md                   # Public project description
├── USER_PROFILE_TEMPLATE.md    # Blank template for athlete goals/constraints
├── .env.example                # Blank template for API keys & biometrics
├── requirements.txt            # Python dependencies
├── docker-compose.yml          # Container orchestration
└── src/
    ├── ingestion/              # API scripts (intervals_api.py, garmin.py)
    ├── analytics/              # Math models (readiness.py, threshold.py)
    └── agent/                  # LLM integration (prompt_builder.py, llm_client.py)

```

**Privacy Rules:**

1. All API keys, user weight, and baseline biometrics live exclusively in `.env`.
2. All subjective goals, training history, and schedule topologies live exclusively in `USER_PROFILE.md`.
3. Neither file is ever committed to version control.

---

## 5. Development Implementation Roadmap

The local agent must build the system chronologically:

### Phase 1: The Data Layer

* Write `src/ingestion/intervals_api.py`.
* Authenticate using credentials from `.env`.
* Extract the last 90 days of daily wellness data and FIT file arrays.
* Store locally in a lightweight SQLite database.

### Phase 2: The Math Layer

* Write `src/analytics/readiness.py` to calculate the 30-day rolling baselines, standard deviation normal bands, and output the current Coping/Sympathetic/Parasympathetic state.
* Write `src/analytics/threshold.py` to calculate the DFA-a1/Power intercepts.

### Phase 3: The Context Layer

* Write `src/agent/prompt_builder.py`.
* This script reads `USER_PROFILE.md`, reads today's output from the Math Layer, and constructs a strict system prompt.
* *Example Prompt Injection:* "The user is in State 2 (Sympathetic Stress). Their terrain map limits today to 1 hour on the Road Bike. Generate a recovery-focused training plan."

### Phase 4: The Automation Layer

* Write `src/agent/llm_client.py` to post the constructed prompt to the local LLM endpoint (e.g., Ollama at `http://localhost:11434`).
* Containerize the entire stack with Docker.
* Set a daily cron job to run the pipeline at 05:00.
* Push the final LLM string output via MQTT to a local dashboard (e.g., Home Assistant).