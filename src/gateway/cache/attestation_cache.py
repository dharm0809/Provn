"""Attestation cache: (provider, model_id) -> CachedAttestation. Fail-closed when expired and control plane unreachable."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

BLOCKED_STATUSES = ("revoked", "tampered", "failed")


@dataclass
class CachedAttestation:
    attestation_id: str
    model_id: str
    provider: str
    status: str
    fetched_at: datetime
    ttl_seconds: int
    tenant_id: str = ""
    verification_level: str = "self_reported"
    last_verified_at: str | None = None
    model_hash: str | None = None

    @property
    def is_expired(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.fetched_at).total_seconds()
        return elapsed > self.ttl_seconds

    @property
    def is_blocked(self) -> bool:
        return (self.status or "").lower() in BLOCKED_STATUSES


class AttestationCache:
    """In-memory cache keyed by (provider, model_id). Fail-closed on expiry if refresh fails."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str], CachedAttestation] = {}
        self._lock: Any = None  # optional asyncio.Lock if needed

    def _key(self, provider: str, model_id: str) -> tuple[str, str]:
        return (provider.strip().lower(), (model_id or "").strip())

    def get(self, provider: str, model_id: str) -> CachedAttestation | None:
        return self._cache.get(self._key(provider, model_id))

    def set(self, entry: CachedAttestation) -> None:
        self._cache[self._key(entry.provider, entry.model_id)] = entry

    def set_from_proof(self, provider: str, proof: dict) -> None:
        model_id = proof.get("model_id") or ""
        status = (proof.get("status") or "pending").lower()
        entry = CachedAttestation(
            attestation_id=proof.get("attestation_id") or "",
            model_id=model_id,
            provider=provider,
            status=status,
            fetched_at=datetime.now(timezone.utc),
            ttl_seconds=self._ttl,
            tenant_id=proof.get("tenant_id") or "",
            verification_level=proof.get("verification_level") or "self_reported",
            last_verified_at=proof.get("last_verified_at"),
            model_hash=proof.get("model_hash") or None,
        )
        self.set(entry)

    def invalidate(self, provider: str, model_id: str) -> None:
        self._cache.pop(self._key(provider, model_id), None)

    def clear(self) -> None:
        self._cache.clear()

    @property
    def entry_count(self) -> int:
        return len(self._cache)
