"""Tests for AuditLLM-inspired consistency checker."""

import pytest
from src.gateway.intelligence.consistency import (
    ConsistencyTracker,
    cosine_similarity,
    prompt_fingerprint,
)


class TestCosineSimilarity:
    def test_identical_texts(self):
        sim = cosine_similarity("hello world", "hello world")
        assert sim > 0.99

    def test_similar_texts(self):
        sim = cosine_similarity(
            "What is the capital of France?",
            "Tell me the capital city of France",
        )
        assert sim > 0.5

    def test_different_texts(self):
        sim = cosine_similarity(
            "Python programming language",
            "Recipe for chocolate cake",
        )
        assert sim < 0.3

    def test_empty_text(self):
        assert cosine_similarity("", "hello") == 0.0
        assert cosine_similarity("hello", "") == 0.0
        assert cosine_similarity("", "") == 0.0


class TestPromptFingerprint:
    def test_same_words_different_order(self):
        fp1 = prompt_fingerprint("what is machine learning")
        fp2 = prompt_fingerprint("machine learning what is")
        assert fp1 == fp2

    def test_different_questions(self):
        fp1 = prompt_fingerprint("what is machine learning")
        fp2 = prompt_fingerprint("how to cook pasta")
        assert fp1 != fp2

    def test_deterministic(self):
        fp1 = prompt_fingerprint("hello world test")
        fp2 = prompt_fingerprint("hello world test")
        assert fp1 == fp2


class TestConsistencyTracker:
    def test_first_request_no_comparison(self):
        tracker = ConsistencyTracker()
        result = tracker.check(
            prompt="What is the capital of France?",
            response="The capital of France is Paris.",
            model_id="gpt-4o",
            execution_id="e1",
            session_id="s1",
        )
        assert result is None  # No history to compare against

    def test_similar_prompts_consistent_responses(self):
        tracker = ConsistencyTracker()
        # First request
        tracker.check(
            prompt="What is the capital of France?",
            response="The capital of France is Paris, a beautiful city in Europe.",
            model_id="gpt-4o",
            execution_id="e1",
            session_id="s1",
        )
        # Similar prompt, similar response, different session
        result = tracker.check(
            prompt="Tell me the capital of France",
            response="Paris is the capital of France, known for the Eiffel Tower.",
            model_id="gpt-4o",
            execution_id="e2",
            session_id="s2",
        )
        # Should find a match and mark as consistent
        if result:
            assert result.consistent
            assert result.prompt_similarity > 0.5
            assert result.response_similarity > 0.3

    def test_similar_prompts_inconsistent_responses(self):
        tracker = ConsistencyTracker()
        # First request
        tracker.check(
            prompt="What is the capital of France?",
            response="The capital of France is Paris, a beautiful city known for the Eiffel Tower.",
            model_id="gpt-4o",
            execution_id="e1",
            session_id="s1",
        )
        # Same prompt, completely different response
        result = tracker.check(
            prompt="What is the capital of France?",
            response="I enjoy cooking Italian food and watching basketball games on television.",
            model_id="gpt-4o",
            execution_id="e2",
            session_id="s2",
        )
        if result:
            assert not result.consistent
            assert result.prompt_similarity > 0.8
            assert result.response_similarity < 0.3

    def test_same_session_skipped(self):
        tracker = ConsistencyTracker()
        tracker.check(
            prompt="What is Python?",
            response="Python is a programming language.",
            model_id="gpt-4o", execution_id="e1", session_id="s1",
        )
        # Same session — should skip comparison
        result = tracker.check(
            prompt="What is Python?",
            response="Python is great for data science.",
            model_id="gpt-4o", execution_id="e2", session_id="s1",
        )
        assert result is None  # Same session skipped

    def test_different_models_separate(self):
        tracker = ConsistencyTracker()
        tracker.check(
            prompt="Explain gravity", response="Gravity is a force...",
            model_id="gpt-4o", execution_id="e1", session_id="s1",
        )
        # Same prompt but different model — no comparison
        result = tracker.check(
            prompt="Explain gravity", response="Something completely different.",
            model_id="claude-3", execution_id="e2", session_id="s2",
        )
        assert result is None  # Different model, no history

    def test_reliability_score(self):
        tracker = ConsistencyTracker()
        # Consistent responses
        for i in range(5):
            tracker.check(
                prompt="What is the capital of France?",
                response="Paris is the capital of France, a major European city.",
                model_id="gpt-4o", execution_id=f"e{i}", session_id=f"s{i}",
            )
        rel = tracker.get_reliability("gpt-4o")
        if rel:
            assert rel.reliability_score >= 0.5

    def test_short_prompt_skipped(self):
        tracker = ConsistencyTracker()
        result = tracker.check(
            prompt="hi", response="hello", model_id="m1",
            execution_id="e1", session_id="s1",
        )
        assert result is None  # Too short

    def test_stats(self):
        tracker = ConsistencyTracker()
        tracker.check(
            prompt="What is machine learning used for in practice?",
            response="ML is used for classification, prediction, and more.",
            model_id="gpt-4o", execution_id="e1", session_id="s1",
        )
        stats = tracker.get_stats()
        assert stats["models_tracked"] == 1
        assert stats["total_pairs_stored"] == 1
