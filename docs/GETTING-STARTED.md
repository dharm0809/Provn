# Getting Started — Walacor Gateway

Quick guide for developers and testers to connect to the gateway.

## 1. Gateway URL

| Environment | URL | Dashboard |
|------------|-----|-----------|
| EC2 (current) | `http://<EC2-PUBLIC-IP>:8002` | `http://<EC2-PUBLIC-IP>:8002/lineage/` |
| Docker local | `http://localhost:8002` | `http://localhost:8002/lineage/` |
| OpenWebUI | `http://<EC2-PUBLIC-IP>:3000` | Built-in chat UI |

## 2. API Key (Required)

The gateway requires an API key for all API calls. Without it you get `401 Unauthorized`.

### Get the key

**Option A — Set your own key** (recommended for production):

```bash
# In .env or docker-compose environment:
WALACOR_GATEWAY_API_KEYS=your-secret-key-here

# Multiple keys (comma-separated):
WALACOR_GATEWAY_API_KEYS=key-for-alice,key-for-bob,key-for-ci-pipeline
```

Restart the gateway after changing.

**Option B — Auto-generated key** (development):

If no keys are configured and the control plane is enabled (default), the gateway auto-generates a key at startup. Find it in the logs:

```bash
# Docker:
docker compose logs gateway 2>&1 | grep "Auto-generated key"

# Native:
grep "Auto-generated key" /tmp/gateway.log
```

Output: `Auto-generated key: wgk-aBcDeFgH...`

### Use the key

**In HTTP headers:**

```bash
curl http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY_HERE" \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"Hello"}]}'
```

**In OpenWebUI:**

OpenWebUI connects to the gateway internally (Docker network). Set the key in `docker-compose.yml`:

```yaml
openwebui:
  environment:
    - OPENAI_API_KEY=YOUR_KEY_HERE
```

**In the Lineage Dashboard:**

1. Open `http://<IP>:8002/lineage/`
2. Click the **Settings** gear icon (bottom-left)
3. Enter your API key
4. The dashboard uses it for all API calls

**In Python scripts:**

```python
import requests

API_KEY = "YOUR_KEY_HERE"
GATEWAY = "http://localhost:8002"

r = requests.post(f"{GATEWAY}/v1/chat/completions",
    headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
    json={"model": "qwen3:8b", "messages": [{"role": "user", "content": "Hello"}]})
print(r.json())
```

**In test scripts:**

```bash
export GATEWAY_API_KEY=YOUR_KEY_HERE
python3.12 tests/production/tier7_gauntlet.py
```

## 3. Available Models

| Model | Best for | Size | Thinking | Tools |
|-------|---------|------|----------|-------|
| **qwen3:8b** | Primary — reasoning + tool use | 5GB | Yes (stored in audit) | Yes |
| llama3.1:8b | Fast tool workloads | 4.9GB | No | Yes |

## 4. Key Endpoints

| Endpoint | Auth? | Description |
|----------|-------|-------------|
| `POST /v1/chat/completions` | Yes | Chat with any model (OpenAI-compatible) |
| `GET /health` | No | Gateway health check |
| `GET /metrics` | No | Prometheus metrics |
| `GET /v1/models` | No | List available models |
| `GET /v1/lineage/sessions` | Yes | List all chat sessions |
| `GET /v1/lineage/sessions/{id}` | Yes | Session execution detail |
| `GET /v1/lineage/executions/{id}` | Yes | Full execution record with tool events |
| `GET /v1/lineage/attempts` | Yes | Request attempt log |
| `GET /v1/lineage/verify/{id}` | Yes | Verify session chain integrity |
| `GET /v1/control/status` | Yes | Control plane status |
| `GET /v1/control/discover` | Yes | Discover available models |
| `GET /lineage/` | No | Dashboard UI (static HTML) |

## 5. Quick Test

```bash
# Set your key
export KEY="YOUR_KEY_HERE"

# Health check (no auth needed)
curl http://localhost:8002/health

# Chat (auth required)
curl -s http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"What is 2+2?"}],"max_tokens":50}'

# List sessions (auth required)
curl -s http://localhost:8002/v1/lineage/sessions -H "X-API-Key: $KEY"

# Verify a session chain
curl -s http://localhost:8002/v1/lineage/verify/SESSION_ID_HERE -H "X-API-Key: $KEY"
```

## 6. Running the Test Suite

```bash
# Set env vars
export GATEWAY_API_KEY="YOUR_KEY_HERE"
export GATEWAY_MODEL="qwen3:8b"

# Individual tiers
python3.12 tests/production/tier1_live.py       # Health, chain, WAL
python3.12 tests/production/tier2_security.py    # Auth, CORS, no stack traces
python3.12 tests/production/tier6_advanced.py    # Tools, attachments, content analysis
python3.12 tests/production/tier6_mcp.py         # MCP fetch/time, multi-tool
python3.12 tests/production/tier7_gauntlet.py    # 89 checks: CRUD, identity, PII, streaming, chain
python3.12 tests/production/tier8_security_deep.py  # 44 security checks
```

## 7. User Identity in Audit Trail

Every request is tracked with caller identity. Send these headers for richer audit:

```bash
curl http://localhost:8002/v1/chat/completions \
  -H "X-API-Key: $KEY" \
  -H "X-User-Id: alice@company.com" \
  -H "X-Team-Id: engineering" \
  -H "X-User-Roles: developer,reviewer" \
  -d '...'
```

These appear in the lineage dashboard next to each session.

If no identity headers are sent, the gateway logs the client IP as `anonymous@<IP>`.

## 8. Security Notes

- **API keys are required** for all `/v1/` endpoints except health, metrics, and models
- **CORS** is restricted to configured origins (default: same-origin only). Set `WALACOR_CORS_ALLOWED_ORIGINS=http://your-frontend.com` if needed
- **Rate limiting** is enabled by default (120 requests/minute per API key)
- **Request body limit** is 50MB (configurable via `WALACOR_MAX_REQUEST_BODY_MB`)
- **Content analysis** runs PII detection on all responses. High-risk PII (credit cards, SSNs) is blocked; low-risk (IPs, emails) triggers a warning
- **Thinking content** from reasoning models (qwen3) is stripped from the response but stored separately in the audit trail for compliance
