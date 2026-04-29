"""Unit tests for Ed25519 record signing."""

import os
import tempfile

import pytest


def _has_cryptography():
    try:
        import cryptography  # noqa: F401

        return True
    except ImportError:
        return False


needs_cryptography = pytest.mark.skipif(
    not _has_cryptography(), reason="cryptography not installed"
)


_CANONICAL_KWARGS = dict(
    record_id="rec-1",
    previous_record_id=None,
    sequence_number=0,
    execution_id="exec-1",
    timestamp="2026-01-01T00:00:00Z",
)


@needs_cryptography
def test_generate_keypair():
    """Generate keypair creates a valid PEM file."""
    from gateway.crypto.signing import generate_keypair

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        assert generate_keypair(key_path) is True
        assert os.path.exists(key_path)
        content = open(key_path, "rb").read()
        assert b"PRIVATE KEY" in content


@needs_cryptography
def test_load_and_sign():
    """Load key and sign canonical metadata."""
    from gateway.crypto import signing

    # Reset module state
    signing._signing_key = None
    signing._verify_key = None

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        signing.generate_keypair(key_path)
        assert signing.load_signing_key(key_path) is True
        sig = signing.sign_canonical(**_CANONICAL_KWARGS)
        assert sig is not None
        assert len(sig) > 0

    # Cleanup
    signing._signing_key = None
    signing._verify_key = None


@needs_cryptography
def test_sign_and_verify():
    """Sign and verify round-trip."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        signing.generate_keypair(key_path)
        signing.load_signing_key(key_path)

        sig = signing.sign_canonical(**_CANONICAL_KWARGS)
        assert sig is not None
        assert signing.verify_canonical(signature=sig, **_CANONICAL_KWARGS) is True

    signing._signing_key = None
    signing._verify_key = None


@needs_cryptography
def test_verify_wrong_metadata():
    """Verification fails when canonical metadata changes."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        signing.generate_keypair(key_path)
        signing.load_signing_key(key_path)

        sig = signing.sign_canonical(**_CANONICAL_KWARGS)
        assert sig is not None
        tampered = {**_CANONICAL_KWARGS, "execution_id": "exec-tampered"}
        assert signing.verify_canonical(signature=sig, **tampered) is False

    signing._signing_key = None
    signing._verify_key = None


@needs_cryptography
def test_verify_wrong_signature():
    """Verification fails for corrupted signature."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        signing.generate_keypair(key_path)
        signing.load_signing_key(key_path)

        assert signing.verify_canonical(signature="bm90YXNpZw==", **_CANONICAL_KWARGS) is False

    signing._signing_key = None
    signing._verify_key = None


def test_sign_without_key():
    """Signing without loaded key returns None."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None
    assert signing.sign_canonical(**_CANONICAL_KWARGS) is None


def test_verify_without_key():
    """Verification without loaded key returns False."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None
    assert signing.verify_canonical(signature="sig", **_CANONICAL_KWARGS) is False


def test_load_nonexistent_key():
    """Loading nonexistent key returns False."""
    from gateway.crypto.signing import load_signing_key

    assert load_signing_key("/nonexistent/path/key.pem") is False


@needs_cryptography
def test_get_public_key_pem():
    """Public key PEM export works after loading."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None

    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "test_key.pem")
        signing.generate_keypair(key_path)
        signing.load_signing_key(key_path)

        pem = signing.get_public_key_pem()
        assert pem is not None
        assert "PUBLIC KEY" in pem

    signing._signing_key = None
    signing._verify_key = None


def test_get_public_key_without_key():
    """Public key export returns None without loaded key."""
    from gateway.crypto import signing

    signing._signing_key = None
    signing._verify_key = None
    assert signing.get_public_key_pem() is None


def test_deprecated_aliases_removed():
    """sign_hash and verify_signature must no longer exist on the module."""
    from gateway.crypto import signing

    assert not hasattr(signing, "sign_hash"), "sign_hash should be deleted"
    assert not hasattr(signing, "verify_signature"), "verify_signature should be deleted"
