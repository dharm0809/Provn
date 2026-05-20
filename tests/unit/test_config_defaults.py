"""Prod-safe default invariants.

These defaults exist because every fresh install was lighting up DEP-03 +
FEA-01 red — see ``docs/PRODUCTION_CHECKLIST.md`` and the comment above
``llama_guard_enabled`` in ``src/gateway/config.py``. If you flip these,
update the checklist and the readiness rollup expectations.
"""

from gateway.config import Settings, get_settings


def setup_function(_func):
    get_settings.cache_clear()


def teardown_function(_func):
    get_settings.cache_clear()


def test_llama_guard_defaults_off(monkeypatch, tmp_path):
    """Most deployments don't run Ollama; off-by-default keeps fresh
    installs green on DEP-03 / FEA-01."""
    # Isolate from any .env.gateway / .env in the dev tree and from any
    # WALACOR_LLAMA_GUARD_ENABLED already exported in the shell.
    monkeypatch.delenv("WALACOR_LLAMA_GUARD_ENABLED", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.llama_guard_enabled is False
    assert "llama_guard_enabled" not in settings.model_fields_set
