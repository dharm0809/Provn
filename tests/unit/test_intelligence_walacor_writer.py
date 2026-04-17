"""Phase 25 Task 21: LifecycleEventWriter tests.

Exercises the retry + mirror semantics with a fake Walacor client.
`asyncio.sleep` is swapped for a no-op so the retry tests run without
actually waiting.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.events import (
    EventType,
    build_candidate_created,
    build_training_fingerprint,
)
from gateway.intelligence.walacor_writer import LifecycleEventWriter


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _no_sleep(_delay: float) -> None:
    return None


class _FakeWalacorOk:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.etid_seen: list[int | None] = []

    async def write_record(self, record, *, etid=None):
        self.records.append(record)
        self.etid_seen.append(etid)
        return {"id": f"wal-{len(self.records)}"}


class _FakeWalacorFlaky:
    """Fails the first N calls, then returns OK."""
    def __init__(self, fail_n: int) -> None:
        self.fail_n = fail_n
        self.calls = 0

    async def write_record(self, record, *, etid=None):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError(f"simulated transient fail {self.calls}")
        return {"id": f"wal-{self.calls}"}


class _FakeWalacorDead:
    def __init__(self) -> None:
        self.calls = 0

    async def write_record(self, record, *, etid=None):
        self.calls += 1
        raise ConnectionError("walacor unreachable")


class _FakeWalacorPositional:
    """Client that only accepts the positional signature (no etid kwarg)."""
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def write_record(self, record):
        self.records.append(record)
        return "positional-id"


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _read_mirror(db: IntelligenceDB, row_id: int) -> dict:
    conn = sqlite3.connect(db.path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM lifecycle_events_mirror WHERE id=?", (row_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ── Happy path ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_write_event_succeeds_on_first_try(tmp_path):
    db = _make_db(tmp_path)
    client = _FakeWalacorOk()
    writer = LifecycleEventWriter(db, client, etid=9000024, sleep=_no_sleep)

    event = build_training_fingerprint(
        model_name="intent", row_ids=[1, 2, 3], content_hash="abc",
    )
    mirror_id = await writer.write_event(event)

    # Walacor saw exactly one write with the configured ETId.
    assert len(client.records) == 1
    assert client.etid_seen == [9000024]

    row = _read_mirror(db, mirror_id)
    assert row["event_type"] == EventType.TRAINING_DATASET_FINGERPRINT.value
    assert row["write_status"] == "written"
    assert row["walacor_record_id"] == "wal-1"
    assert row["attempts"] == 1
    assert row["error_reason"] is None
    # Payload round-trips.
    payload = json.loads(row["payload_json"])
    assert payload["model_name"] == "intent"
    assert payload["row_ids"] == [1, 2, 3]


# ── Retry ───────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_write_event_retries_then_succeeds(tmp_path):
    db = _make_db(tmp_path)
    client = _FakeWalacorFlaky(fail_n=2)  # 2 fails, 3rd succeeds
    writer = LifecycleEventWriter(db, client, etid=9000024, sleep=_no_sleep)

    event = build_candidate_created(
        model_name="safety", candidate_version="v1",
        dataset_hash="d1", training_sample_count=42,
    )
    mirror_id = await writer.write_event(event)

    assert client.calls == 3
    row = _read_mirror(db, mirror_id)
    assert row["write_status"] == "written"
    assert row["attempts"] == 3
    assert row["walacor_record_id"] == "wal-3"


@pytest.mark.anyio
async def test_write_event_records_failure_when_all_attempts_fail(tmp_path):
    db = _make_db(tmp_path)
    client = _FakeWalacorDead()
    writer = LifecycleEventWriter(
        db, client, etid=9000024, max_attempts=3, sleep=_no_sleep,
    )

    event = build_training_fingerprint(
        model_name="intent", row_ids=[1], content_hash="x",
    )
    # Must not raise — writer is fail-open by contract.
    mirror_id = await writer.write_event(event)

    assert client.calls == 3
    row = _read_mirror(db, mirror_id)
    assert row["write_status"] == "failed"
    assert row["attempts"] == 3
    assert row["walacor_record_id"] is None
    assert "walacor unreachable" in (row["error_reason"] or "")


@pytest.mark.anyio
async def test_max_attempts_one_disables_retry(tmp_path):
    db = _make_db(tmp_path)
    client = _FakeWalacorFlaky(fail_n=5)
    writer = LifecycleEventWriter(
        db, client, etid=9000024, max_attempts=1, sleep=_no_sleep,
    )

    event = build_training_fingerprint(
        model_name="intent", row_ids=[1], content_hash="x",
    )
    await writer.write_event(event)

    assert client.calls == 1


# ── Client variant handling ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_writer_tolerates_positional_only_client(tmp_path):
    # Older client implementations don't accept the `etid` kwarg — the
    # writer must gracefully fall back to the positional call.
    db = _make_db(tmp_path)
    client = _FakeWalacorPositional()
    writer = LifecycleEventWriter(db, client, etid=9000024, sleep=_no_sleep)

    event = build_training_fingerprint(
        model_name="safety", row_ids=[1], content_hash="x",
    )
    mirror_id = await writer.write_event(event)

    assert len(client.records) == 1
    row = _read_mirror(db, mirror_id)
    assert row["write_status"] == "written"
    assert row["walacor_record_id"] == "positional-id"


@pytest.mark.anyio
async def test_writer_records_success_even_when_client_returns_empty_id(tmp_path):
    # An unrecognized response shape (no id field) still counts as a
    # successful write — the mirror just has a NULL walacor_record_id
    # so operators can flag it.
    db = _make_db(tmp_path)

    class _OddShape:
        async def write_record(self, record, *, etid=None):
            return {"not_an_id_field": "x"}

    writer = LifecycleEventWriter(db, _OddShape(), etid=9000024, sleep=_no_sleep)
    event = build_training_fingerprint(
        model_name="intent", row_ids=[1], content_hash="x",
    )
    mirror_id = await writer.write_event(event)
    row = _read_mirror(db, mirror_id)
    # status is "written" because submit didn't raise, but id is null.
    assert row["write_status"] == "written"
    assert row["walacor_record_id"] is None


# ── Constructor guards ──────────────────────────────────────────────────────

def test_rejects_none_client(tmp_path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="walacor_client is required"):
        LifecycleEventWriter(db, None, etid=9000024)


def test_max_attempts_clamped_to_backoff_length(tmp_path):
    # Asking for 99 attempts clamps to 1 + len(_BACKOFF_SECONDS) = 4 so
    # `write_event` can't wait for a backoff that doesn't exist.
    db = _make_db(tmp_path)
    client = _FakeWalacorOk()
    writer = LifecycleEventWriter(db, client, etid=9000024, max_attempts=99)
    from gateway.intelligence.walacor_writer import _BACKOFF_SECONDS
    assert writer._max_attempts == 1 + len(_BACKOFF_SECONDS)
