#!/usr/bin/env bash
# Provision Walacor schemas required by the gateway (ETIDs 9000031/32/33).
#
# Idempotent: re-running is a no-op once schemas exist.
# Reads WALACOR_SERVER / WALACOR_USERNAME / WALACOR_PASSWORD from .env
# (or the surrounding environment). Run BEFORE `docker compose up` on a
# fresh tenant — without provisioning, every write fails with 400
# "Invalid ETId" and the WAL fills indefinitely.
#
# Usage:
#     scripts/provision-walacor.sh                    # uses ./.env
#     scripts/provision-walacor.sh path/to/other.env  # explicit env file
#     WALACOR_SERVER=... WALACOR_USERNAME=... WALACOR_PASSWORD=... \
#         scripts/provision-walacor.sh --no-env       # env-only, no file
#
# Requires `docker` (uses the gateway image so the host needs no Python).

set -euo pipefail

ENV_FILE="${1:-.env}"
USE_ENV_FILE=1
if [[ "${1:-}" == "--no-env" ]]; then
    USE_ENV_FILE=0
fi

if [[ $USE_ENV_FILE -eq 1 ]]; then
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "ERROR: env file not found: $ENV_FILE" >&2
        echo "  Copy .env.example to .env and fill WALACOR_SERVER/USERNAME/PASSWORD," >&2
        echo "  or run with --no-env after exporting those vars." >&2
        exit 2
    fi
    # Extract only the three vars we need (avoids `source` errors on quoted/spaced values).
    WALACOR_SERVER=$(grep -E '^WALACOR_SERVER=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
    WALACOR_USERNAME=$(grep -E '^WALACOR_USERNAME=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
    WALACOR_PASSWORD=$(grep -E '^WALACOR_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
fi

: "${WALACOR_SERVER:?WALACOR_SERVER not set (in $ENV_FILE or environment)}"
: "${WALACOR_USERNAME:?WALACOR_USERNAME not set (in $ENV_FILE or environment)}"
: "${WALACOR_PASSWORD:?WALACOR_PASSWORD not set (in $ENV_FILE or environment)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_PY="$SCRIPT_DIR/setup_walacor_schemas.py"
if [[ ! -f "$SETUP_PY" ]]; then
    echo "ERROR: $SETUP_PY not found" >&2
    exit 2
fi

# Pick the running gateway image if present, else fall back to a sensible default.
GATEWAY_IMAGE="${GATEWAY_IMAGE:-}"
if [[ -z "$GATEWAY_IMAGE" ]] && [[ $USE_ENV_FILE -eq 1 ]]; then
    GATEWAY_IMAGE=$(grep -E '^GATEWAY_IMAGE=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
fi
if [[ -z "$GATEWAY_IMAGE" ]]; then
    # Default to whatever's running locally with `docker compose`.
    GATEWAY_IMAGE=$(docker inspect gateway-app --format '{{.Config.Image}}' 2>/dev/null || true)
fi
if [[ -z "$GATEWAY_IMAGE" ]]; then
    echo "ERROR: GATEWAY_IMAGE not set and no gateway-app container found." >&2
    echo "  Either set GATEWAY_IMAGE in $ENV_FILE or pull/run the gateway image first." >&2
    exit 2
fi

echo "Provisioning Walacor schemas on $WALACOR_SERVER using image $GATEWAY_IMAGE"

docker run --rm \
    -e WALACOR_SERVER="$WALACOR_SERVER" \
    -e WALACOR_USERNAME="$WALACOR_USERNAME" \
    -e WALACOR_PASSWORD="$WALACOR_PASSWORD" \
    -e PYTHONUNBUFFERED=1 \
    -v "$SETUP_PY:/tmp/setup_walacor_schemas.py:ro" \
    --entrypoint python \
    "$GATEWAY_IMAGE" -u /tmp/setup_walacor_schemas.py
