"""Sync client: pull attestations and policies from control plane. Startup sync required."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from gateway.config import get_settings
from gateway.cache.attestation_cache import AttestationCache
from gateway.cache.policy_cache import PolicyCache

logger = logging.getLogger(__name__)

_ATTESTATION_FETCH_LIMIT = 1000
_POLICY_FETCH_LIMIT = 500


class SyncClient:
    """Pull attestations and policies from control plane. Fail-closed if startup sync fails."""

    def __init__(
        self,
        control_plane_url: str,
        tenant_id: str,
        attestation_cache: AttestationCache,
        policy_cache: PolicyCache,
        api_key: str | None = None,
    ) -> None:
        self._base = control_plane_url.rstrip("/")
        self._tenant_id = tenant_id
        self._attestation_cache = attestation_cache
        self._policy_cache = policy_cache
        self._api_key = api_key
        self._last_attestation_sync: datetime | None = None
        self._last_policy_sync: datetime | None = None
        self._session: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    async def _client(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                http2=True,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.is_closed:
            await self._session.aclose()
            self._session = None

    async def sync_attestations(self, provider: str = "openai") -> bool:
        """Pull all attestation proofs for tenant (paginated) and fill attestation cache. Returns True on success."""
        try:
            client = await self._client()
            limit = _ATTESTATION_FETCH_LIMIT
            all_proofs: list[dict] = []
            offset = 0
            while True:
                r = await client.get(
                    f"{self._base}/v1/attestation-proofs",
                    params={"tenant_id": self._tenant_id, "limit": limit, "offset": offset},
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
                proofs = data.get("proofs") or []
                all_proofs.extend(proofs)
                if len(proofs) < limit:
                    break
                offset += limit
            self._attestation_cache.clear()
            for p in all_proofs:
                self._attestation_cache.set_from_proof(provider, p)
            self._last_attestation_sync = datetime.now(timezone.utc)
            logger.info("Synced %d attestations for tenant %s", len(all_proofs), self._tenant_id)
            return True
        except Exception as e:
            logger.warning("Attestation sync failed: %s", e)
            return False

    async def sync_policies(self) -> bool:
        """Pull policies for tenant and fill policy cache. Returns True on success."""
        try:
            client = await self._client()
            r = await client.get(
                f"{self._base}/v1/policies",
                params={"tenant_id": self._tenant_id, "limit": _POLICY_FETCH_LIMIT, "offset": 0},
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()
            policies = data.get("policies") or []
            version = self._policy_cache.next_version()
            self._policy_cache.set_policies(version, policies)
            self._last_policy_sync = datetime.now(timezone.utc)
            logger.info("Synced %d policies (version %d) for tenant %s", len(policies), version, self._tenant_id)
            return True
        except Exception as e:
            logger.warning("Policy sync failed: %s", e)
            return False

    async def startup_sync(self, provider: str = "openai") -> None:
        """Full sync at startup. Raises if control plane unreachable (fail-closed)."""
        a_ok = await self.sync_attestations(provider=provider)
        p_ok = await self.sync_policies()
        if not a_ok:
            raise RuntimeError("Gateway startup sync failed: could not fetch attestations from control plane")
        if not p_ok:
            raise RuntimeError("Gateway startup sync failed: could not fetch policies from control plane")

    @property
    def last_attestation_sync(self) -> datetime | None:
        return self._last_attestation_sync

    @property
    def last_policy_sync(self) -> datetime | None:
        return self._last_policy_sync
