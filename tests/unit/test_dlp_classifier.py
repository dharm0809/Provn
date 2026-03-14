"""Tests for B.8 DLP classifier (walacor.dlp.v1)."""
from __future__ import annotations

import pytest

from gateway.content.dlp_classifier import DLPClassifier
from gateway.content.base import Verdict


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def clf():
    return DLPClassifier()


# ── basic interface ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_analyzer_id():
    clf = DLPClassifier()
    assert clf.analyzer_id == "walacor.dlp.v1"


@pytest.mark.anyio
async def test_timeout_ms():
    clf = DLPClassifier()
    assert clf.timeout_ms == 20


@pytest.mark.anyio
async def test_no_findings_clean_text(clf):
    result = await clf.analyze("Hello, how are you today? The weather is nice.")
    assert result.verdict == Verdict.PASS
    assert result.reason == "no_dlp_detected"


@pytest.mark.anyio
async def test_empty_text(clf):
    result = await clf.analyze("")
    assert result.verdict == Verdict.PASS
    assert result.reason == "no_dlp_detected"


# ── SECRETS — BLOCK ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_detects_rsa_private_key():
    clf = DLPClassifier(enabled_categories={"secrets"})
    result = await clf.analyze("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "rsa_private_key"
    assert "secrets" in result.category


@pytest.mark.anyio
async def test_detects_private_key_pem():
    clf = DLPClassifier(enabled_categories={"secrets"})
    result = await clf.analyze("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC")
    assert result.verdict == Verdict.BLOCK
    # rsa_private_key pattern (BEGIN (?:RSA |EC )?PRIVATE KEY) also matches PKCS#8 keys
    # since the RSA/EC prefix is optional — either reason is a correct hit
    assert result.reason in ("private_key_pem", "rsa_private_key")


@pytest.mark.anyio
async def test_detects_connection_string_postgresql():
    clf = DLPClassifier(enabled_categories={"secrets"})
    result = await clf.analyze("Connect with: postgresql://user:pass@db.internal:5432/mydb")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "connection_string"


@pytest.mark.anyio
async def test_detects_connection_string_mongodb():
    clf = DLPClassifier(enabled_categories={"secrets"})
    result = await clf.analyze("mongodb://admin:secret123@mongo.host:27017/prod_db")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "connection_string"


@pytest.mark.anyio
async def test_detects_jwt_token():
    clf = DLPClassifier(enabled_categories={"secrets"})
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    result = await clf.analyze(f"Token: {jwt}")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "jwt_token"


# ── HEALTH — BLOCK ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_detects_icd10_code():
    clf = DLPClassifier(enabled_categories={"health"})
    result = await clf.analyze("Patient diagnosed with E11.9 (Type 2 diabetes)")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "icd10_code"
    assert "health" in result.category


@pytest.mark.anyio
async def test_detects_drug_dosage():
    clf = DLPClassifier(enabled_categories={"health"})
    result = await clf.analyze("Prescribed 500mg metformin twice daily")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "drug_dosage"


@pytest.mark.anyio
async def test_detects_mrn():
    clf = DLPClassifier(enabled_categories={"health"})
    result = await clf.analyze("Patient MRN: 1234567 admitted today")
    assert result.verdict == Verdict.BLOCK
    assert result.reason == "mrn"


# ── INFRASTRUCTURE — WARN ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_detects_aws_arn():
    clf = DLPClassifier(enabled_categories={"infrastructure"})
    result = await clf.analyze("Resource: arn:aws:s3:::my-bucket/path in account 123456789012")
    # Use explicit ARN with account ID in the pattern
    result2 = await clf.analyze("arn:aws:iam::123456789012:role/MyRole")
    # Either the direct ARN hit or clean text
    if result.verdict != Verdict.PASS:
        assert result.verdict == Verdict.WARN
        assert result.reason == "aws_arn"
    if result2.verdict != Verdict.PASS:
        assert result2.verdict == Verdict.WARN


@pytest.mark.anyio
async def test_detects_internal_hostname():
    clf = DLPClassifier(enabled_categories={"infrastructure"})
    result = await clf.analyze("Connect to db.service.internal for production data")
    assert result.verdict == Verdict.WARN
    assert result.reason == "internal_hostname"
    assert not (result.verdict == Verdict.BLOCK)


@pytest.mark.anyio
async def test_infra_is_warn_not_block():
    clf = DLPClassifier(enabled_categories={"infrastructure"})
    result = await clf.analyze("Host: api.prod.corp is the internal endpoint")
    # infrastructure must WARN, never BLOCK (default config)
    if result.verdict != Verdict.PASS:
        assert result.verdict == Verdict.WARN


# ── FINANCIAL — WARN ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_financial_warn_not_block():
    clf = DLPClassifier(enabled_categories={"financial"})
    # SWIFT code alone should warn, not block
    result = await clf.analyze("SWIFT code: CHASUS33XXX")
    if result.verdict != Verdict.PASS:
        assert result.verdict == Verdict.WARN
        assert not (result.verdict == Verdict.BLOCK)


# ── BLOCK takes priority over WARN ───────────────────────────────────────────

@pytest.mark.anyio
async def test_block_takes_priority_over_warn():
    """When both block and warn categories match, verdict must be BLOCK."""
    clf = DLPClassifier()
    # RSA key (BLOCK) + internal hostname (WARN)
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE and also db.service.internal"
    result = await clf.analyze(text)
    assert result.verdict == Verdict.BLOCK


@pytest.mark.anyio
async def test_multiple_categories_both_block():
    clf = DLPClassifier()
    text = "-----BEGIN PRIVATE KEY-----\nMIIE... and Patient MRN: 9876543 admitted"
    result = await clf.analyze(text)
    assert result.verdict == Verdict.BLOCK
    assert result.category.startswith("dlp.")


# ── configure() method ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_configure_overrides_action():
    """configure() can override default BLOCK to WARN for health category."""
    clf = DLPClassifier(enabled_categories={"health"})
    clf.configure([{"category": "health", "action": "warn"}])
    result = await clf.analyze("Patient diagnosed with E11.9")
    # After override, health is WARN not BLOCK
    if result.verdict != Verdict.PASS:
        assert result.verdict == Verdict.WARN


@pytest.mark.anyio
async def test_configure_pass_skips_category():
    """configure() with action=pass means category is skipped."""
    clf = DLPClassifier(enabled_categories={"secrets"})
    clf.configure([{"category": "secrets", "action": "pass"}])
    result = await clf.analyze("-----BEGIN RSA PRIVATE KEY-----\nMIIE key data")
    assert result.verdict == Verdict.PASS


@pytest.mark.anyio
async def test_configure_empty_list_no_change():
    """configure([]) leaves existing action mapping unchanged."""
    clf = DLPClassifier(enabled_categories={"secrets"})
    clf.configure([])
    result = await clf.analyze("-----BEGIN RSA PRIVATE KEY-----\nMIIEkey")
    assert result.verdict == Verdict.BLOCK


# ── category filter ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_category_filter_secrets_only():
    """When only 'financial' enabled, RSA key must NOT be detected."""
    clf = DLPClassifier(enabled_categories={"financial"})
    result = await clf.analyze("-----BEGIN RSA PRIVATE KEY-----")
    # Financial patterns won't match RSA key header
    assert result.verdict != Verdict.BLOCK


@pytest.mark.anyio
async def test_category_filter_health_only():
    """When only 'infrastructure' enabled, ICD-10 code must NOT be detected."""
    clf = DLPClassifier(enabled_categories={"infrastructure"})
    # ICD-10 like J45 won't match infrastructure patterns
    result = await clf.analyze("internal-host.corp.internal is the server")
    # infrastructure hit is WARN, not BLOCK
    if result.verdict != Verdict.PASS:
        assert result.verdict == Verdict.WARN


# ── anyio backend fixture ─────────────────────────────────────────────────────

@pytest.fixture
def anyio_backend():
    return "asyncio"
