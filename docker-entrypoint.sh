#!/usr/bin/env bash
set -euo pipefail

export DATA_DIR="${DATA_DIR:-/app/data}"
export WA_AUTH_DIR="${WA_AUTH_DIR:-${DATA_DIR}/wa_auth}"
export WA_BRIDGE_PORT="${WA_BRIDGE_PORT:-8091}"
export WHATSAPP_BRIDGE_URL="${WHATSAPP_BRIDGE_URL:-http://127.0.0.1:${WA_BRIDGE_PORT}}"
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=160}"
PORT="${PORT:-8080}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"

mkdir -p "${DATA_DIR}" "${WA_AUTH_DIR}"

# Run local loopback WhatsApp Linked-Device bridge in a self-restarting supervisor loop
if [ "${ENABLE_WHATSAPP_BRIDGE:-true}" = "true" ] && [ -f "/app/whatsapp_bridge/bridge.mjs" ]; then
  (
    while true; do
      echo "[entrypoint] Starting WhatsApp QR bridge on 127.0.0.1:${WA_BRIDGE_PORT}..."
      node /app/whatsapp_bridge/bridge.mjs || true
      echo "[entrypoint] WhatsApp bridge exited; restarting in 3s..."
      sleep 3
    done
  ) &
  WA_SUPERVISOR_PID=$!
  trap 'kill "${WA_SUPERVISOR_PID}" 2>/dev/null || true' EXIT TERM INT
fi

echo "[entrypoint] Launching Superset Job Pilot on 0.0.0.0:${PORT} (DATA_DIR=${DATA_DIR}, threads=${GUNICORN_THREADS})..."
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads "${GUNICORN_THREADS}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  wsgi:app
