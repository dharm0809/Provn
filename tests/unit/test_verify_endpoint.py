"""Tests for the verify endpoint behaviour (C2 + C6 read-side).

Two big shifts versus the pre-fix code:

* **`verification_level`** distinguishes `verified` (round-trip confirmed),
  `structural` (chain intact but no round-trip), and `unverifiable`
  (anything went wrong). The top-level `valid` field is True only when the
  level is `verified`. The pre-fix code incremented `anchor_ok` for records
  with anchor fields populated but no round-trip (no EId), then accepted
  that as `valid=true` — meaning network partitions and partial records
  silently passed.

* **per-record `valid`** is set on every record returned by `verify_chain`,
  so the dashboard's per-row tick reflects that row's status, not the
  aggregate session verdict.

Also: `_compute_chain_status_map` on the Walacor reader is exercised — that
function builds `chain_status` per session for the list view (C6 read-side
on the WalacorLineageReader). Sessions with consistent `previous_record_id`
linkage return `verified`; broken chains return `warn`.
"""

from __future__ import annotations

import sqlite3
import tempfile
import json
import os

import pytest

from gateway.lineage.reader import LineageReader, _empty_verify_result


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_db_with_records(records: list[dict]) -> str:
    """Build a temp WAL DB so the SQLite reader's verify_chain can exercise
    structural / signature / anchor classification end-to-end. Each record is
    serialised as JSON for the record_json column; the indexed columns are
    populated separately so the queries find them."""
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute("""
        CREATE TABLE wal_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id TEXT,
            session_id TEXT,
            sequence_number INTEGER,
            event_type TEXT DEFAULT 'execution',
            record_json TEXT,
            created_at TEXT DEFAULT '2026-01-01T00:00:00',
            timestamp TEXT,
            model_id TEXT,
            provider TEXT,
            policy_result TEXT,
            user TEXT,
            disposition TEXT,
            tenant_id TEXT,
            path TEXT,
            request_id TEXT,
            request_type TEXT,
            parent_execution_id TEXT,
            tool_name TEXT,
            tool_type TEXT,
            reason TEXT,
            status_code INTEGER
        )
    """)
    for r in records:
        conn.execute(
            """INSERT INTO wal_records
               (execution_id, session_id, sequence_number, event_type, record_json,
                timestamp, model_id, provider, policy_result, user, request_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r.get("execution_id"),
                r.get("session_id"),
                r.get("sequence_number"),
                "execution",
                json.dumps(r),
                r.get("timestamp"),
                r.get("model_id"),
                r.get("provider"),
                r.get("policy_result", "pass"),
                r.get("user"),
                r.get("request_type", "user_message"),
            ),
        )
    conn.commit()
    conn.close()
    return tmp


def _make_record(seq: int, record_id: str, prev_id: str | None,
                 *, session: str = "s1", anchored: bool = False) -> dict:
    """A minimal execution record. `anchored=True` populates the
    walacor_block_id / trans_id / dh fields so the verify path treats this
    record as anchor-present."""
    r = {
        "execution_id": f"exec-{seq}",
        "session_id": session,
        "sequence_number": seq,
        "record_id": record_id,
        "previous_record_id": prev_id,
        "timestamp": f"2026-05-12T10:0{seq}:00+00:00",
        "model_id": "test-model",
    }
    if anchored:
        r["walacor_block_id"] = f"block-{seq}"
        r["walacor_trans_id"] = f"trans-{seq}"
        r["walacor_dh"] = f"dh-{seq}"
    return r


# ── verification_level field (C2) ─────────────────────────────────────────


def test_empty_session_reports_structural_level():
    """A session with zero records should not look "verified" — there's no
    Walacor evidence either way. `verification_level: structural` makes the
    dashboard render the amber chip."""
    result = _empty_verify_result("s-nonexistent")
    assert result["verification_level"] == "structural"
    # `valid` stays True (nothing disproved) for back-compat.
    assert result["valid"] is True


def test_sqlite_chain_intact_no_anchors_reports_structural():
    """A clean ID-pointer chain with no anchors at all (legacy records)
    should classify as `structural`. The pre-fix code had no verification_level
    field — the dashboard had no way to tell the difference between "chain
    intact" and "chain verified end-to-end"."""
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "id-0"),
        _make_record(2, "id-2", "id-1"),
    ]
    db = _make_db_with_records(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert result["valid"] is True
        # SQLite reader has no Walacor round-trip — strongest level is
        # "structural", never "verified".
        assert result["verification_level"] == "structural"
        assert result["records_checked"] == 3
    finally:
        os.unlink(db)


def test_sqlite_chain_broken_reports_unverifiable():
    """A chain break must surface as `unverifiable` (not `structural`)."""
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "WRONG-POINTER"),  # break
    ]
    db = _make_db_with_records(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert result["valid"] is False
        assert result["verification_level"] == "unverifiable"
    finally:
        os.unlink(db)


# ── per-record `valid` field (C6 read-side) ───────────────────────────────


def test_per_record_valid_flag_marks_broken_row():
    """Each record returned by verify_chain carries a boolean `valid` so the
    dashboard's per-row tick reflects that row, not the aggregate verdict.
    The pre-fix code only returned `structural_ok` — and Sessions.jsx checks
    `r.valid !== false` for the per-row tick, which always evaluated true."""
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "WRONG-POINTER"),  # this row is broken
        _make_record(2, "id-2", "id-1"),
    ]
    db = _make_db_with_records(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        per_rec = result["records"]
        assert len(per_rec) == 3
        assert per_rec[0]["valid"] is True
        # Index 1 has previous_record_id = WRONG-POINTER (expected id-0).
        assert per_rec[1]["valid"] is False
        # Index 2 is also marked broken because expected_prev advances to
        # whatever record 1 carried — but the structural check at index 2
        # compares against record 1's actual record_id ("id-1"), which
        # matches. So per-row 2 is structurally fine.
        # (This is the contract: per_record.valid reflects THIS row's break,
        # not the session-level cascade.)
    finally:
        os.unlink(db)


# ── Walacor reader verification_level distinction ──────────────────────────


@pytest.mark.anyio
async def test_walacor_anchor_present_without_roundtrip_is_not_verified():
    """C2 regression: a record with anchor fields populated but no EId
    must NOT round-trip. The pre-fix code incremented `anchor_ok` here, then
    `valid` was True. With the fix, this counts as `present` (not `verified`)
    and the session-level verdict becomes `structural`, not `verified`.

    Test geometry: the Walacor pipeline normalises anchor fields from the
    envelope ``$lookup`` sub-array. So the record must carry an ``env`` list
    rather than top-level walacor_block_id, otherwise `_normalize_record`
    will null them out.
    """
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    # Record fixture mirrors what Walacor's $lookup returns: anchor fields
    # nested under `env`. No `EId` is set — so the verify_chain round-trip
    # branch has nothing to round-trip against.
    records_with_anchor_but_no_eid = [
        {
            "execution_id": "ex-1",
            "session_id": "s1",
            "sequence_number": 0,
            "record_id": "id-0",
            "previous_record_id": None,
            "timestamp": "2026-05-12T10:00:00+00:00",
            "env": [{"BlockId": "block-0", "TransId": "trans-0", "DH": "dh-0"}],
            # NO EId — so no round-trip can be attempted.
        },
    ]
    client.query_complex = AsyncMock(return_value=records_with_anchor_but_no_eid)
    reader = WalacorLineageReader(client)
    result = await reader.verify_chain("s1")
    assert result["records_checked"] == 1
    # Anchor present on the record body but never round-tripped — must be
    # `structural`, not `verified`. Top-level valid is False.
    assert result["verification_level"] == "structural"
    assert result["valid"] is False
    assert result["checks"]["anchors"]["present"] == 1
    assert result["checks"]["anchors"]["verified"] == 0


@pytest.mark.anyio
async def test_walacor_anchor_roundtrip_match_reports_verified():
    """When every record's anchor round-trips and matches, level is `verified`
    and `valid` is True."""
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import MagicMock

    client = MagicMock()
    record = {
        "execution_id": "ex-1",
        "session_id": "s1",
        "sequence_number": 0,
        "record_id": "id-0",
        "previous_record_id": None,
        "timestamp": "2026-05-12T10:00:00+00:00",
        # Walacor's $lookup attaches the envelope under `env`. The reader's
        # normalize step lifts BlockId/TransId/DH to top-level
        # walacor_block_id/trans_id/dh.
        "env": [{"BlockId": "block-0", "TransId": "trans-0", "DH": "dh-0"}],
        "EId": 42,  # used for round-trip
    }
    matching_envelope_row = {
        "EId": 42,
        "env": [{"BlockId": "block-0", "TransId": "trans-0", "DH": "dh-0"}],
    }
    call_count = {"n": 0}

    async def fake_query(etid, pipeline):
        call_count["n"] += 1
        # First call is `get_session_timeline`. Subsequent ones are round-trip.
        if call_count["n"] == 1:
            return [record]
        return [matching_envelope_row]

    client.query_complex = fake_query
    reader = WalacorLineageReader(client)
    result = await reader.verify_chain("s1")
    assert result["verification_level"] == "verified"
    assert result["valid"] is True
    assert result["checks"]["anchors"]["verified"] == 1


@pytest.mark.anyio
async def test_walacor_anchor_roundtrip_failure_is_unverifiable():
    """Transport error during the round-trip → `unverifiable`. The pre-fix
    spec captures the core property: a network partition must NOT produce
    valid:true."""
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import MagicMock

    client = MagicMock()
    record = {
        "execution_id": "ex-1",
        "session_id": "s1",
        "sequence_number": 0,
        "record_id": "id-0",
        "previous_record_id": None,
        "timestamp": "2026-05-12T10:00:00+00:00",
        "env": [{"BlockId": "block-0", "TransId": "trans-0", "DH": "dh-0"}],
        "EId": 42,
    }
    call_count = {"n": 0}

    async def fake_query(etid, pipeline):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [record]
        raise RuntimeError("walacor unreachable")

    client.query_complex = fake_query
    reader = WalacorLineageReader(client)
    result = await reader.verify_chain("s1")
    assert result["verification_level"] == "unverifiable"
    assert result["valid"] is False
    assert result["checks"]["anchors"]["unverifiable"] == 1


# ── chain_status map (C6) ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_walacor_chain_status_map_verified_session():
    """A session with intact previous_record_id linkage returns
    `chain_status: verified`. The dashboard reads this on every row."""
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    rows = [
        {"session_id": "s1", "sequence_number": 0, "record_id": "id-0", "previous_record_id": None},
        {"session_id": "s1", "sequence_number": 1, "record_id": "id-1", "previous_record_id": "id-0"},
    ]
    client.query_complex = AsyncMock(return_value=rows)
    reader = WalacorLineageReader(client)
    result = await reader._compute_chain_status_map(["s1"])
    assert result["s1"] == "verified"


@pytest.mark.anyio
async def test_walacor_chain_status_map_broken_session():
    """A session with a chain break returns `chain_status: warn`."""
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    rows = [
        {"session_id": "s1", "sequence_number": 0, "record_id": "id-0", "previous_record_id": None},
        # Break: pointer doesn't match predecessor.
        {"session_id": "s1", "sequence_number": 1, "record_id": "id-1", "previous_record_id": "WRONG"},
    ]
    client.query_complex = AsyncMock(return_value=rows)
    reader = WalacorLineageReader(client)
    result = await reader._compute_chain_status_map(["s1"])
    assert result["s1"] == "warn"


@pytest.mark.anyio
async def test_walacor_chain_status_map_handles_query_error():
    """A Walacor query error must not blow up the session listing — return
    empty map so the dashboard falls back to "verified" (the listing still
    renders; just no per-row chain badge differentiation)."""
    from gateway.lineage.walacor_reader import WalacorLineageReader
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.query_complex = AsyncMock(side_effect=RuntimeError("walacor down"))
    reader = WalacorLineageReader(client)
    result = await reader._compute_chain_status_map(["s1", "s2"])
    assert result == {}


def test_sqlite_chain_status_map_marks_break():
    """SQLite path of the same chain_status walk."""
    records = [
        _make_record(0, "id-0", None, session="s1"),
        _make_record(1, "id-1", "WRONG-POINTER", session="s1"),
    ]
    db = _make_db_with_records(records)
    try:
        reader = LineageReader(db)
        status_map = reader._compute_chain_status_map(["s1"])
        assert status_map["s1"] == "warn"
    finally:
        os.unlink(db)


def test_sqlite_chain_status_map_marks_verified():
    records = [
        _make_record(0, "id-0", None, session="s1"),
        _make_record(1, "id-1", "id-0", session="s1"),
        _make_record(2, "id-2", "id-1", session="s1"),
    ]
    db = _make_db_with_records(records)
    try:
        reader = LineageReader(db)
        status_map = reader._compute_chain_status_map(["s1"])
        assert status_map["s1"] == "verified"
    finally:
        os.unlink(db)


@pytest.fixture
def anyio_backend():
    return "asyncio"
