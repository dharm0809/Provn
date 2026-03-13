# tests/unit/test_capability_registry.py
"""Tests for model capability registry with TTL."""
import pytest
import time
from gateway.adaptive.capability_registry import CapabilityRegistry, ModelCapability


def test_unknown_model_returns_none():
    reg = CapabilityRegistry(ttl_seconds=3600)
    assert reg.supports_tools("unknown-model") is None


def test_record_and_query():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    assert reg.supports_tools("qwen3:4b") is True


def test_record_false():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("gemma3:1b", supports_tools=False, provider="ollama")
    assert reg.supports_tools("gemma3:1b") is False


def test_ttl_expiry():
    reg = CapabilityRegistry(ttl_seconds=1)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    # Manually expire
    reg._cache["qwen3:4b"] = reg._cache["qwen3:4b"]._replace(
        probed_at=time.time() - 10)
    assert reg.supports_tools("qwen3:4b") is None  # stale


def test_get_timeout_default():
    reg = CapabilityRegistry(ttl_seconds=3600)
    assert reg.get_timeout("unknown", default=60.0) == 60.0


def test_get_timeout_reasoning():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama",
               model_type="reasoning")
    assert reg.get_timeout("qwen3:4b", default=60.0) == 120.0


def test_get_timeout_embedding():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("embed-model", supports_tools=False, provider="openai",
               model_type="embedding")
    assert reg.get_timeout("embed-model", default=60.0) == 30.0


def test_get_stale_models():
    reg = CapabilityRegistry(ttl_seconds=1)
    reg.record("m1", supports_tools=True, provider="ollama")
    reg.record("m2", supports_tools=False, provider="ollama")
    # Expire m1
    reg._cache["m1"] = reg._cache["m1"]._replace(probed_at=time.time() - 10)
    stale = reg.get_stale_models()
    assert "m1" in stale
    assert "m2" not in stale


def test_mark_for_reprobe():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    reg.mark_for_reprobe("qwen3:4b")
    assert reg.supports_tools("qwen3:4b") is None


def test_all_capabilities():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("m1", supports_tools=True, provider="ollama")
    reg.record("m2", supports_tools=False, provider="openai")
    caps = reg.all_capabilities()
    assert len(caps) == 2
