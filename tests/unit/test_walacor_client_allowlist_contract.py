"""CI contract: every key the orchestrator builds into an execution record
must be ON the Walacor schema allowlist OR the intentional-drop list.

Why this exists (CLAUDE.md "Failure modes & guards"): the in-product guard
in ``WalacorClient.write_execution`` *detects* drift after the fact —
WARNING + ``schema_stripped_keys`` embedded in metadata_json. That covers
prod, but the regression-window between "merge to main" and "field is
visible only on local LineageReader, missing on WalacorLineageReader" was
months in the ``timings`` case. This test PREVENTS the drift at PR time:
if a developer adds a field to ``build_execution_record`` (or any other
execution-dict assembler) without also updating ``_EXECUTION_SCHEMA_FIELDS``
/ ``_INTENTIONAL_NON_SCHEMA_KEYS``, CI fails.

Fix-by-instructions: if this test fails, choose one of:
  * Add the field to ``_EXECUTION_SCHEMA_FIELDS`` (Walacor schema gets a
    new column — also update ``scripts/setup_walacor_schemas.py``).
  * Add the field to ``_INTENTIONAL_NON_SCHEMA_KEYS`` (gateway-only).
  * Rehome it into ``metadata_json``.
"""
from __future__ import annotations

import pytest

from gateway.adapters.base import ModelCall, ModelResponse
from gateway.pipeline.hasher import build_execution_record
from gateway.walacor.client import WalacorClient


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _realistic_record() -> dict:
    """Build a record using the *same* helper the orchestrator uses.

    Every kwarg the orchestrator passes today is exercised so the fixture
    surfaces NEW orchestrator-side fields automatically — adding a kwarg
    to ``build_execution_record`` without re-running this test means the
    kwarg's key is silently dropped at the Walacor boundary.
    """
    call = ModelCall(
        provider="openai",
        model_id="gpt-4o-mini",
        prompt_text="hello",
        raw_body=b"{}",
        is_streaming=False,
        metadata={},
    )
    resp = ModelResponse(
        content="hi there",
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cached_tokens": 2,
            "cache_creation_tokens": 1,
            "cache_hit": True,
        },
        raw_body=b"{}",
        provider_request_id="req-1",
        model_hash="sha:abc",
        thinking_content="<think>…</think>",
    )
    return build_execution_record(
        call=call,
        model_response=resp,
        attestation_id="att-1",
        policy_version=3,
        policy_result="pass",
        tenant_id="tenant-1",
        gateway_id="gw-1",
        user="alice@example.com",
        session_id="ses-1",
        metadata={"timings": {"forward_ms": 100.0}},
        model_id="gpt-4o-mini",
        provider="openai",
        latency_ms=123.4,
        retry_of=None,
        timings={"forward_ms": 100.0, "total_ms": 110.0},
        variant_id="v-1",
        file_metadata=[{"name": "foo.txt"}],
    )


def test_build_execution_record_has_no_unexpected_keys():
    """Every key built into the execution record must be accounted for."""
    record = _realistic_record()
    unexpected = WalacorClient.unexpected_execution_keys(record)
    assert unexpected == [], (
        "build_execution_record produced fields the Walacor write path will "
        "silently drop on the way out (visible locally, missing on the "
        "WalacorLineageReader round-trip): "
        f"{unexpected}. Either add to _EXECUTION_SCHEMA_FIELDS, "
        "_INTENTIONAL_NON_SCHEMA_KEYS, or rehome into metadata_json. "
        "See CLAUDE.md 'Failure modes & guards' → Walacor schema-allowlist drift."
    )


def test_unexpected_execution_keys_detects_drift():
    """Sanity check: the predicate flags a synthetic foreign key."""
    record = _realistic_record()
    record["brand_new_dashboard_panel_field"] = "would-be-dropped"
    unexpected = WalacorClient.unexpected_execution_keys(record)
    assert "brand_new_dashboard_panel_field" in unexpected


def test_intentional_drops_are_not_flagged():
    """Keys on the intentional-drop list must not surface as unexpected."""
    record = {k: "x" for k in WalacorClient._INTENTIONAL_NON_SCHEMA_KEYS}
    assert WalacorClient.unexpected_execution_keys(record) == []


def test_none_values_are_not_flagged():
    """None-valued unknown keys are not "lost" (they'd be stripped regardless)."""
    record = {"genuinely_unknown_but_null": None}
    assert WalacorClient.unexpected_execution_keys(record) == []
