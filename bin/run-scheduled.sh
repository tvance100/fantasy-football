#!/bin/bash
# Wrapper for launchd / cron. Self-guarding: safe to run every few minutes —
# it only sends an email when a send window's deadline is near and unsent.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

exec >>"$LOG_DIR/scheduled.log" 2>&1
echo "----- $(date '+%Y-%m-%d %H:%M:%S %Z') -----"

# Use the project venv directly (no PATH / uv dependency).
"$PROJECT_DIR/.venv/bin/python" run.py --scheduled "$@"
