"""Contract tests for audit_classifier, audit_intelligence, and compliance/api shapes."""
from __future__ import annotations
import pytest
from gateway.middleware.audit_classifier import classify_request
from gateway.compliance.audit_intelligence import assess_audit_readiness


# ── audit_classifier contracts ────────────────────────────────────────────────

def test_classify_request_returns_required_keys() -> None:
    result = classify_request({"messages": [{"role": "user", "content": "Hello"}]})
    required = {"user_question", "conversation_turns", "total_messages",
                "has_rag_context", "has_files", "classified_by"}
    assert required.issubset(result.keys())


def test_classify_request_empty_messages_returns_minimal() -> None:
    result = classify_request({"messages": []})
    assert result["total_messages"] == 0
    assert result["classified_by"] == "gateway_fallback"


def test_classify_request_extracts_last_user_message() -> None:
    result = classify_request({"messages": [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow-up"},
    ]})
    assert result["user_question"] == "follow-up"
    assert result["conversation_turns"] == 1  # exclude current


def test_classify_request_detects_rag_context() -> None:
    rag_body = {"messages": [
        {"role": "user", "content": "Use the following context to answer: Some document text\n\nQuestion: What is this?"}
    ]}
    result = classify_request(rag_body)
    assert result["has_rag_context"] is True


def test_classify_request_passthrough_if_openwebui_classified() -> None:
    existing = {"classified_by": "openwebui_plugin", "user_question": "already done"}
    body = {"messages": [], "metadata": {"walacor_audit": existing}}
    result = classify_request(body)
    assert result is existing


def test_classify_request_detects_images_in_content_list() -> None:
    result = classify_request({"messages": [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "What is in this image?"},
        ]}
    ]})
    assert result.get("has_images") is True


def test_classify_request_multipart_text_content_extracted() -> None:
    result = classify_request({"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]}
    ]})
    assert "part one" in result["user_question"]
    assert "part two" in result["user_question"]


# ── audit_intelligence contracts ──────────────────────────────────────────────

def _minimal_summary(total: int = 100) -> dict:
    return {"total_requests": total, "allowed": total, "denied": 0,
            "models_used": ["qwen3:4b"], "content_analysis_coverage": 0.8}


def test_assess_audit_readiness_returns_required_structure() -> None:
    result = assess_audit_readiness(
        summary=_minimal_summary(),
        attestations=[{"model_id": "qwen3:4b", "status": "active"}],
        executions=[],
        chain_report=[{"session_id": "s1", "valid": True}],
    )
    required = {"score", "grade", "dimensions", "gaps", "strengths", "recommendations"}
    assert required.issubset(result.keys())


def test_assess_audit_readiness_score_in_range() -> None:
    result = assess_audit_readiness(
        summary=_minimal_summary(1000),
        attestations=[{"model_id": "qwen3:4b", "status": "active"}],
        executions=[],
        chain_report=[{"session_id": "s1", "valid": True}],
    )
    assert 0 <= result["score"] <= 100


def test_assess_audit_readiness_grade_is_letter() -> None:
    result = assess_audit_readiness(
        summary=_minimal_summary(),
        attestations=[], executions=[], chain_report=[],
    )
    assert result["grade"] in ("A", "B", "C", "D", "F")


def test_assess_audit_readiness_zero_requests_gives_critical_gap() -> None:
    result = assess_audit_readiness(
        summary={"total_requests": 0, "allowed": 0, "denied": 0,
                 "models_used": [], "content_analysis_coverage": 0},
        attestations=[], executions=[], chain_report=[],
    )
    severities = [g["severity"] for g in result["gaps"]]
    assert "critical" in severities


def test_assess_audit_readiness_each_dimension_has_score_and_weight() -> None:
    result = assess_audit_readiness(
        summary=_minimal_summary(), attestations=[], executions=[], chain_report=[],
    )
    for dim in result["dimensions"]:
        assert 0 <= dim["score"] <= 100
        assert dim["weight"] > 0
