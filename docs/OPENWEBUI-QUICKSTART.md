# OpenWebUI + Walacor Gateway — 2-Minute Quickstart

> **What you get:** Every conversation in your existing OpenWebUI is now part of a provable, immutable audit trail — Merkle-chain verified, PII-detected, policy-governed. Your users see zero difference.

---

## Option A: Fresh Stack (Recommended)

One command gives you Ollama + Gateway + OpenWebUI, fully wired:

```bash
git clone https://github.com/your-org/walacor-gateway
cd walacor-gateway/Gateway

WALACOR_GATEWAY_API_KEYS=your-secret-key \
docker compose -f deploy/docker-compose.yml \
  --profile openwebui --profile ollama \
  up -d
```

| Service | URL |
|---------|-----|
| Chat UI (OpenWebUI) | http://localhost:3000 |
| Governance Dashboard | http://localhost:8002/lineage/ |
| Gateway API | http://localhost:8002 |

Pull a model then start chatting:
```bash
docker exec -it $(docker compose ps -q ollama) ollama pull qwen3:4b
```

Every message you send appears in the governance dashboard within seconds with its chain sequence, policy verdict, and PII status.

---

## Option B: Add to Existing OpenWebUI

If you already run OpenWebUI, change two env vars and restart:

```bash
# Before (direct to Ollama)
OPENAI_API_BASE_URL=http://ollama:11434/v1

# After (through Gateway)
OPENAI_API_BASE_URL=http://gateway:8000/v1
OPENAI_API_KEY=your-gateway-key
ENABLE_FORWARD_USER_INFO_HEADERS=true
ENABLE_DIRECT_CONNECTIONS=false
ENABLE_OLLAMA_API=false
```

Your users notice nothing different. Gateway now governs every request.

---

## What Gateway Adds Invisibly

For every message sent through OpenWebUI, Gateway:

1. **Attests the model** — cryptographic proof of which model handled the request
2. **Evaluates policy** — configurable rules (block PII, restrict models, enforce budgets)
3. **Detects PII** — credit cards, SSNs, API keys blocked before reaching the model
4. **Chains the session** — every turn in a conversation is Merkle-linked; tamper-evident
5. **Records the audit trail** — immutable SQLite WAL, exportable for compliance

---

## Governance Dashboard

After sending a few messages, visit http://localhost:8002/lineage/ to see:

- **Sessions** — every conversation, linked by session chain
- **Chain verification** — cryptographic proof each turn is unmodified
- **Policy results** — ALLOWED / BLOCKED per request
- **PII incidents** — what was detected and what action was taken
- **Token usage** — per-user, per-model, per-period

---

## Required OpenWebUI Settings (for governed deployments)

| Setting | Value | Why |
|---------|-------|-----|
| `ENABLE_OLLAMA_API` | `false` | Forces all chat through Gateway |
| `ENABLE_FORWARD_USER_INFO_HEADERS` | `true` | User identity in audit trail |
| `ENABLE_DIRECT_CONNECTIONS` | `false` | Prevents governance bypass |
| `OPENAI_API_BASE_URL` | `http://gateway:8000/v1` | Routes traffic to Gateway |

> **Note:** `ENABLE_DIRECT_CONNECTIONS=false` is non-negotiable for a governed deployment. If a user adds their own API key in OpenWebUI's settings, their conversations bypass Gateway entirely and have no audit trail.

---

## Troubleshooting

**OpenWebUI shows no models**
Gateway auto-discovers models from Ollama. If the model selector is empty, ensure Ollama has at least one model pulled: `ollama pull qwen3:4b`. The model list refreshes every 60 seconds.

**Conversations don't appear in the dashboard**
Check that `ENABLE_FORWARD_USER_INFO_HEADERS=true` and `ENABLE_DIRECT_CONNECTIONS=false` are set. Verify Gateway is healthy: `curl http://gateway:8000/health`.

**Gateway returns 401**
Ensure `OPENAI_API_KEY` in OpenWebUI matches `WALACOR_GATEWAY_API_KEYS` in Gateway. These must be identical.
