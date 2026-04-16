"""Pipeline context: caches, sync client, WAL, Walacor client, delivery worker. Set at app startup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway.cache.attestation_cache import AttestationCache
    from gateway.cache.policy_cache import PolicyCache
    from gateway.cache.semantic_cache import SemanticCache
    from gateway.content.base import ContentAnalyzer
    from gateway.export.base import AuditExporter
    from gateway.intelligence.db import IntelligenceDB
    from gateway.intelligence.harvesters import HarvesterRunner
    from gateway.intelligence.registry import ModelRegistry
    from gateway.intelligence.retention import RetentionSweeper
    from gateway.intelligence.verdict_buffer import VerdictBuffer
    from gateway.mcp.registry import ToolRegistry
    from gateway.pipeline.budget_tracker import BudgetTracker
    from gateway.pipeline.session_chain import SessionChainTracker
    from gateway.storage.router import StorageRouter
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
        # Storage abstraction layer (fans out to WAL + Walacor)
        self.storage: StorageRouter | None = None
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
        self.alert_bus_task: Any | None = None
        # Phase 23: Adaptive Gateway
        self.startup_probe_results: dict = {}
        self.request_classifier = None
        self.identity_validator = None
        self.resource_monitor = None
        self.capability_registry = None
        self.effective_wal_max_gb: float | None = None
        self.resource_monitor_task: Any | None = None
        self.event_loop_lag_task: Any | None = None
        # Phase 18 (Task 18): Batch WAL writer
        self.batch_writer: Any | None = None
        # Phase 24: Merkle tree checkpoint task
        self.merkle_checkpoint_task: Any | None = None
        # Multimodal audit: attachment notification cache
        self.attachment_cache: Any | None = None
        # Multimodal audit: image OCR analyzer
        self.image_ocr_analyzer: Any | None = None
        # B.2: Audit log exporter (file, webhook, s3)
        self.audit_exporter: AuditExporter | None = None
        # B.4: Semantic cache (exact-match tier)
        self.semantic_cache: SemanticCache | None = None
        # Phase 25: ONNX self-learning intelligence layer
        self.verdict_buffer: VerdictBuffer | None = None
        self.intelligence_db: IntelligenceDB | None = None
        self.intelligence_flush_task: Any | None = None
        self.intelligence_flush_worker: Any | None = None
        self.intelligence_retention_task: Any | None = None
        self.intelligence_retention_sweeper: Any | None = None
        # Phase 25 Task 12: ONNX model registry (directory-backed artifact store).
        # Clients resolve their `.onnx` file via
        # `ctx.model_registry.production_path(model_name)` and rebuild their
        # session when the per-model generation counter moves.
        self.model_registry: ModelRegistry | None = None
        # Phase 25 Task 13: verdict harvester runner. Tasks 14-16 register
        # per-model harvesters that back-write divergence signals onto the
        # verdict log. Signals are enqueued fire-and-forget from the
        # orchestrator's audit-finalization path.
        self.harvester_runner: HarvesterRunner | None = None


_ctx = PipelineContext()


def get_pipeline_context() -> PipelineContext:
    return _ctx
