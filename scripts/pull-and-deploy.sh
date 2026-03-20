#!/usr/bin/env bash
# Pull latest gateway image from GHCR and deploy.
# Run on EC2: bash scripts/pull-and-deploy.sh
#
# First-time setup:
#   echo "GHCR_TOKEN" | docker login ghcr.io -u dharm0809 --password-stdin

set -euo pipefail

IMAGE="ghcr.io/dharm0809/walacor-gateway:latest"

echo "=== Pull & Deploy Gateway ==="

echo "[1/4] Pulling latest image..."
docker pull "$IMAGE"

echo "[2/4] Updating docker-compose to use GHCR image..."
# Override the gateway service to use the pre-built image instead of building
cd ~/Gateway
docker compose down gateway

echo "[3/4] Starting gateway with new image..."
GATEWAY_IMAGE="$IMAGE" docker compose up -d gateway

echo "[4/4] Waiting for healthy..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8002/health > /dev/null 2>&1; then
        echo "  Gateway healthy!"
        curl -s http://localhost:8002/health | python3.12 -m json.tool | head -5
        exit 0
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done

echo "  Gateway failed to start. Check: docker compose logs gateway --tail=20"
exit 1
