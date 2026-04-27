"""Optional Ed25519 record signing for non-repudiation."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_signing_key: Any = None
_verify_key: Any = None


def load_signing_key(key_path: str) -> bool:
    """Load Ed25519 private key from PEM file. Returns True on success."""
    global _signing_key, _verify_key
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key_data = Path(key_path).read_bytes()
        _signing_key = load_pem_private_key(key_data, password=None)
        _verify_key = _signing_key.public_key()
        logger.info("Ed25519 signing key loaded from %s", key_path)
        return True
    except ImportError:
        logger.warning("cryptography package not installed — record signing disabled")
        return False
    except FileNotFoundError:
        logger.warning("Signing key not found: %s", key_path)
        return False
    except Exception as e:
        logger.warning("Failed to load signing key: %s", e)
        return False


def ensure_signing_key(key_path: str) -> bool:
    """Load the signing key at *key_path*, generating a new one if absent.

    Idempotent across restarts: once generated, the same key is reused so
    historical signatures remain verifiable. The key file is persisted with
    mode 0600 (owner read/write only). Fail-open: if key generation or
    persistence fails, records are written unsigned and ``verify_chain``
    reports signature status as "unverifiable" instead of failing the chain.
    """
    path = Path(key_path)
    if path.exists():
        return load_signing_key(str(path))

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption,
        )
    except ImportError:
        logger.warning("cryptography package not installed — record signing disabled")
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        # Write atomically with restrictive permissions so the key is never
        # briefly visible in world-readable form.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(pem)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass  # Best-effort on platforms where chmod is meaningless.
        tmp.replace(path)
        logger.info("Generated new Ed25519 signing key at %s (first run)", path)
    except Exception as e:
        logger.warning("Failed to auto-generate signing key at %s: %s", path, e)
        return False

    return load_signing_key(str(path))


def _canonical_bytes(
    record_id: str | None,
    previous_record_id: str | None,
    sequence_number: int,
    execution_id: str,
    timestamp: str,
) -> bytes:
    return "|".join([
        record_id or "",
        previous_record_id or "",
        str(sequence_number),
        execution_id,
        timestamp,
    ]).encode("utf-8")


def sign_bytes(canonical: bytes, private_key: Any = None) -> str | None:
    """Sign an arbitrary canonical byte string with Ed25519.

    Used by Pillar 4 of agent tracing (AgentRunManifest) — the manifest's
    canonical layout differs from the execution-chain shape baked into
    :func:`sign_canonical`, but the underlying key and base64-encoding
    convention are shared.
    """
    key = private_key if private_key is not None else _signing_key
    if key is None:
        return None
    try:
        return base64.b64encode(key.sign(canonical)).decode("ascii")
    except Exception as e:  # pragma: no cover — fail-open path
        logger.warning("sign_bytes failed (fail-open): %s", e)
        return None


def sign_canonical(
    *,
    record_id: str | None,
    previous_record_id: str | None,
    sequence_number: int,
    execution_id: str,
    timestamp: str,
    private_key: Any = None,
) -> str | None:
    """Sign a canonical ID+metadata string with Ed25519.

    Returns base64-encoded signature or None. Uses the module-level key
    when private_key is omitted (normal runtime path); pass private_key
    explicitly in tests.
    """
    key = private_key if private_key is not None else _signing_key
    if key is None:
        return None
    try:
        msg = _canonical_bytes(record_id, previous_record_id, sequence_number, execution_id, timestamp)
        signature = key.sign(msg)
        return base64.b64encode(signature).decode("ascii")
    except Exception as e:
        logger.warning("Record signing failed (fail-open): %s", e)
        return None


def verify_canonical(
    *,
    record_id: str | None,
    previous_record_id: str | None,
    sequence_number: int,
    execution_id: str,
    timestamp: str,
    signature: str,
    public_key: Any = None,
) -> bool:
    """Verify an Ed25519 signature over the canonical ID string."""
    key = public_key if public_key is not None else _verify_key
    if key is None:
        return False
    try:
        msg = _canonical_bytes(record_id, previous_record_id, sequence_number, execution_id, timestamp)
        sig_bytes = base64.b64decode(signature)
        key.verify(sig_bytes, msg)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def signing_key_available() -> bool:
    """True when a verify key is loaded — i.e. Ed25519 signature checks are possible."""
    return _verify_key is not None


def verify_record_signature(record: dict) -> str:
    """Classify the signature status for a lineage record.

    Returns one of:
      - ``"valid"``  — ``record_signature`` verified against the loaded verify key
      - ``"invalid"`` — signature present but doesn't verify (tampering or key mismatch)
      - ``"absent"`` — no signature on the record (legacy or signing disabled at write time)
      - ``"unverifiable"`` — signature present but no verify key loaded in this process,
        so we can't make a claim either way
    """
    sig = record.get("record_signature")
    if not sig:
        return "absent"
    if _verify_key is None:
        return "unverifiable"
    ok = verify_canonical(
        record_id=record.get("record_id"),
        previous_record_id=record.get("previous_record_id"),
        sequence_number=int(record.get("sequence_number") or 0),
        execution_id=str(record.get("execution_id") or ""),
        timestamp=str(record.get("timestamp") or ""),
        signature=sig,
    )
    return "valid" if ok else "invalid"


def sign_hash(record_hash: str) -> str | None:
    """Sign a record hash with Ed25519. Returns base64-encoded signature or None.

    Deprecated: use sign_canonical instead. Kept for one release cycle.
    """
    if _signing_key is None:
        return None
    logger.debug("sign_hash is deprecated; migrate callers to sign_canonical")
    try:
        signature = _signing_key.sign(record_hash.encode("utf-8"))
        return base64.b64encode(signature).decode("ascii")
    except Exception as e:
        logger.warning("Record signing failed (fail-open): %s", e)
        return None


def verify_signature(record_hash: str, signature_b64: str) -> bool:
    """Verify an Ed25519 signature against a record hash.

    Deprecated: use verify_canonical instead. Kept for one release cycle.
    """
    if _verify_key is None:
        return False
    logger.debug("verify_signature is deprecated; migrate callers to verify_canonical")
    try:
        sig_bytes = base64.b64decode(signature_b64)
        _verify_key.verify(sig_bytes, record_hash.encode("utf-8"))
        return True
    except ImportError:
        return False
    except Exception:
        return False


def get_public_key_pem() -> str | None:
    """Return the public key in PEM format for verification distribution."""
    if _verify_key is None:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return _verify_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    except Exception:
        return None


def generate_keypair(key_path: str) -> bool:
    """Generate a new Ed25519 keypair and save private key to file. For setup/testing."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        Path(key_path).write_bytes(pem)
        logger.info("Ed25519 keypair generated: %s", key_path)
        return True
    except ImportError:
        logger.warning("cryptography package not installed")
        return False
    except Exception as e:
        logger.warning("Keypair generation failed: %s", e)
        return False
