# 🚴‍♂️ Personal Cycling Agent

Cycling telemetry analytics with Garmin Connect sync, physiological readiness, and AI-powered insights. Install as a [Home Assistant add-on](#home-assistant-add-on) for automatic sync and a live dashboard, or run [standalone](#standalone-cli) for full control.

## Home Assistant Add-on

The recommended way to run the agent. Automatic Garmin sync, analytics, and a live dashboard — all embedded in your Home Assistant interface.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL prefilled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Feddietheengineer%2Fpersonal-cycling-agent)

**Or add manually:**

1. Go to **Settings → Add-ons → Add-on Store → ⋮ (menu) → Repositories**.
2. Add the URL: `https://github.com/eddietheengineer/personal-cycling-agent`
3. Find **Personal Cycling Agent** in the store and install it.
4. Configure your Garmin Connect credentials and AI API keys in the add-on configuration.
5. Start the add-on — the dashboard appears as a panel in Home Assistant via Ingress.

### Configuration

Configure your Garmin credentials, athlete profile, and AI keys through the add-on configuration UI.

| Option | Description |
|---|---|
| **Garmin email / password** | Required for automatic activity sync |
| **Athlete name** | Your name (shown in dashboard) |
| **Weight / Height** | Body metrics for power-to-weight calculations |
| **Discipline** | Primary discipline (road / gravel / MTB / TT) |
| **FTP / Max HR / Resting HR** | Physiological baselines for zone calculations |
| **LT1 / LT2 Power** | Aerobic and anaerobic thresholds (0 if unknown) |
| **Primary / Secondary Goal** | Training goals |
| **Training Days / Max Session** | Schedule constraints |
| **Terrain / Equipment** | Environment and gear notes |
| **OpenAI / Anthropic API key** | Optional, for AI-powered cycling insights |
| **Sync interval** | Hours between Garmin sync attempts |

You can also edit your profile directly in the **Profile** tab of the dashboard after the add-on is running.

## Architecture

```
Garmin Connect API → Ingestion → SQLite → Analytics → AI Insights → Dashboard
```

### Analytics Engine

| Module | Purpose |
|---|---|
| **Readiness** | Two-factor autonomic state (Coping / Sympathetic Stress / Parasympathetic Hyperactivity) |
| **Thresholds** | DFA-a1 power intercepts for LT1 (0.75) and LT2 (0.50) |
| **W' Tracking** | Anaerobic battery drawdown/reconstitution with progression triggers |
| **Durability** | Power-Duration Curves at fresh, fatigued, and deeply fatigued states |
| **Decoupling** | Cardiac drift (Pw:HR ratio) between ride halves |
| **Training Load** | CTL/ATL/TSB with exponential moving averages |
| **Route Heatmap** | GPS route visualization with geocoding |

## Standalone CLI

For users who prefer direct control or don't run Home Assistant.

### 1. Run the Setup Wizard

```bash
python setup.py
```

This interactively configures your credentials, LLM endpoint, and MQTT settings. All secrets are stored in `~/cycling-agent-data/` — **outside the git repository** — so they can never be accidentally committed. Passwords are stored as SHA-256 hashes. Raw FIT files from activities are archived in `~/cycling-agent-data/raw/`. Re-run anytime to update settings.

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Your Profile

Launch the dashboard and use the **Profile** tab to set your athlete details, physiological baselines, and training goals:

```bash
python -m src.main --visualize
```

Edit and save your profile directly in the browser.

### 4. Run the Pipeline

```bash
python -m src.main
```

Individual stages:

```bash
python -m src.main --ingest      # fetch data from Garmin Connect
python -m src.main --analyze     # run analytics on stored data
python -m src.main --prescribe   # generate LLM prescription
python -m src.main --visualize   # launch Streamlit dashboard
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
| `~/cycling-agent-data/raw/` | Raw FIT/TCX/GPX files downloaded from Garmin Connect |

**Password hashing:** API secrets and MQTT passwords are stored as `hash:<sha256>` with the plaintext kept in a `_RAW` companion variable. At runtime, the hash is verified before the plaintext is loaded into the process environment. If the hash doesn't match, the credential is rejected.

**Raw data archival:** Every activity file downloaded from Garmin Connect is saved to `~/cycling-agent-data/raw/` alongside the processed database, so you always have the original telemetry.

The repo itself contains zero secrets. Even if you accidentally run `git add -f .` on everything, nothing sensitive is in the tree.

Override the vault location with the `CYCLING_AGENT_VAULT` environment variable:

```bash
export CYCLING_AGENT_VAULT=/path/to/my/vault
```

## Project Structure

```
setup.py                   # Interactive setup wizard
setup_cron.sh              # Install daily cron job
repository.json            # Home Assistant add-on repository manifest
requirements.txt           # Python dependencies
personal_cycling_agent/  # Home Assistant add-on
├── config.yaml            # Add-on manifest (ingress, options schema)
├── Dockerfile             # Container build
├── run.sh                 # Entrypoint (config, sync, Streamlit)
├── DOCS.md                # Add-on user documentation
├── CHANGELOG.md           # Add-on changelog
├── icon.png / logo.png    # Add-on store assets
└── translations/          # Localized option descriptions
src/
├── config.py                # Vault path resolver & env loader
├── main.py                  # Pipeline orchestrator
├── visualize.py             # Streamlit dashboard (ingress-compatible)
├── ingestion/
│   ├── garmin_connect.py    # Garmin Connect API client
│   └── garmin_export.py     # Garmin data export importer
├── analytics/
│   ├── readiness.py         # Two-factor readiness engine
│   ├── threshold.py         # DFA-a1 threshold modeler
│   ├── w_prime.py           # W' tracking
│   ├── durability.py        # Power-Duration Curves
│   ├── decoupling.py        # Aerobic decoupling
│   └── training_load.py     # CTL/ATL/TSB calculations
├── agent/
│   ├── prompt_builder.py    # LLM prompt construction
│   ├── llm_client.py        # Ollama client
│   └── mqtt_publisher.py    # MQTT dashboard output
└── db/
    └── store.py             # SQLite persistence
```

## Requirements

- Python 3.10+
- Garmin Connect account
- Home Assistant (for add-on mode) or local Python environment (for standalone)
- Optional: OpenAI / Anthropic API key for AI insights
- Optional: MQTT broker (Mosquitto) for dashboard integration

## References

Detailed documentation for the calculation methods used by the analytics engine:

- [Calculation Assumptions & Sources](docs/calculations.md) — Formulas, sources, and assumptions for CP, W', TSS, CTL, DFA-a1, and more.
- [System Architecture Plan](docs/PLAN.md) — Full system design, data architecture, and implementation roadmap.