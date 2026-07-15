#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec python -m uvicorn app.main:app --host "${DOLA_HOST:-0.0.0.0}" --port "${DOLA_PORT:-8787}"
