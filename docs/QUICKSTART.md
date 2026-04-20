# Walacor Gateway — 5-minute quickstart

## 1. Install

From the repo root:

```bash
pip install -e ./Gateway
```

Optional dependencies:

```bash
pip install ddgs                          # DuckDuckGo web search (ddgs library)
pip install 'walacor-gateway[redis]'      # Redis session/budget backends
pip install 'walacor-gateway[auth]'       # JWT/SSO authentication
pip install 'walacor-gateway[telemetry]'  # OpenTelemetry export
pip install 'walacor-gateway[compliance]' # PDF compliance report export (WeasyPrint)
```

> **macOS (Apple Silicon) note for compliance/PDF export:** WeasyPrint requires
> pango and cairo. After `brew install pango`, set the library path before
> running the gateway:
>
> ```bash
> export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
> ```

## 2. Environment file

Copy `.env.example` to `.env.gateway` (preferred) or `.env` and fill in the values
for your deployment. The gateway loads `.env.gateway` first, falling back to `.env`.

```bash
cp .env.example .env.gateway
# edit .env.gateway with your settings
```

## 3. Run as transparent proxy (no governance)

Skip attestation/policy/WAL for a quick test:

```bash
export WALACOR_SKIP_GOVERNANCE=true
export WALACOR_PROVIDER_OPENAI_KEY=sk-your-key   # optional, for forwarding to OpenAI

uvicorn gateway.main:app --host 0.0.0.0 --port 8002
```

Send a request:

```bash
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"Hi"}]}'
```

## 4. Run with full governance (embedded control plane)

No external control plane needed — the gateway manages attestations and policies locally:

```bash
export WALACOR_GATEWAY_TENANT_ID=dev-tenant
export WALACOR_GATEWAY_API_KEYS=my-secret-key
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
export WALACOR_GATEWAY_PROVIDER=ollama

uvicorn gateway.main:app --host 0.0.0.0 --port 8002
```

Models are auto-attested on first use. Manage attestations, policies, and budgets via the Control tab in the lineage dashboard or the `/v1/control/*` API.

## 5. Run with remote control plane (fleet mode)

For multi-gateway fleets, point secondary gateways at a primary:

```bash
export WALACOR_GATEWAY_TENANT_ID=dev-tenant
export WALACOR_CONTROL_PLANE_URL=http://primary-gateway:8000
export WALACOR_CONTROL_PLANE_API_KEY=shared-key

uvicorn gateway.main:app --host 0.0.0.0 --port 8002
```

## 6. Health, metrics, and dashboard

- `GET http://localhost:8002/health` — JSON health (cache, WAL, chain status)
- `GET http://localhost:8002/metrics` — Prometheus metrics
- `http://localhost:8002/lineage/` — Lineage dashboard with the following tabs:
  - **Overview** — live throughput chart, token usage + latency charts, session/attempts summary
  - **Intelligence** — ONNX model registry, candidate promotions, shadow metrics, verdict inspector
  - **Sessions** — browse sessions with user identity, question preview, per-session drill-down (chain verification, blockchain proof, pipeline trace waterfall)
  - **Attempts** — completeness-invariant attempt log with disposition statistics
  - **Control** — manage model attestations, policies, and budgets; discover models from providers
  - **Compliance** — export EU AI Act / NIST AI RMF / SOC 2 / ISO 42001 compliance reports (PDF)
  - **Playground** — interactive prompt testing against attested models with governance readout

## 7. Web search

Enable built-in web search so tool-aware models can query the web:

```bash
export WALACOR_WEB_SEARCH_ENABLED=true
export WALACOR_WEB_SEARCH_PROVIDER=duckduckgo   # or brave / serpapi
```

The DuckDuckGo provider uses the `ddgs` library (`pip install ddgs`) for full
web search results — not limited to encyclopedia/Instant Answers lookups.
Brave and SerpAPI providers require `WALACOR_WEB_SEARCH_API_KEY`.

## 8. Docker

From repo root:

```bash
docker compose up --build
```

This starts Gateway + Ollama + OpenWebUI. Gateway listens on port 8002, chat UI on port 3000.
