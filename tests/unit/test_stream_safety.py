"""Unit tests for streaming content safety checks."""

from gateway.content.stream_safety import check_stream_safety, check_stream_pii


def test_s4_safety_triggers():
    """S4 patterns trigger safety check."""
    assert check_stream_safety("child exploitation material found") is True


def test_s4_safety_clean():
    """Clean text doesn't trigger S4."""
    assert check_stream_safety("Hello, how can I help you today?") is False


def test_pii_ssn_detected():
    """SSN pattern detected in stream."""
    text = "a " * 250 + "The SSN is 123-45-6789 in the record"
    found, new_len = check_stream_pii(text, 0)
    assert found is True
    assert new_len == len(text)


def test_pii_credit_card_detected():
    """Credit card pattern detected."""
    text = "x" * 400 + " card: 4111-1111-1111-1111 " + "x" * 100
    found, new_len = check_stream_pii(text, 0)
    assert found is True


def test_pii_aws_key_detected():
    """AWS access key pattern detected."""
    text = "x" * 400 + " key: AKIA1234567890ABCDEF " + "x" * 100
    found, new_len = check_stream_pii(text, 0)
    assert found is True


def test_pii_clean_text():
    """Clean text doesn't trigger PII check."""
    text = "x" * 600
    found, new_len = check_stream_pii(text, 0)
    assert found is False
    assert new_len == len(text)


def test_pii_interval_skip():
    """PII check skips when interval not reached."""
    text = "SSN: 123-45-6789"  # Only 17 chars, interval is 500
    found, new_len = check_stream_pii(text, 0)
    assert found is False
    assert new_len == 0  # Didn't check, returns old position


def test_pii_incremental_check():
    """PII check works incrementally as text grows."""
    base = "x" * 400
    found, checked = check_stream_pii(base, 0)
    assert found is False
    assert checked == 0  # Not enough growth

    # Add more text to cross interval
    text = base + "x" * 200
    found, checked = check_stream_pii(text, 0)
    assert found is False
    assert checked == len(text)

    # Next check starts from checked position
    text2 = text + " SSN: 123-45-6789 " + "x" * 500
    found, checked2 = check_stream_pii(text2, checked)
    assert found is True


def test_pii_overlap_boundary():
    """PII pattern at boundary of last checked position is caught."""
    # Put SSN right at the boundary with spaces for word boundary matching
    text = "x" * 499 + " 123-45-6789 " + "x" * 499
    found, checked = check_stream_pii(text, 500)
    assert found is True  # Should catch via 50-char overlap
