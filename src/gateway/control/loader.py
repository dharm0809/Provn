"""Self-sync at startup and periodic refresh from local control plane store."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def load_into_caches(store, ctx, settings) -> None:
    """Load attestations, policies, and budgets from DB into in-memory caches.

    Skips attestation/policy loading when a remote SyncClient is active
    (remote sync takes precedence).
    """
    tenant_id = settings.gateway_tenant_id or ""

    if ctx.sync_client is None:
        # Attestations
        if ctx.attestation_cache is not None:
            ctx.attestation_cache.clear()
            proofs = store.get_attestation_proofs(tenant_id)
            for p in proofs:
                ctx.attestation_cache.set_from_proof(p.get("provider", "ollama"), p)
            logger.info("Control plane: loaded %d attestations into cache", len(proofs))

        # Policies
        if ctx.policy_cache is not None:
            policies = store.get_active_policies(tenant_id)
            version = ctx.policy_cache.next_version()
            ctx.policy_cache.set_policies(version, policies)
            logger.info("Control plane: loaded %d active policies (version %d)", len(policies), version)

    # Budgets (always load — not covered by remote sync)
    if ctx.budget_tracker is not None:
        budgets = store.list_budgets(tenant_id)
        for b in budgets:
            ctx.budget_tracker.configure(
                b["tenant_id"], b.get("user") or None, b["period"], b["max_tokens"],
            )
        logger.info("Control plane: loaded %d budgets into tracker", len(budgets))

    # Load content policies into analyzers
    if store:
        from gateway.control.api import _refresh_content_policies
        _refresh_content_policies()


async def _run_local_sync_loop(settings, ctx) -> None:
    """Periodic refresh from local DB. Keeps policy_cache.fetched_at fresh,
    which fixes the fail_closed issue (staleness threshold never reached).

    Only created when no remote SyncClient (embedded mode).
    """
    store = ctx.control_store
    if store is None:
        return
    while True:
        await asyncio.sleep(settings.sync_interval)
        try:
            load_into_caches(store, ctx, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Local sync loop error: %s", e, exc_info=True)
