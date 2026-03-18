#!/usr/bin/env bash
# Full production test suite — run ON the EC2 from ~/Gateway
#
# Usage:
#   bash tests/production/run_all_tiers.sh
#
# Optional env vars:
#   GATEWAY_MODEL=qwen3:1.7b      (default)
#   GATEWAY_API_KEY=prod-test-key (set after adding key to .env)
#   QUICK=1                        (5-min sustained instead of 30-min)

set -euo pipefail

export GATEWAY_IP="${GATEWAY_IP:-localhost}"
export GATEWAY_PORT="${GATEWAY_PORT:-8002}"
export GATEWAY_MODEL="${GATEWAY_MODEL:-qwen3:1.7b}"

echo "==========================================="
echo "  Walacor Gateway — Production Test Suite"
echo "  Target: http://$GATEWAY_IP:$GATEWAY_PORT"
echo "  Model:  $GATEWAY_MODEL"
echo "==========================================="

# ── Install Python deps if missing ──────────────────────────────────────────
echo ""
echo "[setup] Checking Python dependencies..."
python3.12 -c "import aiohttp, requests" 2>/dev/null || {
    echo "  Installing aiohttp and requests..."
    python3.12 -m pip install --quiet aiohttp requests
}
echo "  Dependencies OK"

# ── Wait for gateway to be healthy ───────────────────────────────────────────
echo ""
echo "[setup] Waiting for gateway to be healthy..."
for i in $(seq 1 30); do
    if curl -sf "http://$GATEWAY_IP:$GATEWAY_PORT/health" > /dev/null 2>&1; then
        echo "  Gateway healthy"
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 5
done

# ── Tier 1: Audit Integrity (local) ─────────────────────────────────────────
echo ""
echo "=== TIER 1: Audit Integrity (local tests) ==="
bash tests/production/tier1_local.sh || { echo "TIER 1 LOCAL GATE FAILED"; exit 1; }

# ── Tier 1: Audit Integrity (live) ───────────────────────────────────────────
echo ""
echo "=== TIER 1: Audit Integrity (live checks) ==="
python3.12 tests/production/tier1_live.py || { echo "TIER 1 LIVE GATE FAILED"; exit 1; }

# ── Tier 2: Security Controls ────────────────────────────────────────────────
echo ""
echo "=== TIER 2: Security Controls ==="
python3.12 tests/production/tier2_security.py || { echo "TIER 2 GATE FAILED"; exit 1; }

# ── Tier 3: Performance Baseline ─────────────────────────────────────────────
echo ""
echo "=== TIER 3: Performance Baseline ==="
QUICK_FLAG="${QUICK:+--quick}"
python3.12 tests/production/tier3_performance.py ${QUICK_FLAG:-} || { echo "TIER 3 GATE FAILED"; exit 1; }

# ── Governance stress (populates sessions for Tier 5 chain audit) ─────────────
echo ""
echo "=== Pre-Tier 5: Governance Stress Run ==="
GATEWAY_URL="http://$GATEWAY_IP:$GATEWAY_PORT/v1/chat/completions" \
GATEWAY_MODEL="$GATEWAY_MODEL" \
python3.12 tests/governance_stress.py 2>&1 | tee tests/artifacts/governance_stress_output.txt || true
echo "  Stress run complete (failures above are non-blocking)"

# ── Tier 4: Resilience ────────────────────────────────────────────────────────
echo ""
echo "=== TIER 4: Resilience ==="
python3.12 tests/production/tier4_resilience.py || { echo "TIER 4 GATE FAILED"; exit 1; }

# ── Tier 5: Compliance Artifacts ──────────────────────────────────────────────
echo ""
echo "=== TIER 5: Compliance Artifacts ==="
python3.12 tests/production/tier5_compliance.py || { echo "TIER 5 GATE FAILED"; exit 1; }

echo ""
echo "==========================================="
echo "  ALL TIERS PASSED — LAUNCH READY"
echo "  Artifacts: tests/artifacts/"
ls tests/artifacts/ 2>/dev/null | sed 's/^/    /'
echo "==========================================="
