#!/usr/bin/env bash
# Ask every agent one live question over real ACP stdio. Needs network access.
#
# The a2a-bridge agent is probed too when ACP_A2A_ENDPOINTS points at a running A2A server:
#   ACP_A2A_ENDPOINTS="nyc311=http://127.0.0.1:8787" bash scripts/probe-all.sh
set -u

cd "$(dirname "$0")/.." || exit 1

python="${PYTHON:-python3}"
exec "$python" tools/probe_all.py "$@"
