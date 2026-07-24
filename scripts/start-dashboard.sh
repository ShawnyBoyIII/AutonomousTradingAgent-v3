#!/usr/bin/env bash
# Launch the canonical FastAPI + SSE + Jinja monitoring dashboard.
# Defaults: config.yaml, port 8080, bind 127.0.0.1.
# Usage:   ./scripts/start-dashboard.sh [--config FILE] [--port N] [--host H]
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG="config.yaml"
PORT="8080"
HOST="127.0.0.1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --port)   PORT="$2";   shift 2 ;;
    --host)   HOST="$2";   shift 2 ;;
    -h|--help)
      sed -n '2,4p; /^### /,/^$/p; /^### /,/^$/p' "$0" | head -20
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--config FILE] [--port N] [--host H]" >&2
      exit 2
      ;;
  esac
done

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "error: $PY not found — run setup.sh or 'python3 -m venv .venv && .venv/bin/python -m pip install -e .[dev]'" >&2
  exit 1
fi

# Refuse to run if the port is already bound (clearer error than uvicorn's).
if command -v lsof >/dev/null 2>&1 && lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "error: port $PORT is already in use. Free it or pass --port <N>." >&2
  echo "       (lsof -ti :$PORT | xargs kill)" >&2
  exit 1
fi

CONFIG_PATH="$CONFIG" "$PY" -m uvicorn ui.dashboard.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info
