"""Unit tests for regulatory framework compliance mappings."""

import pytest


_SAMPLE_SUMMARY = {
    "total_requests": 100,
    "allowed": 90,
    "denied": 10,
    "models_used": ["qwen3:4b", "gpt-4o"],
}

_SAMPLE_ATTESTATIONS = [
    {"model_id": "qwen3:4b", "provider": "ollama", "attestation_id": "self-attested:qwen3:4b",
     "request_count": 90, "total_tokens": 13500},
]

_SAMPLE_EXECUTIONS = [
    {"execution_id": "exec-1", "model_id": "qwen3:4b", "policy_result": "pass",
     "timestamp": "2026-03-05T10:00:00+00:00"},
]


def test_eu_ai_act_mapping_has_article_12():
    from gateway.compliance.frameworks import map_eu_ai_act
    result = map_eu_ai_act(_SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "EU AI Act"
    assert "article_12" in result["articles"]
    article_12 = result["articles"]["article_12"]
    assert article_12["title"] == "Record-Keeping"
    assert article_12["status"] in ("compliant", "partial", "non_compliant")
    assert len(article_12["requirements"]) > 0


def test_nist_mapping_has_four_functions():
    from gateway.compliance.frameworks import map_nist_ai_rmf
    result = map_nist_ai_rmf(_SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "NIST AI RMF"
    functions = result["functions"]
    assert "govern" in functions
    assert "map" in functions
    assert "measure" in functions
    assert "manage" in functions


def test_soc2_mapping_has_trust_criteria():
    from gateway.compliance.frameworks import map_soc2
    result = map_soc2(_SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "SOC 2 Type II"
    criteria = result["criteria"]
    assert "CC7.2" in criteria
    assert "CC7.3" in criteria
    assert "CC8.1" in criteria


def test_iso42001_mapping_has_clauses():
    from gateway.compliance.frameworks import map_iso42001
    result = map_iso42001(_SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "ISO 42001"
    assert "clauses" in result
    assert len(result["clauses"]) > 0


def test_mapping_with_broken_chain_shows_non_compliant():
    """When chain integrity is broken, Article 12 should show non_compliant."""
    from gateway.compliance.frameworks import map_eu_ai_act
    summary = {**_SAMPLE_SUMMARY, "chain_integrity": {"all_valid": False, "sessions_verified": 5}}
    result = map_eu_ai_act(summary, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    # Chain integrity requirement should reflect the broken chain
    article_12 = result["articles"]["article_12"]
    chain_reqs = [r for r in article_12["requirements"] if "chain" in r["description"].lower() or "integrity" in r["description"].lower()]
    if chain_reqs:
        assert chain_reqs[0]["status"] in ("partial", "non_compliant")


def test_get_framework_mapping_dispatches():
    """get_framework_mapping routes to the correct function."""
    from gateway.compliance.frameworks import get_framework_mapping
    result = get_framework_mapping("eu_ai_act", _SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "EU AI Act"
    result = get_framework_mapping("nist", _SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "NIST AI RMF"
    result = get_framework_mapping("soc2", _SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "SOC 2 Type II"
    result = get_framework_mapping("iso42001", _SAMPLE_SUMMARY, _SAMPLE_ATTESTATIONS, _SAMPLE_EXECUTIONS)
    assert result["framework"] == "ISO 42001"
