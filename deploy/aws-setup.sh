#!/usr/bin/env bash
# Walacor Gateway — one-command AWS setup for m6a.xlarge
#
# Usage:
#   ssh ec2-user@<IP>
#   git clone <repo> Gateway && cd Gateway
#   bash deploy/aws-setup.sh
#
# Supports: Amazon Linux 2023, Ubuntu 22.04/24.04

set -euo pipefail

echo "=========================================="
echo "  Walacor Gateway — AWS Setup"
echo "=========================================="

# ── Detect OS ────────────────────────────────────────────
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="$ID"
else
    echo "ERROR: Cannot detect OS. Exiting."
    exit 1
fi

echo "[1/6] Installing Docker..."

if command -v docker &>/dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    case "$OS_ID" in
        amzn)
            sudo dnf update -y -q
            sudo dnf install -y -q docker
            sudo systemctl enable docker
            sudo systemctl start docker
            sudo usermod -aG docker "$USER"
            ;;
        ubuntu)
            sudo apt-get update -qq
            sudo apt-get install -y -qq ca-certificates curl
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
                sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
            sudo apt-get update -qq
            sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
            sudo usermod -aG docker "$USER"
            ;;
        *)
            echo "ERROR: Unsupported OS: $OS_ID. Install Docker manually, then re-run."
            exit 1
            ;;
    esac
    echo "  Docker installed: $(sudo docker --version)"
fi

# Ensure docker compose plugin is available
if ! docker compose version &>/dev/null && ! sudo docker compose version &>/dev/null; then
    echo "[1b/6] Installing Docker Compose plugin..."
    case "$OS_ID" in
        amzn)
            COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | head -1 | cut -d'"' -f4)
            sudo mkdir -p /usr/local/lib/docker/cli-plugins
            sudo curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
                -o /usr/local/lib/docker/cli-plugins/docker-compose
            sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
            ;;
        ubuntu)
            echo "  Docker Compose plugin should be installed with docker-ce. Checking..."
            ;;
    esac
fi

# Use sudo for docker if user not yet in docker group (new session needed)
DOCKER="docker"
if ! docker info &>/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

echo "[2/6] Applying m6a.xlarge tuning..."

# Create .env with CPU-inference optimized settings if not exists
if [ ! -f .env ]; then
    cat > .env <<'ENVEOF'
# Walacor Gateway — m6a.xlarge tuning (CPU-only Ollama)
WALACOR_PROVIDER_CONNECT_TIMEOUT=3.0
WALACOR_PROVIDER_TIMEOUT=90.0
WALACOR_LOG_LEVEL=INFO
ENVEOF
    echo "  Created .env with CPU inference tuning"
else
    echo "  .env already exists — skipping"
fi

echo "[3/6] Building Gateway image..."
$DOCKER compose build gateway

echo "[4/6] Starting services..."
$DOCKER compose up -d

echo "[5/6] Waiting for services to be healthy..."
# Wait for gateway health (up to 60s)
for i in $(seq 1 30); do
    if curl -sf http://localhost:8002/health >/dev/null 2>&1; then
        echo "  Gateway healthy"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  WARNING: Gateway not healthy after 60s. Check: $DOCKER compose logs gateway"
    fi
    sleep 2
done

# Wait for Ollama
for i in $(seq 1 15); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "  Ollama healthy"
        break
    fi
    sleep 2
done

echo "[6/6] Pulling default model (qwen3:1.7b)..."
$DOCKER exec gateway-ollama-1 ollama pull qwen3:1.7b || echo "  Model pull failed — you can pull manually later"

PUBLIC_IP=$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<YOUR_PUBLIC_IP>")

echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "  Chat UI:           http://${PUBLIC_IP}:3000"
echo "  Gateway Health:    http://${PUBLIC_IP}:8002/health"
echo "  Lineage Dashboard: http://${PUBLIC_IP}:8002/lineage/"
echo ""
echo "  Pull more models:  docker exec gateway-ollama-1 ollama pull <model>"
echo "  View logs:         docker compose logs -f"
echo "  Stop:              docker compose down"
echo ""
echo "  Tip: Stop the EC2 instance when not testing to save costs."
echo "=========================================="
