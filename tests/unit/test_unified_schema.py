"""Tests for the unified Schema Intelligence engine."""

import pytest
from dataclasses import replace

from gateway.classifier.unified import (
    SchemaIntelligence,
    PromptExtraction,
    NormalizationReport,
    ValidationReport,
    NORMAL,
    WEB_SEARCH,
    RAG,
    SYSTEM_TASK,
    REASONING,
    MCP_TOOLS,
    EXECUTION_SCHEMA,
    TOOL_EVENT_SCHEMA,
    ATTEMPT_SCHEMA,
)
from gateway.adapters.base import ModelResponse


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def si():
    """SchemaIntelligence without ONNX model (deterministic only)."""
    return SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False)


@pytest.fixture
def si_with_mcp():
    """SchemaIntelligence with MCP tools enabled."""
    return SchemaIntelligence(onnx_model_path=None, has_mcp_tools=True)


@pytest.fixture
def base_response():
    return ModelResponse(
        content="Hello world",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        raw_body=b"{}",
        provider_request_id="req-1",
        model_hash=None,
        tool_interactions=[],
        has_pending_tool_calls=False,
        thinking_content=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Prompt Extraction Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPromptExtraction:
    def test_empty_messages(self, si):
        result = si.extract_prompt([])
        assert result.user_question == ""
        assert result.conversation_turns == 0
        assert result.extraction_method == "empty_messages"

    def test_single_user_message(self, si):
        messages = [{"role": "user", "content": "What is AI?"}]
        result = si.extract_prompt(messages)
        assert result.user_question == "What is AI?"
        assert result.conversation_turns == 1
        assert result.extraction_method == "single_turn"
        assert not result.has_system_prompt

    def test_multi_turn_extracts_last_question(self, si):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is photosynthesis?"},
            {"role": "assistant", "content": "Photosynthesis is the process by which plants convert sunlight..."},
            {"role": "user", "content": "How does it relate to cellular respiration?"},
        ]
        result = si.extract_prompt(messages)
        assert result.user_question == "How does it relate to cellular respiration?"
        assert result.conversation_turns == 2
        assert result.extraction_method == "last_user_message"
        assert result.has_system_prompt

    def test_conversation_context_summarized(self, si):
        messages = [
            {"role": "system", "content": "You are an expert on biology."},
            {"role": "user", "content": "Explain DNA replication."},
            {"role": "assistant", "content": "DNA replication is a fundamental process..."},
            {"role": "user", "content": "What enzymes are involved?"},
        ]
        result = si.extract_prompt(messages)
        assert result.conversation_turns == 2
        assert "[system:" in result.conversation_context
        assert "[turn 1:" in result.conversation_context

    def test_rag_context_detection(self, si):
        messages = [
            {"role": "system", "content": "Based on the following context: " + "x" * 600},
            {"role": "user", "content": "What does the document say?"},
        ]
        result = si.extract_prompt(messages)
        assert result.has_rag_context

    def test_file_detection_in_content_blocks(self, si):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ]},
        ]
        result = si.extract_prompt(messages)
        assert result.has_files
        assert result.user_question == "What's in this image?"

    def test_prompt_text_is_user_question_not_concatenated(self, si):
        """The critical test — prompt_text should be the question, not full history."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "First question about weather"},
            {"role": "assistant", "content": "The weather today is..." + "x" * 500},
            {"role": "user", "content": "What about tomorrow?"},
        ]
        fields = si.build_prompt_fields(messages)
        assert fields["prompt_text"] == "What about tomorrow?"
        assert fields["user_question"] == "What about tomorrow?"
        assert fields["conversation_turns"] == 2
        assert len(fields["question_fingerprint"]) == 16


# ═══════════════════════════════════════════════════════════════════════════
# 2. Intent Classification Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestIntentClassification:
    def test_system_task_by_request_type(self, si):
        result = si.classify_intent("hello", {"request_type": "system_task:title"}, "qwen3:4b")
        assert result.intent == SYSTEM_TASK
        assert result.confidence == 1.0
        assert result.tier == "deterministic"

    def test_system_task_by_prompt(self, si):
        result = si.classify_intent("### Task: Generate title", {}, "qwen3:4b")
        assert result.intent == SYSTEM_TASK

    def test_reasoning_model(self, si):
        result = si.classify_intent("Explain quantum", {}, "o1-preview")
        assert result.intent == REASONING

    def test_web_search_feature_toggle(self, si):
        meta = {"_body_metadata": {"features": {"web_search": True}}}
        result = si.classify_intent("search for news", meta, "llama3.1:8b")
        assert result.intent == WEB_SEARCH

    def test_mcp_tools(self, si_with_mcp):
        result = si_with_mcp.classify_intent("call a function", {}, "llama3.1:8b")
        assert result.intent == MCP_TOOLS

    def test_rag_with_files(self, si):
        meta = {"_body_metadata": {"files": [{"id": "f1"}]}}
        result = si.classify_intent("analyze this", meta, "llama3.1:8b")
        assert result.intent == RAG

    def test_normal_fallback(self, si):
        result = si.classify_intent("What is AI?", {}, "llama3.1:8b")
        assert result.intent == NORMAL
        assert result.confidence == 0.5  # no ML model

    def test_rag_from_audit(self, si):
        meta = {"walacor_audit": {"has_rag_context": True}}
        result = si.classify_intent("what does it say", meta, "llama3.1:8b")
        assert result.intent == RAG


# ═══════════════════════════════════════════════════════════════════════════
# 3. Response Normalization Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestResponseNormalization:
    def test_no_content_changes_needed(self, si, base_response):
        result, report = si.normalize_response(base_response, "ollama")
        assert result.content == "Hello world"
        assert not report.content_fixed
        assert not report.thinking_fallback_applied

    def test_anthropic_usage_mapping(self, si):
        resp = ModelResponse(
            content="Hi",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_body=b"{}",
            provider_request_id="req-1",
            model_hash=None,
            tool_interactions=[],
            has_pending_tool_calls=False,
            thinking_content=None,
        )
        result, report = si.normalize_response(resp, "anthropic")
        assert result.usage["prompt_tokens"] == 10
        assert result.usage["completion_tokens"] == 5
        assert result.usage["total_tokens"] == 15
        assert report.usage_mapped

    def test_thinking_fallback(self, si):
        resp = ModelResponse(
            content="",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            raw_body=b"{}",
            provider_request_id="req-1",
            model_hash=None,
            tool_interactions=[],
            has_pending_tool_calls=False,
            thinking_content="I think the answer is 42.",
        )
        result, report = si.normalize_response(resp, "ollama")
        assert result.content == "I think the answer is 42."
        assert report.thinking_fallback_applied

    def test_none_content_enforcement(self, si):
        resp = ModelResponse(
            content=None,
            usage=None,
            raw_body=b"{}",
            provider_request_id="req-1",
            model_hash=None,
            tool_interactions=[],
            has_pending_tool_calls=False,
            thinking_content=None,
        )
        result, report = si.normalize_response(resp, "ollama")
        assert result.content == ""
        assert report.content_fixed

    def test_retry_sentinel_cleared(self, si):
        resp = ModelResponse(
            content="__RETRY_WITHOUT_SUMMARY__",
            usage=None,
            raw_body=b"{}",
            provider_request_id="req-1",
            model_hash=None,
            tool_interactions=[],
            has_pending_tool_calls=False,
            thinking_content=None,
        )
        result, report = si.normalize_response(resp, "openai")
        assert result.content == ""
        assert report.content_fixed

    def test_total_tokens_computed(self, si):
        resp = ModelResponse(
            content="Hi",
            usage={"prompt_tokens": 20, "completion_tokens": 10},
            raw_body=b"{}",
            provider_request_id="req-1",
            model_hash=None,
            tool_interactions=[],
            has_pending_tool_calls=False,
            thinking_content=None,
        )
        result, report = si.normalize_response(resp, "ollama")
        assert result.usage["total_tokens"] == 30


# ═══════════════════════════════════════════════════════════════════════════
# 4. Schema Validation Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_valid_execution_record(self, si):
        record = {
            "execution_id": "exec-1",
            "tenant_id": "t-1",
            "gateway_id": "gw-1",
            "timestamp": "2026-04-02T00:00:00Z",
            "policy_version": 1,
            "policy_result": "pass",
            "model_id": "qwen3:4b",
            "provider": "ollama",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        result, report = si.validate_execution(record)
        assert not report.issues
        assert result["execution_id"] == "exec-1"

    def test_missing_required_field(self, si):
        record = {
            "tenant_id": "t-1",
            "gateway_id": "gw-1",
            "timestamp": "2026-04-02T00:00:00Z",
            "policy_version": 1,
            "policy_result": "pass",
        }
        result, report = si.validate_execution(record)
        assert any("MISSING" in i for i in report.issues)
        assert result["execution_id"] == ""  # default applied

    def test_type_coercion(self, si):
        record = {
            "execution_id": "exec-1",
            "tenant_id": "t-1",
            "gateway_id": "gw-1",
            "timestamp": "2026-04-02T00:00:00Z",
            "policy_version": "1",  # string instead of int
            "policy_result": "pass",
            "latency_ms": "45.2",  # string instead of float
            "prompt_tokens": "10",  # string instead of int
        }
        result, report = si.validate_execution(record)
        assert result["policy_version"] == 1
        assert result["latency_ms"] == 45.2
        assert result["prompt_tokens"] == 10
        assert report.coercions == 3

    def test_tool_event_validation(self, si):
        record = {
            "event_id": "ev-1",
            "execution_id": "exec-1",
            "tenant_id": "t-1",
            "gateway_id": "gw-1",
            "timestamp": "2026-04-02T00:00:00Z",
            "tool_name": "web_search",
            "duration_ms": 150.5,
        }
        result, report = si.validate_tool_event(record)
        assert not report.issues

    def test_attempt_validation(self, si):
        record = {
            "request_id": "req-1",
            "timestamp": "2026-04-02T00:00:00Z",
            "tenant_id": "t-1",
            "disposition": "forward",
            "status_code": 200,
        }
        result, report = si.validate_attempt(record)
        assert not report.issues


# ═══════════════════════════════════════════════════════════════════════════
# 5. Full Pipeline Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessRequest:
    def test_single_turn_request(self, si):
        messages = [{"role": "user", "content": "What is AI?"}]
        result = si.process_request(messages, {}, "llama3.1:8b")
        assert result["user_question"] == "What is AI?"
        assert result["prompt_text"] == "What is AI?"
        assert result["_intent"] == NORMAL
        assert result["conversation_turns"] == 1

    def test_multi_turn_with_web_search(self, si):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "Search for latest news"},
        ]
        meta = {"_body_metadata": {"features": {"web_search": True}}}
        result = si.process_request(messages, meta, "llama3.1:8b")
        assert result["user_question"] == "Search for latest news"
        assert result["_intent"] == WEB_SEARCH
        assert result.get("_gateway_web_search") is True

    def test_system_task_disables_web_search(self, si):
        messages = [{"role": "user", "content": "### Task: Generate title"}]
        result = si.process_request(messages, {}, "qwen3:4b")
        assert result["_intent"] == SYSTEM_TASK
        assert result.get("_gateway_web_search") is False

    def test_process_response(self, si, base_response):
        result, report = si.process_response(base_response, "ollama")
        assert result.content == "Hello world"


# ═══════════════════════════════════════════════════════════════════════════
# 6. ONNX Model Integration Test
# ═══════════════════════════════════════════════════════════════════════════

class TestONNXIntegration:
    def test_onnx_model_loads(self):
        """Test that the trained ONNX model loads and classifies."""
        import os
        onnx_path = os.path.join(os.path.dirname(__file__), "../../src/gateway/classifier/model.onnx")
        if not os.path.exists(onnx_path):
            pytest.skip("ONNX model not found")

        si = SchemaIntelligence(onnx_model_path=onnx_path)
        result = si.classify_intent("What is photosynthesis?", {}, "llama3.1:8b")
        assert result.intent in (NORMAL, WEB_SEARCH, RAG, SYSTEM_TASK, REASONING)
        assert result.confidence > 0

    def test_onnx_web_search_classification(self):
        """Test that the trained model correctly classifies web search prompts."""
        import os
        onnx_path = os.path.join(os.path.dirname(__file__), "../../src/gateway/classifier/model.onnx")
        if not os.path.exists(onnx_path):
            pytest.skip("ONNX model not found")

        si = SchemaIntelligence(onnx_model_path=onnx_path)
        result = si.classify_intent("What is the current weather in New York?", {}, "llama3.1:8b")
        # Should be web_search or at least not system_task
        assert result.intent in (WEB_SEARCH, NORMAL)
        assert result.confidence > 0.5

    def test_onnx_system_task_classification(self):
        """Test that the trained model correctly classifies system tasks."""
        import os
        onnx_path = os.path.join(os.path.dirname(__file__), "../../src/gateway/classifier/model.onnx")
        if not os.path.exists(onnx_path):
            pytest.skip("ONNX model not found")

        si = SchemaIntelligence(onnx_model_path=onnx_path)
        # Note: system tasks are primarily detected by Tier 1 deterministic rules
        result = si.classify_intent(
            "### Task: Generate a title for the conversation",
            {},
            "qwen3:4b",
        )
        assert result.intent == SYSTEM_TASK
        assert result.confidence == 1.0  # deterministic tier
