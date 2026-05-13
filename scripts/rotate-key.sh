#!/usr/bin/env bash
# Rotate the gateway API key safely.
#
# Run on the EC2/Docker host where the gateway + openwebui containers live:
#   bash scripts/rotate-key.sh new-key                       # cut over
#   bash scripts/rotate-key.sh new-key --append              # add alongside old key
#
# Why this script exists: Docker bakes env vars into containers at *creation*
# time. Changing the key in a file and running `docker restart` leaves the old
# key in the running container's env. This script edits the right files AND
# does `docker compose up -d --force-recreate` for both services so the new
# value actually propagates.
#
# Files updated:
#   .env.gateway      → WALACOR_GATEWAY_API_KEYS (the gateway's allowlist)
#   .env.openwebui    → OPENAI_API_KEY (OWUI's client credential when calling
#                       the gateway; pinned to the NEW key, not the comma-list)

set -euo pipefail

GATEWAY_ENV="${GATEWAY_ENV:-./.env.gateway}"
OWUI_ENV="${OWUI_ENV:-./.env.openwebui}"
COMPOSE_FILE="${COMPOSE_FILE:-./docker-compose.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8002/health}"

usage() {
    cat <<EOF
Usage: bash scripts/rotate-key.sh NEW_KEY [--append]

  NEW_KEY     The new wgk-* key to install.
  --append    Keep the existing key(s) in the gateway allowlist alongside the
              new one (cut-over window). Without --append the old keys are
              replaced entirely. OWUI is always pinned to the new key.

Env overrides:
  GATEWAY_ENV   (default: ./.env.gateway)   path to gateway env file
  OWUI_ENV      (default: ./.env.openwebui) path to OpenWebUI env file
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

for f in "$GATEWAY_ENV" "$OWUI_ENV" "$COMPOSE_FILE"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: $f not found" >&2
        exit 1
    fi
done

echo "=== Gateway API key rotation ==="
echo "  gateway env: $GATEWAY_ENV"
echo "  owui env:    $OWUI_ENV"
echo "  compose:     $COMPOSE_FILE"
echo "  mode:        $([ "$APPEND" -eq 1 ] && echo append || echo replace)"
echo

# ── .env.gateway: WALACOR_GATEWAY_API_KEYS ──────────────────────────────
EXISTING="$(grep -E '^WALACOR_GATEWAY_API_KEYS=' "$GATEWAY_ENV" | head -1 | cut -d= -f2- || true)"
if [ "$APPEND" -eq 1 ] && [ -n "$EXISTING" ]; then
    UPDATED="$EXISTING,$NEW_KEY"
else
    UPDATED="$NEW_KEY"
fi

# Atomic write: rewrite the file with the key replaced, then mv into place.
TMP_GW="$(mktemp "${GATEWAY_ENV}.XXXXXX")"
trap 'rm -f "$TMP_GW" "$TMP_OWUI"' EXIT
grep -vE '^WALACOR_GATEWAY_API_KEYS=' "$GATEWAY_ENV" > "$TMP_GW"
echo "WALACOR_GATEWAY_API_KEYS=$UPDATED" >> "$TMP_GW"
mv "$TMP_GW" "$GATEWAY_ENV"

# ── .env.openwebui: OPENAI_API_KEY pinned to the new key ───────────────
# OWUI ships this as `Authorization: Bearer …` to the gateway; we want one
# value, not the comma-list, so cut-over windows still leave OWUI on a
# single deterministic key.
TMP_OWUI="$(mktemp "${OWUI_ENV}.XXXXXX")"
grep -vE '^OPENAI_API_KEY=' "$OWUI_ENV" > "$TMP_OWUI"
echo "OPENAI_API_KEY=$NEW_KEY" >> "$TMP_OWUI"
mv "$TMP_OWUI" "$OWUI_ENV"
trap - EXIT

echo "[1/3] env files updated."
grep -E '^WALACOR_GATEWAY_API_KEYS=' "$GATEWAY_ENV" | sed 's/=.*/=***redacted***/'
grep -E '^OPENAI_API_KEY='            "$OWUI_ENV"    | sed 's/=.*/=***redacted***/'

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
