"""Unit tests for error-specific fallback routing."""

from gateway.routing.fallback import classify_error, select_fallback
from gateway.routing.balancer import Endpoint, LoadBalancer, ModelGroup


def test_classify_context_overflow():
    assert classify_error(400, "maximum context length exceeded") == "context_overflow"


def test_classify_rate_limited():
    assert classify_error(429, "Rate limit exceeded") == "rate_limited"


def test_classify_content_policy():
    assert classify_error(400, "content policy violation") == "content_policy"


def test_classify_server_error():
    assert classify_error(503, "Service Unavailable") == "server_error"


def test_classify_other():
    assert classify_error(400, "invalid model") == "other"


def test_content_policy_no_fallback():
    """Content policy errors should not be retried."""
    group = ModelGroup(
        pattern="gpt-*",
        endpoints=[
            Endpoint(url="https://api1.example.com", api_key="sk-1"),
            Endpoint(url="https://api2.example.com", api_key="sk-2"),
        ],
    )
    lb = LoadBalancer([group])
    result = select_fallback("content_policy", "gpt-4", lb, exclude_url="https://api1.example.com")
    assert result is None


def test_fallback_skips_failed_endpoint():
    """Fallback should not select the endpoint that just failed."""
    group = ModelGroup(
        pattern="gpt-*",
        endpoints=[
            Endpoint(url="https://api1.example.com", api_key="sk-1"),
            Endpoint(url="https://api2.example.com", api_key="sk-2"),
        ],
    )
    lb = LoadBalancer([group])
    for _ in range(20):
        result = select_fallback("server_error", "gpt-4", lb, exclude_url="https://api1.example.com")
        assert result is not None
        assert result.url == "https://api2.example.com"
