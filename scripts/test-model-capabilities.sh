#!/usr/bin/env bash
# Test if a model supports both tool calling AND thinking/reasoning.
# Usage: MODEL=phi4:14b bash scripts/test-model-capabilities.sh
# Or:   MODEL=phi4:14b KEY=wgk-xxx bash scripts/test-model-capabilities.sh

set -uo pipefail

MODEL="${MODEL:-${GATEWAY_MODEL:-phi4:14b}}"
PORT="${GATEWAY_PORT:-8002}"
OLLAMA="http://localhost:11434"
GATEWAY="http://localhost:$PORT"
KEY="${KEY:-${GATEWAY_API_KEY:-}}"

echo "============================================"
echo "  Model Capability Test: $MODEL"
echo "============================================"
echo ""

# ── Test 1: Direct Ollama — Tool Calling ──────────────────────
echo "[1/4] Tool calling (direct Ollama)..."
TOOL_RESP=$(curl -s --max-time 180 -X POST "$OLLAMA/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "'"$MODEL"'",
        "messages": [{"role": "user", "content": "Search for: artificial intelligence"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for information",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        }],
        "stream": false
    }')

if [ -z "$TOOL_RESP" ]; then
    echo "  FAIL: Empty response (timeout or model not loaded)"
    echo "  Try: docker exec gateway-ollama-1 ollama pull $MODEL"
else
    echo "$TOOL_RESP" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    c = d['choices'][0]
    fr = c.get('finish_reason', '')
    tc = c.get('message', {}).get('tool_calls', [])
    content = (c.get('message', {}).get('content') or '')[:150]
    if tc or fr == 'tool_calls':
        print(f'  PASS: Model emitted tool_calls (finish_reason={fr}, {len(tc)} calls)')
    else:
        print(f'  FAIL: No tool_calls (finish_reason={fr})')
        if content:
            print(f'  Content: {content}')
except Exception as e:
    print(f'  ERROR: {e}')
    print(f'  Raw: {raw[:200]}')
"
fi

# ── Test 2: Direct Ollama — Thinking/Reasoning ────────────────
echo ""
echo "[2/4] Thinking/reasoning (direct Ollama)..."
THINK_RESP=$(curl -s --max-time 180 -X POST "$OLLAMA/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "'"$MODEL"'",
        "messages": [{"role": "user", "content": "What is 17 times 23? Think through each step."}],
        "stream": false,
        "max_tokens": 500
    }')

if [ -z "$THINK_RESP" ]; then
    echo "  FAIL: Empty response"
else
    echo "$THINK_RESP" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    msg = d['choices'][0]['message']
    content = msg.get('content', '')
    reasoning = msg.get('reasoning', '') or msg.get('reasoning_content', '')
    has_think_tags = '<think>' in content
    has_reasoning_field = bool(reasoning)
    has_step_reasoning = any(w in content.lower() for w in ['step', 'first', 'multiply', 'therefore', 'so,', 'let me'])

    print(f'  Content ({len(content)} chars): {content[:200]}')
    if reasoning:
        print(f'  Reasoning field ({len(reasoning)} chars): {reasoning[:200]}')
    print(f'  <think> tags: {has_think_tags}')
    print(f'  Reasoning field: {has_reasoning_field}')
    print(f'  Shows step-by-step: {has_step_reasoning}')
    if has_think_tags or has_reasoning_field or has_step_reasoning:
        print(f'  PASS: Model shows reasoning')
    else:
        print(f'  WARN: No explicit reasoning detected')
except Exception as e:
    print(f'  ERROR: {e}')
    print(f'  Raw: {raw[:200]}')
"
fi

# ── Test 3: Through Gateway — Tool Calling ────────────────────
echo ""
echo "[3/4] Tool calling (through gateway)..."
if [ -z "$KEY" ]; then
    echo "  SKIP: No KEY set (gateway requires auth)"
else
    GW_SESSION="cap-test-$(date +%s)"
    GW_RESP=$(curl -s --max-time 180 -X POST "$GATEWAY/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $KEY" \
        -H "X-Session-Id: $GW_SESSION" \
        -d '{
            "model": "'"$MODEL"'",
            "messages": [{"role": "user", "content": "Search for: test"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]
                    }
                }
            }],
            "stream": false
        }')

    if [ -z "$GW_RESP" ]; then
        echo "  FAIL: Empty response"
    else
        echo "$GW_RESP" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    if 'error' in d:
        print(f'  FAIL: {d[\"error\"][:150]}')
    else:
        content = (d.get('choices', [{}])[0].get('message', {}).get('content') or '')[:150]
        fr = d.get('choices', [{}])[0].get('finish_reason', '')
        print(f'  finish_reason: {fr}')
        print(f'  Content: {content}')
        # Gateway active tool loop consumes tool_calls, so finish_reason=stop is expected
        if 'search' in content.lower() or 'result' in content.lower():
            print(f'  PASS: Gateway executed tool loop (response mentions search)')
        elif fr == 'stop' and content:
            print(f'  OK: Got response (tool may or may not have been called)')
        else:
            print(f'  WARN: Unclear if tools were used')
except Exception as e:
    print(f'  ERROR: {e}')
    print(f'  Raw: {raw[:200]}')
"

    # Check lineage for tool events
    sleep 3
    LINEAGE=$(curl -s --max-time 10 "$GATEWAY/v1/lineage/sessions/$GW_SESSION" -H "X-API-Key: $KEY")
    echo "$LINEAGE" | python3.12 -c "
import sys, json, requests
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    records = d.get('records', [])
    if records:
        rec = records[0]
        tc = rec.get('thinking_content') or ''
        rc = rec.get('response_content') or ''
        print(f'  Lineage: response={len(rc)}c, thinking={len(tc)}c')
        for r in records:
            eid = r.get('execution_id') or r.get('id')
            if eid:
                er = requests.get(f'http://localhost:8002/v1/lineage/executions/{eid}', headers={'X-API-Key': '$KEY'})
                te = er.json().get('tool_events', [])
                if te:
                    print(f'  Lineage: {len(te)} tool events found')
                    for t in te:
                        print(f'    - {t.get(\"tool_name\")}: input_hash={t.get(\"input_hash\",\"\")[:16]}...')
                    break
    else:
        print(f'  Lineage: no records yet')
except:
    pass
" 2>/dev/null
    fi
fi

# ── Test 4: Through Gateway — Thinking Storage ────────────────
echo ""
echo "[4/4] Thinking content storage (through gateway)..."
if [ -z "$KEY" ]; then
    echo "  SKIP: No KEY set"
else
    THINK_SESSION="think-test-$(date +%s)"
    THINK_GW=$(curl -s --max-time 180 -X POST "$GATEWAY/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $KEY" \
        -H "X-Session-Id: $THINK_SESSION" \
        -d '{
            "model": "'"$MODEL"'",
            "messages": [{"role":"user","content":"What is 17 times 23? Think step by step."}],
            "stream": false,
            "max_tokens": 500
        }')

    if [ -z "$THINK_GW" ]; then
        echo "  FAIL: Empty response"
    else
        echo "$THINK_GW" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    if 'error' in d:
        print(f'  FAIL: {d[\"error\"][:150]}')
    else:
        content = (d.get('choices', [{}])[0].get('message', {}).get('content') or '')
        print(f'  Response ({len(content)} chars): {content[:200]}')
        print(f'  <think> stripped from response: {\"<think>\" not in content}')
except Exception as e:
    print(f'  ERROR: {e}')
" 2>/dev/null

        sleep 3
        curl -s --max-time 10 "$GATEWAY/v1/lineage/sessions/$THINK_SESSION" \
            -H "X-API-Key: $KEY" | python3.12 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    records = d.get('records', [])
    if records:
        rec = records[0]
        tc = rec.get('thinking_content') or ''
        rc = rec.get('response_content') or ''
        print(f'  Stored: response={len(rc)}c, thinking={len(tc)}c')
        if tc:
            print(f'  Thinking preview: {tc[:150]}')
            print(f'  PASS: Thinking content stored separately!')
        elif rc:
            print(f'  Response preview: {rc[:150]}')
            print(f'  OK: Response stored (no separate thinking — model may not use <think> tags)')
        else:
            print(f'  WARN: Both fields empty')
    else:
        print(f'  No execution records found')
except:
    print(f'  Could not parse lineage response')
" 2>/dev/null
    fi
fi

echo ""
echo "============================================"
echo "  Test complete for: $MODEL"
echo "============================================"
