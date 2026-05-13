"""Tests for the extracted gateway.pipeline.chain_helpers module (C5).

The helper consolidates the (reserve → stamp → sign → advance) sequence that
used to live as bit-for-bit copies in `pipeline/orchestrator.py` and
`openwebui/governance.py`. These tests verify:

* `apply_session_chain` stamps chain fields onto the record when chain
  tracking is enabled (and no-ops otherwise).
* `advance_session_chain` is the unconditional follow-up on successful write
  (C7) — the lock + advance no longer gates the tracker update on signing
  success.
* `session_chain_critical_section` actually acquires the lock returned by the
  tracker, so concurrent calls serialise (C4).
* `record_signing_enabled` (C3) actually controls whether `sign_canonical` is
  called: off → no signing attempt; on + key loaded → signed; on + no key →
  logged loudly, record advances unsigned.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.pipeline.chain_helpers import (
    PreInferenceResult,
    advance_session_chain,
    apply_session_chain,
    run_pre_inference,
    session_chain_critical_section,
)
from gateway.pipeline.session_chain import ChainValues, SessionChainTracker


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ctx_with_tracker(tracker):
    ctx = MagicMock()
    ctx.session_chain = tracker
    return ctx


def _settings(*, chain_enabled: bool = True, signing_enabled: bool = False):
    s = MagicMock()
    s.session_chain_enabled = chain_enabled
    s.record_signing_enabled = signing_enabled
    return s


# ── apply_session_chain ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_apply_session_chain_stamps_seq_and_prev_id():
    """First record in a fresh session gets seq=0 and previous_record_id=None."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-1",
        "record_id": "01234567-89ab-7cde-f012-345678901234",
        "timestamp": "2026-05-12T10:00:00+00:00",
    }
    res = await apply_session_chain(record, "s1", ctx, _settings())
    assert res.applied is True
    assert res.sequence_number == 0
    assert res.previous_record_id is None
    assert record["sequence_number"] == 0
    assert record["previous_record_id"] is None
    # Signing was disabled → never even tried.
    assert res.record_signature_attempted is False
    assert res.record_signature_ok is False
    assert "record_signature" not in record


@pytest.mark.anyio
async def test_apply_session_chain_disabled():
    """settings.session_chain_enabled=False → no stamping."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {"execution_id": "ex-1"}
    res = await apply_session_chain(record, "s1", ctx, _settings(chain_enabled=False))
    assert res.applied is False
    assert "sequence_number" not in record


@pytest.mark.anyio
async def test_apply_session_chain_no_session_id():
    """session_id=None → no stamping."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    res = await apply_session_chain({}, None, ctx, _settings())
    assert res.applied is False


@pytest.mark.anyio
async def test_apply_session_chain_no_tracker():
    """ctx.session_chain is None → no stamping."""
    ctx = MagicMock()
    ctx.session_chain = None
    res = await apply_session_chain({}, "s1", ctx, _settings())
    assert res.applied is False


@pytest.mark.anyio
async def test_apply_session_chain_handles_tracker_error():
    """next_chain_values raising must NOT propagate; chain is skipped."""
    tracker = MagicMock()
    tracker.next_chain_values = AsyncMock(side_effect=RuntimeError("redis down"))
    ctx = _ctx_with_tracker(tracker)
    res = await apply_session_chain({"execution_id": "x"}, "s1", ctx, _settings())
    assert res.applied is False


# ── Signing gate (C3) ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_signing_disabled_skips_sign_canonical_call():
    """record_signing_enabled=False → sign_canonical is never invoked, even
    if a key happens to be loaded. The previous code called it unconditionally
    whenever the import succeeded, making the flag meaningless."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-2",
        "record_id": "01234567-89ab-7cde-f012-345678901235",
        "timestamp": "2026-05-12T10:01:00+00:00",
    }
    with patch("gateway.crypto.signing.sign_canonical") as mock_sign:
        await apply_session_chain(record, "s1", ctx, _settings(signing_enabled=False))
    mock_sign.assert_not_called()


@pytest.mark.anyio
async def test_signing_enabled_but_no_key_logs_and_continues(caplog):
    """record_signing_enabled=True + no key loaded → loud log + record still
    advances unsigned. Best-effort signing must never block the chain."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-3",
        "record_id": "01234567-89ab-7cde-f012-345678901236",
        "timestamp": "2026-05-12T10:02:00+00:00",
    }
    with (
        patch("gateway.crypto.signing.signing_key_available", return_value=False),
        patch("gateway.crypto.signing.sign_canonical") as mock_sign,
        caplog.at_level(logging.ERROR, logger="gateway.pipeline.chain_helpers"),
    ):
        res = await apply_session_chain(record, "s1", ctx, _settings(signing_enabled=True))
    # Sign was attempted (flag was true) but no key loaded → not invoked.
    assert res.record_signature_attempted is True
    assert res.record_signature_ok is False
    mock_sign.assert_not_called()
    assert "record_signing_enabled=true but no signing key loaded" in caplog.text
    # Chain still applied normally.
    assert res.applied is True
    assert record["sequence_number"] == 0


@pytest.mark.anyio
async def test_signing_enabled_with_key_actually_signs():
    """record_signing_enabled=True + key loaded → sign_canonical IS called and
    record_signature is stamped on the record."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-4",
        "record_id": "01234567-89ab-7cde-f012-345678901237",
        "timestamp": "2026-05-12T10:03:00+00:00",
    }
    with (
        patch("gateway.crypto.signing.signing_key_available", return_value=True),
        patch("gateway.crypto.signing.sign_canonical", return_value="sig-bytes-b64"),
    ):
        res = await apply_session_chain(record, "s1", ctx, _settings(signing_enabled=True))
    assert res.record_signature_attempted is True
    assert res.record_signature_ok is True
    assert record.get("record_signature") == "sig-bytes-b64"


# ── advance_session_chain (C7) ───────────────────────────────────────────


@pytest.mark.anyio
async def test_advance_session_chain_advances_tracker():
    """After a successful record write, the tracker MUST advance so the next
    request sees the new last_record_id."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-1",
        "record_id": "01234567-89ab-7cde-f012-345678901234",
        "timestamp": "2026-05-12T10:00:00+00:00",
    }
    res = await apply_session_chain(record, "s1", ctx, _settings())
    await advance_session_chain(record, "s1", ctx, res)

    # Next reservation must see the new record_id as previous_record_id.
    next_vals = await tracker.next_chain_values("s1")
    assert next_vals.sequence_number == 1
    assert next_vals.previous_record_id == record["record_id"]


@pytest.mark.anyio
async def test_advance_session_chain_independent_of_signing_failure():
    """C7 regression: signing failure must NOT prevent tracker advance.

    The pre-fix code computed `record_hash_val = _apply_session_chain(...)`
    and gated `session_chain.update(...)` on that boolean. If signing raised,
    the helper returned False — so update() never ran, leaving the tracker
    stuck. Next request would forge a wrong `previous_record_id`.
    """
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)
    record = {
        "execution_id": "ex-5",
        "record_id": "01234567-89ab-7cde-f012-345678901238",
        "timestamp": "2026-05-12T10:04:00+00:00",
    }
    # Simulate "signing was enabled and blew up" — the helper still applies
    # chain fields. The orchestrator's contract: advance regardless.
    with (
        patch("gateway.crypto.signing.signing_key_available", return_value=True),
        patch("gateway.crypto.signing.sign_canonical", side_effect=RuntimeError("hsm timeout")),
    ):
        res = await apply_session_chain(record, "s1", ctx, _settings(signing_enabled=True))
    # Signature ran but failed — record_signature NOT stamped.
    assert "record_signature" not in record
    # CHAIN STILL APPLIED.
    assert res.applied is True
    await advance_session_chain(record, "s1", ctx, res)
    # And the tracker advanced — next request sees this record's id as the predecessor.
    next_vals = await tracker.next_chain_values("s1")
    assert next_vals.previous_record_id == record["record_id"]


@pytest.mark.anyio
async def test_advance_session_chain_skips_on_failed_apply():
    """If apply_session_chain skipped (applied=False), advance is a no-op."""
    tracker = AsyncMock()
    tracker.update = AsyncMock()
    ctx = _ctx_with_tracker(tracker)
    from gateway.pipeline.chain_helpers import ChainResult
    skipped = ChainResult(False, None, None, False, False)
    await advance_session_chain({}, "s1", ctx, skipped)
    tracker.update.assert_not_called()


@pytest.mark.anyio
async def test_advance_session_chain_swallows_tracker_failure(caplog):
    """A Redis/transport hiccup during update() must not propagate — the
    record is already on disk, and the next request will compute a stale
    pointer that the dashboard's chain_status can flag."""
    tracker = MagicMock()
    tracker.update = AsyncMock(side_effect=RuntimeError("redis offline"))
    ctx = _ctx_with_tracker(tracker)
    from gateway.pipeline.chain_helpers import ChainResult
    res = ChainResult(True, 5, None, False, False)
    with caplog.at_level(logging.ERROR, logger="gateway.pipeline.chain_helpers"):
        # MUST NOT RAISE.
        await advance_session_chain({"record_id": "rid-1"}, "s1", ctx, res)
    assert "Session chain update failed" in caplog.text


# ── Per-session lock (C4) ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_critical_section_serialises_concurrent_calls():
    """Two concurrent tasks for the same session_id must serialise — only
    one should be inside the critical section at a time. Without this the
    plugin governance path could produce two records pointing at the same
    `previous_record_id`."""
    tracker = SessionChainTracker()
    ctx = _ctx_with_tracker(tracker)

    # Order-of-entry log: tasks that enter the section append; the one that
    # exits first appends a marker. If serialisation works, the log looks
    # like ["A-enter", "A-exit", "B-enter", "B-exit"] regardless of which
    # task starts first.
    events: list[str] = []

    async def one(label: str):
        async with session_chain_critical_section(ctx, "s1"):
            events.append(f"{label}-enter")
            await asyncio.sleep(0.02)
            events.append(f"{label}-exit")

    await asyncio.gather(one("A"), one("B"))
    # The two slots must be non-interleaved.
    assert events[0].endswith("-enter")
    assert events[1].endswith("-exit")
    assert events[1].split("-")[0] == events[0].split("-")[0]


@pytest.mark.anyio
async def test_critical_section_noops_without_session():
    """No session_id → no lock — just yield. Must not crash."""
    ctx = MagicMock()
    ctx.session_chain = None
    async with session_chain_critical_section(ctx, None):
        pass  # smoke


# ── run_pre_inference (C1) ───────────────────────────────────────────────


def test_run_pre_inference_returns_typed_result():
    """`run_pre_inference` returns a PreInferenceResult with 5 fields. The
    underlying evaluate_pre_inference returns a 5-tuple; the typed wrapper
    is the C1 anti-regression guard — callers can't accidentally unpack 4."""
    from starlette.responses import JSONResponse

    fake_resp = JSONResponse({"error": "blocked"}, status_code=403)
    with patch("gateway.pipeline.policy_evaluator.evaluate_pre_inference",
               return_value=(True, 7, "blocked_by_policy", fake_resp, "test reason")):
        result = run_pre_inference(MagicMock(), MagicMock(), "att-1", {})
    assert isinstance(result, PreInferenceResult)
    assert result.blocked is True
    assert result.policy_version == 7
    assert result.policy_result == "blocked_by_policy"
    assert result.error_response is fake_resp
    assert result.failure_reason == "test reason"


def test_run_pre_inference_pass():
    """Happy path — caller can read `result.policy_result == "pass"` etc."""
    with patch("gateway.pipeline.policy_evaluator.evaluate_pre_inference",
               return_value=(False, 1, "pass", None, None)):
        result = run_pre_inference(MagicMock(), MagicMock(), "att-1", {})
    assert result.blocked is False
    assert result.error_response is None
    assert result.policy_result == "pass"
