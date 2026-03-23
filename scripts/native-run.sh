#!/usr/bin/env bash
set -e

cd ~/Gateway

echo "=== Pulling latest code ==="
git pull

echo "=== Installing Gateway ==="
python3.12 -m pip install -e . --quiet

echo "=== Stopping existing services ==="
# Stop gateway if running
pkill -f "uvicorn gateway.main:app" 2>/dev/null || true
# Stop OpenWebUI if running
docker rm -f openwebui 2>/dev/null || true

echo "=== Starting Ollama (if not running) ==="
if ! pgrep -x ollama > /dev/null; then
    ollama serve &
    sleep 3
fi

echo "=== Starting Gateway ==="
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_GATEWAY_API_KEYS=dev-key
export WALACOR_WAL_PATH=./wal
export WALACOR_LINEAGE_ENABLED=true
export WALACOR_CONTROL_PLANE_ENABLED=true
export WALACOR_PROVIDER_TIMEOUT=300
nohup python3.12 -m uvicorn gateway.main:app --host 0.0.0.0 --port 8002 > gateway.log 2>&1 &
echo "Gateway PID: $!"

echo "=== Waiting for Gateway health ==="
for i in $(seq 1 20); do
    if curl -sf http://localhost:8002/health > /dev/null 2>&1; then
        echo "Gateway healthy!"
        break
    fi
    sleep 3
done

echo "=== Starting Open WebUI ==="
docker run -d --name openwebui \
  -p 3000:8080 \
  -v webui-data:/app/backend/data \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8002/v1 \
  -e OPENAI_API_KEY=dev-key \
  -e ENABLE_OLLAMA_API=false \
  -e ENABLE_DIRECT_CONNECTIONS=false \
  -e WEBUI_NAME="Walacor Chat" \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  ghcr.io/open-webui/open-webui:main

echo ""
echo "=== All services running ==="
echo "Open WebUI:  http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo localhost):3000"
echo "Gateway:     http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo localhost):8002/health"
echo "Lineage:     http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo localhost):8002/lineage/"
