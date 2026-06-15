#!/usr/bin/env bash
# Convenience launcher for the Nexus AI tracker daemon.
# Activates the local venv (if present) and starts the tracker.
set -euo pipefail
cd "$(dirname "$0")"

if [ -d "venv" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

exec python -m tracker.tracker
