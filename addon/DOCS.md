# Personal Cycling Agent

Cycling telemetry analytics with Garmin Connect sync, physiological readiness, and AI-powered insights.

## Getting Started

1. **Install the add-on** from the Home Assistant add-on store.
2. **Configure** your Garmin Connect credentials, athlete profile, and optional AI API keys in the add-on configuration.
3. **Start the add-on** — the dashboard appears as a panel in Home Assistant via Ingress.
4. **Edit your profile** in the **Profile** tab of the dashboard to set your athlete details, physiological baselines, and training goals.

## Configuration

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

## Dashboard

The add-on provides a live dashboard with four tabs:

- **Activity Detail** — Power, heart rate, and cadence charts for individual rides.
- **Trends** — Training load (CTL/ATL/TSB), W' capacity, and zone distribution over time.
- **Map** — GPS route heatmap with geocoding.
- **Profile** — Edit your athlete profile, physiological baselines, and training goals. Changes are saved to your vault directory and used by the analytics engine.

## Data Storage

All data is stored in the add-on's `/data` directory and persists across updates:

- `/data/config.env` — API keys and biometrics
- `/data/user_profile.md` — Athlete profile (editable in dashboard)
- `/data/data/cycling_agent.sqlite` — Activity and analytics database
- `/data/raw/fit/` — Raw FIT files from Garmin Connect
- `/data/.garminconnect/` — Cached Garmin auth tokens