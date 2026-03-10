"""Step 4: Build execution record for WAL and control plane.

Gateway does not hash; we send prompt_text and response_content. Walcor backend hashes them.
Records are built as dicts (no prompt_hash/response_hash) so no schema dependency on hashes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from gateway.adapters.base import ModelCall, ModelResponse


def build_execution_record(
    call: ModelCall,
    model_response: ModelResponse,
    attestation_id: str,
    policy_version: int,
    policy_result: str,
    tenant_id: str,
    gateway_id: str,
    user: str | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
    model_id: str | None = None,
    provider: str | None = None,
    latency_ms: float | None = None,
    retry_of: str | None = None,
    timings: dict | None = None,
    variant_id: str | None = None,
) -> dict:
    """Build execution record as dict (no prompt_hash/response_hash — backend hashes from content)."""
    usage = model_response.usage or {}
    return {
        "execution_id": str(uuid.uuid4()),
        "model_attestation_id": attestation_id,
        "model_id": model_id or call.model_id,
        "provider": provider,
        "policy_version": policy_version,
        "policy_result": policy_result,
        "tenant_id": tenant_id,
        "gateway_id": gateway_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "session_id": session_id,
        "metadata": metadata,
        "prompt_text": call.prompt_text or None,
        "response_content": model_response.content or None,
        "provider_request_id": model_response.provider_request_id,
        "model_hash": model_response.model_hash,
        "thinking_content": model_response.thinking_content or None,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "retry_of": retry_of,
        "timings": timings,
        "cache_hit": usage.get("cache_hit", False),
        "cached_tokens": usage.get("cached_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
        "variant_id": variant_id,
    }
