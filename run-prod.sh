#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export DOLA_ENV="${DOLA_ENV:-production}"
export DOLA_DEMO_MODE="${DOLA_DEMO_MODE:-false}"
export DOLA_HOST="${DOLA_HOST:-0.0.0.0}"
export DOLA_PORT="${DOLA_PORT:-8787}"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Load .env if present
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Force live unless user explicitly wants demo
export DOLA_DEMO_MODE="${DOLA_DEMO_MODE:-false}"
export DOLA_ENV="${DOLA_ENV:-production}"

echo "▶ Starting Dola Seedance LIVE on ${DOLA_HOST}:${DOLA_PORT} (demo=${DOLA_DEMO_MODE})"
# Single worker: in-process batch worker must not be multi-process forked
exec python -m uvicorn app.main:app \
  --host "${DOLA_HOST}" \
  --port "${DOLA_PORT}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips='*' \
  --log-level info
