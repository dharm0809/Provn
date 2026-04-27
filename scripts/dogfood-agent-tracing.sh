#!/usr/bin/env bash
# Deploy + dogfood the agent-tracing v1 stack on gateway_dharm @ 35.165.21.8.
# Run from your laptop AFTER the feature branch is merged to main (or pass a
# branch name to deploy that branch directly for pre-merge smoke).
#
# Usage:
#   ./scripts/dogfood-agent-tracing.sh           # deploys main
#   ./scripts/dogfood-agent-tracing.sh feat-x    # deploys a specific branch
#
# Env overrides:
#   EC2_KEY=/path/to/key.pem  (default: $HOME/AWS/gateway_key.pem)

set -euo pipefail

EC2_HOST="ec2-user@35.165.21.8"
EC2_KEY="${EC2_KEY:-$HOME/AWS/gateway_key.pem}"
GW_DIR="~/Gateway_dharm"
GW_PORT=8100
BRANCH="${1:-main}"

ssh_run() { ssh -i "$EC2_KEY" -o StrictHostKeyChecking=accept-new "$EC2_HOST" "$@"; }

echo "==> [1/7] preflight"
ssh_run "echo connected as \$(whoami) on \$(hostname); uptime"

echo "==> [2/7] fetch + checkout $BRANCH"
ssh_run "cd $GW_DIR && git fetch --all --prune && git checkout $BRANCH && git pull --ff-only origin $BRANCH && git log --oneline | head -6"

echo "==> [3/7] capture pre-deploy version + Prometheus baseline"
ssh_run "cd $GW_DIR && git rev-parse --short HEAD > /tmp/gateway_dharm_version_before.txt; \
  curl -s http://127.0.0.1:$GW_PORT/metrics 2>/dev/null \
    | grep -E '^walacor_agent_reconstructor|^walacor_agent_run_manifests|^walacor_walacor_delivery' \
    > /tmp/gateway_dharm_metrics_before.txt 2>/dev/null \
    || echo 'no prior metrics (cold start ok)'"

echo "==> [4/7] restart gateway via existing helper (handles stop+start)"
ssh_run "bash ~/start_gateway_dharm.sh; sleep 4; tail -25 /tmp/gateway_dharm.log"

echo "==> [5/7] health + readiness"
ssh_run "curl -fsS http://127.0.0.1:$GW_PORT/health | head -c 400; echo; \
         curl -fsS http://127.0.0.1:$GW_PORT/v1/readiness | head -c 600; echo"

echo "==> [6/7] verify new agent-tracing endpoints"
ssh_run "
  echo '--- new lineage route ---'
  curl -fsS -o /dev/null -w 'GET /v1/lineage/agent-runs -> %{http_code}\n' \
    http://127.0.0.1:$GW_PORT/v1/lineage/agent-runs?limit=5 || true
  echo '--- new connections tile ---'
  curl -fsS http://127.0.0.1:$GW_PORT/v1/connections \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); \
        t=[t for t in d.get(\"tiles\",[]) if \"reconstructor\" in str(t).lower() or \"agent\" in str(t.get(\"id\",\"\")).lower()]; \
        print(json.dumps(t, indent=2) if t else \"NO RECONSTRUCTOR TILE — investigate\")'
  echo '--- new metrics ---'
  curl -s http://127.0.0.1:$GW_PORT/metrics \
    | grep -E '^walacor_agent_reconstructor|^walacor_agent_run_manifests' | head -20
"

echo "==> [7/7] kill-criteria watch commands (paste later)"
cat <<EOF

=== Kill-criteria watch (run from your laptop) ===

# 1. Cache size — hard kill at 1 GB
ssh -i \$EC2_KEY $EC2_HOST 'curl -s http://127.0.0.1:$GW_PORT/metrics | grep walacor_agent_reconstructor_cache_bytes'

# 2. Eviction rate — sudden spike = thrashing
ssh -i \$EC2_KEY $EC2_HOST 'curl -s http://127.0.0.1:$GW_PORT/metrics | grep walacor_agent_reconstructor_evictions_total'

# 3. Manifest delivery health
ssh -i \$EC2_KEY $EC2_HOST 'curl -s http://127.0.0.1:$GW_PORT/v1/connections | python3 -c "import json,sys; d=json.load(sys.stdin); print([t for t in d.get(\"tiles\",[]) if t.get(\"id\")==\"walacor_delivery\"])"'

# Tunnel (leave open in another terminal):
#   ssh -i \$EC2_KEY -L $GW_PORT:127.0.0.1:$GW_PORT $EC2_HOST -N
# Then visit:
#   http://127.0.0.1:$GW_PORT/lineage/?view=agent-runs
#   http://127.0.0.1:$GW_PORT/lineage/?view=connections

=== Generate real agent traffic ===

# Option A — Claude Code through the gateway (via tunnel):
#   export ANTHROPIC_BASE_URL=http://127.0.0.1:$GW_PORT/anthropic
#   export ANTHROPIC_API_KEY=\$(ssh -i \$EC2_KEY $EC2_HOST 'sudo cat /tmp/walacor-wal-dharm/gateway-bootstrap-key.txt')
#   claude   # do multi-turn tool sessions

# Option B — OpenWebUI on EC2 (already proxies through the gateway on port 3100)

# Option C — minimal OpenAI Agents SDK script: see README in this dir

EOF

echo "==> deploy complete. Let it soak >= 1h, then re-check kill-criteria above."
