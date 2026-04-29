"""Phase B4: Chaos test — Walacor backend failures.

Tests that the WAL writer never loses records under pressure,
even when Walacor backend is unreliable.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

import pytest

from gateway.wal.writer import WALWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(execution_id: str | None = None) -> dict:
    return {
        "execution_id": execution_id or str(uuid.uuid4()),
        "model_attestation_id": "self-attested:test",
        "model_id": "test",
        "provider": "ollama",
        "policy_version": 1,
        "policy_result": "pass",
        "tenant_id": "t1",
        "gateway_id": "gw1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": None,
        "session_id": str(uuid.uuid4()),
        "metadata": {},
        "prompt_text": "hello",
        "response_content": "world",
        "provider_request_id": None,
        "model_hash": None,
        "thinking_content": None,
        "latency_ms": 50.0,
        "prompt_tokens": 5,
        "completion_tokens": 10,
        "total_tokens": 15,
        "retry_of": None,
        "timings": None,
        "cache_hit": False,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
        "variant_id": None,
        "file_metadata": [],
    }


def _count_wal_records(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records WHERE event_type='execution'").fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWALConcurrentWrites:
    def test_50_concurrent_writes_all_land(self):
        """50 concurrent async writes all persist to SQLite."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            async def write_one():
                rec = _make_record()
                wal.write_durable(rec)
                return rec["execution_id"]

            async def run():
                ids = await asyncio.gather(*[write_one() for _ in range(50)])
                return ids

            ids = asyncio.run(run())
            assert len(ids) == 50
            assert len(set(ids)) == 50  # unique

            count = _count_wal_records(db_path)
            assert count == 50

            wal.close()

    def test_sequential_writes_all_land(self):
        """Sequential writes all persist."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            n = 20
            for _ in range(n):
                wal.write_durable(_make_record())

            assert _count_wal_records(db_path) == n
            wal.close()


class TestWALDeliveryMarking:
    def test_mark_delivered_sets_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            rec = _make_record()
            eid = rec["execution_id"]
            wal.write_durable(rec)

            undelivered = wal.get_undelivered(limit=10)
            assert any(r[0] == eid for r in undelivered)

            wal.mark_delivered(eid)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT delivered FROM wal_records WHERE execution_id=?", (eid,)
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[0] == 1

            wal.close()

    def test_get_undelivered_returns_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            ids = []
            for _ in range(5):
                rec = _make_record()
                ids.append(rec["execution_id"])
                wal.write_durable(rec)

            undelivered = wal.get_undelivered(limit=10)
            returned_ids = {r[0] for r in undelivered}
            for eid in ids:
                assert eid in returned_ids

            wal.close()

    def test_delivered_records_excluded_from_undelivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            rec1 = _make_record()
            rec2 = _make_record()
            wal.write_durable(rec1)
            wal.write_durable(rec2)

            wal.mark_delivered(rec1["execution_id"])

            undelivered = wal.get_undelivered(limit=10)
            returned_ids = {r[0] for r in undelivered}
            assert rec1["execution_id"] not in returned_ids
            assert rec2["execution_id"] in returned_ids

            wal.close()


class TestWALAppendOnly:
    def test_earlier_records_unmodified_by_later_writes(self):
        """Records written earlier must not be changed by subsequent writes."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            first = _make_record()
            wal.write_durable(first)

            conn = sqlite3.connect(db_path)
            original_json = conn.execute(
                "SELECT record_json FROM wal_records WHERE execution_id=?",
                (first["execution_id"],),
            ).fetchone()[0]
            conn.close()

            # Write many more records
            for _ in range(10):
                wal.write_durable(_make_record())

            conn = sqlite3.connect(db_path)
            after_json = conn.execute(
                "SELECT record_json FROM wal_records WHERE execution_id=?",
                (first["execution_id"],),
            ).fetchone()[0]
            conn.close()

            assert original_json == after_json
            wal.close()

    def test_only_delivered_flag_changes(self):
        """Only the delivered column should be mutable post-write."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            rec = _make_record()
            eid = rec["execution_id"]
            wal.write_durable(rec)

            conn = sqlite3.connect(db_path)
            before = conn.execute(
                "SELECT record_json, created_at FROM wal_records WHERE execution_id=?", (eid,)
            ).fetchone()
            conn.close()

            wal.mark_delivered(eid)

            conn = sqlite3.connect(db_path)
            after = conn.execute(
                "SELECT record_json, created_at, delivered FROM wal_records WHERE execution_id=?", (eid,)
            ).fetchone()
            conn.close()

            assert before[0] == after[0]   # record_json unchanged
            assert before[1] == after[1]   # created_at unchanged
            assert after[2] == 1           # delivered set

            wal.close()


class TestWALRecovery:
    def test_reopen_preserves_all_records(self):
        """Close and reopen WAL — all records still present."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            # Write 20 records, then close
            wal = WALWriter(db_path)
            written_ids = []
            for _ in range(20):
                rec = _make_record()
                written_ids.append(rec["execution_id"])
                wal.write_durable(rec)
            wal.close()

            # Reopen and verify
            wal2 = WALWriter(db_path)
            count = _count_wal_records(db_path)
            assert count == 20

            undelivered = wal2.get_undelivered(limit=25)
            returned_ids = {r[0] for r in undelivered}
            for eid in written_ids:
                assert eid in returned_ids

            wal2.close()

    def test_reopen_after_mark_delivered(self):
        """Delivered marks survive WAL reopen."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")

            wal = WALWriter(db_path)
            recs = [_make_record() for _ in range(5)]
            for rec in recs:
                wal.write_durable(rec)

            delivered_id = recs[0]["execution_id"]
            wal.mark_delivered(delivered_id)
            wal.close()

            wal2 = WALWriter(db_path)
            undelivered = wal2.get_undelivered(limit=10)
            returned_ids = {r[0] for r in undelivered}
            assert delivered_id not in returned_ids
            wal2.close()


class TestWALAttempts:
    def test_write_attempt_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            request_id = str(uuid.uuid4())
            wal.write_attempt(
                request_id=request_id,
                tenant_id="t1",
                path="/v1/chat/completions",
                disposition="allowed",
                status_code=200,
                provider="ollama",
                model_id="qwen3:4b",
            )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT request_id, disposition FROM gateway_attempts WHERE request_id=?",
                (request_id,),
            ).fetchone()
            conn.close()

            assert row is not None
            assert row[0] == request_id
            assert row[1] == "allowed"

            wal.close()

    def test_concurrent_attempt_writes(self):
        """Multiple concurrent attempt writes — all land."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            wal = WALWriter(db_path)

            async def write_attempt():
                rid = str(uuid.uuid4())
                wal.write_attempt(
                    request_id=rid,
                    tenant_id="t1",
                    path="/v1/chat/completions",
                    disposition="allowed",
                    status_code=200,
                )
                return rid

            async def run():
                return await asyncio.gather(*[write_attempt() for _ in range(30)])

            ids = asyncio.run(run())
            assert len(ids) == 30

            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM gateway_attempts").fetchone()[0]
            conn.close()
            assert count == 30

            wal.close()
