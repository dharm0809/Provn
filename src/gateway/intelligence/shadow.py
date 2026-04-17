"""Phase 25 Task 22: shadow inference runtime.

The plan is "run the candidate in parallel for every production inference
and record a paired comparison". Three concerns:

1. **Session cache** — each candidate has its own `InferenceSession`;
   loading ORT costs ~10-50ms so we keep a per-(model, version) cache.
   When a candidate is promoted or rejected the registry's marker moves
   and the cache entry for the old version stays resident — acceptable,
   process restart clears it; a dedicated evict path is Phase H work.
2. **Row recording** — paired `(production_prediction,
   candidate_prediction)` + errors land in `shadow_comparisons`. This
   is what Task 23's metrics module reads.
3. **Non-blocking** — the client never awaits shadow work. The
   orchestration is fire-and-forget (`asyncio.create_task`), session
   load + inference run in threads, and errors are logged / recorded
   as `candidate_error` never raised back at the caller.

The runner is deliberately a thin library, not a queue-based worker:
shadow fan-out is proportional to production inference traffic, which
is already bounded by the request pipeline. Adding a second queue here
would just be another backpressure surface to reason about without
bounding anything the request path doesn't already bound.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.registry import ALLOWED_MODEL_NAMES, Candidate

logger = logging.getLogger(__name__)


class ShadowRunner:
    """Owns candidate `InferenceSession` caching + shadow_comparisons writes.

    The concrete inference call is done by the caller (via the `infer`
    callable) because every ONNX client has a slightly different input
    shape. This runner stays model-agnostic.
    """

    def __init__(self, db: IntelligenceDB) -> None:
        self._db = db
        # Keyed by `(model, version)`. `InferenceSession` objects are
        # safe to share across asyncio tasks on the same loop (ORT
        # serializes internally).
        self._sessions: dict[tuple[str, str], Any] = {}
        # Callers invoke `get_session` inside `asyncio.to_thread`, so two
        # concurrent shadow tasks can both miss the cache and both
        # construct an `InferenceSession` for the same key. The loser's
        # session leaks in ORT's arena. Serialize the check-then-set.
        self._sessions_lock = threading.Lock()

    def get_session(self, model: str, candidate: Candidate) -> Any:
        """Return the cached `InferenceSession` for `candidate`, loading once.

        Loads synchronously. Callers that care about latency wrap this
        call in `asyncio.to_thread`.
        """
        if model not in ALLOWED_MODEL_NAMES:
            raise ValueError(f"unknown model name {model!r}")
        key = (model, candidate.version)
        cached = self._sessions.get(key)
        if cached is not None:
            return cached
        from onnxruntime import InferenceSession  # lazy — optional dep.
        with self._sessions_lock:
            cached = self._sessions.get(key)
            if cached is not None:
                return cached
            session = InferenceSession(
                str(candidate.path), providers=["CPUExecutionProvider"],
            )
            self._sessions[key] = session
            return session

    async def record(
        self,
        *,
        model: str,
        candidate_version: str,
        input_hash: str,
        production_prediction: str,
        production_confidence: float,
        candidate_prediction: str | None,
        candidate_confidence: float | None,
        candidate_error: str | None = None,
    ) -> None:
        """Write a paired-prediction row to `shadow_comparisons`.

        Fail-open: a write error is logged and swallowed — observational
        data loss is acceptable, the alternative (breaking inference
        response) is not.
        """
        try:
            await asyncio.to_thread(
                self._insert_row,
                model, candidate_version, input_hash,
                production_prediction, production_confidence,
                candidate_prediction, candidate_confidence,
                candidate_error,
            )
        except Exception:
            logger.debug("shadow comparison write failed", exc_info=True)

    def _insert_row(
        self,
        model: str,
        candidate_version: str,
        input_hash: str,
        production_prediction: str,
        production_confidence: float,
        candidate_prediction: str | None,
        candidate_confidence: float | None,
        candidate_error: str | None,
    ) -> None:
        conn = sqlite3.connect(self._db.path)
        try:
            conn.execute(
                "INSERT INTO shadow_comparisons "
                "(model_name, candidate_version, input_hash, "
                "production_prediction, production_confidence, "
                "candidate_prediction, candidate_confidence, "
                "candidate_error, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model, candidate_version, input_hash,
                    production_prediction, float(production_confidence),
                    candidate_prediction,
                    None if candidate_confidence is None else float(candidate_confidence),
                    candidate_error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def hash_input(text: str) -> str:
    """SHA256 hash matching the production verdict-row hash scheme."""
    return hashlib.sha256(text.encode()).hexdigest()


def maybe_fire_shadow(
    runner: "ShadowRunner | None",
    registry: Any | None,
    *,
    model_name: str | None,
    input_text: str,
    production_prediction: str,
    production_confidence: float,
    infer_on_session: Any,
) -> None:
    """Fire-and-forget entry point for ONNX client hooks.

    Silently no-ops when shadow support is not wired (runner/registry/
    model_name unset) or no active candidate is registered. When a
    candidate IS active, creates a background task that runs the
    candidate session and records the comparison — the caller never
    waits and never sees errors from the shadow path.
    """
    if runner is None or registry is None or not model_name:
        return
    cand = registry.active_candidate(model_name)
    if cand is None:
        return
    try:
        asyncio.create_task(
            fire_shadow_text(
                runner,
                model=model_name,
                candidate=cand,
                input_text=input_text,
                production_prediction=production_prediction,
                production_confidence=production_confidence,
                infer_on_session=infer_on_session,
            ),
            name=f"shadow-{model_name}-{cand.version}",
        )
    except RuntimeError:
        # `asyncio.create_task` raises `RuntimeError` when no loop is
        # running (e.g. sync-test call sites). Skip rather than break.
        logger.debug("no running event loop; shadow fire skipped")


async def fire_shadow_text(
    runner: ShadowRunner,
    *,
    model: str,
    candidate: Candidate,
    input_text: str,
    production_prediction: str,
    production_confidence: float,
    infer_on_session: Any,
) -> None:
    """Run a candidate inference and record the comparison.

    `infer_on_session(session, input_text) -> (label, confidence)` is
    the client-provided function that knows how to drive the candidate
    session's actual tensor shape. All work is isolated in a single
    try/except so no error ever escapes — candidate_error is recorded
    instead.
    """
    input_hash = hash_input(input_text)
    try:
        session = await asyncio.to_thread(runner.get_session, model, candidate)
        cand_label, cand_conf = await asyncio.to_thread(
            infer_on_session, session, input_text,
        )
        await runner.record(
            model=model,
            candidate_version=candidate.version,
            input_hash=input_hash,
            production_prediction=production_prediction,
            production_confidence=production_confidence,
            candidate_prediction=str(cand_label),
            candidate_confidence=float(cand_conf),
        )
    except Exception as exc:
        # The shadow path must never surface errors to the caller.
        # Record the candidate failure for Task 23's error-rate metric.
        logger.debug("shadow inference failed (recording as candidate_error)", exc_info=True)
        try:
            from gateway.metrics.prometheus import shadow_inference_errors_total
            shadow_inference_errors_total.labels(model=model).inc()
        except Exception:
            pass
        await runner.record(
            model=model,
            candidate_version=candidate.version,
            input_hash=input_hash,
            production_prediction=production_prediction,
            production_confidence=production_confidence,
            candidate_prediction=None,
            candidate_confidence=None,
            candidate_error=str(exc) or type(exc).__name__,
        )
