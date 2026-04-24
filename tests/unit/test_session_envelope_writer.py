"""Unit tests for Phase 24 SessionEnvelopeWriter (Phase A scope).

Covers in-memory state, per-session locking, dual-write ordering, failure
isolation, rollover-cap behaviour, verbatim content storage, record_hash
preservation, and the redaction tombstone.

The writer mocks both the WAL side (synchronous .write_session_envelope)
and the Walacor side (async .write_session_envelope). Per CLAUDE.md testing
conventions, tests are @pytest.mark.anyio with an asyncio backend fixture
and do NOT use pytest.mark.asyncio.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.sessions.envelope_writer import SessionEnvelopeWriter
from gateway.sessions.state import SessionEnvelopeState


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_settings(max_turns: int = 500, max_tokens: int = 500_000):
    return SimpleNamespace(
        session_envelope_enabled=True,
        session_envelope_etid=9000014,
        session_envelope_flush_mode="per_turn",
        session_envelope_max_turns=max_turns,
        session_envelope_max_tokens=max_tokens,
        gateway_tenant_id="tenant-x",
        gateway_id="gw-test",
    )


def _make_identity(user_id: str = "alice"):
    return SimpleNamespace(
        user_id=user_id,
        email=f"{user_id}@example.com",
        roles=["engineer"],
        team="platform",
        source="jwt",
    )


def _make_execution_record(
    execution_id: str = "exec-1",
    sequence_number: int = 1,
    record_hash: str = "h" * 128,
    prompt_text: str = "hello there",
    response_content: str = "general kenobi",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
) -> dict:
    return {
        "execution_id": execution_id,
        "sequence_number": sequence_number,
        "timestamp": "2026-04-22T10:00:00+00:00",
        "model_id": "qwen3:4b",
        "provider": "ollama",
        "prompt_text": prompt_text,
        "response_content": response_content,
        "thinking_content": "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": 420.0,
        "record_hash": record_hash,
        "policy_result": "allowed",
    }


def _make_writer(settings=None, *, wal=None, walacor=None):
    settings = settings or _make_settings()
    wal_mock = wal if wal is not None else MagicMock()
    wal_mock.write_session_envelope = wal_mock.write_session_envelope  # ensure attr
    walacor_mock = walacor if walacor is not None else MagicMock()
    walacor_mock.write_session_envelope = AsyncMock()
    writer = SessionEnvelopeWriter(
        wal_writer=wal_mock,
        walacor_client=walacor_mock,
        settings=settings,
    )
    return writer, wal_mock, walacor_mock


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_on_turn_complete_creates_state():
    writer, wal, walacor = _make_writer()

    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(),
        tool_events_count=0,
        identity=_make_identity("alice"),
    )

    state = writer._states["s1"]
    assert isinstance(state, SessionEnvelopeState)
    assert state.session_id == "s1"
    assert state.tenant_id == "tenant-x"
    assert state.gateway_id == "gw-test"
    assert state.turn_count == 1
    assert state.participant_user_id == "alice"
    assert state.participant_team == "platform"
    assert state.first_record_hash == state.latest_record_hash == "h" * 128
    assert state.latest_sequence_number == 1


@pytest.mark.anyio
async def test_on_turn_complete_appends_turn():
    writer, wal, walacor = _make_writer()

    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-1", sequence_number=1, record_hash="a" * 128,
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-2", sequence_number=2, record_hash="b" * 128,
            prompt_tokens=7, completion_tokens=3, total_tokens=10,
        ),
        tool_events_count=2,
        identity=_make_identity(),
    )

    state = writer._states["s1"]
    assert state.turn_count == 2
    assert len(state.turns) == 2
    assert state.prompt_tokens == 17
    assert state.completion_tokens == 8
    assert state.total_tokens == 25
    assert state.first_record_hash == "a" * 128
    assert state.latest_record_hash == "b" * 128
    assert state.latest_sequence_number == 2
    assert state.turns[1]["tool_events_count"] == 2


@pytest.mark.anyio
async def test_concurrent_turns_same_session_serialized():
    """Per-session lock must serialise concurrent updates to one session id."""
    writer, wal, walacor = _make_writer()

    # Block the Walacor write until both tasks have entered the method; the
    # lock must force the second one to wait for the first. If the lock
    # didn't work, both would observe the same initial state and produce
    # a non-monotonic turn_count.
    order: list[str] = []
    start_event = asyncio.Event()
    release = asyncio.Event()

    async def delayed_submit(_record):
        order.append(f"submit_start:{_record['turn_count']}")
        start_event.set()
        await release.wait()
        order.append(f"submit_end:{_record['turn_count']}")

    walacor.write_session_envelope = AsyncMock(side_effect=delayed_submit)

    async def turn(record_hash: str, seq: int):
        await writer.on_turn_complete(
            session_id="shared",
            execution_record=_make_execution_record(
                execution_id=f"exec-{seq}", sequence_number=seq,
                record_hash=record_hash,
            ),
            tool_events_count=0,
            identity=_make_identity(),
        )

    t1 = asyncio.create_task(turn("a" * 128, 1))
    # Wait until the first task is inside the critical section
    await start_event.wait()
    t2 = asyncio.create_task(turn("b" * 128, 2))
    # Give the scheduler a tick — t2 should be blocked on the lock now
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(t1, t2)

    state = writer._states["shared"]
    # Both turns landed; ordering must be strict: submit for turn 1 finishes
    # before submit for turn 2 begins.
    assert state.turn_count == 2
    assert order == [
        "submit_start:1",
        "submit_end:1",
        "submit_start:2",
        "submit_end:2",
    ]


@pytest.mark.anyio
async def test_dual_write_wal_then_walacor():
    """Spec: WAL write happens before Walacor submit."""
    writer, wal, walacor = _make_writer()
    order: list[str] = []

    wal.write_session_envelope = MagicMock(side_effect=lambda row: order.append("wal"))

    async def _record(_r):
        order.append("walacor")

    walacor.write_session_envelope = AsyncMock(side_effect=_record)

    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(),
        tool_events_count=0,
        identity=_make_identity(),
    )

    assert order == ["wal", "walacor"]


@pytest.mark.anyio
async def test_walacor_failure_does_not_propagate():
    writer, wal, walacor = _make_writer()
    walacor.write_session_envelope = AsyncMock(side_effect=RuntimeError("walacor down"))

    # Must not raise.
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(),
        tool_events_count=0,
        identity=_make_identity(),
    )

    state = writer._states["s1"]
    assert state.turn_count == 1
    # WAL side was still called before Walacor failed.
    wal.write_session_envelope.assert_called_once()


@pytest.mark.anyio
async def test_wal_failure_does_not_propagate():
    writer, wal, walacor = _make_writer()
    wal.write_session_envelope = MagicMock(side_effect=RuntimeError("disk full"))

    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(),
        tool_events_count=0,
        identity=_make_identity(),
    )

    # Walacor submit still attempted even though WAL failed.
    walacor.write_session_envelope.assert_awaited_once()


@pytest.mark.anyio
async def test_rollover_at_max_turns():
    settings = _make_settings(max_turns=3, max_tokens=1_000_000)
    writer, wal, walacor = _make_writer(settings=settings)

    # Three turns fill the envelope exactly to cap.
    for i in range(1, 4):
        await writer.on_turn_complete(
            session_id="s1",
            execution_record=_make_execution_record(
                execution_id=f"exec-{i}", sequence_number=i,
                record_hash=f"{i:0128d}",
            ),
            tool_events_count=0,
            identity=_make_identity(),
        )
    state = writer._states["s1"]
    assert state.turn_count == 3
    assert state.status == "open"

    # Fourth turn triggers rollover — not added, status flipped.
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-4", sequence_number=4, record_hash="4" * 128,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )

    state = writer._states["s1"]
    assert state.status == "rolled_over"
    assert state.turn_count == 3  # Not incremented
    assert all(t["execution_id"] != "exec-4" for t in state.turns)

    # Fifth attempt is dropped silently (state already rolled_over).
    prior_calls = walacor.write_session_envelope.await_count
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-5", sequence_number=5, record_hash="5" * 128,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )
    assert walacor.write_session_envelope.await_count == prior_calls


@pytest.mark.anyio
async def test_rollover_at_max_tokens():
    settings = _make_settings(max_turns=1_000, max_tokens=100)
    writer, wal, walacor = _make_writer(settings=settings)

    # One turn of 60 tokens — under cap.
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-1", sequence_number=1, record_hash="a" * 128,
            prompt_tokens=30, completion_tokens=30, total_tokens=60,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )
    # Second turn of 50 tokens pushes us to 110 total — reaches cap.
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-2", sequence_number=2, record_hash="b" * 128,
            prompt_tokens=25, completion_tokens=25, total_tokens=50,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )
    assert writer._states["s1"].total_tokens == 110

    # Third turn is refused — rolled_over.
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-3", sequence_number=3, record_hash="c" * 128,
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )
    state = writer._states["s1"]
    assert state.status == "rolled_over"
    assert state.total_tokens == 110
    assert state.turn_count == 2


@pytest.mark.anyio
async def test_verbatim_content_stored():
    writer, wal, walacor = _make_writer()

    prompt = "Please summarise the uploaded PDF verbatim."
    response = "Here is the summary: ..."
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            prompt_text=prompt, response_content=response,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )

    state = writer._states["s1"]
    assert state.turns[0]["prompt_text"] == prompt
    assert state.turns[0]["response_content"] == response

    # Verify the same verbatim content survives WAL serialisation.
    wal_call = wal.write_session_envelope.call_args[0][0]
    envelope = json.loads(wal_call["envelope_json"])
    turns = json.loads(envelope["turns_json"])
    assert turns[0]["prompt_text"] == prompt
    assert turns[0]["response_content"] == response


@pytest.mark.anyio
async def test_record_hash_embedded():
    writer, wal, walacor = _make_writer()

    hash_val = "deadbeef" * 16  # 128 chars
    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(record_hash=hash_val),
        tool_events_count=0,
        identity=_make_identity(),
    )

    state = writer._states["s1"]
    # Hash preserved verbatim on both the turn and envelope summary fields.
    assert state.turns[0]["record_hash"] == hash_val
    assert state.first_record_hash == hash_val
    assert state.latest_record_hash == hash_val

    # Walacor record carries the same hash through the JSON payload.
    submitted = walacor.write_session_envelope.call_args[0][0]
    assert submitted["latest_record_hash"] == hash_val
    turns = json.loads(submitted["turns_json"])
    assert turns[0]["record_hash"] == hash_val


@pytest.mark.anyio
async def test_redact_turn_scrubs_content_keeps_hash():
    writer, wal, walacor = _make_writer()

    await writer.on_turn_complete(
        session_id="s1",
        execution_record=_make_execution_record(
            execution_id="exec-1", prompt_text="my SSN is 123-45-6789",
            response_content="thanks, I'll remember", record_hash="abcd" * 32,
        ),
        tool_events_count=0,
        identity=_make_identity(),
    )

    scrubbed = await writer.redact_turn("s1", "exec-1", reason="GDPR request")
    assert scrubbed is True

    turn = writer._states["s1"].turns[0]
    assert turn["prompt_text"] == ""
    assert turn["response_content"] == ""
    assert turn["thinking_content"] == ""
    assert turn["redacted"] is True
    assert turn["redaction_reason"] == "GDPR request"
    # record_hash preserved — the session chain integrity must survive redaction.
    assert turn["record_hash"] == "abcd" * 32

    # A follow-up write reflecting the redaction must have been dual-written.
    assert wal.write_session_envelope.call_count >= 2
    assert walacor.write_session_envelope.await_count >= 2

    # Redacting an unknown execution_id returns False and does not write again.
    prior_wal = wal.write_session_envelope.call_count
    prior_walacor = walacor.write_session_envelope.await_count
    not_found = await writer.redact_turn("s1", "exec-nope", reason="oops")
    assert not_found is False
    assert wal.write_session_envelope.call_count == prior_wal
    assert walacor.write_session_envelope.await_count == prior_walacor
