#!/usr/bin/env bash
# Rotate the gateway API key safely.
#
# Run on the EC2/Docker host where the gateway + openwebui containers live:
#   bash scripts/rotate-key.sh new-key                       # cut over
#   bash scripts/rotate-key.sh new-key --append              # add alongside old key
#
# Why this script exists: Docker bakes env vars into containers at *creation*
# time. Changing WALACOR_GATEWAY_API_KEYS in .env and then running
# `docker restart` leaves the old key in the running OpenWebUI container's env,
# so OWUI keeps sending the stale key and the gateway returns 401. This script
# uses `docker compose up -d --force-recreate` for every container that bakes
# the gateway key — currently `gateway` and `openwebui` — so the env actually
# propagates.

set -euo pipefail

ENV_FILE="${ENV_FILE:-./.env}"
COMPOSE_FILE="${COMPOSE_FILE:-./docker-compose.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8002/health}"

usage() {
    cat <<EOF
Usage: bash scripts/rotate-key.sh NEW_KEY [--append]

  NEW_KEY     The new wgk-* key to install.
  --append    Keep the existing key(s) alongside the new one (cut-over window).
              Without --append the old keys are replaced entirely.

Env overrides:
  ENV_FILE      (default: ./.env)
  COMPOSE_FILE  (default: ./docker-compose.yml)
  HEALTH_URL    (default: http://localhost:8002/health)
EOF
    exit 1
}

[ $# -ge 1 ] || usage
NEW_KEY="$1"
shift
APPEND=0
case "${1:-}" in
    --append) APPEND=1 ;;
    "")       ;;
    *)        usage ;;
esac

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found" >&2
    exit 1
fi
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "ERROR: $COMPOSE_FILE not found" >&2
    exit 1
fi

echo "=== Gateway API key rotation ==="
echo "  env file:     $ENV_FILE"
echo "  compose file: $COMPOSE_FILE"
echo "  mode:         $([ "$APPEND" -eq 1 ] && echo append || echo replace)"
echo

# Compute the new value for WALACOR_GATEWAY_API_KEYS.
EXISTING="$(grep -E '^WALACOR_GATEWAY_API_KEYS=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
if [ "$APPEND" -eq 1 ] && [ -n "$EXISTING" ]; then
    UPDATED="$EXISTING,$NEW_KEY"
else
    UPDATED="$NEW_KEY"
fi

# Atomic .env update — sed -i in place isn't atomic on cross-FS targets.
TMP="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
grep -vE '^WALACOR_GATEWAY_API_KEYS=' "$ENV_FILE" > "$TMP"
echo "WALACOR_GATEWAY_API_KEYS=$UPDATED" >> "$TMP"
# Pin the single OWUI-facing key to the *new* key so OpenWebUI always uses one
# value, not the comma-list.
grep -vE '^WEBUI_GATEWAY_API_KEY=' "$TMP" > "$TMP.2"
mv "$TMP.2" "$TMP"
echo "WEBUI_GATEWAY_API_KEY=$NEW_KEY" >> "$TMP"
mv "$TMP" "$ENV_FILE"
trap - EXIT

echo "[1/3] .env updated."
grep -E '^(WALACOR_GATEWAY_API_KEYS|WEBUI_GATEWAY_API_KEY)=' "$ENV_FILE" | sed 's/=.*/=***redacted***/'

echo
echo "[2/3] Force-recreating gateway + openwebui so the new env is picked up..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate gateway openwebui

echo
echo "[3/3] Waiting for gateway to be healthy..."
for i in $(seq 1 30); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo "  Gateway healthy."
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 2
    if [ "$i" -eq 30 ]; then
        echo "ERROR: gateway never became healthy. Check 'docker logs gateway-app'." >&2
        exit 1
    fi
done

echo
echo "=== Verifying new key with curl ==="
status=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "X-API-Key: $NEW_KEY" \
    "${HEALTH_URL%/health}/v1/control/status")
if [ "$status" = "200" ]; then
    echo "  /v1/control/status with new key → HTTP 200 ✓"
else
    echo "  /v1/control/status with new key → HTTP $status ✗" >&2
    exit 1
fi

if [ "$APPEND" -eq 1 ] && [ -n "$EXISTING" ]; then
    echo
    echo "=== Cut-over window active ==="
    echo "  Old key(s) still accepted by the gateway:"
    echo "    $EXISTING"
    echo "  Re-run without --append once external callers are on the new key:"
    echo "    bash scripts/rotate-key.sh $NEW_KEY"
fi

echo
echo "Done."
