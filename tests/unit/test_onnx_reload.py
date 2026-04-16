"""Phase 25 Task 11: InferenceSession reload signaling.

Verifies that a `ModelRegistry.promote`/`.rollback` bumps a per-model
generation counter, and that ONNX clients rebuild their `InferenceSession`
from the current production path when the counter has moved.

`onnxruntime` is not a test-env dependency — a tiny fake module is injected
into `sys.modules` so the clients' `from onnxruntime import InferenceSession`
resolves to the mock. Tests only assert on the reload mechanism (path
passed, call count, fail-safe behavior) — they never run a real ONNX
inference.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_onnxruntime(monkeypatch):
    """Inject a fake `onnxruntime` module so clients can import InferenceSession.

    The fake tracks every construction call so tests can assert how many
    times a session was (re)built and with which path.
    """
    fake = types.ModuleType("onnxruntime")
    calls: list[dict] = []

    class _FakeSession:
        def __init__(self, path, providers=None):
            calls.append({"path": str(path), "providers": providers})
            self.path = str(path)
            # Minimal surface to satisfy client constructors that peek at
            # `.get_inputs()[0].name`. `run()` returns trivial zero-labels
            # + uniform-prob outputs so exercising the full inference path
            # doesn't crash — reload-flag-count assertions are what matters.
            self._input = types.SimpleNamespace(name="features", shape=[None, 10])

        def get_inputs(self):
            return [self._input]

        def run(self, output_names, feeds):
            import numpy as np
            # Infer batch size from the single input tensor.
            batch = 1
            for arr in feeds.values():
                try:
                    batch = len(arr)
                except TypeError:
                    pass
                break
            labels = np.zeros(batch, dtype=np.int64)
            probs = [{0: 1.0} for _ in range(batch)]
            return [labels, probs]

    fake.InferenceSession = _FakeSession
    fake._calls = calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    return fake


def _seed_production(tmp_path: Path, model: str, payload: bytes = b"x") -> Path:
    """Create `<tmp>/production/<model>.onnx` with trivial bytes."""
    (tmp_path / "production").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "production" / f"{model}.onnx"
    path.write_bytes(payload)
    return path


# ───────────────────────────────────────────────────────────────────────────
# IntentClassifier (classifier/intent.py — dormant but kept)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_intent_classifier_reloads_session_after_promote(
    tmp_path, fake_onnxruntime
):
    from gateway.classifier.intent import IntentClassifier
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    prod = _seed_production(tmp_path, "intent", b"v1")

    clf = IntentClassifier(registry=r, model_name="intent")
    # First inference triggers initial load via the registry path.
    clf.classify(prompt="what's the weather?", metadata={}, model_id="llama")
    assert len(fake_onnxruntime._calls) == 1
    assert fake_onnxruntime._calls[0]["path"].endswith("/production/intent.onnx")

    # Simulate a promotion — write a new candidate, swap it in.
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    await r.promote("intent", "v2")

    # Next inference must rebuild the session from the new production path.
    clf.classify(prompt="explain quantum tunneling", metadata={}, model_id="llama")
    assert len(fake_onnxruntime._calls) == 2
    assert fake_onnxruntime._calls[1]["path"] == str(prod)


def test_intent_classifier_does_not_reload_without_generation_bump(
    tmp_path, fake_onnxruntime
):
    from gateway.classifier.intent import IntentClassifier
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    _seed_production(tmp_path, "intent")

    clf = IntentClassifier(registry=r, model_name="intent")
    for i in range(5):
        clf.classify(prompt=f"q{i}", metadata={}, model_id="llama")

    # Exactly one session construction: the initial load. No promotions
    # happened, so no rebuilds should fire.
    assert len(fake_onnxruntime._calls) == 1


def test_intent_classifier_reload_missing_file_is_noop(
    tmp_path, fake_onnxruntime, caplog
):
    from gateway.classifier.intent import IntentClassifier
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    # No production file — `production/intent.onnx` does not exist.

    clf = IntentClassifier(registry=r, model_name="intent")
    # Must not raise; must not attempt to construct a session.
    clf.classify(prompt="anything", metadata={}, model_id="llama")
    assert fake_onnxruntime._calls == []


@pytest.mark.anyio
async def test_intent_classifier_reload_failure_keeps_old_session(
    tmp_path, monkeypatch, fake_onnxruntime
):
    from gateway.classifier.intent import IntentClassifier
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    _seed_production(tmp_path, "intent", b"v1")
    clf = IntentClassifier(registry=r, model_name="intent")
    clf.classify(prompt="q1", metadata={}, model_id="llama")
    first_session = clf._onnx_session
    assert first_session is not None

    # Break the next InferenceSession construction.
    def _boom(*a, **kw):
        raise RuntimeError("corrupt onnx")

    monkeypatch.setattr(fake_onnxruntime, "InferenceSession", _boom)

    # Bump the generation and call again — reload raises internally, but
    # classify must not propagate, and the previous session must remain.
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    await r.promote("intent", "v2")

    clf.classify(prompt="q2", metadata={}, model_id="llama")
    assert clf._onnx_session is first_session


# ───────────────────────────────────────────────────────────────────────────
# SchemaIntelligence (classifier/unified.py — production intent path)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_schema_intelligence_reloads_after_promote(
    tmp_path, fake_onnxruntime
):
    from gateway.classifier.unified import SchemaIntelligence
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    _seed_production(tmp_path, "intent")

    si = SchemaIntelligence(registry=r, model_name="intent")
    si.classify_intent(prompt="ordinary question", metadata={}, model_id="llama")
    assert len(fake_onnxruntime._calls) == 1

    (tmp_path / "candidates" / "intent-v3.onnx").write_bytes(b"v3")
    await r.promote("intent", "v3")

    si.classify_intent(prompt="another question", metadata={}, model_id="llama")
    assert len(fake_onnxruntime._calls) == 2


# ───────────────────────────────────────────────────────────────────────────
# SchemaMapper (schema/mapper.py)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_schema_mapper_reloads_after_promote(tmp_path, fake_onnxruntime):
    from gateway.intelligence.registry import ModelRegistry
    from gateway.schema.mapper import SchemaMapper

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    _seed_production(tmp_path, "schema_mapper")

    mapper = SchemaMapper(registry=r, model_name="schema_mapper")
    mapper.map_response({"id": "x", "choices": [{"message": {"content": "hi"}}]})
    assert len(fake_onnxruntime._calls) == 1
    assert fake_onnxruntime._calls[0]["path"].endswith("/production/schema_mapper.onnx")

    (tmp_path / "candidates" / "schema_mapper-v2.onnx").write_bytes(b"v2")
    await r.promote("schema_mapper", "v2")

    mapper.map_response({"id": "y", "choices": [{"message": {"content": "there"}}]})
    assert len(fake_onnxruntime._calls) == 2


# ───────────────────────────────────────────────────────────────────────────
# SafetyClassifier (content/safety_classifier.py)
# ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_safety_classifier_reloads_after_promote(
    tmp_path, fake_onnxruntime
):
    from gateway.content.safety_classifier import SafetyClassifier
    from gateway.intelligence.registry import ModelRegistry

    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    _seed_production(tmp_path, "safety")

    sc = SafetyClassifier(registry=r, model_name="safety")
    sc.analyze("ordinary text")
    # The registry-backed path should have been consulted. At least one
    # InferenceSession was constructed using it.
    paths = [c["path"] for c in fake_onnxruntime._calls]
    assert any(p.endswith("/production/safety.onnx") for p in paths)
    calls_after_initial = len(fake_onnxruntime._calls)

    (tmp_path / "candidates" / "safety-v9.onnx").write_bytes(b"v9")
    await r.promote("safety", "v9")

    sc.analyze("another text")
    # Exactly one additional session construction from the generation bump.
    assert len(fake_onnxruntime._calls) == calls_after_initial + 1
