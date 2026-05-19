"""Regression tests for Walacor write-path audit fidelity.

These tests pin the dual-write contract: every field the orchestrator hands
to ``WalacorClient.write_execution`` / ``write_tool_event`` must end up
either as a top-level Walacor column OR inside the corresponding JSON
extension field (``metadata_json`` for executions, ``content_analysis`` for
tool events). Silent drops are the bug class these tests guard against.

Covers:
  B1 — execution-schema allowlist coverage of audit-critical fields
       (via metadata_json serialisation).
  B2 — metadata_json truncation preserves the audit-critical ``_keep``
       set and records ``metadata_truncated_keys``.
  B3 — tool-event extras (event_type, tool_id, mcp_server_url,
       client_context) fold into ``content_analysis._extras`` instead of
       being silently dropped; oversized input_data / output_data /
       sources blobs are capped and marked in
       ``tool_event_truncated_keys``.
  B4 — gateway-internal classifier keys (``_intent``, ``_intent_*``,
       ``schema_mapper_*``, ``_translated_from_openai``) rehome under
       ``metadata._internal``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from gateway.walacor.client import WalacorClient, _split_internal_keys


# ──────────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_client() -> WalacorClient:
    """Return a WalacorClient with no HTTP wiring; ``start()`` is never called."""
    return WalacorClient(server="http://localhost:9999", username="u", password="p")


@pytest.fixture
def captured_submit(monkeypatch):
    """Replace ``_submit`` with an AsyncMock; yield the mock for assertions."""
    captured: list[tuple[int, list[dict]]] = []

    async def _capture(self, etid, records):  # noqa: ANN001 — bound method shape
        captured.append((etid, [dict(r) for r in records]))

    monkeypatch.setattr(WalacorClient, "_submit", _capture)
    return captured


def _full_execution_record(**overrides) -> dict:
    """An execution record carrying every field the orchestrator emits today."""
    record: dict = {
        # Schema-supported top-level
        "execution_id": "exec-1",
        "tenant_id": "tenant-a",
        "gateway_id": "gw-1",
        "timestamp": "2026-05-12T00:00:00Z",
        "model_attestation_id": "att-1",
        "model_id": "llama3:8b",
        "provider": "ollama",
        "policy_version": 1,
        "policy_result": "pass",
        "session_id": "sess-1",
        "user": "alice",
        "prompt_text": "Hello",
        "response_content": "Hi there",
        "thinking_content": None,
        "provider_request_id": "prov-1",
        "model_hash": "sha256:abc",
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_hit": False,
        "latency_ms": 12.3,
        "sequence_number": 1,
        "record_id": "uuid7-1",
        "previous_record_id": None,
        "record_hash": None,
        "previous_record_hash": None,
        "record_signature": None,
        "variant_id": None,
        "retry_of": None,
        # Audit-critical extension fields (live inside metadata)
        "metadata": {
            # Caller identity
            "user": "alice",
            "session_id": "sess-1",
            "caller_email": "alice@example.com",
            "caller_roles": ["analyst"],
            "identity_source": "jwt",
            # Audit correlation
            "prompt_id": "req-1",
            "received_at": "2026-05-12T00:00:00Z",
            "client_context": {"ip": "10.0.0.1", "user_agent": "curl/8.0"},
            "request_type": "chat",
            # Governance decisions
            "analyzer_decisions": [
                {"analyzer_id": "truzenai.safety.v1", "verdict": "pass",
                 "confidence": 0.99, "category": "safe", "reason": ""}
            ],
            "pii_decisions": [{"type": "email", "matches": 1}],
            "response_policy_version": 2,
            "response_policy_result": "pass",
            "input_analysis": [{"analyzer_id": "x", "verdict": "pass"}],
            "enforcement_mode": "blocking",
            "walacor_audit": {"user_question": "Hello"},
            # Completeness
            "delivery_error": None,
            # Internal classifier outputs (must rehome under _internal)
            "_intent": "code_question",
            "_intent_confidence": 0.95,
            "_intent_tier": "onnx",
            "_intent_reason": "matched code-context regex",
            "_translated_from_openai": False,
            "schema_mapper_confidence": 0.88,
            "schema_mapper_mapped": 12,
            "schema_mapper_unmapped": 1,
            # Tool audit
            "tool_strategy": "active",
            "tool_interaction_count": 2,
            "tool_interactions": [
                {"tool_name": "web_search", "input_hash": "h1", "output_hash": "h2"},
            ],
        },
        "file_metadata": [{"filename": "x.pdf", "size_bytes": 1024}],
    }
    record.update(overrides)
    return record


# ──────────────────────────────────────────────────────────────────────────
# B1 — execution-schema allowlist covers audit-critical fields
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_b1_audit_critical_fields_land_in_metadata_json(captured_submit):
    """Every field the orchestrator stuffs in metadata must reach Walacor.

    The execution schema can't host arbitrary keys, so they're serialised
    into ``metadata_json``. Verifies every audit-critical field is present
    after submit.
    """
    client = _make_client()
    record = _full_execution_record()

    await client.write_execution(record)

    assert len(captured_submit) == 1
    _etid, submitted = captured_submit[0]
    body = submitted[0]
    assert "metadata_json" in body, "metadata_json must be set when metadata present"
    meta = json.loads(body["metadata_json"])

    # Caller identity must survive
    for key in ("caller_email", "caller_roles", "identity_source"):
        assert key in meta, f"caller identity field {key!r} missing from metadata_json"

    # Audit correlation
    for key in ("prompt_id", "received_at", "client_context", "request_type"):
        assert key in meta, f"audit correlation field {key!r} missing"

    # Governance decisions
    for key in ("analyzer_decisions", "pii_decisions", "response_policy_version",
                "response_policy_result", "input_analysis", "enforcement_mode",
                "walacor_audit"):
        assert key in meta, f"governance field {key!r} missing"

    # File metadata
    assert "file_metadata" in meta


@pytest.mark.anyio
async def test_b1_no_truncated_marker_when_record_fits(captured_submit):
    """A small record must NOT carry ``metadata_truncated_keys``.

    The marker should only appear when truncation actually occurred — its
    presence is the signal investigators use to detect divergence.
    """
    client = _make_client()
    record = _full_execution_record()

    await client.write_execution(record)

    body = captured_submit[0][1][0]
    meta = json.loads(body["metadata_json"])
    assert "metadata_truncated_keys" not in meta


# ──────────────────────────────────────────────────────────────────────────
# B2 — truncation preserves _keep set and records what was dropped
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_b2_truncation_preserves_keep_fields(captured_submit, monkeypatch):
    """When metadata exceeds the size cap, every key in the keep-set must survive."""
    # Lower the cap so we can trigger truncation with a manageable payload.
    monkeypatch.setattr("gateway.walacor.client._METADATA_JSON_MAX_BYTES", 1024)

    client = _make_client()
    record = _full_execution_record()
    # Inflate metadata so it exceeds the cap.
    big_payload = "x" * 4096
    record["metadata"]["bulky_unimportant"] = big_payload
    record["metadata"]["another_bulky_field"] = [big_payload] * 4

    await client.write_execution(record)

    body = captured_submit[0][1][0]
    meta = json.loads(body["metadata_json"])

    # Truncation marker must be present.
    assert "metadata_truncated_keys" in meta, (
        "truncation must record the dropped keys in metadata_truncated_keys"
    )
    dropped = meta["metadata_truncated_keys"]
    assert "bulky_unimportant" in dropped
    assert "another_bulky_field" in dropped

    # The keep-set must survive verbatim.
    must_survive = [
        "analyzer_decisions", "pii_decisions", "walacor_audit",
        "input_analysis", "response_policy_version", "response_policy_result",
        "client_context", "prompt_id", "session_id", "request_type",
        "caller_email", "caller_roles", "identity_source",
        "tool_strategy", "tool_interaction_count",
    ]
    for key in must_survive:
        assert key in meta, f"keep-set field {key!r} dropped on truncation"


@pytest.mark.anyio
async def test_b2_truncation_cap_matches_documented_limit():
    """The truncation threshold must match the documented constant."""
    from gateway.walacor.client import _METADATA_JSON_MAX_BYTES

    # Walacor metadata_json column is TEXT(65535) per
    # scripts/setup_walacor_schemas.py. Our cap stays comfortably below
    # that hard ceiling so JSON escape inflation can't push us over.
    assert _METADATA_JSON_MAX_BYTES < 65535
    assert _METADATA_JSON_MAX_BYTES >= 32_000, (
        "cap shouldn't be below 32KB or we drop legitimate audit content"
    )


# ──────────────────────────────────────────────────────────────────────────
# B3 — tool-event extras fold into content_analysis._extras
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_b3_tool_event_extras_survive(captured_submit):
    """event_type / tool_id / mcp_server_url / client_context fold into _extras.

    These keys exist in the orchestrator's tool-event payload but have no
    column in the gateway_tool_events schema. They must NOT be silently
    dropped — they're folded into content_analysis._extras instead.
    """
    client = _make_client()
    event = {
        "event_id": "evt-1",
        "execution_id": "exec-1",
        "session_id": "sess-1",
        "tenant_id": "tenant-a",
        "gateway_id": "gw-1",
        "prompt_id": "req-1",
        "timestamp": "2026-05-12T00:00:00Z",
        # Schema-supported tool fields
        "tool_name": "web_search",
        "tool_type": "web_search",
        "source": "gateway",
        "mcp_server_name": "ddg",
        "input_data": json.dumps({"q": "hello"}),
        "output_data": json.dumps([{"title": "x", "url": "y"}]),
        "sources": json.dumps([{"url": "y", "title": "x"}]),
        "duration_ms": 12.5,
        "iteration": 1,
        "is_error": False,
        # Extras with no dedicated column — must NOT be silently dropped
        "event_type": "tool_call",
        "tool_id": "tool-uuid-1",
        "mcp_server_url": "http://ddg:8080",
        "client_context": {"ip": "10.0.0.1"},
    }

    await client.write_tool_event(event)

    assert len(captured_submit) == 1
    body = captured_submit[0][1][0]

    # Extras land inside content_analysis._extras
    ca_raw = body.get("content_analysis")
    assert ca_raw is not None, "content_analysis must be populated when extras exist"
    ca = json.loads(ca_raw)
    extras = ca.get("_extras") or {}
    for key in ("event_type", "tool_id", "mcp_server_url", "client_context"):
        assert key in extras, f"tool-event extra {key!r} dropped silently"

    # Schema fields still land on the top-level columns
    assert body["tool_source"] == "gateway"
    assert body["mcp_server_name"] == "ddg"


@pytest.mark.anyio
async def test_b3_tool_event_blob_size_cap_and_marker(captured_submit):
    """Oversized input_data / output_data / sources are capped + marked."""
    client = _make_client()
    huge = "z" * 80_000  # > 60K cap
    event = {
        "event_id": "evt-2",
        "execution_id": "exec-1",
        "tenant_id": "tenant-a",
        "gateway_id": "gw-1",
        "timestamp": "2026-05-12T00:00:00Z",
        "tool_name": "tool",
        "tool_type": "function",
        "source": "gateway",
        "input_data": huge,
        "output_data": huge,
        "sources": "small",  # within cap; must NOT appear in truncated list
    }

    await client.write_tool_event(event)

    body = captured_submit[0][1][0]
    # Blobs got capped
    assert len(body["input_data"]) < 80_000
    assert len(body["output_data"]) < 80_000
    assert "truncated" in body["input_data"]
    # Marker present and lists only the actually-truncated keys
    ca = json.loads(body["content_analysis"])
    truncated = ca.get("tool_event_truncated_keys") or []
    assert "input_data" in truncated
    assert "output_data" in truncated
    assert "sources" not in truncated


@pytest.mark.anyio
async def test_b3_tool_event_without_extras_or_truncation(captured_submit):
    """A clean tool event must NOT acquire a spurious content_analysis."""
    client = _make_client()
    event = {
        "event_id": "evt-3",
        "execution_id": "exec-1",
        "tenant_id": "tenant-a",
        "gateway_id": "gw-1",
        "timestamp": "2026-05-12T00:00:00Z",
        "tool_name": "tool",
        "tool_type": "function",
        "source": "gateway",
        "input_data": "small",
        "output_data": "small",
    }

    await client.write_tool_event(event)

    body = captured_submit[0][1][0]
    # No content_analysis is created when nothing was packed and nothing
    # was truncated.
    assert "content_analysis" not in body


# ──────────────────────────────────────────────────────────────────────────
# B4 — gateway-internal classifier keys rehome under _internal
# ──────────────────────────────────────────────────────────────────────────


def test_b4_split_internal_keys_namespaces_underscore_prefixed():
    """Any key starting with ``_`` (and the explicit internal set) rehomes."""
    meta = {
        "session_id": "sess-1",
        "_intent": "chat",
        "_intent_confidence": 0.9,
        "_translated_from_openai": True,
        "schema_mapper_confidence": 0.88,
        "schema_mapper_mapped": 5,
        "analyzer_decisions": [],
    }
    out = _split_internal_keys(meta)

    # Public keys stay top-level
    assert out["session_id"] == "sess-1"
    assert out["analyzer_decisions"] == []
    # Internals are bucketed
    internal = out["_internal"]
    assert internal["_intent"] == "chat"
    assert internal["_intent_confidence"] == 0.9
    assert internal["_translated_from_openai"] is True
    assert internal["schema_mapper_confidence"] == 0.88
    assert internal["schema_mapper_mapped"] == 5
    # Internals are not duplicated at the top level
    for key in ("_intent", "_translated_from_openai", "schema_mapper_confidence"):
        assert key not in out


def test_b4_split_internal_keys_is_idempotent():
    """Running the rehoming twice must not double-nest ``_internal``."""
    meta = {"_intent": "chat", "x": 1}
    once = _split_internal_keys(meta)
    twice = _split_internal_keys(once)
    assert twice["_internal"]["_intent"] == "chat"
    # No nested _internal under _internal
    assert "_internal" not in twice["_internal"]


def test_b4_split_internal_keys_preserves_existing_internal_bucket():
    """If the caller already populated ``_internal``, we merge into it."""
    meta = {
        "_internal": {"prior_key": "prior_value"},
        "_intent": "chat",
        "x": 1,
    }
    out = _split_internal_keys(meta)
    assert out["_internal"]["prior_key"] == "prior_value"
    assert out["_internal"]["_intent"] == "chat"


@pytest.mark.anyio
async def test_b4_internal_keys_end_up_in_walacor_metadata_internal(captured_submit):
    """End-to-end: internals submitted to Walacor live under metadata_json._internal."""
    client = _make_client()
    record = _full_execution_record()

    await client.write_execution(record)

    body = captured_submit[0][1][0]
    meta = json.loads(body["metadata_json"])

    assert "_internal" in meta
    internal = meta["_internal"]
    for key in ("_intent", "_intent_confidence", "_intent_tier",
                "_intent_reason", "_translated_from_openai",
                "schema_mapper_confidence", "schema_mapper_mapped",
                "schema_mapper_unmapped"):
        assert key in internal, f"internal classifier key {key!r} not namespaced"

    # And NOT duplicated at the top level of metadata
    for key in ("_intent", "schema_mapper_confidence"):
        assert key not in meta


# ──────────────────────────────────────────────────────────────────────────
# Schema-allowlist coverage sanity check
# ──────────────────────────────────────────────────────────────────────────


def test_execution_schema_fields_match_setup_script():
    """``_EXECUTION_SCHEMA_FIELDS`` must mirror scripts/setup_walacor_schemas.py.

    If the schema script defines a column we don't list here, Walacor will
    receive ``None`` for that column on every write. If we list a column
    that isn't in the schema, Walacor silently rejects the record. Either
    drift is a fidelity bug. This test fails loud so the two stay in sync.
    """
    import importlib.util
    from pathlib import Path

    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "setup_walacor_schemas.py"
    )
    spec = importlib.util.spec_from_file_location("setup_walacor_schemas", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The script reads env vars at import time and will exit if they're
    # missing. Stub them out for the duration of the import.
    import os
    saved = {k: os.environ.get(k) for k in ("WALACOR_SERVER", "WALACOR_USERNAME", "WALACOR_PASSWORD")}
    os.environ.update({"WALACOR_SERVER": "https://x.example", "WALACOR_USERNAME": "u", "WALACOR_PASSWORD": "p"})
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    schema_fields = {f["FieldName"] for f in module.EXECUTIONS_FIELDS}
    client_fields = WalacorClient._EXECUTION_SCHEMA_FIELDS

    missing_from_client = schema_fields - client_fields
    extra_in_client = client_fields - schema_fields
    assert not missing_from_client, (
        f"setup_walacor_schemas.py defines columns the client doesn't write: "
        f"{sorted(missing_from_client)}. Those columns will always be NULL "
        f"on Walacor — either drop them from the schema or add to "
        f"_EXECUTION_SCHEMA_FIELDS."
    )
    assert not extra_in_client, (
        f"client writes columns the Walacor schema doesn't declare: "
        f"{sorted(extra_in_client)}. Walacor will silently reject these — "
        f"either add them to setup_walacor_schemas.py or fold them into "
        f"metadata_json."
    )


def test_tool_event_schema_fields_match_setup_script():
    """Same sync check for tool-event schema."""
    import importlib.util
    from pathlib import Path

    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "setup_walacor_schemas.py"
    )
    spec = importlib.util.spec_from_file_location("setup_walacor_schemas", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import os
    saved = {k: os.environ.get(k) for k in ("WALACOR_SERVER", "WALACOR_USERNAME", "WALACOR_PASSWORD")}
    os.environ.update({"WALACOR_SERVER": "https://x.example", "WALACOR_USERNAME": "u", "WALACOR_PASSWORD": "p"})
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    schema_fields = {f["FieldName"] for f in module.TOOL_EVENTS_FIELDS}
    client_fields = WalacorClient._TOOL_EVENT_SCHEMA_FIELDS

    missing_from_client = schema_fields - client_fields
    extra_in_client = client_fields - schema_fields
    assert not missing_from_client, (
        f"setup_walacor_schemas.py defines tool-event columns the client "
        f"doesn't write: {sorted(missing_from_client)}."
    )
    assert not extra_in_client, (
        f"client writes tool-event columns the Walacor schema doesn't "
        f"declare: {sorted(extra_in_client)}."
    )


# ──────────────────────────────────────────────────────────────────────────
# OpenWebUI bulky-field strip stays in place
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_openwebui_bulky_fields_stripped(captured_submit):
    """OpenWebUI metadata bloat (features, tool_ids, files, ...) must be
    stripped before metadata_json serialisation."""
    client = _make_client()
    record = _full_execution_record()
    record["metadata"].update({
        "features": {"web_search": True},
        "tool_ids": ["a", "b", "c"],
        "files": [{"name": "x.pdf"}],
        "variables": {"foo": "bar"},
        "params": {"temperature": 0.7},
        "knowledge": [{"id": "kb-1"}],
        "citations": [{"url": "x"}],
    })

    await client.write_execution(record)

    body = captured_submit[0][1][0]
    meta = json.loads(body["metadata_json"])
    for stripped in ("features", "tool_ids", "files", "variables",
                     "params", "knowledge", "citations"):
        assert stripped not in meta, f"OpenWebUI bulk field {stripped!r} leaked"


# ──────────────────────────────────────────────────────────────────────────
# B5 — pipeline timings round-trip + self-revealing unexpected-drop guard
#
# This is the regression guard for the bug class where ``timings`` was added
# to the execution record for the dashboard Pipeline Trace but never added to
# the Walacor schema allowlist: it stayed visible locally (LineageReader reads
# the SQLite WAL, which has it) and vanished only on the Walacor read path —
# a prod-only, test-invisible regression for months. These tests pin the
# contract at the trust boundary so it cannot silently recur.
# ──────────────────────────────────────────────────────────────────────────


_SAMPLE_TIMINGS = {
    "attestation_ms": 0.4, "policy_ms": 5.8, "budget_ms": 0.1,
    "forward_ms": 812.3, "content_analysis_ms": 41.2,
    "chain_ms": 2.1, "write_ms": 9.7, "total_ms": 871.6,
}


@pytest.mark.anyio
async def test_b5_timings_rehomed_into_metadata_json(captured_submit):
    """``timings`` is not a Walacor column — it must survive via metadata_json."""
    client = _make_client()
    record = _full_execution_record(timings=dict(_SAMPLE_TIMINGS))

    await client.write_execution(record)

    body = captured_submit[0][1][0]
    assert "timings" not in body, "timings is not a schema column — must not be top-level"
    meta = json.loads(body["metadata_json"])
    assert meta.get("timings") == _SAMPLE_TIMINGS, (
        "timings lost on the Walacor write path — the Pipeline Trace "
        "waterfall would render empty in production"
    )


@pytest.mark.anyio
async def test_b5_timings_recoverable_via_walacor_reader(captured_submit, monkeypatch):
    """End-to-end: serialized record → WalacorLineageReader.get_execution_trace
    must return non-empty timings. This is the exact assertion that would have
    caught the original Pipeline Trace bug."""
    from gateway.lineage.walacor_reader import WalacorLineageReader

    client = _make_client()
    await client.write_execution(_full_execution_record(timings=dict(_SAMPLE_TIMINGS)))
    walacor_body = captured_submit[0][1][0]

    reader = WalacorLineageReader(client=client)

    async def _fake_get_execution(self, execution_id):  # noqa: ANN001
        return dict(walacor_body)

    async def _fake_get_tool_events(self, execution_id):  # noqa: ANN001
        return []

    monkeypatch.setattr(WalacorLineageReader, "get_execution", _fake_get_execution)
    monkeypatch.setattr(WalacorLineageReader, "get_tool_events", _fake_get_tool_events)

    trace = await reader.get_execution_trace("exec-1")
    assert trace is not None
    assert trace["timings"] == _SAMPLE_TIMINGS, (
        "WalacorLineageReader could not recover timings from metadata_json — "
        "Pipeline Trace would be invisible in production"
    )


@pytest.mark.anyio
async def test_b5_unexpected_non_schema_field_is_flagged(captured_submit, caplog):
    """A field added to the execution record but forgotten in the allowlist
    must NOT vanish silently: it is logged and audit-marked in
    ``schema_stripped_keys`` inside metadata_json."""
    import logging

    client = _make_client()
    record = _full_execution_record(brand_new_dashboard_metric={"p99_ms": 42})

    with caplog.at_level(logging.WARNING):
        await client.write_execution(record)

    assert any("UNEXPECTED non-None fields" in r.message for r in caplog.records), (
        "an un-allowlisted field was dropped without a warning — the silent-"
        "drop guard is not working"
    )
    body = captured_submit[0][1][0]
    assert "brand_new_dashboard_metric" not in body
    meta = json.loads(body["metadata_json"])
    assert meta.get("schema_stripped_keys") == ["brand_new_dashboard_metric"], (
        "forensic evidence of the dropped field must be embedded in the "
        "record so the loss is discoverable by query, not just by log scrape"
    )


@pytest.mark.anyio
async def test_b5_intentional_drops_are_not_flagged(captured_submit, caplog):
    """prompt_hash / response_hash are deliberately not sent (Walacor hashes
    on ingest). They must not trip the unexpected-drop guard."""
    import logging

    client = _make_client()
    record = _full_execution_record(
        prompt_hash="sha3:deadbeef", response_hash="sha3:cafebabe",
    )

    with caplog.at_level(logging.WARNING):
        await client.write_execution(record)

    assert not any("UNEXPECTED non-None fields" in r.message for r in caplog.records), (
        "intentional non-schema drops (prompt_hash/response_hash) must not "
        "raise false alarms"
    )
    body = captured_submit[0][1][0]
    meta = json.loads(body["metadata_json"])
    assert "schema_stripped_keys" not in meta
