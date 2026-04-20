"""SchemaMapper harvester — overflow-key training signal.

When the SchemaMapper fails to classify a field its path lands in
`canonical.overflow`; the orchestrator surfaces the first ~30 such paths
in `metadata.canonical.overflow_keys`. This harvester re-applies the
same leaf+path fallback rules (shared with `mapper.py::_apply_path_fallbacks`
via the module-level `classify_overflow_path` helper) to those keys. If a
rule matches, the harvester back-writes the rule's canonical label onto
the matching `onnx_verdicts` row as `divergence_signal` — the label the
distillation worker (Task 17+) will treat as the "correct" answer when
building the next training dataset.

SQLite work runs in `asyncio.to_thread` so the harvester loop never
blocks on disk I/O.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters.base import Harvester, HarvesterSignal
from gateway.schema.mapper import classify_overflow_path

logger = logging.getLogger(__name__)


class SchemaMapperHarvester(Harvester):
    target_model = "schema_mapper"

    def __init__(self, db: IntelligenceDB) -> None:
        self._db = db

    async def process(self, signal: HarvesterSignal) -> None:
        import asyncio

        if signal.request_id is None:
            # Without a request_id we can't correlate the signal to a
            # verdict row. The orchestrator always sets it; a None value
            # here means the signal came from a context that's already
            # un-joinable, so there's nothing to back-write.
            return

        overflow = _extract_overflow_keys(signal.response_payload)
        if not overflow:
            return

        # Walk the overflow paths and pick the FIRST matching canonical
        # label. Each overflow entry is a candidate — we don't need to
        # aggregate because the verdict row stores a single signal. If
        # multiple overflow paths match, the earliest in the list wins.
        # (`overflow_keys` preserves insertion order from `cr.overflow`,
        # which is the field-walk order.)
        label: str | None = None
        for path in overflow:
            if not isinstance(path, str):
                continue
            match = classify_overflow_path(path)
            if match is not None:
                label = match
                break

        if label is None:
            return

        await asyncio.to_thread(self._update_divergence, signal.request_id, label)

    # SQLite work runs on a worker thread. `sqlite3.connect` opens a
    # fresh connection per call — the IntelligenceDB's module-level
    # connections are reserved for other writers (VerdictFlushWorker,
    # RetentionSweeper), each of which also opens its own connection.
    def _update_divergence(self, request_id: str, label: str) -> None:
        conn = sqlite3.connect(self._db.path)
        try:
            conn.execute(
                """
                UPDATE onnx_verdicts
                SET divergence_signal = ?,
                    divergence_source = 'schema_overflow_fallback'
                WHERE id = (
                    SELECT id FROM onnx_verdicts
                    WHERE request_id = ? AND model_name = 'schema_mapper'
                    ORDER BY timestamp DESC
                    LIMIT 1
                )
                """,
                (label, request_id),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "SchemaMapperHarvester UPDATE failed request_id=%r label=%r",
                request_id, label, exc_info=True,
            )
        finally:
            conn.close()


def _extract_overflow_keys(payload: Any) -> list[str]:
    """Pull `canonical.overflow_keys` out of the orchestrator's metadata dict.

    Defensive against shape drift — the payload flows from
    `_build_and_write_record` through `_emit_harvester_signals`; missing
    or mistyped branches return an empty list so `process` falls through
    to its no-op path.
    """
    if not isinstance(payload, dict):
        return []
    canonical = payload.get("canonical")
    if not isinstance(canonical, dict):
        return []
    keys = canonical.get("overflow_keys")
    if not isinstance(keys, list):
        return []
    return keys
