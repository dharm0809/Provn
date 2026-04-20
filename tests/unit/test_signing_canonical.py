"""sign_canonical / verify_canonical must round-trip and be sensitive to each field."""
from __future__ import annotations
import pytest

pytest.importorskip("cryptography", reason="cryptography package required")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from gateway.crypto.signing import sign_canonical, verify_canonical


def _make_keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def test_sign_canonical_and_verify_round_trip() -> None:
    priv, pub = _make_keypair()
    sig = sign_canonical(
        record_id="rec-1",
        previous_record_id=None,
        sequence_number=0,
        execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        private_key=priv,
    )
    assert sig is not None
    assert verify_canonical(
        record_id="rec-1",
        previous_record_id=None,
        sequence_number=0,
        execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        signature=sig,
        public_key=pub,
    ) is True


def test_sign_canonical_sensitive_to_record_id() -> None:
    priv, pub = _make_keypair()
    sig = sign_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z", private_key=priv,
    )
    assert verify_canonical(
        record_id="rec-CHANGED", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        signature=sig, public_key=pub,
    ) is False


def test_sign_canonical_sensitive_to_sequence_number() -> None:
    priv, pub = _make_keypair()
    sig = sign_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z", private_key=priv,
    )
    assert verify_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=99, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        signature=sig, public_key=pub,
    ) is False


def test_sign_canonical_sensitive_to_execution_id() -> None:
    priv, pub = _make_keypair()
    sig = sign_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z", private_key=priv,
    )
    assert verify_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-CHANGED",
        timestamp="2026-04-20T00:00:00Z",
        signature=sig, public_key=pub,
    ) is False
