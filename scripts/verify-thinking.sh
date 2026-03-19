#!/usr/bin/env bash
# Verify that thinking content is stored separately in the audit trail.
# Usage: KEY=wgk-xxx bash scripts/verify-thinking.sh

set -euo pipefail

PORT="${GATEWAY_PORT:-8002}"
BASE="http://localhost:$PORT"
KEY="${KEY:-${GATEWAY_API_KEY:-}}"

if [ -z "$KEY" ]; then
    echo "ERROR: Set KEY or GATEWAY_API_KEY env var"
    exit 1
fi

echo "=== Verify Thinking Content Storage ==="
echo ""

# Step 1: Send a math question to qwen3:4b (thinking model)
echo "[1/3] Sending request to qwen3:4b..."
MODEL="${GATEWAY_MODEL:-qwen3:4b}"
SESSION_ID="thinking-test-$(date +%s)"

echo "  Model: $MODEL"
echo "  (Use GATEWAY_MODEL=qwen3:4b for thinking model test)"
echo ""

RESPONSE=$(curl -s -X POST "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -H "X-Session-Id: $SESSION_ID" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 17 times 23?\"}],\"max_tokens\":500}")

echo "$RESPONSE" | python3.12 -c "
import sys, json
d = json.load(sys.stdin)
msg = d.get('choices',[{}])[0].get('message',{})
content = msg.get('content','')
print(f'  Response ({len(content)} chars): {content[:200]}')
has_think = '<think>' in content
print(f'  <think> tags in response: {has_think}')
if not has_think:
    print('  (Good — thinking was stripped before sending to client)')
"

# Step 2: Wait for WAL write
echo ""
echo "[2/3] Waiting for WAL write..."
sleep 4

# Step 3: Check execution record
echo ""
echo "[3/3] Checking execution record in lineage..."

curl -s "$BASE/v1/lineage/sessions/$SESSION_ID" \
    -H "X-API-Key: $KEY" | python3.12 -c "
import sys, json
d = json.load(sys.stdin)
records = d.get('records', [])
if not records:
    print('  ERROR: No execution records found for this session')
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
    print('  RESULT: PASS — thinking stored separately from response')
elif rc and not tc:
    print('  RESULT: WARN — response exists but no thinking (model may not have used <think>)')
elif tc and not rc:
    print('  RESULT: WARN — thinking exists but response empty (all content was thinking)')
else:
    print('  RESULT: FAIL — both fields empty')
"
