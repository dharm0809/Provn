#!/usr/bin/env bash
# Pull latest gateway image from AWS ECR and deploy.
# Run on EC2: bash scripts/pull-and-deploy.sh
#
# Prerequisites (one-time):
#   aws configure  (set access key with ECR pull permissions)
#
# Override defaults with env vars:
#   AWS_REGION=us-east-1 AWS_ACCOUNT_ID=123456789012 bash scripts/pull-and-deploy.sh

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${ECR_REGISTRY}/provn-gateway:latest"

if [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "ERROR: Set AWS_ACCOUNT_ID env var or pass it inline:"
    echo "  AWS_ACCOUNT_ID=123456789012 bash scripts/pull-and-deploy.sh"
    exit 1
fi

echo "=== Pull & Deploy Gateway from ECR ==="

echo "[1/5] Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

echo "[2/5] Pulling latest image..."
docker pull "$IMAGE"

echo "[3/5] Stopping gateway..."
cd ~/Gateway
docker compose down gateway

echo "[4/5] Starting gateway with new image..."
GATEWAY_IMAGE="$IMAGE" docker compose up -d --no-build --force-recreate gateway

echo "[5/5] Waiting for healthy..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8002/health > /dev/null 2>&1; then
        echo "  Gateway healthy!"
        curl -s http://localhost:8002/health | python3 -m json.tool | head -5
        exit 0
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done

echo "  Gateway failed to start. Check: docker compose logs gateway --tail=20"
exit 1
