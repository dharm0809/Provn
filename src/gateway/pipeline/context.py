"""Pipeline context: caches, sync client, WAL, Walacor client, delivery worker. Set at app startup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway.cache.attestation_cache import AttestationCache
    from gateway.cache.policy_cache import PolicyCache
    from gateway.content.base import ContentAnalyzer
    from gateway.mcp.registry import ToolRegistry
    from gateway.pipeline.budget_tracker import BudgetTracker
    from gateway.pipeline.session_chain import SessionChainTracker
    from gateway.sync.sync_client import SyncClient
    from gateway.wal.delivery_worker import DeliveryWorker
    from gateway.wal.writer import WALWriter
    from gateway.walacor.client import WalacorClient


class PipelineContext:
    """Shared pipeline state. Populated in main.py on startup."""

    def __init__(self) -> None:
        # Phase 1–4
        self.attestation_cache: AttestationCache | None = None
        self.policy_cache: PolicyCache | None = None
        self.sync_client: SyncClient | None = None
        self.wal_writer: WALWriter | None = None
        self.delivery_worker: DeliveryWorker | None = None
        self.sync_loop_task: Any = None        # asyncio.Task for periodic sync
        self.skip_governance: bool = False     # transparent proxy mode
        # Phase 9
        self.http_client: Any = None           # shared httpx.AsyncClient
        # Phase 10
        self.content_analyzers: list[ContentAnalyzer] = []
        # Phase 11
        self.budget_tracker: BudgetTracker | None = None
        # Phase 13
        self.session_chain: SessionChainTracker | None = None
        # Walacor backend storage (replaces SQLite WAL when configured)
        self.walacor_client: WalacorClient | None = None
        # Phase 14: tool-aware gateway (active strategy)
        self.tool_registry: ToolRegistry | None = None
        # Phase 15: Redis client for shared state (multi-replica)
        self.redis_client: Any | None = None
        # Phase 17: OTel tracer (None when disabled or SDK not installed)
        self.tracer: Any | None = None
        # Phase 18: Lineage dashboard reader
        self.lineage_reader: Any | None = None
        # Phase 20: Embedded control plane store
        self.control_store: Any | None = None
        # Phase 20: Local sync loop task
        self.local_sync_task: Any | None = None
        # Phase 25: Resilience layer
        self.load_balancer: Any | None = None
        self.circuit_breakers: Any | None = None
        # Phase 26: Rate limiting + alerting
        self.rate_limiter: Any | None = None
        self.alert_bus: Any | None = None


_ctx = PipelineContext()


def get_pipeline_context() -> PipelineContext:
    return _ctx
