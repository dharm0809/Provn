"""Tests for SchemaMapper, AnomalyDetector, FieldRegistry, and IntelligenceWorker."""

import pytest
from src.gateway.schema.mapper import SchemaMapper
from src.gateway.schema.anomaly import AnomalyDetector, AnomalyReport
from src.gateway.schema.overflow import FieldRegistry, build_overflow_envelope
from src.gateway.schema.features import flatten_json, extract_features, FEATURE_DIM
from src.gateway.schema.canonical import CanonicalResponse, CANONICAL_LABELS


# ═══════════════════════════════════════════════════════════════════════════
# SchemaMapper Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaMapper:
    @pytest.fixture
    def mapper(self):
        return SchemaMapper()

    def test_openai_format(self, mapper):
        resp = {
            "id": "chatcmpl-abc", "model": "gpt-4o",
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        r = mapper.map_response(resp)
        assert r.content == "Hello!"
        assert r.usage.prompt_tokens == 5
        assert r.usage.completion_tokens == 3
        assert r.usage.total_tokens == 8
        assert r.finish_reason == "stop"
        assert r.model == "gpt-4o"

    def test_anthropic_format(self, mapper):
        resp = {
            "id": "msg_abc", "model": "claude-3",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Response."}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        r = mapper.map_response(resp)
        assert r.content == "Response."
        assert r.usage.prompt_tokens == 10
        assert r.usage.completion_tokens == 20
        assert r.finish_reason == "stop"

    def test_ollama_native_format(self, mapper):
        resp = {
            "model": "qwen3:4b",
            "message": {"content": "Hi."},
            "done_reason": "stop",
            "prompt_eval_count": 8,
            "eval_count": 6,
        }
        r = mapper.map_response(resp)
        assert r.content == "Hi."
        assert r.usage.prompt_tokens == 8
        assert r.usage.completion_tokens == 6

    def test_gemini_format(self, mapper):
        resp = {
            "candidates": [{"content": {"parts": [{"text": "Gemini says hi."}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 12, "totalTokenCount": 19},
        }
        r = mapper.map_response(resp)
        assert r.content == "Gemini says hi."
        assert r.usage.prompt_tokens == 7
        assert r.usage.total_tokens == 19

    def test_deepseek_thinking(self, mapper):
        resp = {
            "id": "d1", "model": "deepseek-r1",
            "choices": [{"message": {"content": "42", "reasoning_content": "Let me think..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
        }
        r = mapper.map_response(resp)
        assert r.content == "42"
        assert r.thinking_content == "Let me think..."

    def test_huggingface_format(self, mapper):
        resp = {
            "generated_text": "HF output.",
            "details": {"finish_reason": "eos_token", "generated_tokens": 18},
        }
        r = mapper.map_response(resp)
        assert r.content == "HF output."
        assert r.usage.completion_tokens == 18

    def test_titan_format(self, mapper):
        resp = {
            "inputTextTokenCount": 12,
            "results": [{"outputText": "Titan.", "tokenCount": 20, "completionReason": "FINISHED"}],
        }
        r = mapper.map_response(resp)
        assert r.content == "Titan."
        assert r.usage.prompt_tokens == 12

    def test_short_content(self, mapper):
        resp = {
            "choices": [{"message": {"content": "No."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        r = mapper.map_response(resp)
        assert r.content == "No."

    def test_empty_response(self, mapper):
        r = mapper.map_response({})
        assert r._mapping_incomplete

    def test_invalid_input(self, mapper):
        r = mapper.map_response(None)
        assert r._mapping_incomplete

    def test_unknown_format_content_by_value(self, mapper):
        """Content found by value semantics (longest NL string)."""
        resp = {
            "resultado": "The weather today is sunny and warm with clear skies expected through the afternoon.",
            "estado": "ok",
            "modelo": "test-v1",
        }
        r = mapper.map_response(resp)
        assert "weather" in r.content

    def test_overflow_captured(self, mapper):
        resp = {
            "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "x_custom_field": "custom_value",
            "system_fingerprint": "fp_abc",
        }
        r = mapper.map_response(resp)
        assert r.content == "Hi"
        # Unmapped fields go to overflow or are classified


# ═══════════════════════════════════════════════════════════════════════════
# Feature Extraction Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFeatureExtraction:
    def test_feature_dim_consistent(self):
        assert FEATURE_DIM > 100  # Should be ~139

    def test_flatten_openai(self):
        resp = {"choices": [{"message": {"content": "test"}}], "usage": {"prompt_tokens": 5}}
        fields = flatten_json(resp)
        assert len(fields) > 3
        paths = [f.path for f in fields]
        assert "choices.0.message.content" in paths
        assert "usage.prompt_tokens" in paths

    def test_int_siblings_detected(self):
        resp = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        fields = flatten_json(resp)
        total_field = [f for f in fields if f.key == "total_tokens"][0]
        assert 10 in total_field.int_siblings
        assert 20 in total_field.int_siblings
        assert 30 in total_field.int_siblings

    def test_features_no_nan(self):
        resp = {"content": "test", "count": 5, "items": [1, 2]}
        fields = flatten_json(resp)
        for f in fields:
            vec = extract_features(f)
            assert len(vec) == FEATURE_DIM
            assert all(v == v for v in vec), "NaN in features"  # NaN != NaN


# ═══════════════════════════════════════════════════════════════════════════
# Anomaly Detector Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAnomalyDetector:
    def test_empty_response_flagged(self):
        detector = AnomalyDetector()
        record = {"execution_id": "e1", "response_content": "", "prompt_text": "hello"}
        report = detector.detect(record)
        assert "empty_response" in report.anomalies

    def test_token_sum_mismatch_flagged(self):
        detector = AnomalyDetector()
        record = {
            "execution_id": "e1", "response_content": "ok",
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 100,
        }
        report = detector.detect(record)
        assert "token_sum_mismatch" in report.anomalies

    def test_correct_record_no_anomalies(self):
        detector = AnomalyDetector()
        record = {
            "execution_id": "e1", "session_id": "s1", "model_id": "gpt-4o",
            "response_content": "Hello!", "prompt_text": "hi",
            "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
            "latency_ms": 500,
        }
        report = detector.detect(record)
        assert not report.has_anomalies

    def test_extreme_latency_flagged(self):
        detector = AnomalyDetector()
        record = {
            "execution_id": "e1", "response_content": "ok",
            "latency_ms": 500000,
        }
        report = detector.detect(record)
        assert "latency_extreme" in report.anomalies

    def test_missing_execution_id(self):
        detector = AnomalyDetector()
        record = {"response_content": "ok"}
        report = detector.detect(record)
        assert "missing_execution_id" in report.anomalies

    def test_ema_needs_warmup(self):
        detector = AnomalyDetector()
        # First 9 records should not trigger EMA anomalies
        for i in range(9):
            record = {
                "execution_id": f"e{i}", "model_id": "m1",
                "response_content": "ok", "latency_ms": 500,
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }
            report = detector.detect(record)
            assert not any("sigma" in a for a in report.anomalies)


# ═══════════════════════════════════════════════════════════════════════════
# Field Registry Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFieldRegistry:
    def test_record_and_count(self):
        reg = FieldRegistry()
        reg.record("reasoning_tokens", 100, "int", "openai")
        reg.record("reasoning_tokens", 150, "int", "openai")
        stats = reg.to_dict()
        assert len(stats) == 1
        assert stats[0]["count"] == 2

    def test_noise_filtered(self):
        reg = FieldRegistry()
        reg.record("object", "chat.completion", "string", "openai")
        reg.record("type", "message", "string", "anthropic")
        assert len(reg.to_dict()) == 0  # Noise keys filtered

    def test_promotion_after_threshold(self):
        reg = FieldRegistry()
        for i in range(15):
            reg.record("reasoning_tokens", i, "int", "openai")
        candidates = reg.get_promotion_candidates()
        assert "reasoning_tokens" in candidates

    def test_meaningful_detection(self):
        reg = FieldRegistry()
        assert reg.is_meaningful("reasoning_tokens")
        assert reg.is_meaningful("cache_creation_input_tokens")
        assert not reg.is_meaningful("x_groq_id")

    def test_overflow_envelope(self):
        overflow = {"reasoning_tokens": 142, "system_fingerprint": "fp_abc"}
        env = build_overflow_envelope(overflow, "openai")
        assert "_schema_version" in env
        assert "_overflow_fields" in env
        assert "reasoning_tokens" in env["_overflow_fields"]
        assert env["_overflow_fields"]["reasoning_tokens"]["type"] == "int"


# ═══════════════════════════════════════════════════════════════════════════
# Canonical Schema Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCanonicalSchema:
    def test_usage_compute_total(self):
        from src.gateway.schema.canonical import CanonicalUsage
        u = CanonicalUsage(prompt_tokens=10, completion_tokens=20)
        u.compute_total()
        assert u.total_tokens == 30

    def test_response_completeness(self):
        r = CanonicalResponse(content="Hello")
        assert r.is_complete()

    def test_incomplete_response(self):
        r = CanonicalResponse(_mapping_incomplete=True)
        assert not r.is_complete()

    def test_labels_defined(self):
        assert len(CANONICAL_LABELS) >= 19
        assert "content" in CANONICAL_LABELS
        assert "UNKNOWN" in CANONICAL_LABELS
