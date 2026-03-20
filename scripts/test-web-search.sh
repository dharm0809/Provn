#!/usr/bin/env bash
# Test web search with full DDG results
KEY="${KEY:-${GATEWAY_API_KEY:-}}"
MODEL="${MODEL:-${GATEWAY_MODEL:-qwen3:30b}}"
PORT="${GATEWAY_PORT:-8002}"
QUERY="${1:-What is Python programming language}"

if [ -z "$KEY" ]; then
    KEY=$(docker compose logs gateway 2>&1 | grep "Auto-generated key" | tail -1 | grep -oP 'wgk-\S+')
fi

echo "Model: $MODEL | Query: $QUERY"
echo ""

curl -s --max-time 180 -X POST "http://localhost:$PORT/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Search: $QUERY\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"web_search\",\"description\":\"Search the web\",\"parameters\":{\"type\":\"object\",\"properties\":{\"query\":{\"type\":\"string\"}},\"required\":[\"query\"]}}}],\"stream\":false}" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    if 'error' in d:
        print(f'Error: {d[\"error\"]}')
    else:
        content = d.get('choices', [{}])[0].get('message', {}).get('content', '')
        print(f'Response ({len(content)} chars):')
        print(content[:500])
except Exception as e:
    print(f'Parse error: {e}')
    print(f'Raw: {raw[:300]}')
"
