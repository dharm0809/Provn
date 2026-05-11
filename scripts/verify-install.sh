#!/usr/bin/env bash
# Post-deploy verification: did `docker compose up` produce a healthy gateway?
#
# Checks the things that silently went wrong in the past:
#   * gateway-app container exists and is healthy
#   * env vars actually reached the container (WALACOR_SERVER, provider keys,
#     ETIDs, model routing) — missing values mean .env / compose mismatch
#   * cryptography is importable — without it records are written UNSIGNED
#   * Ed25519 signing key file exists in WAL volume
#   * Walacor delivery tile is GREEN, no recent 400s in the log
#
# Exits 0 on full green, 1 on any gap. Reads optional .env to find the admin
# API key for hitting /v1/readiness; falls back to /health (no auth needed).

set -euo pipefail

ENV_FILE="${1:-.env}"
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
gaps=0
note() { echo "  $1"; }
ok()   { echo "${GREEN}✓${NC} $1"; }
warn() { echo "${YELLOW}!${NC} $1"; gaps=$((gaps+1)); }
fail() { echo "${RED}✗${NC} $1"; gaps=$((gaps+1)); }

# ── Container present and healthy ───────────────────────────────────────────
if ! docker inspect gateway-app >/dev/null 2>&1; then
    fail "gateway-app container not found — run 'docker compose up -d' first"
    exit 1
fi
state=$(docker inspect -f '{{.State.Status}}' gateway-app)
health=$(docker inspect -f '{{.State.Health.Status}}' gateway-app 2>/dev/null || echo "none")
if [[ "$state" == "running" && "$health" == "healthy" ]]; then
    ok "gateway-app running + healthy"
else
    fail "gateway-app state=$state health=$health"
fi

# ── Env wiring ──────────────────────────────────────────────────────────────
env_check() {
    local var="$1" label="${2:-$1}"
    local val
    val=$(docker exec gateway-app sh -c "printenv $var 2>/dev/null" || true)
    if [[ -z "$val" ]]; then
        fail "$label is empty inside container"
    else
        ok "$label set"
    fi
}
env_check WALACOR_SERVER
env_check WALACOR_USERNAME
env_check WALACOR_PASSWORD
env_check WALACOR_GATEWAY_API_KEYS "API keys"
env_check WALACOR_EXECUTIONS_ETID "executions ETID"
env_check WALACOR_ATTEMPTS_ETID "attempts ETID"
env_check WALACOR_TOOL_EVENTS_ETID "tool_events ETID"

# Provider keys — at least one should be set or it's an Ollama-only install.
openai=$(docker exec gateway-app printenv WALACOR_PROVIDER_OPENAI_KEY 2>/dev/null || true)
anth=$(docker exec gateway-app printenv WALACOR_PROVIDER_ANTHROPIC_KEY 2>/dev/null || true)
if [[ -z "$openai" && -z "$anth" ]]; then
    note "no OpenAI or Anthropic key in container — Ollama-only install"
else
    [[ -n "$openai" ]] && ok "OpenAI key set"
    [[ -n "$anth" ]] && ok "Anthropic key set"
fi

# ── Cryptography importable (required for record signing) ──────────────────
if docker exec gateway-app python -c "import cryptography" 2>/dev/null; then
    ok "cryptography importable (record signing available)"
else
    fail "cryptography NOT importable — records will be written UNSIGNED. Rebuild image with [signing] extra."
fi

# ── Signing key persisted ──────────────────────────────────────────────────
if docker exec gateway-app test -f /var/walacor/wal/record-signing.ed25519.pem; then
    ok "Ed25519 signing key present in WAL volume"
else
    warn "Ed25519 signing key not yet generated (boots may need a minute or cryptography missing)"
fi

# ── /health response ───────────────────────────────────────────────────────
port=$(docker port gateway-app 8000/tcp 2>/dev/null | head -1 | awk -F: '{print $NF}')
port=${port:-8002}
if curl -s -m 5 "http://localhost:$port/health" >/tmp/_health.json; then
    status=$(python3 -c "import json,sys; print(json.load(open('/tmp/_health.json')).get('status'))" 2>/dev/null || echo "?")
    if [[ "$status" == "healthy" ]]; then
        ok "/health responds healthy on port $port"
    else
        fail "/health returned status=$status"
    fi
else
    fail "/health unreachable on port $port"
fi

# ── /v1/connections walacor_delivery tile ──────────────────────────────────
admin_key=""
if [[ -f "$ENV_FILE" ]]; then
    admin_key=$(grep -E '^WALACOR_GATEWAY_API_KEYS=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | cut -d, -f1 || true)
fi
if [[ -n "$admin_key" ]]; then
    if curl -s -m 5 -H "X-API-Key: $admin_key" "http://localhost:$port/v1/connections" >/tmp/_conn.json; then
        delivery_state=$(python3 -c "
import json,sys
d = json.load(open('/tmp/_conn.json'))
for t in d.get('tiles', []):
    if t.get('id') == 'walacor_delivery':
        print(t.get('status', '?'), t.get('subline', ''))
        break
else:
    print('?')" 2>/dev/null)
        if [[ "$delivery_state" == green* ]]; then
            ok "walacor_delivery: $delivery_state"
        elif [[ "$delivery_state" == "?" ]]; then
            note "no walacor_delivery tile yet (gateway may have just started)"
        else
            fail "walacor_delivery: $delivery_state — check provisioning and credentials"
        fi
    else
        warn "/v1/connections unreachable with API key"
    fi
else
    note "no API key found in $ENV_FILE — skipping /v1/connections check"
fi

# ── Recent Walacor write errors? ───────────────────────────────────────────
recent_errors=$(docker logs --since 2m gateway-app 2>&1 | grep -c "Walacor.*400" || true)
if [[ "$recent_errors" -gt 0 ]]; then
    fail "$recent_errors Walacor 400 errors in last 2m — likely 'Invalid ETId'. Run scripts/provision-walacor.sh."
fi

echo
if [[ $gaps -eq 0 ]]; then
    echo "${GREEN}All checks passed.${NC}"
    exit 0
else
    echo "${RED}$gaps gap(s) found.${NC} See messages above."
    exit 1
fi
