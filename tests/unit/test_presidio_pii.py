"""Unit tests for Presidio NER PII detector."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_analyzer():
    """Create a PresidioPIIAnalyzer with mocked engine (no real presidio needed)."""
    from gateway.content.presidio_pii import PresidioPIIAnalyzer

    analyzer = PresidioPIIAnalyzer.__new__(PresidioPIIAnalyzer)
    analyzer._engine = MagicMock()
    analyzer._available = True
    analyzer._block_entities = {"CREDIT_CARD", "US_SSN", "US_BANK_NUMBER", "IBAN_CODE", "CRYPTO"}
    analyzer._warn_entities = {
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION", "DATE_TIME",
        "NRP", "MEDICAL_LICENSE", "US_DRIVER_LICENSE", "US_PASSPORT", "UK_NHS",
        "IP_ADDRESS",
    }
    return analyzer


def test_analyzer_id():
    """Analyzer ID is set correctly."""
    from gateway.content.presidio_pii import PresidioPIIAnalyzer
    analyzer = _make_analyzer()
    assert analyzer.analyzer_id == "walacor.presidio_pii.v1"


def test_timeout_ms():
    """Timeout is higher than regex-based detectors (NER is slower)."""
    analyzer = _make_analyzer()
    assert analyzer.timeout_ms == 200


def test_presidio_unavailable_passes():
    """When presidio not installed, analyzer marks itself unavailable."""
    with patch.dict("sys.modules", {"presidio_analyzer": None}):
        import importlib
        from gateway.content import presidio_pii

        importlib.reload(presidio_pii)
        analyzer = presidio_pii.PresidioPIIAnalyzer()
        assert not analyzer._available


@pytest.mark.anyio
async def test_presidio_unavailable_returns_pass(anyio_backend):
    """When unavailable, analyze() returns PASS with confidence 0."""
    analyzer = _make_analyzer()
    analyzer._available = False
    result = await analyzer.analyze("Hello world")
    assert result.verdict.value == "pass"
    assert result.confidence == 0.0
    assert result.reason == "unavailable"


@pytest.mark.anyio
async def test_presidio_no_pii(anyio_backend):
    """Clean text returns pass verdict."""
    analyzer = _make_analyzer()
    analyzer._engine.analyze.return_value = []
    result = await analyzer.analyze("Hello world, how are you?")
    assert result.verdict.value == "pass"
    assert result.confidence == 0.0
    assert result.reason == "no_pii_detected"


@pytest.mark.anyio
async def test_presidio_detects_credit_card(anyio_backend):
    """Credit card triggers block verdict."""
    analyzer = _make_analyzer()
    mock_result = MagicMock()
    mock_result.entity_type = "CREDIT_CARD"
    mock_result.score = 0.95
    analyzer._engine.analyze.return_value = [mock_result]
    result = await analyzer.analyze("My card is 4111-1111-1111-1111")
    assert result.verdict.value == "block"
    assert result.category == "pii"
    assert result.confidence == 0.95
    assert "credit_card" in result.reason


@pytest.mark.anyio
async def test_presidio_detects_ssn(anyio_backend):
    """US SSN triggers block verdict."""
    analyzer = _make_analyzer()
    mock_result = MagicMock()
    mock_result.entity_type = "US_SSN"
    mock_result.score = 0.92
    analyzer._engine.analyze.return_value = [mock_result]
    result = await analyzer.analyze("SSN: 123-45-6789")
    assert result.verdict.value == "block"
    assert "us_ssn" in result.reason


@pytest.mark.anyio
async def test_presidio_detects_person(anyio_backend):
    """Person name triggers warn verdict."""
    analyzer = _make_analyzer()
    mock_result = MagicMock()
    mock_result.entity_type = "PERSON"
    mock_result.score = 0.85
    analyzer._engine.analyze.return_value = [mock_result]
    result = await analyzer.analyze("John Smith lives here")
    assert result.verdict.value == "warn"
    assert "person" in result.reason


@pytest.mark.anyio
async def test_presidio_detects_email(anyio_backend):
    """Email address triggers warn verdict."""
    analyzer = _make_analyzer()
    mock_result = MagicMock()
    mock_result.entity_type = "EMAIL_ADDRESS"
    mock_result.score = 0.99
    analyzer._engine.analyze.return_value = [mock_result]
    result = await analyzer.analyze("Contact me at john@example.com")
    assert result.verdict.value == "warn"
    assert "email_address" in result.reason


@pytest.mark.anyio
async def test_presidio_analysis_error(anyio_backend):
    """Analysis error returns pass (fail-open)."""
    analyzer = _make_analyzer()
    analyzer._engine.analyze.side_effect = RuntimeError("NER failed")
    result = await analyzer.analyze("test text")
    assert result.verdict.value == "pass"
    assert result.reason == "error"
    assert result.confidence == 0.0


@pytest.mark.anyio
async def test_presidio_multiple_entities(anyio_backend):
    """Multiple entities: highest confidence wins for verdict."""
    analyzer = _make_analyzer()
    r1 = MagicMock()
    r1.entity_type = "PERSON"
    r1.score = 0.7
    r2 = MagicMock()
    r2.entity_type = "CREDIT_CARD"
    r2.score = 0.95
    analyzer._engine.analyze.return_value = [r1, r2]
    result = await analyzer.analyze("John's card is 4111111111111111")
    assert result.verdict.value == "block"  # CREDIT_CARD has higher score
    assert result.confidence == 0.95
    assert "2_entities" in result.reason


@pytest.mark.anyio
async def test_presidio_unknown_entity_type(anyio_backend):
    """Unknown entity type defaults to warn."""
    analyzer = _make_analyzer()
    mock_result = MagicMock()
    mock_result.entity_type = "CUSTOM_ENTITY"
    mock_result.score = 0.88
    analyzer._engine.analyze.return_value = [mock_result]
    result = await analyzer.analyze("some text with custom entity")
    assert result.verdict.value == "warn"


def test_configure_updates_entity_sets():
    """configure() updates block/warn entity sets from content policies."""
    analyzer = _make_analyzer()
    policies = [
        {"category": "person", "action": "pass"},
        {"category": "credit_card", "action": "block"},
        {"category": "email_address", "action": "warn"},
    ]
    analyzer.configure(policies)
    assert "CREDIT_CARD" in analyzer._block_entities
    assert "EMAIL_ADDRESS" in analyzer._warn_entities
    assert "PERSON" not in analyzer._block_entities
    assert "PERSON" not in analyzer._warn_entities


def test_configure_empty_policies_noop():
    """configure() with empty list is a no-op."""
    analyzer = _make_analyzer()
    original_block = set(analyzer._block_entities)
    original_warn = set(analyzer._warn_entities)
    analyzer.configure([])
    assert analyzer._block_entities == original_block
    assert analyzer._warn_entities == original_warn
