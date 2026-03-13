# tests/unit/test_request_classifier.py
"""Tests for smart request classification."""
import pytest
from gateway.adaptive.request_classifier import DefaultRequestClassifier


@pytest.fixture
def classifier():
    return DefaultRequestClassifier()


class TestBodyTaskDetection:
    def test_title_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "title_generation"}) == "system_task:title_generation"

    def test_tags_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "tags_generation"}) == "system_task:tags_generation"

    def test_query_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "query_generation"}) == "system_task:query_generation"

    def test_emoji_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "emoji_generation"}) == "system_task:emoji_generation"

    def test_follow_up_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "follow_up_generation"}) == "system_task:follow_up_generation"

    def test_metadata_task(self, classifier):
        body = {"metadata": {"task": "title_generation"}}
        assert classifier.classify("", {}, body) == "system_task:title_generation"


class TestSyntheticDetection:
    def test_curl(self, classifier):
        assert classifier.classify("hello", {"user-agent": "curl/8.1.2"}, {}) == "synthetic"

    def test_httpie(self, classifier):
        assert classifier.classify("hello", {"user-agent": "HTTPie/3.2"}, {}) == "synthetic"

    def test_python_requests(self, classifier):
        assert classifier.classify("hi", {"user-agent": "python-requests/2.31"}, {}) == "synthetic"

    def test_python_httpx(self, classifier):
        assert classifier.classify("hi", {"user-agent": "python-httpx/0.28"}, {}) == "synthetic"

    def test_k6_load_tester(self, classifier):
        assert classifier.classify("hi", {"user-agent": "k6/0.50"}, {}) == "synthetic"

    def test_real_browser_not_synthetic(self, classifier):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        assert classifier.classify("hi", {"user-agent": ua}, {}) == "user_message"


class TestPromptFallback:
    def test_title_generation_prompt(self, classifier):
        prompt = "Generate a concise title for this conversation"
        assert classifier.classify(prompt, {}, {}).startswith("system_task:")

    def test_follow_up_prompt(self, classifier):
        prompt = "Generate 3 follow-up questions based on the response"
        assert classifier.classify(prompt, {}, {}).startswith("system_task:")

    def test_autocomplete_prompt(self, classifier):
        prompt = "### Task: You are an autocompletion system. Continue the text"
        assert classifier.classify(prompt, {}, {}).startswith("system_task")

    def test_normal_user_message(self, classifier):
        assert classifier.classify("What is quantum computing?", {}, {}) == "user_message"

    def test_empty_prompt(self, classifier):
        assert classifier.classify("", {}, {}) == "user_message"


class TestPriority:
    def test_body_takes_priority_over_prompt(self, classifier):
        """Body task field wins over prompt-based detection."""
        result = classifier.classify(
            "What is AI?",
            {},
            {"task": "title_generation"})
        assert result == "system_task:title_generation"

    def test_body_takes_priority_over_synthetic_ua(self, classifier):
        """Body task field wins over user-agent detection."""
        result = classifier.classify(
            "", {"user-agent": "curl/8.1"}, {"task": "title_generation"})
        assert result == "system_task:title_generation"
