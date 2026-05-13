"""Unit tests for the SSM-hydrating container entrypoint.

We avoid spinning up moto / aws-mock because the entrypoint's
``_hydrate_from_ssm`` is a thin wrapper around two boto3 calls; a hand
stub keeps the test fast and dep-free.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from gateway import entrypoint as ep


class _FakeSSM:
    """Drop-in for ``boto3.client('ssm')`` covering only ``get_parameter``."""

    def __init__(self, store: dict[str, str], fail: set[str] | None = None) -> None:
        self._store = store
        self._fail = fail or set()
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:
        self.calls.append(Name)
        assert WithDecryption is True, "SecureString params must use WithDecryption=True"
        if Name in self._fail:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "test fail"}},
                "GetParameter",
            )
        if Name not in self._store:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "test"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self._store[Name], "Type": "SecureString"}}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test starts with the SECRET_MAP env vars unset."""
    for env_var in ep.SECRET_MAP.values():
        monkeypatch.delenv(env_var, raising=False)


def _install_fake_ssm(monkeypatch: pytest.MonkeyPatch, fake: _FakeSSM) -> None:
    """Replace ``boto3.client`` so any call to ``boto3.client('ssm', ...)``
    yields our ``_FakeSSM``."""
    import boto3
    monkeypatch.setattr(
        boto3, "client",
        lambda service, region_name=None: fake if service == "ssm" else (_ for _ in ()).throw(
            AssertionError(f"unexpected boto3.client({service!r})")
        ),
    )


def test_hydrates_every_mapped_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """All SECRET_MAP entries land in os.environ with their values."""
    prefix = "/walacor-gateway"
    fake_store = {
        f"{prefix}/{suffix}": f"value_for_{suffix}"
        for suffix in ep.SECRET_MAP
    }
    fake = _FakeSSM(fake_store)
    _install_fake_ssm(monkeypatch, fake)

    n = ep._hydrate_from_ssm(prefix, "us-east-1")

    assert n == len(ep.SECRET_MAP)
    for suffix, env_var in ep.SECRET_MAP.items():
        assert os.environ[env_var] == f"value_for_{suffix}"
    # Every parameter was requested exactly once.
    assert sorted(fake.calls) == sorted(fake_store.keys())


def test_existing_env_var_is_not_clobbered(monkeypatch: pytest.MonkeyPatch) -> None:
    """If an operator pre-sets a secret via env, the entrypoint must not
    overwrite it with the SSM value — escape hatch for rotation."""
    prefix = "/walacor-gateway"
    fake_store = {f"{prefix}/openai_api_key": "ssm_value"}
    fake = _FakeSSM(fake_store)
    _install_fake_ssm(monkeypatch, fake)
    monkeypatch.setenv("OPENAI_API_KEY", "operator_override")

    ep._hydrate_from_ssm(prefix, "us-east-1")

    assert os.environ["OPENAI_API_KEY"] == "operator_override"
    # The pre-set var must NOT have been fetched from SSM.
    assert f"{prefix}/openai_api_key" not in fake.calls


def test_missing_parameter_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If one parameter is missing from SSM the entrypoint logs and
    continues — must not raise, must not abort the rest of the fetch."""
    prefix = "/walacor-gateway"
    fake_store = {f"{prefix}/walacor_password": "pw"}  # only this one
    fail = {f"{prefix}/openai_api_key"}                # this one 404s
    fake = _FakeSSM(fake_store, fail=fail)
    _install_fake_ssm(monkeypatch, fake)
    caplog.set_level(logging.INFO, logger="gateway.entrypoint")

    n = ep._hydrate_from_ssm(prefix, "us-east-1")

    # The one that exists landed.
    assert os.environ.get("WALACOR_PASSWORD") == "pw"
    # The 404'd one did not.
    assert "OPENAI_API_KEY" not in os.environ
    assert n == 1
    assert any("openai_api_key" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_no_secret_values_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The entrypoint must never log a secret's value, only its name."""
    prefix = "/walacor-gateway"
    secret_value = "super-secret-do-not-leak-1234567890"
    fake_store = {f"{prefix}/walacor_password": secret_value}
    fake = _FakeSSM(fake_store)
    _install_fake_ssm(monkeypatch, fake)
    caplog.set_level(logging.DEBUG, logger="gateway.entrypoint")

    ep._hydrate_from_ssm(prefix, "us-east-1")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_value not in log_text, "secret value leaked into logs"


def test_boto3_missing_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """If boto3 isn't installed, the entrypoint logs once and returns 0
    — local-dev path where no SSM hydration is desired."""
    import sys
    monkeypatch.setitem(sys.modules, "boto3", None)

    n = ep._hydrate_from_ssm("/walacor-gateway", "us-east-1")

    assert n == 0


# `os` is imported lazily so the autouse fixture can do `monkeypatch.delenv`
# without name resolution gymnastics. Test bodies pull it in here.
import os  # noqa: E402
