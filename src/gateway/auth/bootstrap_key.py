"""Persist auto-generated bootstrap API key across restarts.

When the control plane is enabled but no API keys are configured, the gateway
auto-generates a `wgk-*` key so the control plane isn't exposed unauthenticated.
Without persistence, this key rotates on every restart — every prior session
token is invalidated and any script holding the key breaks.

This module makes auto-generation idempotent: the key is written once to
`{wal_path}/gateway-bootstrap-key.txt` (mode 0600) and reloaded on every
subsequent boot. Same pattern as `crypto.signing.ensure_signing_key`.

Fail-open: if persistence fails (read-only FS, permission denied), we fall
back to in-memory generation — keeping today's behaviour intact, just without
stability across restarts.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

_KEY_FILENAME = "gateway-bootstrap-key.txt"
_KEY_PREFIX = "wgk-"


def _key_path(wal_path: str) -> Path:
    return Path(wal_path) / _KEY_FILENAME


def ensure_bootstrap_key(wal_path: str) -> tuple[str, bool]:
    """Return a persistent `wgk-*` API key, generating one on first run.

    Returns ``(key, stable)`` where ``stable`` is True when the key was
    successfully read from or persisted to disk (i.e. will survive a restart),
    and False when we fell back to an in-memory key (transient).
    """
    path = _key_path(wal_path)

    # Reload existing key
    if path.exists():
        try:
            key = path.read_text().strip()
            if key.startswith(_KEY_PREFIX) and len(key) >= len(_KEY_PREFIX) + 16:
                logger.info("Bootstrap key loaded from %s", path)
                return key, True
            logger.warning("Bootstrap key at %s is malformed — regenerating", path)
        except Exception as exc:
            logger.warning("Failed to read bootstrap key at %s: %s — regenerating", path, exc)

    # Generate and persist.
    #
    # SECURITY: set a tight umask BEFORE creating the file so it is born 0600.
    # If we relied on chmod-after-write, a SIGINT or chmod failure between the
    # write and the chmod would leave the key world-readable on systems with a
    # default umask of 022. Using os.open(..., O_CREAT | O_EXCL, 0o600) plus a
    # umask of 0o077 guarantees the file is created with the right mode in one
    # syscall. We still chmod as defense-in-depth in case an existing tmp file
    # had broader permissions.
    key = f"{_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Remove any leftover tmp from a prior interrupted run so O_EXCL succeeds.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o077)
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(key)
            except Exception:
                # Best-effort cleanup so a retry can use the same tmp path.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                raise
        finally:
            os.umask(old_umask)
        # Defense in depth — the file should already be 0600 from O_CREAT.
        try:
            tmp.chmod(0o600)
        except OSError as exc:
            logger.error(
                "chmod(0o600) failed on bootstrap key tmp file %s: %s — file may be world-readable",
                tmp, exc,
            )
        tmp.replace(path)
        logger.info("Generated and persisted bootstrap key at %s (mode 0600)", path)
        return key, True
    except Exception as exc:
        logger.warning(
            "Could not persist bootstrap key to %s: %s — using in-memory key (rotates on restart)",
            path, exc,
        )
        return key, False


def bootstrap_key_stable(wal_path: str) -> bool:
    """True when a persisted bootstrap key file exists and is readable."""
    path = _key_path(wal_path)
    if not path.exists():
        return False
    try:
        key = path.read_text().strip()
        return key.startswith(_KEY_PREFIX) and len(key) >= len(_KEY_PREFIX) + 16
    except Exception:
        return False
