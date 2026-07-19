# Architecture

Personal Cycling Agent — end-to-end data flow from Garmin Connect to the Streamlit dashboard and AI coach.

## Overview

```
Garmin Connect API ──→ Ingestion ──→ SQLite ──→ Analytics ──→ UI / Coach
```

Five layers:

1. **Ingestion** — pulls data from Garmin Connect (wellness + activities)
2. **Database** — SQLite with raw, merged, and computed tables
3. **Analytics** — 16 modules computing training load, power metrics, readiness, etc.
4. **Coach Agent** — LLM with wiki knowledge base and direct DB query access
5. **UI** — Streamlit dashboard with 7 pages

---

## 1. Garmin Connect Ingestion

**Module**: `src/ingestion/garmin_connect.py`

### Authentication

Uses `garmin-auth` 0.3.0 wrapping Garmin SSO OAuth. Cached tokens persist across restarts. Token fallback chain:

1. `GARMIN_TOKENSTORE` environment variable
2. `/data/.garminconnect/` (container vault)
3. `~/.garminconnect/` (home directory)

First login requires email, password, and MFA OTP. After that, cached tokens auto-reuse.

### Rate Limiting

- **Default**: 1.0s between calls (60 req/min ceiling)
- **On 429**: Exponential backoff (×2, capped at 300s), uses `Retry-After` header
- **Retry wrapper**: `_retry_on_rate_limit(fn, max_retries=3)`
- **Reset**: `reset_rate_limiter()` at start of each sync session

### Wellness Sync — `sync_garmin()`

Two-phase fetch:

**Bulk (once per session, date range):**

| Garmin API | DB Column |
|---|---|
| `get_weigh_ins(start, end)` | `wellness.weight` |
| `get_body_composition(start, end)` | `wellness.weight` (fallback) |
| `get_daily_steps(start, end)` | `wellness.steps` |
| `get_endurance_score(start, end)` | `wellness.endurance_score` |
| `get_hill_score(start, end)` | `wellness.hill_score` |

**Per-day (only for dates with bulk data, not already in DB):**

| Garmin API | DB Column(s) |
|---|---|
| `get_hrv_data(date)` | `wellness.rmssd` |
| `get_sleep_data(date)` | `wellness.sleep_score`, `sleep_hours` |
| `get_stats(date)` | `wellness.resting_hr`, `stress` |
| `get_heart_rates(date)` | `wellness.resting_hr`, `min_hr`, `max_hr` |
| `get_respiration_data(date)` | `wellness.respiration_rate` |
| `get_spo2_data(date)` | `wellness.spo2` |
| `get_hydration_data(date)` | `wellness.hydration_ml` |
| `get_intensity_minutes_data(date)` | `wellness.intensity_minutes` |
| `get_body_battery(date)` | `wellness.body_battery` |
| `get_floors(date)` | `wellness.floors` |
| `get_training_readiness(date)` | `wellness.training_readiness_score` |
| `get_user_summary(date)` | `wellness.calories`, `active_calories`, `distance_m` |
| `get_lifestyle_logging_data(date)` | `raw_wellness` only |
| `get_morning_training_readiness(date)` | `raw_wellness` only |

All per-day responses are also stored as full JSON in `raw_wellness` for later re-extraction.

**Optimization**: Per-day endpoints are called only for dates that have bulk data (weight/steps), naturally limiting to days when a watch was worn.

### Activities Sync — `sync_activities()`

Three-phase pipeline:

| Phase | Action | DB Target |
|---|---|---|
| 1 | `get_activities(start, limit=100)` paginated | `raw_activities` (immutable) |
| 1.5 | Transform to store format | `activities` (initial write) |
| 2 | Download FIT, parse with `fitdecode` | `raw_fit_sessions`, `activity_streams` |
| 3 | Rebuild from all sources | `activities` (final merge) |

**Incremental sync**: Paginates from offset 0, stops when activity date ≤ `last_synced_date`. Resume offset saved on 429.

### FIT vs API Merge Strategy

Three-tier source of truth:

1. **API** (`raw_activities`): Baseline. Provides API-only fields: training effects, VO2 max, elevation, temperature, zones, max avg power durations.
2. **FIT** (`raw_fit_sessions`): Overrides API for HR and power metrics (avg_hr, max_hr, avg_power, max_power, calories). Device data is more accurate than server-side aggregation.
3. **Streams** (`activity_streams`): Stream-derived duration overrides both API and FIT (most accurate).

`refresh_activities()` rebuilds the `activities` table from all three sources. Source indicators (`source_duration`, `source_distance`, `source_power`, `source_hr`, `source_calories`) stored as `'FIT'` or `'API'` for provenance.

### Auto-Sync Scheduler

**Module**: `src/tasks/scheduler.py`

Daemon thread running in the background. Two independent cycles:

| Cycle | Interval | Call |
|---|---|---|
| Activities | 30 min (configurable) | `sync_activities(days=1)` |
| Wellness | 6 hours (configurable) | `sync_garmin(days=1)` |

Configuration via environment variables:

| Setting | Env Var | Default |
|---|---|---|
| Enabled | `AUTO_SYNC_ENABLED` | `false` |
| Activity interval | `AUTO_SYNC_ACTIVITY_MINUTES` | 30 |
| Wellness interval | `AUTO_SYNC_WELLNESS_HOURS` | 6 |

Collision avoidance: checks if a manual `BackgroundSync` is active; if so, skips the auto-sync cycle. On enable, triggers an immediate sync for both cycles.

---

## 2. Database

**Module**: `src/db/store.py`

SQLite at `/data/data/cycling_agent.sqlite`. WAL journal mode, NORMAL sync, 64MB cache.

### Core Tables

**`wellness`** (PK: `date`) — 24 columns

Daily health metrics: weight, resting_hr, rmssd, stress, sleep_score, sleep_hours, steps, spo2, body_battery, respiration_rate, floors, hydration_ml, intensity_minutes, training_readiness_score, endurance_score, hill_score, calories, active_calories, distance_m, min_hr, max_hr.

Upsert on `date`.

**`raw_wellness`** (PK: `date`, `source`) — 4 columns

Full JSON from each of the 14 Garmin per-day API endpoints. Source values: `hrv`, `sleep`, `stats`, `heart_rates`, `respiration`, `spo2`, `hydration`, `intensity_minutes`, `body_battery`, `floors`, `training_readiness`, `user_summary`, `lifestyle`, `morning_readiness`.

**`activities`** (PK: `id` = `garmin_{garmin_id}`) — 60+ columns

One row per ride. Core: id, start_date, activity_type, activity_name, duration, distance, average_power, max_power, average_hr, max_hr, calories, tss, normalized_power, intensity_factor. API-only: training effects, VO2 max, elevation, temperature, zones, max avg power at 13 durations. Provenance: source indicators for duration, distance, power, HR, calories.

Rebuilt from raw tables via `refresh_activities()`.

**`raw_activities`** (PK: `garmin_id`) — 19 columns

Immutable Garmin API activity summaries. Never overwritten after initial sync.

**`raw_fit_sessions`** (PK: `garmin_id`) — 11 columns

Immutable FIT-parsed session metrics. Never overwritten after initial parse.

**`activity_streams`** (PK: auto-increment `id`) — 5 columns

Per-second time series: activity_id, elapsed, metric, value. Metrics: `power`, `heart_rate`, `cadence`, `speed`, `altitude`. Indexed on `(activity_id)`, `(metric)`, `(activity_id, metric)`.

**`activity_metrics`** (PK: `activity_id`) — 13 columns

Computed analytics per ride: normalized_power, intensity_factor, tss, variability_index, w_prime_capacity, w_prime_min_balance, decoupling_drift, duration_sec, ftp_used, cp_used, hr_tss, hr_trimp.

**`sync_state`** (PK: `source`) — 4 columns

Last sync timestamps and resume offsets. Sources: `garmin_wellness`, `garmin_activities`.

### Supporting Tables

| Table | Purpose |
|---|---|
| `morning_checkin` | User-submitted daily readiness (soreness, mood, energy, etc.) |
| `daily_readiness` | Computed readiness combining wellness + checkin |
| `activity_routes` | GPS coordinates per activity |
| `training_log` | Planned vs actual training execution |
| `hr_calibration` | HR-to-power calibration factor |
| `validation_log` | Data quality check results |

---

## 3. Analytics

16 modules in `src/analytics/`. Each reads from DB tables and writes computed results back.

### Per-Activity Analytics

| Module | Input | Output | Stored In |
|---|---|---|---|
| `power_metrics.py` | activity_streams (power) | NP, IF, TSS, VI, time-in-zones, power-duration curve | `activity_metrics`, `activities` |
| `w_prime.py` | activity_streams (power) | W' capacity, W' balance | `activity_metrics` |
| `durability.py` | activity_streams (power) | Durability score, fatigue index | `activity_metrics` |
| `decoupling.py` | activity_streams (power + cadence) | Decoupling drift | `activity_metrics` |
| `strain_score.py` | activity_streams (power), CP | Multi-dimensional strain (CP, W', Pmax) | — |
| `threshold.py` | activity_streams (power) | FTP, LT1, LT2 estimates | — |
| `hr_training_load.py` | activity_streams (HR) | HR-TSS, HR-TRIMP | `activity_metrics` |

### Longitudinal Analytics

| Module | Input | Output | Stored In |
|---|---|---|---|
| `training_load.py` | activities (TSS) | CTL, ATL, TSB, fitness/fatigue | `daily_readiness` |
| `readiness.py` | wellness + training_load | Composite score, state, recommendation | `daily_readiness` |
| `recovery_model.py` | wellness + activities | ML recovery prediction (LASSO) | — |
| `three_dim_ir.py` | activities | 3D impulse-response model | — |
| `feedback_loop.py` | activities + metrics | Zone drift, intensity analysis | — |

### Planning

| Module | Input | Output |
|---|---|---|
| `prescription_engine.py` | readiness + load + profile | Training prescription |
| `weekly_planner.py` | readiness + schedule + profile | 7-day training plan |

### Key Formulas

**CTL / ATL** (exponential moving average):

```
alpha = 1 - exp(-ln(2) / half_life)
EMA[i] = (1 - alpha) * EMA[i-1] + alpha * TSS[i]
```

- CTL: half_life = 42 days (alpha ≈ 0.016, ~98.4% weight on previous)
- ATL: half_life = 7 days (alpha ≈ 0.094, ~90.6% weight on previous)
- TSB = CTL - ATL
- Fitness/Fatigue = CTL / ATL

**Normalized Power** (4th-power method, 30s MA):

```
NP = ( mean( MA_30s(power)^4 ) ) ^ 0.25
```

30-second moving average of raw power, then 4th power, then mean, then 4th root. Matches TrainingPeaks and Intervals.icu.

**TSS**:

```
TSS = NP * duration_seconds / (FTP * 3600) * 100
```

**Intensity Factor**: `IF = NP / FTP`

**Variability Index**: `VI = avg_power / NP`

**Readiness** (multi-modal composite):

Combines HRV deviation from 30-day baseline, RHR deviation, daily stress, and recent training load into a 0-100 score. States: optimal, coping, sympathetic_stress, parasympathetic_hyperactivity, exhausted. Based on Kiviniemi et al. 2007 and Alfonso et al. 2025.

---

## 4. Coach Agent

**Modules**: `src/agent/prompt_builder.py`, `src/agent/db_query.py`, `src/agent/llm_client.py`

### System Prompt

Built by `build_system_prompt()` with these sections:

1. **Role**: Expert cycling coach AI
2. **Rider Profile**: From `config.user_profile_path()`
3. **Biometrics**: Weight, date
4. **Today's Readiness**: State, RMSSD, RHR, recommendation
5. **Thresholds**: LT1/LT2 power
6. **W'**: Capacity and balance
7. **Durability**: Fatigue metrics
8. **Decoupling**: Power/cadence drift
9. **Recent Activities**: Last 14 rides
10. **Full Analysis**: CTL/ATL/TSB trend, power metrics, strain scores, W', CP, 3D IR

### Wiki Retrieval

Lazy, keyword-triggered. Checks user message for domain keywords:

| Domain | Keywords (examples) |
|---|---|
| Performance | CTL, ATL, TSS, W', CP, threshold, FTP |
| Wellness | HRV, RHR, readiness, recovery, sleep |
| ML | model, prediction, accuracy, feature |
| Health | knee, patellar, ITB, meniscus, tendinopathy, ligament |

If matched, searches the wiki and injects top 5 page snippets (~1KB). Preserves context window for reasoning.

### Database Query Tool

Coach can execute `QUERY: <SQL>` to fetch any data. Security:

- **Blocked keywords**: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, UNION, PRAGMA, VACUUM, LOAD_EXTENSION, READFILE, WRITEFILE
- **Blocked patterns**: `--` (comments), `/*`, `*/`, `;` (statement separator)
- **Auto-limit**: LIMIT 100 injected if not present
- **Read-only**: Must start with SELECT

### Conversation Logging

Every exchange (user input + coach response) saved to `memory_journal.md` with timestamps. Key facts extracted in the background.

---

## 5. UI — Streamlit

**Module**: `src/visualize.py` (3,428 lines)

Framework: Streamlit + Plotly. Wide layout. Sidebar navigation via `st.session_state.nav_page`. Auto-detects dark/light theme.

### Pages

| Page | Data Sources | Key Visualizations |
|---|---|---|
| **Dashboard** | wellness, activities, readiness, training_load | 7-day week strip with weather, readiness card, morning check-in, compact coach chat |
| **Activities** | activities, activity_streams, activity_metrics | Activity select, metric cards, power/HR zones, power-duration curve, stream charts |
| **Trends** | activities, wellness, activity_metrics | CTL/ATL/TSB chart, CP chart, HRV, RHR, weight, sleep, stress |
| **Map** | activity_routes | Route scatter map with city/radius filter |
| **Profile** | user_profile.json | Identity, FTP, baselines, goals, equipment, schedule (7×24 grid) |
| **Settings** | sync_state, config.env | Garmin auth, sync buttons, auto-sync config, LLM settings, memory journal |
| **Wiki** | wiki engine | Ingest, query, browse, lint, digest tabs |

### Sync in UI

- **Dashboard `🔄 Sync`**: Last 1 day, progress shows only on Dashboard
- **Settings `Sync All Historical`**: Full history (3650 days), progress shows only on Settings
- **Settings `Reparse FIT`**: Re-parses all FIT files, progress shows only on Settings
- **Settings `Force Resync`**: Clears sync state, re-downloads all activities

Background sync uses `BackgroundSync` (thread-safe progress). Manual syncs block auto-sync via collision detection.

---

## 6. LLM Wiki

**Module**: `src/wiki/`

Markdown-based knowledge base with structured ingestion. Three-phase ingest: source summary → entity/concept extraction → page writing. Prevents LLM output truncation.

### Structure

```
wiki/
  index.md          — auto-generated index
  log.md            — chronological operation log
  entities/         — named entities (people, organizations, studies)
  concepts/         — concepts and ideas
  sources/          — source paper summaries
  analyses/         — analytical pages
  syntheses/        — weekly digest pages
```

### Operations

| Operation | Module | Description |
|---|---|---|
| **Ingest** | `ingest.py` | Three-phase: summarize → extract → write pages |
| **Search** | `engine.py` | Keyword matching with relevance scoring |
| **Lint** | `lint.py` | Detect orphans, broken links, thin pages, stale pages, missing cross-refs |
| **Digest** | `digest.py` | Generate weekly synthesis pages |

### Current Content

208 pages: 82 entities, 111 concepts, 15 sources. Covers performance (3D IR, W' balance, ACWR, detraining), wellness (HRV, multi-modal readiness), ML (recovery prediction), and health (knee injury diagnostics, recovery protocols).

---

## 7. Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      GARMIN CONNECT API                         │
│  19 wellness endpoints + activity list + FIT downloads          │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                     INGESTION LAYER                              │
│  sync_garmin()     → wellness + raw_wellness                    │
│  sync_activities() → raw_activities → FIT parse → activities    │
│  scheduler.py      → auto-sync every 30min (act) / 6h (well)   │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                       DATABASE (SQLite)                          │
│  raw_activities    raw_fit_sessions    activity_streams          │
│  activities        activity_metrics    wellness                  │
│  raw_wellness      daily_readiness     morning_checkin           │
│  sync_state        activity_routes                               │
└────────┬──────────────────┬──────────────────────────────────────┘
         │                  │
         ▼                  ▼
┌────────────────┐  ┌─────────────────────────────────────────────┐
│   ANALYTICS    │  │              COACH AGENT                     │
│  16 modules    │  │  prompt_builder + wiki + db_query + LLM     │
│  CTL/ATL/TSB   │  │  Lazy wiki retrieval, safe SQL queries      │
│  NP/IF/TSS     │  │  Full conversation logging                   │
│  W'/durability │  └─────────────────────────────────────────────┘
│  readiness     │
│  thresholds    │
└────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                        STREAMLIT UI                              │
│  Dashboard  Activities  Trends  Map  Profile  Settings  Wiki    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 8. Deployment

Docker container `pca` (image `pca-test`) on port 8501.

```
docker run -d --name pca -p 8501:8501 \
  -v /home/joshua/cycling-agent-data:/data \
  -e CYCLING_AGENT_VAULT=/data \
  pca-test
```

- Source code baked into image (not volume-mounted)
- Data persists at `/data` (mounted from host)
- Garmin tokens at `/data/.garminconnect/`
- Wiki at `/data/wiki/`
- SQLite DB at `/data/data/cycling_agent.sqlite`

Home Assistant add-on: same image, data persists at `/addon_configs/personal_cycling_agent/`. Port mapping configurable via `config.json` `ports` section.