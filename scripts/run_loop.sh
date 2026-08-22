#!/usr/bin/env bash
# Continuous local/VPS collector. Refresh interval is controlled by
# SCAN_INTERVAL_SECONDS (default 180s) — keep it >= 120s to stay polite.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m app.scan --loop "$@"
