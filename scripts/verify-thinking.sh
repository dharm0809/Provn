#!/usr/bin/env bash
# Verify that thinking content is stored separately in the audit trail.
# Usage: KEY=wgk-xxx bash scripts/verify-thinking.sh

set -uo pipefail

PORT="${GATEWAY_PORT:-8002}"
BASE="http://localhost:$PORT"
KEY="${KEY:-${GATEWAY_API_KEY:-}}"
MODEL="${GATEWAY_MODEL:-qwen3:4b}"
SESSION_ID="thinking-test-$(date +%s)"

if [ -z "$KEY" ]; then
    echo "ERROR: Set KEY or GATEWAY_API_KEY env var"
    exit 1
fi

echo "=== Verify Thinking Content Storage ==="
echo "  Model: $MODEL"
echo "  Key: ${KEY:0:10}..."
echo ""

# Step 1: Send request
echo "[1/3] Sending request to $MODEL (may take 30-60s for thinking models)..."

RESPONSE=$(curl -s --max-time 120 -X POST "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -H "X-Session-Id: $SESSION_ID" \
    -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"What is 17 times 23?"}],"max_tokens":500}')

if [ -z "$RESPONSE" ]; then
    echo "  ERROR: Empty response from gateway"
    echo "  Check: curl -s http://localhost:$PORT/health"
    exit 1
fi

echo "  Raw response (first 200 chars): ${RESPONSE:0:200}"
echo ""

echo "$RESPONSE" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except json.JSONDecodeError:
    print(f'  ERROR: Not valid JSON: {raw[:200]}')
    sys.exit(1)
msg = d.get('choices',[{}])[0].get('message',{})
content = msg.get('content','')
print(f'  Content ({len(content)} chars): {content[:200]}')
has_think = '<think>' in content
print(f'  <think> in response: {has_think}')
if not has_think:
    print('  (Good - thinking was stripped before sending to client)')
"

# Step 2: Wait for WAL write
echo ""
echo "[2/3] Waiting for WAL write..."
sleep 4

# Step 3: Check execution record
echo ""
echo "[3/3] Checking execution record..."

RECORD=$(curl -s --max-time 10 "$BASE/v1/lineage/sessions/$SESSION_ID" -H "X-API-Key: $KEY")

if [ -z "$RECORD" ]; then
    echo "  ERROR: Empty response from lineage API"
    exit 1
fi

echo "$RECORD" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except json.JSONDecodeError:
    print(f'  ERROR: Not valid JSON: {raw[:200]}')
    sys.exit(1)

records = d.get('records', [])
if not records:
    print('  No execution records found for this session')
    print(f'  Response: {raw[:200]}')
    sys.exit(1)

rec = records[0]
tc = rec.get('thinking_content') or ''
rc = rec.get('response_content') or ''

print(f'  response_content: {len(rc)} chars')
if rc:
    print(f'    Preview: {rc[:150]}')
print(f'  thinking_content: {len(tc)} chars')
if tc:
    print(f'    Preview: {tc[:150]}')

print('')
if tc and rc:
    print('  RESULT: PASS - thinking stored separately from response')
elif rc and not tc:
    print('  RESULT: OK - response exists, no thinking (model did not use <think> blocks)')
elif not rc and not tc:
    print('  RESULT: WARN - both fields empty')
else:
    print('  RESULT: WARN - thinking exists but response empty')
"
