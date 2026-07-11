#!/usr/bin/env bash

# Personal Cycling Agent - Entrypoint
# Works both as a Home Assistant add-on (with bashio) and standalone.

set -e

# ── Detect environment ───────────────────────────────────────────────
if command -v bashio >/dev/null 2>&1; then
    # Running inside a Home Assistant add-on container
    GARMIN_EMAIL="$(bashio::config 'garmin_email')"
    GARMIN_PASSWORD="$(bashio::config 'garmin_password')"
    OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
    OPENAI_MODEL="$(bashio::config 'openai_model')"
    ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"
    ANTHROPIC_MODEL="$(bashio::config 'anthropic_model')"
    SYNC_INTERVAL="$(bashio::config 'sync_interval_hours')"
    FTP_WATTS="$(bashio::config 'ftp_watts')"
    MAX_HR="$(bashio::config 'max_hr')"
    RESTING_HR="$(bashio::config 'resting_hr')"
    LT1_POWER="$(bashio::config 'lt1_power')"
    LT2_POWER="$(bashio::config 'lt2_power')"
    DATA_DIR="/data"
else
    # Running standalone — use environment variables (from config.env or .env)
    DATA_DIR="${CYCLING_AGENT_VAULT:-${HOME}/cycling-agent-data}"
fi

# ── Set up data directory ────────────────────────────────────────────
mkdir -p "${DATA_DIR}/data" "${DATA_DIR}/raw/fit"

# ── Write config.env and user_profile.md (HA add-on only) ────────────
if command -v bashio >/dev/null 2>&1; then
    # Merge HA options into config.env without overwriting values set by the UI
    # (e.g. GARMIN_EMAIL/GARMIN_PASSWORD saved via the Settings page).
    # Load existing config.env if it exists.
    if [ -f "${DATA_DIR}/config.env" ]; then
        set -a
        # shellcheck disable=SC1090
        source "${DATA_DIR}/config.env"
        set +a
    fi

    # Only set from options if not already set by the UI.
    GARMIN_EMAIL="${GARMIN_EMAIL:-$(bashio::config 'garmin_email')}"
    GARMIN_PASSWORD="${GARMIN_PASSWORD:-$(bashio::config 'garmin_password')}"
    OPENAI_API_KEY="${OPENAI_API_KEY:-$(bashio::config 'openai_api_key')}"
    OPENAI_MODEL="${OPENAI_MODEL:-$(bashio::config 'openai_model')}"
    ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(bashio::config 'anthropic_api_key')}"
    ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$(bashio::config 'anthropic_model')}"
    FTP_WATTS="${FTP_WATTS:-$(bashio::config 'ftp_watts')}"
    MAX_HR="${MAX_HR:-$(bashio::config 'max_hr')}"
    RESTING_HR="${RESTING_HR:-$(bashio::config 'resting_hr')}"
    LT1_POWER="${LT1_POWER:-$(bashio::config 'lt1_power')}"
    LT2_POWER="${LT2_POWER:-$(bashio::config 'lt2_power')}"
    SYNC_INTERVAL="${SYNC_INTERVAL:-$(bashio::config 'sync_interval_hours')}"

    cat > "${DATA_DIR}/config.env" <<EOF
GARMIN_EMAIL=${GARMIN_EMAIL}
GARMIN_PASSWORD=${GARMIN_PASSWORD}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_MODEL=${ANTHROPIC_MODEL}
FTP_WATTS=${FTP_WATTS}
MAX_HR=${MAX_HR}
RESTING_HR=${RESTING_HR}
LT1_POWER=${LT1_POWER}
LT2_POWER=${LT2_POWER}
SYNC_INTERVAL_HOURS=${SYNC_INTERVAL}
EOF

    # Create user_profile.md from options only if it doesn't already exist
    # (users can edit their profile directly in the dashboard UI).
    if [ ! -f "${DATA_DIR}/user_profile.md" ]; then
        ATHLETE_NAME="$(bashio::config 'athlete_name')"
        WEIGHT_KG="$(bashio::config 'weight_kg')"
        HEIGHT_CM="$(bashio::config 'height_cm')"
        DISCIPLINE="$(bashio::config 'discipline')"
        PRIMARY_GOAL="$(bashio::config 'primary_goal')"
        SECONDARY_GOAL="$(bashio::config 'secondary_goal')"
        TRAINING_DAYS="$(bashio::config 'training_days')"
        MAX_SESSION="$(bashio::config 'max_session_duration')"
        TERRAIN="$(bashio::config 'terrain')"
        BIKES="$(bashio::config 'bikes')"
        POWER_METER="$(bashio::config 'power_meter')"
        HR_MONITOR="$(bashio::config 'hr_monitor')"
        cat > "${DATA_DIR}/user_profile.md" <<EOF
# Athlete Profile

## Identity
- Name: ${ATHLETE_NAME}
- Weight (kg): ${WEIGHT_KG}
- Height (cm): ${HEIGHT_CM}

## Training History
- Primary discipline: ${DISCIPLINE}

## Physiological Baselines
- FTP (watts): ${FTP_WATTS}
- Max HR: ${MAX_HR}
- Resting HR (avg): ${RESTING_HR}
- LT1 power (if known): ${LT1_POWER}
- LT2 power (if known): ${LT2_POWER}

## Goals & Constraints
- Primary goal: ${PRIMARY_GOAL}
- Secondary goal: ${SECONDARY_GOAL}
- Available training days: ${TRAINING_DAYS}
- Max session duration: ${MAX_SESSION}
- Terrain notes: ${TERRAIN}

## Equipment
- Bike(s): ${BIKES}
- Power meter: ${POWER_METER}
- HR monitor: ${HR_MONITOR}
EOF
    fi
fi

# ── Set environment variables for the Python app ─────────────────────
export GARMIN_EMAIL
export GARMIN_PASSWORD
export OPENAI_API_KEY
export OPENAI_MODEL
export ANTHROPIC_API_KEY
export ANTHROPIC_MODEL
export FTP_WATTS
export MAX_HR
export RESTING_HR
export LT1_POWER
export LT2_POWER

# Use existing CYCLING_AGENT_VAULT env var (checked by config.py _vault_dir())
export CYCLING_AGENT_VAULT="${DATA_DIR}"

# Set Garmin token_dir to persistent /data so tokens survive container restart
export GARMIN_TOKENSTORE="${DATA_DIR}/.garminconnect"

# Set HASSIO_INGRESS for Streamlit native ingress support
# HA Supervisor sets HASSIO_INGRESS_ENTRY when ingress is enabled
if [ -n "${HASSIO_INGRESS_ENTRY}" ]; then
    export HASSIO_INGRESS="${HASSIO_INGRESS_ENTRY}"
fi

# ── Initialize database if needed ────────────────────────────────────
cd /app
python3 -c "
from src import config
config.setup()
from src.db.store import CyclingDB
db = CyclingDB(str(config.db_path('cycling_agent.sqlite')))
db.close()
print('Database initialized')
"

# ── Start Streamlit ──────────────────────────────────────────────────
if command -v bashio >/dev/null 2>&1; then
    bashio::log.info "Starting dashboard on port 8501..."
else
    echo "Starting dashboard on port 8501..."
fi

python3 -m streamlit run src/visualize.py \
    --server.headless true \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    --server.enableXsrfProtection false \
    --browser.gatherUsageStats false &
STREAMLIT_PID=$!

# ── Run sync/analyze in background if credentials are set ────────────
if [ -n "${GARMIN_EMAIL}" ] && [ -n "${GARMIN_PASSWORD}" ]; then
    if command -v bashio >/dev/null 2>&1; then
        bashio::log.info "Running sync and analysis in background..."
    else
        echo "Running sync and analysis in background..."
    fi
    (
        python3 -m src.main --sync 2>&1 || {
            if command -v bashio >/dev/null 2>&1; then
                bashio::log.warning "Sync failed or rate-limited"
            else
                echo "Warning: Sync failed or rate-limited"
            fi
        }
        python3 -m src.main --sync-routes 2>&1 || {
            if command -v bashio >/dev/null 2>&1; then
                bashio::log.warning "Route sync failed"
            else
                echo "Warning: Route sync failed"
            fi
        }
        python3 -m src.main --analyze 2>&1 || {
            if command -v bashio >/dev/null 2>&1; then
                bashio::log.warning "Analysis failed"
            else
                echo "Warning: Analysis failed"
            fi
        }
    ) &
fi

# Wait for Streamlit (keeps container alive)
wait ${STREAMLIT_PID}