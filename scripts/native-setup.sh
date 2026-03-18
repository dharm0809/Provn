#!/usr/bin/env bash
# Native gateway setup — runs gateway + MCP servers outside Docker.
# Ollama stays in Docker (models already pulled).
#
# Usage (on EC2):
#   bash scripts/native-setup.sh
#
# To stop:
#   kill $(cat /tmp/gateway.pid)

set -euo pipefail

GATEWAY_PORT="${GATEWAY_PORT:-8002}"
MODEL="${GATEWAY_MODEL:-qwen3:4b}"
WAL_PATH="/tmp/walacor-wal"
MCP_CONFIG="/tmp/mcp-servers.json"
LOG="/tmp/gateway.log"
PID_FILE="/tmp/gateway.pid"

echo "==========================================="
echo "  Walacor Gateway — Native Setup"
echo "==========================================="

# ── Kill any existing native gateway ──────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[1/8] Stopping existing gateway (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
    rm -f "$PID_FILE"
else
    echo "[1/8] No existing gateway to stop"
fi

# ── Stop Docker gateway (keep Ollama) ─────────────────────────────────────
echo "[2/8] Stopping Docker gateway (keeping Ollama)..."
docker compose stop gateway 2>/dev/null || true

# Verify Ollama is accessible
echo -n "  Checking Ollama... "
if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "OK"
else
    echo "Starting Ollama container..."
    docker compose up -d ollama
    for i in $(seq 1 30); do
        if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "  Ollama ready"
            break
        fi
        sleep 2
    done
fi

# Verify model is available
echo -n "  Checking $MODEL... "
if docker exec gateway-ollama-1 ollama list 2>/dev/null | grep -q "$MODEL"; then
    echo "OK"
else
    echo "pulling..."
    docker exec gateway-ollama-1 ollama pull "$MODEL"
fi

# ── Install gateway package ───────────────────────────────────────────────
echo "[3/8] Installing gateway package..."
cd ~/Gateway
python3.12 -m pip install -q -e ".[dev]" 2>&1 | tail -1

# ── Install MCP server packages ──────────────────────────────────────────
echo "[4/8] Installing MCP server packages..."
python3.12 -m pip install -q mcp-server-fetch 2>&1 | tail -1 || echo "  mcp-server-fetch: not found, will skip"
python3.12 -m pip install -q mcp-server-time 2>&1 | tail -1 || echo "  mcp-server-time: not found, will skip"

# ── Discover installed MCP servers ────────────────────────────────────────
echo "[5/8] Building MCP server config..."
MCP_SERVERS="["
FIRST=true

# Check for mcp-server-fetch
if python3.12 -c "import mcp_server_fetch" 2>/dev/null; then
    echo "  Found: mcp-server-fetch"
    FIRST=false
    MCP_SERVERS="$MCP_SERVERS"'{"name":"fetch","transport":"stdio","command":"python3.12","args":["-m","mcp_server_fetch"]}'
elif python3.12 -c "from mcp_server_fetch import server" 2>/dev/null; then
    echo "  Found: mcp-server-fetch (server module)"
    FIRST=false
    MCP_SERVERS="$MCP_SERVERS"'{"name":"fetch","transport":"stdio","command":"python3.12","args":["-m","mcp_server_fetch"]}'
else
    echo "  mcp-server-fetch: not installed"
fi

# Check for mcp-server-time
if python3.12 -c "import mcp_server_time" 2>/dev/null; then
    echo "  Found: mcp-server-time"
    if [ "$FIRST" = false ]; then MCP_SERVERS="$MCP_SERVERS,"; fi
    MCP_SERVERS="$MCP_SERVERS"'{"name":"time","transport":"stdio","command":"python3.12","args":["-m","mcp_server_time"]}'
else
    echo "  mcp-server-time: not installed"
fi

MCP_SERVERS="$MCP_SERVERS]"
echo "$MCP_SERVERS" | python3.12 -m json.tool > "$MCP_CONFIG" 2>/dev/null || echo "$MCP_SERVERS" > "$MCP_CONFIG"
echo "  Config: $MCP_CONFIG"

# ── Prepare WAL directory ─────────────────────────────────────────────────
echo "[6/8] Preparing WAL directory..."
mkdir -p "$WAL_PATH"

# ── Start gateway ─────────────────────────────────────────────────────────
echo "[7/8] Starting gateway on port $GATEWAY_PORT..."

export WALACOR_GATEWAY_TENANT_ID=dev-tenant
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
export WALACOR_SKIP_GOVERNANCE=false
export WALACOR_TOOL_AWARE_ENABLED=true
export WALACOR_WEB_SEARCH_ENABLED=true
export WALACOR_LINEAGE_ENABLED=true
export WALACOR_CONTROL_PLANE_ENABLED=true
export WALACOR_THINKING_STRIP_ENABLED=true
export WALACOR_WAL_PATH="$WAL_PATH"
export WALACOR_MCP_SERVERS_JSON="$MCP_CONFIG"
export WALACOR_LOG_LEVEL=INFO
export WALACOR_GATEWAY_PORT="$GATEWAY_PORT"

cd ~/Gateway
nohup python3.12 -m uvicorn gateway.main:app \
    --host 0.0.0.0 --port "$GATEWAY_PORT" \
    --app-dir src > "$LOG" 2>&1 &
echo $! > "$PID_FILE"
echo "  PID: $(cat $PID_FILE)"
echo "  Log: $LOG"

# ── Wait for healthy ──────────────────────────────────────────────────────
echo "[8/8] Waiting for gateway to be healthy..."
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$GATEWAY_PORT/health" > /dev/null 2>&1; then
        echo "  Gateway healthy!"
        echo ""
        echo "==========================================="
        echo "  Gateway running natively on :$GATEWAY_PORT"
        echo "  Ollama in Docker on :11434"
        echo "  Model: $MODEL"
        echo "  WAL: $WAL_PATH"
        echo "  MCP config: $MCP_CONFIG"
        echo "  Log: $LOG"
        echo "  PID: $(cat $PID_FILE)"
        echo ""
        echo "  Health:  curl http://localhost:$GATEWAY_PORT/health"
        echo "  Lineage: http://localhost:$GATEWAY_PORT/lineage/"
        echo "  Stop:    kill \$(cat $PID_FILE)"
        echo ""
        echo "  Run tests:"
        echo "    GATEWAY_MODEL=$MODEL python3.12 tests/production/tier6_advanced.py"
        echo "    GATEWAY_MODEL=$MODEL bash tests/production/run_all_tiers.sh"
        echo "==========================================="

        # Show health summary
        echo ""
        curl -s "http://localhost:$GATEWAY_PORT/health" | python3.12 -m json.tool 2>/dev/null | head -15
        exit 0
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done

echo "  Gateway failed to start. Check logs:"
echo "    tail -50 $LOG"
tail -20 "$LOG"
exit 1
