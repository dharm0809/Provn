# Walacor Governance Pipeline for OpenWebUI

Surfaces Gateway governance metadata (chain verification, policy results,
content analysis, budget status) directly in the OpenWebUI chat interface.

## Install

1. Copy `governance_pipeline.py` into your OpenWebUI Pipelines server
2. Set environment variables:
   - `WALACOR_GATEWAY_URL` — Gateway base URL (default: `http://gateway:8000`)
   - `WALACOR_GATEWAY_API_KEY` — Gateway API key
3. Enable the pipeline in OpenWebUI admin panel

## What You See

After each assistant message:

```
─── Walacor Governance ─────────────────────────
🔒 Chain #4  ✅ Policy: pass  🛡️ Clean  💰 8,200 tokens remaining (18% used)
Execution: abc123ef... | Model: qwen3:1.7b (attested)
```

Operational alerts appear as system messages when budget thresholds are
crossed or model attestations are revoked.

## Configuration

In OpenWebUI admin, the pipeline exposes these valves:
- `gateway_url` — Gateway endpoint
- `gateway_api_key` — API key for status endpoint
- `show_footer` — Enable/disable governance footer (default: true)
- `show_alerts` — Enable/disable operational alerts (default: true)
