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
sed -i '/^GATEWAY_IMAGE=/d' .env 2>/dev/null || true
echo "GATEWAY_IMAGE=$IMAGE" >> .env
docker compose up -d --no-build --force-recreate gateway

echo "[5/5] Waiting for healthy + verifying install..."
# Read GATEWAY_PORT from .env (default 8002)
GATEWAY_PORT=$(grep -E '^GATEWAY_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
GATEWAY_PORT="${GATEWAY_PORT:-8002}"
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${GATEWAY_PORT}/health" > /dev/null 2>&1; then
        echo "  Gateway healthy on port ${GATEWAY_PORT}"
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done

# Run the post-deploy verifier — catches Walacor 400s, missing env, signing gaps.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$SCRIPT_DIR/verify-install.sh" ]]; then
    echo
    "$SCRIPT_DIR/verify-install.sh" || {
        echo "  Deploy completed with gaps. Check: docker compose logs gateway --tail=20" >&2
        exit 1
    }
fi
