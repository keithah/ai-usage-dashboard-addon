#!/bin/sh
# Home Assistant add-on entrypoint for AI Usage Dashboard.
# Reads /data/options.json, validates options (redacted logs), writes the
# collector runtime config under /data, then polls in a loop.
# Handles SIGTERM/SIGINT for graceful Supervisor stops/restarts.
set -eu

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"
APP_DIR="${APP_DIR:-/app}"
GEN_CONFIG="${GEN_CONFIG:-/data/config.yaml}"
RUNTIME_ENV="${RUNTIME_ENV:-/tmp/aiud-runtime.env}"

echo "[ai-usage-dashboard] validating add-on options..."
if ! python3 -m ai_usage_dashboard.addon_options \
    --options "$OPTIONS_FILE" \
    --write-config "$GEN_CONFIG" \
    --write-env "$RUNTIME_ENV"; then
  echo "[ai-usage-dashboard] invalid options; fix the add-on Configuration tab (see add-on README). Stopping." >&2
  exit 1
fi

# Exports referenced secrets plus AIUD_POLL_INTERVAL / AIUD_STATE_FILE.
# shellcheck disable=SC1090
. "$RUNTIME_ENV"
: "${AIUD_POLL_INTERVAL:=900}"
: "${AIUD_STATE_FILE:=/data/state.json}"

if ! cd "$APP_DIR"; then
  echo "[ai-usage-dashboard] cannot enter $APP_DIR; stopping." >&2
  exit 1
fi

echo "[ai-usage-dashboard] starting collector loop (interval ${AIUD_POLL_INTERVAL}s, state ${AIUD_STATE_FILE})..."
SLEEP_PID=""
on_stop() {
  echo "[ai-usage-dashboard] stopping..."
  if [ -n "$SLEEP_PID" ]; then
    kill "$SLEEP_PID" 2>/dev/null || true
  fi
  exit 0
}
trap 'on_stop' TERM INT
while true; do
  if python3 -m ai_usage_dashboard publish --once \
      --config "$GEN_CONFIG" --state-file "$AIUD_STATE_FILE"; then
    echo "[ai-usage-dashboard] publish OK; next run in ${AIUD_POLL_INTERVAL}s"
  else
    echo "[ai-usage-dashboard] publish run reported errors (transient, auth, or unsupported); retrying in ${AIUD_POLL_INTERVAL}s" >&2
  fi
  sleep "$AIUD_POLL_INTERVAL" & SLEEP_PID=$!
  wait "$SLEEP_PID" || exit 0
  SLEEP_PID=""
done
