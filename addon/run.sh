#!/usr/bin/env bashio

# Personal Cycling Agent - Home Assistant Add-on entrypoint

set -e

# Read configuration from /data/options.json
GARMIN_EMAIL="$(bashio::config 'garmin_email')"
GARMIN_PASSWORD="$(bashio::config 'garmin_password')"
OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
OPENAI_MODEL="$(bashio::config 'openai_model')"
ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"
ANTHROPIC_MODEL="$(bashio::config 'anthropic_model')"
SYNC_INTERVAL="$(bashio::config 'sync_interval_hours')"
FTP_WATTS="$(bashio::config 'ftp_watts')"
MAX_HR="$(bashio::config 'max_hr')"

# Set up data directory
DATA_DIR="/data"
mkdir -p "${DATA_DIR}/data" "${DATA_DIR}/raw/fit"

# Create config.env from options
# Note: passwords are stored plaintext in /data/options.json by HA already;
# this is the add-on trade-off. The existing pbkdf2 hashing layer is bypassed.
cat > "${DATA_DIR}/config.env" <<EOF
GARMIN_EMAIL=${GARMIN_EMAIL}
GARMIN_PASSWORD=${GARMIN_PASSWORD}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_MODEL=${ANTHROPIC_MODEL}
FTP_WATTS=${FTP_WATTS}
MAX_HR=${MAX_HR}
SYNC_INTERVAL_HOURS=${SYNC_INTERVAL}
EOF

# Create user_profile.md from options
cat > "${DATA_DIR}/user_profile.md" <<EOF
# User Profile

- FTP: ${FTP_WATTS}W
- Max HR: ${MAX_HR}
EOF

# Set environment variables for the Python app
export GARMIN_EMAIL
export GARMIN_PASSWORD
export OPENAI_API_KEY
export OPENAI_MODEL
export ANTHROPIC_API_KEY
export ANTHROPIC_MODEL
export FTP_WATTS
export MAX_HR

# Use existing CYCLING_AGENT_VAULT env var (checked by config.py _vault_dir())
export CYCLING_AGENT_VAULT="${DATA_DIR}"

# Set Garmin token_dir to persistent /data so tokens survive container restart
export GARMIN_TOKENSTORE="${DATA_DIR}/.garminconnect"

# Set HASSIO_INGRESS for Streamlit native ingress support
# HA Supervisor sets HASSIO_INGRESS_ENTRY when ingress is enabled
if [ -n "${HASSIO_INGRESS_ENTRY}" ]; then
    export HASSIO_INGRESS="${HASSIO_INGRESS_ENTRY}"
fi

# Initialize database if needed
cd /app
python3 -c "
from src import config
config.setup()
from src.db.store import CyclingDB
db = CyclingDB(str(config.db_path('cycling_agent.sqlite')))
db.close()
print('Database initialized')
"

# Start Streamlit in background first so Ingress panel becomes available immediately
bashio::log.info "Starting dashboard on port 8501..."
python3 -m streamlit run src/visualize.py \
    --server.headless true \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    --server.enableCORS false \
    --server.enableXSSProtection false \
    --browser.gatherUsageStats false &
STREAMLIT_PID=$!

# Run sync/analyze in background if credentials are set
if [ -n "${GARMIN_EMAIL}" ] && [ -n "${GARMIN_PASSWORD}" ]; then
    bashio::log.info "Running sync and analysis in background..."
    (
        python3 -m src.main --sync 2>&1 || bashio::log.warning "Sync failed or rate-limited"
        python3 -m src.main --sync-routes 2>&1 || bashio::log.warning "Route sync failed"
        python3 -m src.main --analyze 2>&1 || bashio::log.warning "Analysis failed"
    ) &
fi

# Wait for Streamlit (keeps container alive)
wait ${STREAMLIT_PID}