"""SchemaMapperHarvester targets per-field rows by `field_path`.

After the producer-side rewrite, each `map_response` call writes ONE
verdict row per field — `input_features_json` carries `feature_vector`
(139 floats) and `field_path` (the JSON path the field came from). The
harvester must:

  * Match overflow paths to the verdict row whose `field_path` equals
    the overflow path.
  * Skip legacy per-response rows (`input_features_json="{}"`) — they
    can't feed the trainer; back-writing on them is wasted work.
  * Fall back to "latest row UPDATE" when no per-field row matches —
    preserves backward compat with fixtures that pre-seed `{}` rows.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters import HarvesterSignal
from gateway.intelligence.harvesters.schema_mapper import SchemaMapperHarvester


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _insert_per_field(
    db: IntelligenceDB,
    *,
    request_id: str,
    field_path: str,
    feature_vector: list[float] | None = None,
) -> int:
    """Insert a per-field schema_mapper verdict row."""
    payload = {
        "feature_vector": feature_vector or [0.0] * 139,
        "field_path": field_path,
    }
    conn = sqlite3.connect(db.path)
    try:
        cur = conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            "confidence, request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "schema_mapper",
                "h" * 64,
                json.dumps(payload),
                "UNKNOWN",
                0.5,
                request_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_legacy(db: IntelligenceDB, *, request_id: str) -> int:
    """Insert a legacy per-response row with empty input_features_json."""
    conn = sqlite3.connect(db.path)
    try:
        cur = conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            "confidence, request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "schema_mapper", "h" * 64, "{}", "complete", 0.9, request_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _read(db: IntelligenceDB, vid: int) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(db.path)
    try:
        row = conn.execute(
            "SELECT divergence_signal, divergence_source "
            "FROM onnx_verdicts WHERE id=?",
            (vid,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _signal(request_id: str, overflow_keys: list[str]) -> HarvesterSignal:
    payload = {"canonical": {"overflow_keys": overflow_keys}}
    return HarvesterSignal(
        request_id=request_id,
        model_name="schema_mapper",
        prediction="complete",
        response_payload=payload,
        context={},
    )


@pytest.mark.anyio
async def test_harvester_targets_per_field_row_by_path(tmp_path):
    """Overflow path matches the verdict row whose `field_path` is identical."""
    db = _make_db(tmp_path)
    # Two per-field rows for the same request, different paths.
    other_vid = _insert_per_field(
        db, request_id="req-1", field_path="usage.prompt_tokens",
    )
    target_vid = _insert_per_field(
        db, request_id="req-1", field_path="weird_wrapper.message.content",
    )

    harv = SchemaMapperHarvester(db)
    await harv.process(_signal("req-1", ["weird_wrapper.message.content"]))

    target_sig, target_src = _read(db, target_vid)
    other_sig, _ = _read(db, other_vid)
    assert target_sig == "content"
    assert target_src == "schema_overflow_fallback"
    # Other per-field row must NOT be touched — its path didn't match.
    assert other_sig is None


@pytest.mark.anyio
async def test_harvester_skips_legacy_rows(tmp_path):
    """Legacy `{}` rows are not updated when per-field rows are present.

    Mixed DB state during a rolling rewrite: a legacy row coexists
    with new per-field rows. The harvester writes to the per-field
    row whose path matches, leaving the legacy row alone.
    """
    db = _make_db(tmp_path)
    legacy_vid = _insert_legacy(db, request_id="req-2")
    target_vid = _insert_per_field(
        db, request_id="req-2", field_path="message.content",
    )

    harv = SchemaMapperHarvester(db)
    await harv.process(_signal("req-2", ["message.content"]))

    legacy_sig, _ = _read(db, legacy_vid)
    target_sig, _ = _read(db, target_vid)
    assert legacy_sig is None
    assert target_sig == "content"


@pytest.mark.anyio
async def test_harvester_falls_back_to_latest_when_no_path_matches(tmp_path):
    """Pure-legacy DB: harvester writes to the most recent row.

    Backward-compat path. The existing harvester test suite seeds
    `{}` rows; that contract must keep working until those rows
    age out.
    """
    db = _make_db(tmp_path)
    legacy_vid = _insert_legacy(db, request_id="req-3")

    harv = SchemaMapperHarvester(db)
    await harv.process(_signal("req-3", ["message.content"]))

    sig, src = _read(db, legacy_vid)
    assert sig == "content"
    assert src == "schema_overflow_fallback"


@pytest.mark.anyio
async def test_harvester_multiple_overflow_paths_update_multiple_rows(tmp_path):
    """Each matching overflow path lights up its own per-field row.

    Previously the harvester picked the first matching label and
    updated exactly one row; with per-field rows, every overflow path
    that has a matching verdict row should land its own divergence
    signal.
    """
    db = _make_db(tmp_path)
    content_vid = _insert_per_field(
        db, request_id="req-4", field_path="message.content",
    )
    finish_vid = _insert_per_field(
        db, request_id="req-4", field_path="choices[0].completionReason",
    )
    unrelated_vid = _insert_per_field(
        db, request_id="req-4", field_path="model",
    )

    harv = SchemaMapperHarvester(db)
    await harv.process(_signal(
        "req-4",
        ["message.content", "choices[0].completionReason"],
    ))

    content_sig, _ = _read(db, content_vid)
    finish_sig, _ = _read(db, finish_vid)
    unrelated_sig, _ = _read(db, unrelated_vid)

    assert content_sig == "content"
    assert finish_sig == "finish_reason"
    assert unrelated_sig is None  # No matching overflow path.


@pytest.mark.anyio
async def test_harvester_logs_legacy_warning_once(tmp_path, caplog):
    """The 'legacy row encountered' INFO log fires at most once per process."""
    import logging

    # Reset the module-level guard so re-running this test in the same
    # process exercises the log path.
    import gateway.intelligence.harvesters.schema_mapper as mod
    mod._LEGACY_ROW_WARNED = False

    db = _make_db(tmp_path)
    _insert_legacy(db, request_id="req-5")
    _insert_per_field(
        db, request_id="req-5", field_path="message.content",
    )

    harv = SchemaMapperHarvester(db)
    with caplog.at_level(logging.INFO,
                          logger="gateway.intelligence.harvesters.schema_mapper"):
        await harv.process(_signal("req-5", ["message.content"]))
        # Second call — must NOT log again.
        await harv.process(_signal("req-5", ["message.content"]))

    legacy_msgs = [
        r for r in caplog.records
        if "legacy per-response verdict rows" in r.getMessage()
    ]
    assert len(legacy_msgs) == 1
