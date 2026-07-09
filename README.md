# 🚴‍♂️ Cycling AI Agent

Privacy-first, locally hosted AI cycling coach. Ingests raw telemetry (HRV, RHR, Power, DFA-a1) from Intervals.icu, calculates physiological readiness, and prescribes daily training via a local LLM.

## Architecture

```
Intervals.icu API → Ingestion → SQLite → Analytics → Prompt Builder → Local LLM → MQTT → Dashboard
```

### Analytics Engine

| Module | Purpose |
|---|---|
| **Readiness** | Two-factor autonomic state (Coping / Sympathetic Stress / Parasympathetic Hyperactivity) |
| **Thresholds** | DFA-a1 power intercepts for LT1 (0.75) and LT2 (0.50) |
| **W' Tracking** | Anaerobic battery drawdown/reconstitution with progression triggers |
| **Durability** | Power-Duration Curves at fresh, fatigued, and deeply fatigued states |
| **Decoupling** | Cardiac drift (Pw:HR ratio) between ride halves |

## Quick Start

### 1. Run the Setup Wizard

```bash
python setup.py
```

This interactively configures your credentials, LLM endpoint, and MQTT settings. All secrets are stored in `~/cycling-agent-data/` — **outside the git repository** — so they can never be accidentally committed. Passwords are stored as SHA-256 hashes. Raw FIT files from activities are archived in `~/cycling-agent-data/raw/`. Re-run anytime to update settings.

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Edit Your Profile

```bash
nano ~/cycling-agent-data/user_profile.md
```

Fill in your training goals, discipline, available days, and equipment.

### 4. Run the Pipeline

```bash
python -m src.main
```

Individual stages:

```bash
python -m src.main --ingest      # fetch data from Intervals.icu
python -m src.main --analyze     # run analytics on stored data
python -m src.main --prescribe   # generate LLM prescription
python -m src.main --verbose     # enable debug logging
```

### 5. Import Garmin History (Optional)

**Data export ZIP** — historical activities + wellness (no HRV):

```bash
python -m src.ingestion.garmin_export ~/Downloads/garmin_export.zip
```

This imports daily wellness (RHR, stress, steps, SpO2, body battery) and all activities (power, HR, TSS, distance). The raw ZIP is archived in `~/cycling-agent-data/raw/`.

**Garmin Connect API** — includes HRV/RMSSD (requires `garminconnect` package):

```bash
pip install garminconnect curl_cffi   # curl_cffi spoofs TLS fingerprints for Garmin's bot detection
python -m src.ingestion.garmin_connect    # syncs last 90 days
python -m src.ingestion.garmin_connect 365  # syncs last year
```

Set `GARMIN_EMAIL` and `GARMIN_PASSWORD` in `~/cycling-agent-data/config.env` (or via `python setup.py`). Auth tokens are cached — you only log in once.

### 6. Daily Automation

```bash
bash setup_cron.sh
```

Runs the pipeline at 05:00 each morning. Log output goes to `~/cycling-agent-data/data/pipeline.log`.

To remove: `bash setup_cron.sh --remove`.

### 7. MQTT Dashboard (Optional)

Install Mosquitto locally (`sudo apt install mosquitto`) or use any MQTT broker. Configure `MQTT_BROKER` and `MQTT_PORT` by re-running `python setup.py`.

Subscribe to `cycling/agent/prescription` from Home Assistant or another MQTT client.

## Security

All sensitive data lives in a **vault directory** outside the repository (`~/cycling-agent-data/` by default):

| Path | Contents |
|---|---|
| `~/cycling-agent-data/config.env` | API keys, LLM endpoint, MQTT, biometrics. Passwords stored as SHA-256 hashes. |
| `~/cycling-agent-data/user_profile.md` | Training goals, constraints, equipment |
| `~/cycling-agent-data/data/` | SQLite database, pipeline logs, prescriptions |
| `~/cycling-agent-data/raw/` | Raw FIT/TCX/GPX files downloaded from Intervals.icu |

**Password hashing:** API secrets and MQTT passwords are stored as `hash:<sha256>` with the plaintext kept in a `_RAW` companion variable. At runtime, the hash is verified before the plaintext is loaded into the process environment. If the hash doesn't match, the credential is rejected.

**Raw data archival:** Every activity file downloaded from Intervals.icu is saved to `~/cycling-agent-data/raw/` alongside the processed database, so you always have the original telemetry.

The repo itself contains zero secrets. Even if you accidentally run `git add -f .` on everything, nothing sensitive is in the tree.

Override the vault location with the `CYCLING_AGENT_VAULT` environment variable:

```bash
export CYCLING_AGENT_VAULT=/path/to/my/vault
```

## Project Structure

```
setup.py                   # Interactive setup wizard
setup_cron.sh              # Install daily cron job
src/
├── config.py                # Vault path resolver & env loader
├── main.py                  # Pipeline orchestrator
├── ingestion/
│   └── intervals_api.py     # Intervals.icu API client
├── analytics/
│   ├── readiness.py         # Two-factor readiness engine
│   ├── threshold.py         # DFA-a1 threshold modeler
│   ├── w_prime.py           # W' tracking
│   ├── durability.py        # Power-Duration Curves
│   └── decoupling.py        # Aerobic decoupling
├── agent/
│   ├── prompt_builder.py    # LLM prompt construction
│   ├── llm_client.py        # Ollama client
│   └── mqtt_publisher.py    # MQTT dashboard output
└── db/
    └── store.py             # SQLite persistence
```

## Requirements

- Python 3.10+
- Intervals.icu account with API key enabled
- Local LLM (Ollama recommended)
- Optional: MQTT broker (Mosquitto) for dashboard integration