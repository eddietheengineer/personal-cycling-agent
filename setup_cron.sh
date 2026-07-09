#!/usr/bin/env bash
# Install a daily cron job to run the cycling agent pipeline at 05:00.
# Usage: bash setup_cron.sh
# To remove: bash setup_cron.sh --remove

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
    echo "Cron job already installed. Use --remove to uninstall."
    exit 0
fi

# Add cron job (05:00 daily)
(crontab -l 2>/dev/null; echo "0 5 * * * ${CRON_CMD}") | crontab -

echo "Installed daily cron job at 05:00."
echo "Log output: ${VAULT}/data/pipeline.log"