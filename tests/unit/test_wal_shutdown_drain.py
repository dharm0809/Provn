"""Regression test for fix A4: shutdown drain of _pending_attempt_writes.

``completeness_middleware`` spawns background tasks via
``asyncio.create_task`` so the slow Walacor HTTP round-trip never sits
on the request tail. Strong refs live in the module-level set
``_pending_attempt_writes``; tasks self-discard on completion.

Before A4, ``on_shutdown`` cancelled the event loop without awaiting
these tasks, which meant any attempt rows still in flight at shutdown
were lost. The new ``drain_pending_attempt_writes`` helper awaits the
set with a bounded timeout.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.middleware.completeness import (
    _pending_attempt_writes,
    drain_pending_attempt_writes,
)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _clear_pending():
    """Make sure each test starts with an empty pending set."""
    _pending_attempt_writes.clear()
    yield
    _pending_attempt_writes.clear()


@pytest.mark.anyio
async def test_drain_awaits_pending_tasks():
    """A4: drain must wait for in-flight attempt writes before returning."""
    results: list[str] = []

    async def _work():
        await asyncio.sleep(0.01)
        results.append("done")

    task = asyncio.create_task(_work())
    _pending_attempt_writes.add(task)
    task.add_done_callback(_pending_attempt_writes.discard)

    await drain_pending_attempt_writes(timeout=2.0)

    assert results == ["done"], "drain must wait for the task to finish"
    assert task.done()


@pytest.mark.anyio
async def test_drain_bounded_timeout():
    """A4: drain must not hang forever on a stuck attempt write."""

    async def _stuck():
        await asyncio.sleep(30.0)

    task = asyncio.create_task(_stuck())
    _pending_attempt_writes.add(task)
    task.add_done_callback(_pending_attempt_writes.discard)

    try:
        await drain_pending_attempt_writes(timeout=0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.anyio
async def test_drain_empty_noop():
    """A4: drain with no pending tasks is a fast no-op."""
    assert not _pending_attempt_writes
    # Should return immediately, not raise.
    await drain_pending_attempt_writes(timeout=0.05)


@pytest.mark.anyio
async def test_drain_tolerates_task_exception():
    """A4: a task that raises must not poison drain."""

    async def _boom():
        raise RuntimeError("kaboom")

    task = asyncio.create_task(_boom())
    _pending_attempt_writes.add(task)
    task.add_done_callback(_pending_attempt_writes.discard)
    # gather(return_exceptions=True) swallows the error.
    await drain_pending_attempt_writes(timeout=1.0)
    assert task.done()
