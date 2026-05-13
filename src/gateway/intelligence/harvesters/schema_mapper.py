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

Per-field rows
--------------
After the producer-side per-field rewrite, each `map_response` call
emits one verdict row PER FIELD with the 139-d feature vector packed
into `input_features_json`. The harvester now matches overflow paths
to the row whose `field_path` (embedded in the JSON payload) equals
the overflow path, so the divergence label lands on the correct
field. When no per-field row matches, the harvester degrades to the
legacy "latest row" UPDATE — useful for older verdict rows still in
the DB and for the safety harvester ordering tests.

Legacy per-response rows (whose `input_features_json` is `"{}"`) are
skipped and trigger a single INFO log per process: those rows can't
feed the trainer (no features), so back-writing a label on them just
expands the no-op set the dataset builder would otherwise filter via
the empty-JSON gate.

SQLite work runs in `asyncio.to_thread` so the harvester loop never
blocks on disk I/O.

D5 — envelope coverage
----------------------
The fallback rule table now covers OpenAI / Anthropic / Ollama envelope
keys (``object``, ``created``, ``role``, ``index``, ``logprobs``,
``service_tier``, …) mapping each to the synthetic ``envelope`` label.
Post D3 those keys never reach ``overflow_keys`` in fresh requests (the
mapper filters them upstream), so the rules primarily serve legacy
verdict rows captured before the upstream filter rolled out. For the
current production model — whose ``schema_mapper_labels.json`` does NOT
contain ``envelope`` — writing ``divergence_signal="envelope"`` would
artificially fail every row at ``compute_accuracy`` time; the harvester
therefore SUPPRESSES envelope candidates unless the active labels file
lists ``envelope``. This gate flips automatically when a future retrain
adds the label.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters.base import Harvester, HarvesterSignal
from gateway.schema.canonical import ENVELOPE_LABEL
from gateway.schema.mapper import classify_overflow_path

logger = logging.getLogger(__name__)

# One-time INFO log when the harvester encounters legacy per-response
# rows (no embedded features). Module-level so the warning fires once
# per process, not once per signal.
_LEGACY_ROW_WARNED = False


def _envelope_label_trainable() -> bool:
    """True when the production labels file lists ``envelope``.

    The harvester suppresses envelope candidates when this is False —
    see module docstring D5 section. We re-read on every call rather
    than caching at import time so a labels.json swap (e.g. a hot
    promotion) is picked up without restarting the worker.
    """
    try:
        import json
        from gateway.schema.mapper import _LABELS_PATH
        if not _LABELS_PATH.exists():
            return False
        with open(_LABELS_PATH) as fh:
            labels = json.load(fh)
        return isinstance(labels, list) and ENVELOPE_LABEL in labels
    except Exception:
        return False


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

        # Build a list of (overflow_path, canonical_label) for every
        # overflow key whose leaf+path token rule matches. Each pair is
        # a candidate UPDATE target — the harvester walks them in
        # insertion order and applies whichever matches a verdict row
        # first. Falls back to the legacy "latest row" UPDATE when no
        # per-field row matches.
        envelope_trainable = _envelope_label_trainable()
        candidates: list[tuple[str, str]] = []
        for path in overflow:
            if not isinstance(path, str):
                continue
            match = classify_overflow_path(path)
            if match is None:
                continue
            if match == ENVELOPE_LABEL and not envelope_trainable:
                # D5: ``envelope`` is a synthetic tag. Until the production
                # model is retrained with ``envelope`` in its label set,
                # writing this as ``divergence_signal`` would deflate the
                # rolling accuracy metric (UNKNOWN ≠ envelope on every
                # row). Skip silently — the upstream D3 filter already
                # keeps these out of fresh ``overflow_keys`` lists.
                continue
            candidates.append((path, match))

        if not candidates:
            return

        await asyncio.to_thread(
            self._update_divergence, signal.request_id, candidates,
        )

    # SQLite work runs on a worker thread. `sqlite3.connect` opens a
    # fresh connection per call — the IntelligenceDB's module-level
    # connections are reserved for other writers (VerdictFlushWorker,
    # RetentionSweeper), each of which also opens its own connection.
    def _update_divergence(
        self, request_id: str, candidates: list[tuple[str, str]],
    ) -> None:
        global _LEGACY_ROW_WARNED
        conn = sqlite3.connect(self._db.path)
        try:
            # Pull every schema_mapper row for this request and try to
            # match each candidate to a row by `field_path`. Per-field
            # rows are the new format (post-rewrite); legacy `{}` rows
            # have no field_path and are skipped after a one-time
            # INFO log.
            rows = conn.execute(
                "SELECT id, input_features_json FROM onnx_verdicts "
                "WHERE request_id = ? AND model_name = 'schema_mapper' "
                "ORDER BY timestamp DESC, id DESC",
                (request_id,),
            ).fetchall()

            saw_legacy = False
            row_by_path: dict[str, int] = {}
            for row_id, features_json in rows:
                if not features_json or features_json == "{}":
                    saw_legacy = True
                    continue
                try:
                    payload = json.loads(features_json)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                fp = payload.get("field_path")
                if isinstance(fp, str) and fp:
                    # First (most recent) row for a given path wins —
                    # `setdefault` matches `_dedupe_by_hash`'s discipline.
                    row_by_path.setdefault(fp, int(row_id))

            if saw_legacy and not _LEGACY_ROW_WARNED:
                logger.info(
                    "SchemaMapperHarvester: legacy per-response verdict rows "
                    "encountered (input_features_json='{}'); skipping — they "
                    "carry no per-field features and can't feed the trainer."
                )
                _LEGACY_ROW_WARNED = True

            updated_any = False
            for overflow_path, label in candidates:
                target_id = row_by_path.get(overflow_path)
                if target_id is None:
                    continue
                conn.execute(
                    """
                    UPDATE onnx_verdicts
                    SET divergence_signal = ?,
                        divergence_source = 'schema_overflow_fallback'
                    WHERE id = ?
                    """,
                    (label, target_id),
                )
                updated_any = True

            if not updated_any:
                # Fall back to the legacy UPDATE — newest row for the
                # request. Preserves test compatibility for fixtures
                # that pre-seed plain `{}` rows without per-field
                # features. Use the FIRST candidate label (overflow_keys
                # preserves insertion order — matches the prior contract).
                first_label = candidates[0][1]
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
                    (first_label, request_id),
                )
            conn.commit()
        except Exception:
            logger.warning(
                "SchemaMapperHarvester UPDATE failed request_id=%r candidates=%r",
                request_id, candidates, exc_info=True,
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
