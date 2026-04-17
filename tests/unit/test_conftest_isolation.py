"""Regression test: `.env.gateway` must not leak into test runs.

`.env.gateway` at the repo root contains real Walacor sandbox creds
in developer checkouts. Pydantic-settings' AliasChoices (which bypasses
`env_prefix`) loads them automatically — even when tests clear the
`WALACOR_*`-prefixed env vars. The fix is to force the three credential
vars to empty strings in `tests/conftest.py` so tests never accidentally
connect to a real Walacor backend.
"""
from __future__ import annotations

import os


def test_walacor_credentials_are_cleared_for_tests():
    """The top-level conftest must zero these three vars so no test
    inherits sandbox credentials from a developer's `.env.gateway`."""
    assert os.environ.get("WALACOR_SERVER", None) == "", (
        "WALACOR_SERVER must be empty during tests (conftest guard). "
        f"Got: {os.environ.get('WALACOR_SERVER', 'UNSET')!r}"
    )
    assert os.environ.get("WALACOR_USERNAME", None) == ""
    assert os.environ.get("WALACOR_PASSWORD", None) == ""


def test_settings_does_not_load_walacor_from_dotenv():
    """Even if `.env.gateway` contains credentials, the settings object
    exposed to tests must see empty values — confirming the conftest
    guard is effective."""
    from gateway.config import get_settings

    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.walacor_server == ""
        assert s.walacor_username == ""
        assert s.walacor_password == ""
        # And therefore:
        assert s.walacor_storage_enabled is False
    finally:
        get_settings.cache_clear()
