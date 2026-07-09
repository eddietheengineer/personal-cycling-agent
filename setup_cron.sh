#!/usr/bin/env bash
# Install a cron job to run the cycling agent pipeline.
#
# Default (backfill): runs every 15 minutes, pulling one day of data per run.
#
# Usage:
#   bash setup_cron.sh          # backfill mode (every 15 min)
#   bash setup_cron.sh --daily  # maintenance mode (once daily at 05:00)
#   bash setup_cron.sh --remove # uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT="${CYCLING_AGENT_VAULT:-$HOME/cycling-agent-data}"
CRON_CMD="cd ${SCRIPT_DIR} && source .venv/bin/activate && python -m src.main >> ${VAULT}/data/pipeline.log 2>&1"

if [ "${1:-}" = "--remove" ]; then
    crontab -l 2>/dev/null | grep -v "python -m src.main" | crontab - || true
    echo "Removed cycling agent cron job."
    exit 0
fi

# Check if already installed
if crontab -l 2>/dev/null | grep -q "python -m src.main"; then
    echo "Cron job already installed. Use --remove to uninstall first."
    exit 0
fi

if [ "${1:-}" = "--daily" ]; then
    # Maintenance mode: once daily at 05:00
    (crontab -l 2>/dev/null; echo "0 5 * * * ${CRON_CMD}") | crontab -
    echo "Installed daily cron job at 05:00."
else
    # Backfill mode: every 15 minutes
    (crontab -l 2>/dev/null; echo "*/15 * * * * ${CRON_CMD}") | crontab -
    echo "Installed backfill cron job (every 15 minutes)."
fi
echo "Log output: ${VAULT}/data/pipeline.log"