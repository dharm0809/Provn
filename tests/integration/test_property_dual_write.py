"""Phase B3: Property-based tests for WAL dual-write invariants (I7)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from gateway.wal.writer import WALWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_record(execution_id=None, session_id=None, model_id="test-model"):
    return {
        "execution_id": execution_id or str(uuid.uuid4()),
        "model_attestation_id": "self-attested:test-model",
        "model_id": model_id,
        "provider": "ollama",
        "policy_version": 1,
        "policy_result": "pass",
        "tenant_id": "test-tenant",
        "gateway_id": "gw-test",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": None,
        "session_id": session_id or str(uuid.uuid4()),
        "metadata": {},
        "prompt_text": "hello",
        "response_content": "world",
        "provider_request_id": None,
        "model_hash": None,
        "thinking_content": None,
        "latency_ms": 100.0,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "retry_of": None,
        "timings": None,
        "cache_hit": False,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
        "variant_id": None,
        "file_metadata": [],
    }


def drain_wal(wal: WALWriter) -> None:
    """Stop the WAL background thread (drains queue via sentinel + join)."""
    wal.stop()
    if wal._thread and wal._thread.is_alive():
        wal._thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_every_write_execution_lands_in_wal_records():
    """write_and_fsync N records → N rows in wal_records."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            n = 10
            for _ in range(n):
                wal.write_and_fsync(make_record())
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT COUNT(*) FROM wal_records WHERE event_type='execution'").fetchone()[0]
            conn.close()
            assert rows == n
        finally:
            wal.close()


def test_every_write_attempt_lands_in_gateway_attempts():
    """write_attempt N records → N rows in gateway_attempts."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            n = 7
            for _ in range(n):
                wal.write_attempt(
                    request_id=str(uuid.uuid4()),
                    tenant_id="tenant-1",
                    path="/v1/chat/completions",
                    disposition="allowed",
                    status_code=200,
                    provider="ollama",
                    model_id="test-model",
                )
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT COUNT(*) FROM gateway_attempts").fetchone()[0]
            conn.close()
            assert rows == n
        finally:
            wal.close()


def test_same_execution_id_twice_upsert():
    """Write the same execution_id twice — second write should upsert (INSERT OR REPLACE)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            eid = str(uuid.uuid4())
            r1 = make_record(execution_id=eid, model_id="model-v1")
            r2 = make_record(execution_id=eid, model_id="model-v2")

            wal.write_and_fsync(r1)
            wal.write_and_fsync(r2)  # should upsert, not error

            conn = sqlite3.connect(db_path)
            count = conn.execute(
                "SELECT COUNT(*) FROM wal_records WHERE execution_id=?", (eid,)
            ).fetchone()[0]
            model = conn.execute(
                "SELECT model_id FROM wal_records WHERE execution_id=?", (eid,)
            ).fetchone()[0]
            conn.close()

            assert count == 1, "expected upsert (1 row), got duplicate"
            assert model == "model-v2", "expected second write to overwrite"
        finally:
            wal.close()


def test_record_content_preserved():
    """Write record with known fields, read back, assert fields match."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            eid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            record = make_record(execution_id=eid, session_id=sid, model_id="llama3:8b")

            wal.write_and_fsync(record)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT execution_id, session_id, model_id FROM wal_records WHERE execution_id=?",
                (eid,)
            ).fetchone()
            conn.close()

            assert row is not None
            assert row[0] == eid
            assert row[1] == sid
            assert row[2] == "llama3:8b"
        finally:
            wal.close()


def test_attempt_disposition_preserved():
    """write_attempt with disposition='denied_policy', read back, assert preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            rid = str(uuid.uuid4())
            wal.write_attempt(
                request_id=rid,
                tenant_id="tenant-1",
                path="/v1/chat/completions",
                disposition="denied_policy",
                status_code=403,
                provider="ollama",
                model_id="test-model",
                reason="blocked by policy",
            )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT disposition, reason FROM gateway_attempts WHERE request_id=?",
                (rid,)
            ).fetchone()
            conn.close()

            assert row is not None
            assert row[0] == "denied_policy"
            assert row[1] == "blocked by policy"
        finally:
            wal.close()


def test_enqueue_concurrent_writes():
    """Enqueue 50 records concurrently via asyncio.gather — all land in wal_records."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        wal.start()  # start background writer thread

        records = [make_record() for _ in range(50)]

        async def _write_all():
            async def _write(r):
                wal.enqueue_write_execution(r)
            await asyncio.gather(*[_write(r) for r in records])

        asyncio.run(_write_all())
        # Stop background thread to drain queue
        wal.close()

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM wal_records WHERE event_type='execution'"
        ).fetchone()[0]
        conn.close()

        assert count == 50, f"expected 50 records, got {count}"


def test_enqueue_attempt_concurrent():
    """Enqueue 30 attempt records concurrently — all land."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        wal.start()

        request_ids = [str(uuid.uuid4()) for _ in range(30)]

        async def _write_all():
            async def _write(rid):
                wal.enqueue_write_attempt(
                    request_id=rid,
                    tenant_id="tenant-1",
                    path="/v1/chat/completions",
                    disposition="allowed",
                    status_code=200,
                )
            await asyncio.gather(*[_write(rid) for rid in request_ids])

        asyncio.run(_write_all())
        wal.close()  # drains queue

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM gateway_attempts").fetchone()[0]
        conn.close()

        assert count == 30, f"expected 30 attempts, got {count}"


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

@given(
    records=st.lists(
        st.tuples(
            st.uuids(),  # execution_id
            st.uuids(),  # session_id
            st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=["L", "N", "P", "Z"])),  # model_id
        ),
        min_size=1,
        max_size=20,
        unique_by=lambda t: t[0],  # unique execution_ids
    )
)
@h_settings(max_examples=20, deadline=10000)
def test_hypothesis_write_all_land(records):
    """All written execution_ids appear in wal_records."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        wal = WALWriter(db_path)
        try:
            written_ids = []
            for eid, sid, model_id in records:
                eid_str = str(eid)
                r = make_record(execution_id=eid_str, session_id=str(sid), model_id=model_id)
                wal.write_and_fsync(r)
                written_ids.append(eid_str)

            conn = sqlite3.connect(db_path)
            stored_ids = set(
                row[0] for row in conn.execute("SELECT execution_id FROM wal_records").fetchall()
            )
            conn.close()

            for eid_str in written_ids:
                assert eid_str in stored_ids, f"execution_id {eid_str} not found in wal_records"
        finally:
            wal.close()
