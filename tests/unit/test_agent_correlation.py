"""Tier 0 parser tests — header + body extraction of agent-tracing IDs."""

from __future__ import annotations

from gateway.util.agent_correlation import (
    AgentCorrelation,
    extract_correlation,
    parse_traceparent,
)


# ── traceparent parsing ───────────────────────────────────────────────────────


def test_traceparent_valid():
    trace_id, parent_id = parse_traceparent(
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    )
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert parent_id == "b7ad6b7169203331"


def test_traceparent_uppercase_normalised():
    trace_id, parent_id = parse_traceparent(
        "00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01"
    )
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert parent_id == "b7ad6b7169203331"


def test_traceparent_unsupported_version():
    # version 01 isn't ratified; Tier 0 only accepts 00 to avoid misreading
    # vendor extensions.
    assert parse_traceparent(
        "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    ) == (None, None)


def test_traceparent_garbage_fails_open():
    assert parse_traceparent("not-a-traceparent") == (None, None)
    assert parse_traceparent("") == (None, None)
    assert parse_traceparent(None) == (None, None)


# ── full extraction ───────────────────────────────────────────────────────────


def test_extract_empty_returns_all_none():
    c = extract_correlation({}, {})
    assert c == AgentCorrelation()
    assert c.is_empty


def test_header_only():
    headers = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    c = extract_correlation(headers, None)
    assert c.trace_id == "a" * 32
    assert c.parent_span_id == "b" * 16
    assert c.agent_run_id is None


def test_body_metadata_only():
    body = {
        "metadata": {
            "trace_id": "trace-from-body",
            "agent_run_id": "run-42",
            "agent_name": "code-reviewer",
            "parent_observation_id": "obs-7",
            "parent_record_id": "rec-prev",
        }
    }
    c = extract_correlation({}, body)
    assert c.trace_id == "trace-from-body"
    assert c.agent_run_id == "run-42"
    assert c.agent_name == "code-reviewer"
    assert c.parent_observation_id == "obs-7"
    assert c.parent_record_id == "rec-prev"


def test_header_wins_over_body_for_trace_id():
    headers = {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    body = {"metadata": {"trace_id": "should-be-ignored"}}
    c = extract_correlation(headers, body)
    assert c.trace_id == "a" * 32


def test_openai_responses_api_fields():
    body = {
        "previous_response_id": "resp_abc",
        "conversation_id": "conv_xyz",
    }
    c = extract_correlation({}, body)
    assert c.previous_response_id == "resp_abc"
    assert c.conversation_id == "conv_xyz"


def test_header_lookup_is_case_insensitive():
    headers = {"TraceParent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    c = extract_correlation(headers, None)
    assert c.trace_id == "a" * 32


def test_oversized_field_clipped():
    body = {"metadata": {"agent_name": "x" * 5000}}
    c = extract_correlation({}, body)
    assert c.agent_name is not None
    assert len(c.agent_name) == 256


def test_non_string_metadata_ignored():
    body = {"metadata": "not-a-dict"}
    c = extract_correlation({}, body)
    assert c.is_empty


def test_blank_strings_become_none():
    body = {"metadata": {"agent_name": "   ", "trace_id": ""}}
    c = extract_correlation({}, body)
    assert c.agent_name is None
    assert c.trace_id is None
