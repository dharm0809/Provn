"""Tests for tenant-scoped attestation lookups in pipeline.model_resolver.

The attestation cache key is (provider, model_id, tenant_id) — the resolver
must thread tenant_id through every cache.get call so a tenant-A
attestation cannot satisfy a tenant-B request.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.cache.attestation_cache import AttestationCache, CachedAttestation
from gateway.pipeline.model_resolver import resolve_attestation


def _att(provider: str, model_id: str, tenant_id: str) -> CachedAttestation:
    return CachedAttestation(
        attestation_id=f"att-{tenant_id}-{model_id}",
        model_id=model_id,
        provider=provider,
        status="active",
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        tenant_id=tenant_id,
    )


@pytest.mark.anyio
async def test_resolver_returns_caller_tenant_attestation(anyio_backend):
    cache = AttestationCache()
    cache.set(_att("openai", "gpt-4", "tenant-a"))
    cache.set(_att("openai", "gpt-4", "tenant-b"))

    entry, err = await resolve_attestation(
        cache, "openai", "gpt-4", tenant_id="tenant-a",
    )
    assert err is None
    assert entry is not None
    assert entry.tenant_id == "tenant-a"
    assert entry.attestation_id == "att-tenant-a-gpt-4"


@pytest.mark.anyio
async def test_resolver_does_not_serve_other_tenants_attestation(anyio_backend):
    """Tenant-A attestation must not satisfy a tenant-B lookup."""
    cache = AttestationCache()
    cache.set(_att("openai", "gpt-4", "tenant-a"))

    entry, err = await resolve_attestation(
        cache, "openai", "gpt-4", tenant_id="tenant-b",
    )
    assert entry is None
    assert err is not None
    assert err.status_code == 403


@pytest.mark.anyio
async def test_resolver_default_tenant_is_empty_string(anyio_backend):
    """Calling without tenant_id matches the untenanted bucket only."""
    cache = AttestationCache()
    cache.set(_att("openai", "gpt-4", ""))  # untenanted

    entry, err = await resolve_attestation(cache, "openai", "gpt-4")
    assert err is None
    assert entry is not None

    # And tenant-a should miss
    entry_a, err_a = await resolve_attestation(
        cache, "openai", "gpt-4", tenant_id="tenant-a",
    )
    assert entry_a is None
    assert err_a is not None
    assert err_a.status_code == 403


@pytest.mark.anyio
async def test_resolver_refresh_uses_caller_tenant(anyio_backend):
    """try_refresh re-reads the cache under the same tenant key."""
    cache = AttestationCache()
    refresh_calls = 0

    async def _refresh() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        # On refresh, populate tenant-a's bucket only
        cache.set(_att("openai", "gpt-4", "tenant-a"))
        return True

    entry, err = await resolve_attestation(
        cache, "openai", "gpt-4", tenant_id="tenant-a", try_refresh=_refresh,
    )
    assert refresh_calls == 1
    assert err is None
    assert entry is not None
    assert entry.tenant_id == "tenant-a"


@pytest.fixture
def anyio_backend():
    return "asyncio"
