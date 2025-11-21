#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

if command -v uv >/dev/null 2>&1; then
  uv sync
else
  if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
  fi
fi

export USE_ASSEMBLY=${USE_ASSEMBLY:-true}
export CONFIG_OVERRIDE_ENV=${CONFIG_OVERRIDE_ENV:-true}
export API_PASSWORD=${API_PASSWORD:-pwd}
export PANEL_PASSWORD=${PANEL_PASSWORD:-pwd}
export HOST=${HOST:-0.0.0.0}
export PORT=${PORT:-7861}
export PYTHONUNBUFFERED=1

if [ -n "${ASSEMBLY_API_KEYS:-}" ]; then
  export ASSEMBLY_API_KEYS
fi

echo "AMB2API 启动: http://${HOST}:${PORT}"
python web.py