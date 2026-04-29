"""Step 1: Resolve (provider, model_id) to attestation. Fail-closed if missing/expired/unreachable."""

from __future__ import annotations

import logging
from starlette.responses import JSONResponse

from gateway.adapters.base import ModelCall
from gateway.cache.attestation_cache import AttestationCache, CachedAttestation

logger = logging.getLogger(__name__)


async def resolve_attestation(
    attestation_cache: AttestationCache,
    provider: str,
    model_id: str,
    *,
    tenant_id: str = "",
    try_refresh: callable | None = None,
) -> tuple[CachedAttestation | None, JSONResponse | None]:
    """
    Look up (provider, model_id, tenant_id) in attestation cache.
    Returns (attestation, None) if allowed; (None, error_response) if blocked or fail-closed.
    try_refresh: optional async callable() -> bool to refresh cache; if returns False, fail-closed.
    tenant_id: scope the cache lookup to the caller's tenant. Default "" matches the
    untenanted bucket used when the caller has no resolved tenant_id.
    """
    entry = attestation_cache.get(provider, model_id, tenant_id)

    if entry is None and try_refresh:
        ok = await try_refresh()
        if not ok:
            return None, JSONResponse(
                {"error": "Attestation cache stale, control plane unreachable"},
                status_code=503,
            )
        entry = attestation_cache.get(provider, model_id, tenant_id)

    if entry is None:
        return None, JSONResponse(
            {"error": "Model not attested or attestation unknown"},
            status_code=403,
        )

    if entry.is_blocked:
        return None, JSONResponse(
            {"error": "Model attestation revoked or tampered"},
            status_code=403,
        )

    if entry.is_expired and try_refresh:
        refreshed = await try_refresh()
        if not refreshed:
            return None, JSONResponse(
                {"error": "Attestation cache stale, control plane unreachable"},
                status_code=503,
            )
        entry = attestation_cache.get(provider, model_id, tenant_id)
        if entry is None or entry.is_blocked:
            return None, JSONResponse(
                {"error": "Model not attested or attestation revoked"},
                status_code=403,
            )
        if entry.is_expired:
            return None, JSONResponse(
                {"error": "Attestation cache stale, control plane unreachable"},
                status_code=503,
            )

    if entry.is_expired:
        return None, JSONResponse(
            {"error": "Attestation cache stale, control plane unreachable"},
            status_code=503,
        )

    return entry, None
