#!/usr/bin/env bash
# Sync src/ -> personal_cycling_agent/src/ to keep the HA add-on in sync.
# Run this before committing or building the add-on.
set -euo pipefail
cd "$(dirname "$0")"
rsync -a --exclude='__pycache__' src/ personal_cycling_agent/src/
echo "Synced src/ -> personal_cycling_agent/src/"