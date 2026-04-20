"""Unit tests for Redis-backed session chain and budget trackers (mocked)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

# Pin anyio tests to asyncio backend (AsyncMock is asyncio-specific)
@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param

from gateway.pipeline.session_chain import (
    RedisSessionChainTracker,
    SessionChainTracker,
    make_session_chain_tracker,
    GENESIS_HASH,
)
from gateway.pipeline.budget_tracker import (
    RedisBudgetTracker,
    BudgetTracker,
    make_budget_tracker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_redis():
    """Build a minimal async Redis mock with pipeline support."""
    client = MagicMock()
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    client.pipeline = MagicMock(return_value=pipe)
    return client, pipe


def _mock_budget_redis():
    """Build a Redis mock for budget tracker tests (eval-based)."""
    client = MagicMock()
    client.eval = AsyncMock(return_value=[1, 900])
    return client


# ---------------------------------------------------------------------------
# RedisSessionChainTracker
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_redis_session_next_chain_values_first_call_returns_genesis():
    client, pipe = _mock_redis()
    # First call: HINCRBY on non-existent key returns 1, HGET hash returns None, HGET record_id returns None
    pipe.execute = AsyncMock(return_value=[1, None, None, True])

    tracker = RedisSessionChainTracker(client, ttl=3600)
    cv = await tracker.next_chain_values("sess-abc")

    # First record should be seq=0 (HINCRBY returns 1, minus 1 = 0)
    assert cv.sequence_number == 0
    assert cv.previous_record_hash == GENESIS_HASH
    assert cv.previous_record_id is None


@pytest.mark.anyio
async def test_redis_session_next_chain_values_subsequent_call_returns_stored_hash():
    client, pipe = _mock_redis()
    stored_hash = "a" * 128
    # HINCRBY increments to 3, HGET returns stored hash, HGET record_id returns None
    pipe.execute = AsyncMock(return_value=[3, stored_hash.encode(), None, True])

    tracker = RedisSessionChainTracker(client, ttl=3600)
    cv = await tracker.next_chain_values("sess-abc")

    assert cv.sequence_number == 2
    assert cv.previous_record_hash == stored_hash


@pytest.mark.anyio
async def test_redis_session_update_writes_seq_hash_and_expire():
    """update() must write BOTH seq and hash atomically (Finding 3 fix)."""
    client, pipe = _mock_redis()
    pipe.execute = AsyncMock(return_value=[1, 1, 1])

    tracker = RedisSessionChainTracker(client, ttl=3600)
    await tracker.update("sess-abc", 1, "hash123")

    # Both seq and hash must be written
    pipe.hset.assert_any_call("gateway:session:sess-abc", "seq", 1)
    pipe.hset.assert_any_call("gateway:session:sess-abc", "hash", "hash123")
    assert pipe.hset.call_count == 2
    pipe.expire.assert_called_once_with("gateway:session:sess-abc", 3600)
    pipe.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_redis_session_active_session_count_returns_sentinel():
    client, _ = _mock_redis()
    tracker = RedisSessionChainTracker(client, ttl=3600)
    assert tracker.active_session_count() == -1


# ---------------------------------------------------------------------------
# RedisSessionChainTracker key format
# ---------------------------------------------------------------------------

def test_redis_session_key_format():
    client, _ = _mock_redis()
    tracker = RedisSessionChainTracker(client, ttl=600)
    assert tracker._key("my-session") == "gateway:session:my-session"


# ---------------------------------------------------------------------------
# RedisBudgetTracker
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_redis_budget_check_and_reserve_calls_eval_with_correct_args():
    client = _mock_budget_redis()

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    allowed, remaining = await tracker.check_and_reserve("tenant-1", None, 100)

    assert allowed is True
    assert remaining == 900
    client.eval.assert_awaited_once()
    call_args = client.eval.call_args
    # KEYS[1] should start with "gateway:budget:tenant-1::"
    assert call_args.args[2].startswith("gateway:budget:tenant-1::")
    # max_tokens arg
    assert call_args.args[3] == "1000"
    # estimated arg
    assert call_args.args[4] == "100"


@pytest.mark.anyio
async def test_redis_budget_check_and_reserve_blocked():
    client = MagicMock()
    client.eval = AsyncMock(return_value=[0, 50])  # blocked, 50 remaining

    tracker = RedisBudgetTracker(client, period="daily", max_tokens=100)
    allowed, remaining = await tracker.check_and_reserve("tenant-1", "user-a", 200)

    assert allowed is False
    assert remaining == 50


@pytest.mark.anyio
async def test_redis_budget_check_and_reserve_redis_error_fails_open():
    """On Redis failure, check_and_reserve fails open (allows request) and logs the error."""
    client = MagicMock()
    client.eval = AsyncMock(side_effect=Exception("Redis down"))

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    allowed, remaining = await tracker.check_and_reserve("tenant-1", None, 100)

    assert allowed is True    # fail-open
    assert remaining == -1   # unlimited sentinel
    # Reservation key must be cleaned up after the failed eval
    assert ("tenant-1", "") not in tracker._reservation_keys


@pytest.mark.anyio
async def test_redis_budget_record_usage_applies_positive_delta():
    """record_usage with actual > estimated applies delta via Lua eval (floor-clamped)."""
    client = _mock_budget_redis()

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    # Seed the reservation key so record_usage uses it
    await tracker.check_and_reserve("tenant-1", None, 80)
    await tracker.record_usage("tenant-1", None, 120, estimated=80)

    # delta = 120 - 80 = 40 → eval called twice (check_and_reserve + record_usage)
    assert client.eval.await_count == 2
    last_call = client.eval.call_args_list[1]
    assert last_call.args[3] == "40"   # delta argument


@pytest.mark.anyio
async def test_redis_budget_record_usage_applies_negative_delta():
    """record_usage with actual < estimated applies negative delta (refund) via Lua eval."""
    client = _mock_budget_redis()

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    await tracker.check_and_reserve("tenant-1", None, 100)
    await tracker.record_usage("tenant-1", None, 60, estimated=100)

    # delta = 60 - 100 = -40 → eval called with delta="-40"
    assert client.eval.await_count == 2
    last_call = client.eval.call_args_list[1]
    assert last_call.args[3] == "-40"  # negative delta = refund


@pytest.mark.anyio
async def test_redis_budget_record_usage_zero_delta_is_noop():
    """record_usage when actual == estimated makes no Redis eval calls."""
    client = _mock_budget_redis()

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    await tracker.record_usage("tenant-1", None, 100, estimated=100)

    # Zero delta: record_usage returns early — eval not called at all
    client.eval.assert_not_awaited()


@pytest.mark.anyio
async def test_redis_budget_record_usage_uses_reservation_key():
    """record_usage uses the key stored by check_and_reserve to avoid period-boundary mismatch."""
    client = _mock_budget_redis()

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)

    # Reserve tokens — stores the period key in FIFO queue
    await tracker.check_and_reserve("t1", None, 100)

    # Confirm the reservation key was stored (FIFO list with one entry)
    assert ("t1", "") in tracker._reservation_keys
    assert len(tracker._reservation_keys[("t1", "")]) == 1

    # Call record_usage — should pop the stored key (delta = 20)
    await tracker.record_usage("t1", None, 120, estimated=100)

    # Key should be consumed (list entry removed; dict key deleted when empty)
    assert ("t1", "") not in tracker._reservation_keys
    # Both check_and_reserve and record_usage called eval
    assert client.eval.await_count == 2


@pytest.mark.anyio
async def test_redis_budget_reservation_keys_fifo_order():
    """Concurrent check_and_reserve calls for same tenant use FIFO ordering in record_usage."""
    client = _mock_budget_redis()
    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)

    # Two successive reservations for the same tenant
    await tracker.check_and_reserve("t1", None, 100)
    await tracker.check_and_reserve("t1", None, 200)

    queue = tracker._reservation_keys[("t1", "")]
    assert len(queue) == 2
    second_key = queue[1][0]

    # FIFO: first record_usage consumes the first reservation slot
    await tracker.record_usage("t1", None, 110, estimated=100)
    assert len(tracker._reservation_keys[("t1", "")]) == 1
    # Remaining slot is the second reservation's key
    assert tracker._reservation_keys[("t1", "")][0][0] == second_key

    # Second record_usage consumes the second slot
    await tracker.record_usage("t1", None, 210, estimated=200)
    assert ("t1", "") not in tracker._reservation_keys


@pytest.mark.anyio
async def test_redis_budget_record_usage_no_prior_reserve_uses_period_key():
    """record_usage without prior check_and_reserve falls back to computing a fresh period key."""
    client = _mock_budget_redis()
    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)

    # No check_and_reserve call — record_usage must use fallback period key
    await tracker.record_usage("t1", None, 50, estimated=0)

    client.eval.assert_awaited_once()
    call_args = client.eval.call_args
    # Key must follow the standard budget key format
    assert call_args.args[2].startswith("gateway:budget:t1::")
    assert call_args.args[3] == "50"  # delta


@pytest.mark.anyio
async def test_redis_budget_record_usage_redis_error_does_not_raise():
    """On Redis eval failure, record_usage logs and does not propagate the error."""
    client = MagicMock()
    client.eval = AsyncMock(side_effect=Exception("Redis down"))

    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)
    # Should not raise (delta = 120 - 100 = 20, so eval is attempted)
    await tracker.record_usage("t1", None, 120, estimated=100)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def test_make_session_chain_tracker_no_redis_returns_in_memory():
    settings = MagicMock()
    settings.session_chain_max_sessions = 1000
    settings.session_chain_ttl = 3600
    tracker = make_session_chain_tracker(None, settings)
    assert isinstance(tracker, SessionChainTracker)


def test_make_session_chain_tracker_with_redis_returns_redis_tracker():
    client = MagicMock()
    settings = MagicMock()
    settings.session_chain_ttl = 3600
    tracker = make_session_chain_tracker(client, settings)
    assert isinstance(tracker, RedisSessionChainTracker)


def test_make_budget_tracker_no_redis_returns_in_memory():
    settings = MagicMock()
    settings.token_budget_period = "monthly"
    settings.token_budget_max_tokens = 1000
    tracker = make_budget_tracker(None, settings)
    assert isinstance(tracker, BudgetTracker)


def test_make_budget_tracker_with_redis_returns_redis_tracker():
    client = MagicMock()
    settings = MagicMock()
    settings.token_budget_period = "monthly"
    settings.token_budget_max_tokens = 1000
    tracker = make_budget_tracker(client, settings)
    assert isinstance(tracker, RedisBudgetTracker)


# ---------------------------------------------------------------------------
# In-memory SessionChainTracker — async interface
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_in_memory_session_chain_async_interface():
    tracker = SessionChainTracker(max_sessions=100, ttl_seconds=3600)
    cv = await tracker.next_chain_values("s1")
    assert cv.sequence_number == 0
    assert cv.previous_record_hash == GENESIS_HASH

    await tracker.update("s1", 0, "hash-abc")

    cv2 = await tracker.next_chain_values("s1")
    assert cv2.sequence_number == 1
    assert cv2.previous_record_hash == "hash-abc"


@pytest.mark.anyio
async def test_in_memory_session_chain_eviction_by_max_sessions():
    """_evict_locked removes the least-recently-active session when count exceeds max_sessions."""
    tracker = SessionChainTracker(max_sessions=2, ttl_seconds=3600)

    # Add two sessions (within limit)
    await tracker.update("s1", 0, "hash-s1")
    await tracker.update("s2", 0, "hash-s2")
    assert tracker.active_session_count() == 2

    # Third session triggers eviction: s1 is oldest → evicted
    await tracker.update("s3", 0, "hash-s3")
    assert tracker.active_session_count() == 2

    # s1 was evicted: next_chain_values returns genesis (new session)
    cv = await tracker.next_chain_values("s1")
    assert cv.sequence_number == 0
    assert cv.previous_record_hash == GENESIS_HASH

    # s2 and s3 are still tracked
    cv2 = await tracker.next_chain_values("s2")
    assert cv2.sequence_number == 1  # continues from stored state


@pytest.mark.anyio
async def test_in_memory_session_chain_ttl_eviction():
    """Sessions inactive beyond TTL are purged before LRU eviction runs."""
    from datetime import datetime, timezone, timedelta

    tracker = SessionChainTracker(max_sessions=1, ttl_seconds=60)

    await tracker.update("old", 0, "hash-old")
    assert tracker.active_session_count() == 1

    # Manually age the session past the TTL
    async with tracker._lock:
        tracker._sessions["old"].last_activity = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        )

    # Adding a new session with max_sessions=1 triggers eviction
    await tracker.update("new", 0, "hash-new")

    # TTL eviction removed "old" before LRU; only "new" remains
    assert tracker.active_session_count() == 1
    assert "old" not in tracker._sessions


# ---------------------------------------------------------------------------
# In-memory BudgetTracker — reservation semantics
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_in_memory_budget_tracker_reserves_on_check():
    """check_and_reserve deducts estimated immediately (Finding 2 fix)."""
    tracker = BudgetTracker()
    tracker.configure("t1", None, "monthly", 1000)

    allowed, remaining = await tracker.check_and_reserve("t1", None, 100)
    assert allowed is True
    # Reservation is immediate: 1000 - 100 = 900 remaining
    assert remaining == 900


@pytest.mark.anyio
async def test_in_memory_budget_tracker_delta_correction():
    """record_usage applies actual-estimated delta to adjust the reservation."""
    tracker = BudgetTracker()
    tracker.configure("t1", None, "monthly", 1000)

    # Reserve 100 estimated
    await tracker.check_and_reserve("t1", None, 100)
    # Actual was 120: delta = 120 - 100 = +20
    await tracker.record_usage("t1", None, 120, estimated=100)

    # tokens_used = 100 (reserved) + 20 (delta) = 120
    # Next reservation of 100: remaining = 1000 - 120 - 100 = 780
    allowed2, remaining2 = await tracker.check_and_reserve("t1", None, 100)
    assert allowed2 is True
    assert remaining2 == 780


@pytest.mark.anyio
async def test_in_memory_budget_tracker_refund_when_actual_less():
    """record_usage refunds when actual < estimated."""
    tracker = BudgetTracker()
    tracker.configure("t1", None, "monthly", 1000)

    # Reserve 200 estimated
    await tracker.check_and_reserve("t1", None, 200)
    # Actual was only 50: delta = 50 - 200 = -150 (refund)
    await tracker.record_usage("t1", None, 50, estimated=200)

    # tokens_used = 200 - 150 = 50
    allowed2, remaining2 = await tracker.check_and_reserve("t1", None, 100)
    assert allowed2 is True
    assert remaining2 == 850  # 1000 - 50 - 100


@pytest.mark.anyio
async def test_in_memory_budget_tracker_blocks_when_exhausted():
    """check_and_reserve returns (False, remaining) when budget is exhausted."""
    tracker = BudgetTracker()
    tracker.configure("t1", None, "monthly", 100)

    # Reserve nearly all budget
    allowed, _ = await tracker.check_and_reserve("t1", None, 90)
    assert allowed is True

    # Next request wants 20 but only 10 remain → blocked
    allowed2, remaining2 = await tracker.check_and_reserve("t1", None, 20)
    assert allowed2 is False
    assert remaining2 == 10


@pytest.mark.anyio
async def test_in_memory_budget_tracker_no_budget_configured_always_allows():
    """When no budget is configured for a tenant, all requests are allowed."""
    tracker = BudgetTracker()
    # No configure() call
    allowed, remaining = await tracker.check_and_reserve("t1", None, 1_000_000)
    assert allowed is True
    assert remaining == -1  # -1 = unlimited


# ---------------------------------------------------------------------------
# Redis error resilience (updated: next_chain_values and update now raise)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_redis_session_next_chain_values_redis_error_raises():
    """On Redis failure, next_chain_values raises so _apply_session_chain can skip chain
    fields rather than forging (0, GENESIS_HASH) for an established session."""
    client, pipe = _mock_redis()
    pipe.execute = AsyncMock(side_effect=Exception("Redis connection refused"))

    tracker = RedisSessionChainTracker(client, ttl=3600)
    with pytest.raises(Exception, match="Redis connection refused"):
        await tracker.next_chain_values("sess-err")


@pytest.mark.anyio
async def test_redis_session_update_redis_error_raises():
    """On Redis failure, update() raises so the caller can log the chain divergence."""
    client, pipe = _mock_redis()
    pipe.execute = AsyncMock(side_effect=Exception("Redis timeout"))

    tracker = RedisSessionChainTracker(client, ttl=3600)
    with pytest.raises(Exception, match="Redis timeout"):
        await tracker.update("sess-err", 1, "hash123")


@pytest.mark.anyio
async def test_redis_session_next_chain_values_str_hash_decoded_correctly():
    """prev_hash is returned correctly whether Redis returns bytes or str."""
    client, pipe = _mock_redis()
    stored_hash = "b" * 128
    # HINCRBY returns 4 (incremented), HGET returns str hash (decode_responses=True), HGET record_id None
    pipe.execute = AsyncMock(return_value=[4, stored_hash, None, True])

    tracker = RedisSessionChainTracker(client, ttl=3600)
    cv = await tracker.next_chain_values("sess-str")

    assert cv.sequence_number == 3
    assert cv.previous_record_hash == stored_hash


# ---------------------------------------------------------------------------
# Redis budget period key — December → January rollover
# ---------------------------------------------------------------------------

def test_redis_budget_period_key_december_rollover():
    """December → January rollover computes a valid key (no month=13 bug)."""
    from unittest.mock import patch
    from datetime import datetime, timezone

    client = _mock_budget_redis()
    tracker = RedisBudgetTracker(client, period="monthly", max_tokens=1000)

    dec_31 = datetime(2024, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
    with patch("gateway.pipeline.budget_tracker.datetime") as mock_dt:
        mock_dt.now.return_value = dec_31
        key, ttl = tracker._period_key("t1", None)

    # Key must contain "202412" (December), not an invalid period
    assert "202412" in key
    assert key.startswith("gateway:budget:t1::")
    # TTL must be positive (≥ 60 seconds remaining in December + 3600 buffer)
    assert ttl > 3600


# ---------------------------------------------------------------------------
# Orchestrator: _record_token_usage streaming refund branch
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_record_token_usage_zero_usage_refunds_reservation():
    """When usage=0 (streaming with no usage field) but estimated>0, budget refund is called."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from gateway.pipeline.orchestrator import _record_token_usage

    mock_tracker = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.budget_tracker = mock_tracker

    # ModelResponse with empty usage (simulates streaming provider with no usage field)
    mock_response = MagicMock()
    mock_response.usage = {}

    with patch("gateway.pipeline.orchestrator.get_pipeline_context", return_value=mock_ctx):
        total = await _record_token_usage(
            mock_response, "tenant-1", "openai", None, estimated=100
        )

    assert total == 0
    # Refund: actual=0, estimated=100 → record_usage(tenant_id, user, 0, estimated=100)
    mock_tracker.record_usage.assert_awaited_once_with("tenant-1", None, 0, 100)


# ---------------------------------------------------------------------------
# Orchestrator: session chain seq/hash round-trip (T4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_and_write_record_session_chain_update_matches_embedded_values():
    """session_chain.update receives the exact seq_num and record_hash that _apply_session_chain
    embedded in the audit record — verifying the round-trip in _build_and_write_record."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from gateway.pipeline.orchestrator import _build_and_write_record, _AuditParams

    # Session chain mock: returns seq=7 and a known previous hash
    from gateway.pipeline.session_chain import ChainValues
    mock_chain = MagicMock()
    mock_chain.next_chain_values = AsyncMock(return_value=ChainValues(
        sequence_number=7, previous_record_hash="prev-hash-xyz", previous_record_id=None
    ))
    mock_chain.update = AsyncMock()

    ctx = MagicMock()
    ctx.session_chain = mock_chain
    ctx.walacor_client = None
    ctx.wal_writer = None
    ctx.tool_registry = None

    settings = MagicMock()
    settings.session_chain_enabled = True
    settings.gateway_tenant_id = "test-tenant"
    settings.gateway_id = "gw-1"
    settings.enforcement_mode = "enforce"

    call = MagicMock()
    call.metadata = {"session_id": "sess-abc", "user": None, "prompt_id": None}

    model_response = MagicMock()
    model_response.completion = "hi"
    model_response.usage = {}
    model_response.tool_interactions = []

    params = _AuditParams(
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        budget_remaining=None,
        audit_metadata={},
        tool_interactions=[],
        tool_strategy="passive",
        tool_iterations=0,
        rp_version=0,
        rp_result="pass",
        rp_decisions=[],
    )

    record = {
        "execution_id": "exec-123",
        "policy_version": 1,
        "policy_result": "pass",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    mock_request = MagicMock()

    with patch("gateway.pipeline.orchestrator._store_execution", new=AsyncMock()), \
         patch("gateway.pipeline.orchestrator._write_tool_events", new=AsyncMock()), \
         patch("gateway.pipeline.orchestrator.build_execution_record", return_value=record):
        await _build_and_write_record(mock_request, call, model_response, params, ctx, settings)

    # Verify the embedded chain fields are consistent
    assert record["sequence_number"] == 7
    assert "previous_record_id" in record  # ID-pointer chain replaces hash chain

    # session_chain.update must be called with record_id (no longer hash)
    mock_chain.update.assert_awaited_once_with(
        "sess-abc",
        record["sequence_number"],
        record_id=record.get("record_id"),
    )
