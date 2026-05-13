"""Regression tests for DeliveryWorker per-record retry + DLQ promotion.

Covers fix A2:

* A stuck record (5xx forever) must NOT block the entire queue — the
  previous code ``break``-ed on the oldest pending record's 5xx, which
  starved every later record indefinitely.

* After N retries (configurable via ``WALACOR_WAL_DELIVERY_MAX_RETRIES``)
  the worker pushes the record to the WAL DLQ and continues.

* A genuinely down aggregator should still trigger a backoff via the
  batch error budget so the worker doesn't pin the CPU.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gateway.config import get_settings
from gateway.wal.delivery_worker import DeliveryWorker


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _settings_isolation(monkeypatch):
    """Force a clean settings cache so each test sees its own env."""
    monkeypatch.setenv("WALACOR_CONTROL_PLANE_URL", "http://cp.example")
    monkeypatch.setenv("WALACOR_WAL_DELIVERY_MAX_RETRIES", "3")
    monkeypatch.setenv("WALACOR_WAL_DELIVERY_BATCH_ERROR_BUDGET", "100")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_wal_with_rows(rows: list[tuple[str, str, str]]) -> MagicMock:
    """Build a MagicMock that looks like a WALWriter for the worker."""
    wal = MagicMock()
    wal.get_undelivered = MagicMock(return_value=rows)
    wal.mark_delivered = MagicMock()
    wal.mark_dead_lettered = MagicMock()
    wal.purge_delivered = MagicMock(return_value=0)
    wal.purge_attempts = MagicMock(return_value=0)
    return wal


def _http_response(status: int, *, text: str = "") -> httpx.Response:
    return httpx.Response(status, text=text)


@pytest.mark.anyio
async def test_5xx_does_not_break_batch(monkeypatch):
    """A2: a stuck record's 5xx must not starve later records.

    The first record returns 500 every time; the rest return 201. The
    worker must mark records 2-3 delivered and only the first one
    accumulates retries.
    """
    wal = _fake_wal_with_rows([
        ("exec-stuck", "{}", "ts"),
        ("exec-ok-1", "{}", "ts"),
        ("exec-ok-2", "{}", "ts"),
    ])
    worker = DeliveryWorker(wal)
    worker._batch_error_budget = 100  # don't back off in this test

    async def _post(url, *, json, headers):
        eid = json.get("execution_id") or ""
        # Body comes back as a JSON string per record_json; we plant a
        # special hint via json.dumps('execution_id') if the caller
        # threads it through. Simpler: distinguish by call order.
        return _http_response(500)

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock()
    # Sequence: 500, 201, 201
    fake_client.post.side_effect = [
        _http_response(500),
        _http_response(201),
        _http_response(201),
    ]

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    await worker._deliver_batch()

    # ok-1 and ok-2 should both be marked delivered — the stuck record
    # must not have short-circuited the loop.
    delivered_ids = {c.args[0] for c in wal.mark_delivered.call_args_list}
    assert "exec-ok-1" in delivered_ids
    assert "exec-ok-2" in delivered_ids
    # The stuck record stays pending (no mark_delivered, no DLQ yet).
    assert "exec-stuck" not in delivered_ids
    assert wal.mark_dead_lettered.call_count == 0


@pytest.mark.anyio
async def test_record_promoted_to_dlq_after_max_retries(monkeypatch):
    """A2: once a record exceeds max_retries it lands in the WAL DLQ."""
    wal = _fake_wal_with_rows([("exec-poison", "{}", "ts")])
    worker = DeliveryWorker(wal)
    worker._max_retries = 3
    worker._batch_error_budget = 100

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock(return_value=_http_response(500))

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    # Run the worker through ``max_retries`` cycles.
    for _ in range(3):
        # Refresh the row mock each cycle — get_undelivered returns the
        # same row until either delivered or DLQ'd.
        wal.get_undelivered.return_value = [("exec-poison", "{}", "ts")]
        await worker._deliver_batch()

    assert wal.mark_dead_lettered.call_count == 1, (
        "Record must be DLQ'd exactly once after max_retries"
    )
    dlq_args = wal.mark_dead_lettered.call_args_list[0]
    assert dlq_args.args[0] == "exec-poison"
    # The reason must include the retry count for operator visibility.
    reason = dlq_args.args[1] if len(dlq_args.args) > 1 else ""
    assert "retries exhausted" in reason or "3/3" in reason


@pytest.mark.anyio
async def test_batch_error_budget_signals_backoff(monkeypatch):
    """A2: when the per-batch error budget is hit, the worker breaks early.

    The return value of ``_deliver_batch`` is the signal that ``_loop``
    uses to apply exponential backoff before the next cycle. Without
    this signal, a fully-down aggregator would generate one POST per
    record per second.
    """
    wal = _fake_wal_with_rows([
        ("exec-1", "{}", "ts"),
        ("exec-2", "{}", "ts"),
        ("exec-3", "{}", "ts"),
        ("exec-4", "{}", "ts"),
    ])
    worker = DeliveryWorker(wal)
    worker._max_retries = 100
    worker._batch_error_budget = 2

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock(return_value=_http_response(503))

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    unhealthy = await worker._deliver_batch()
    assert unhealthy is True, "Batch must report unhealthy when error budget is hit"
    # We should have stopped after the budget hit (POST called at most
    # ``batch_error_budget`` times).
    assert fake_client.post.await_count == 2


@pytest.mark.anyio
async def test_transport_error_continues_to_next_record(monkeypatch):
    """A2: a transport failure (connect refused) must not break the batch."""
    wal = _fake_wal_with_rows([
        ("exec-bad", "{}", "ts"),
        ("exec-good", "{}", "ts"),
    ])
    worker = DeliveryWorker(wal)
    worker._max_retries = 100
    worker._batch_error_budget = 100

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock(side_effect=[
        httpx.ConnectError("connection refused"),
        _http_response(201),
    ])

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    await worker._deliver_batch()

    # ``exec-good`` should have been delivered even though ``exec-bad``
    # blew up with a transport error.
    delivered_ids = {c.args[0] for c in wal.mark_delivered.call_args_list}
    assert "exec-good" in delivered_ids


@pytest.mark.anyio
async def test_success_clears_retry_counter(monkeypatch):
    """A2: a successful delivery must drop the retry counter so a
    later transient failure isn't pre-bumped from previous attempts."""
    wal = _fake_wal_with_rows([("exec-1", "{}", "ts")])
    worker = DeliveryWorker(wal)
    worker._attempt_counts["exec-1"] = 2  # simulate prior failures

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock(return_value=_http_response(201))

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    await worker._deliver_batch()

    assert "exec-1" not in worker._attempt_counts, (
        "Successful delivery must clear the retry counter"
    )


@pytest.mark.anyio
async def test_drain_helper_runs_one_batch(monkeypatch):
    """A4 + A2: the drain helper issues one _deliver_batch within the bounded timeout."""
    wal = _fake_wal_with_rows([("exec-drain", "{}", "ts")])
    worker = DeliveryWorker(wal)
    worker._batch_error_budget = 100

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = AsyncMock(return_value=_http_response(201))

    async def _get_client():
        return fake_client

    monkeypatch.setattr(worker, "_get_client", _get_client)

    await worker.drain(timeout=1.0)
    assert "exec-drain" in {c.args[0] for c in wal.mark_delivered.call_args_list}
