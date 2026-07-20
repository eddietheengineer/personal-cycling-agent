#!/usr/bin/env bash

# Personal Cycling Agent - Entrypoint
# Works both as a Home Assistant add-on and standalone.

set -e

# ── Detect environment ───────────────────────────────────────────────
# HA Supervisor sets HASSIO_INGRESS_ENTRY when ingress is enabled.
# Also check for /data directory (HA always mounts persistent /data).
# bashio is NOT available in custom images (python:3.12-slim base).
if [ -n "${HASSIO_INGRESS_ENTRY}" ] || [ -d "/data" ]; then
    DATA_DIR="/data"
    HA_MODE=true
else
    DATA_DIR="${DATA_DIR:-${CYCLING_AGENT_VAULT:-${HOME}/cycling-agent-data}}"
    HA_MODE=false
fi
export CYCLING_AGENT_VAULT="${DATA_DIR}"

# ── Set up data directory ────────────────────────────────────────────
mkdir -p "${DATA_DIR}/data" "${DATA_DIR}/raw/fit"

# ── Load or bootstrap config.env ─────────────────────────────────────
if [ -f "${DATA_DIR}/config.env" ]; then
    set -a
    source "${DATA_DIR}/config.env"
    set +a
else
    # First run — bootstrap config.env with defaults.
    # The UI (Settings page) is where users enter their actual values.
    GARMIN_EMAIL=""
    GARMIN_PASSWORD=""
    FTP_WATTS=180
    MAX_HR=0
    RESTING_HR=0
    LT1_POWER=0
    LT2_POWER=0
    SYNC_INTERVAL_HOURS=24

    cat > "${DATA_DIR}/config.env" <<EOF
GARMIN_EMAIL=${GARMIN_EMAIL}
# GARMIN_PASSWORD= (set via UI)
FTP_WATTS=${FTP_WATTS}
MAX_HR=${MAX_HR}
RESTING_HR=${RESTING_HR}
LT1_POWER=${LT1_POWER}
LT2_POWER=${LT2_POWER}
SYNC_INTERVAL_HOURS=${SYNC_INTERVAL_HOURS}
EOF
    set -a
    # shellcheck disable=SC1090
    source "${DATA_DIR}/config.env"
    set +a
fi

# ── Set environment variables for the Python app ─────────────────────
export GARMIN_EMAIL
export GARMIN_PASSWORD
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
" || true

# ── Start Streamlit ──────────────────────────────────────────────────
if $HA_MODE; then
    echo "[INFO] Starting dashboard on port 8501..."
else
    echo "Starting dashboard on port 8501..."
fi

if $HA_MODE; then
    xsrf_flag=""
else
    xsrf_flag="--server.enableXsrfProtection true"
fi
python3 -m streamlit run src/visualize.py \
    --server.headless true \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    ${xsrf_flag} \
    --browser.gatherUsageStats false &
STREAMLIT_PID=$!

# ── Run sync/analyze in background only if tokens are cached ─────────
# Do NOT run sync on first boot: the user must authenticate via the UI
# (which handles MFA/OTP). Background sync runs only when tokens exist,
# meaning the user has already completed auth through the dashboard.
TOKEN_DIR="${GARMIN_TOKENSTORE:-${DATA_DIR}/.garminconnect}"
if [ -d "${TOKEN_DIR}" ] && [ -n "$(ls -A "${TOKEN_DIR}" 2>/dev/null)" ]; then
    if $HA_MODE; then
        echo "[INFO] Cached tokens found — running sync and analysis in background..."
    else
        echo "Cached tokens found — running sync and analysis in background..."
    fi
    (
        python3 -m src.main --ingest 2>&1 || {
            if $HA_MODE; then
                echo "[WARNING] Sync failed or rate-limited"
            else
                echo "Warning: Sync failed or rate-limited"
            fi
        }
        python3 -m src.main --sync-routes 2>&1 || {
            if $HA_MODE; then
                echo "[WARNING] Route sync failed"
            else
                echo "Warning: Route sync failed"
            fi
        }
        python3 -m src.main --analyze 2>&1 || {
            if $HA_MODE; then
                echo "[WARNING] Analysis failed"
            else
                echo "Warning: Analysis failed"
            fi
        }
    ) &
else
    if $HA_MODE; then
        echo "[INFO] No cached Garmin tokens — authenticate via the dashboard to enable sync."
    else
        echo "No cached Garmin tokens — authenticate via the dashboard to enable sync."
    fi
fi

# Wait for Streamlit (keeps container alive)
wait ${STREAMLIT_PID}