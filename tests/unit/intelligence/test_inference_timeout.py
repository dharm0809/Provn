"""Hot-path ONNX inference must time out and fall back, not hang the request."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from gateway.intelligence._inference_timeout import (
    InferenceTimeout,
    run_with_timeout,
)


def test_run_with_timeout_returns_result_when_under_budget():
    fn = MagicMock(return_value="ok")
    assert run_with_timeout(fn, 1, 2, timeout_s=1.0, model="test", x=3) == "ok"
    fn.assert_called_once_with(1, 2, x=3)


def test_run_with_timeout_raises_inference_timeout_on_overrun():
    def slow(*_a, **_k):
        time.sleep(2.0)
        return "never"

    with pytest.raises(InferenceTimeout):
        run_with_timeout(slow, timeout_s=0.05, model="test")


def test_run_with_timeout_increments_prometheus_counter():
    from gateway.metrics.prometheus import onnx_inference_timeout_total

    before = onnx_inference_timeout_total.labels(model="counter_test")._value.get()

    def slow():
        time.sleep(1.0)

    with pytest.raises(InferenceTimeout):
        run_with_timeout(slow, timeout_s=0.05, model="counter_test")

    after = onnx_inference_timeout_total.labels(model="counter_test")._value.get()
    assert after == before + 1


def test_safety_classifier_falls_back_when_session_hangs():
    """A blocking InferenceSession.run must not hang analyze(); fail-open PASS."""
    from gateway.content.safety_classifier import SafetyClassifier
    from gateway.content.base import Verdict

    clf = SafetyClassifier()
    # Skip if classifier didn't load packaged ONNX (no labels means broken env)
    if not clf._loaded:
        pytest.skip("SafetyClassifier ONNX bundle not loaded in this env")

    # Replace session with a blocking mock
    blocking = MagicMock()
    blocking.run.side_effect = lambda *a, **k: time.sleep(5.0)
    clf._session = blocking

    # Override default timeout via monkeypatched config to be very short
    from gateway.config import get_settings
    settings = get_settings()
    original = settings.onnx_inference_timeout_ms
    object.__setattr__(settings, "onnx_inference_timeout_ms", 50)
    try:
        start = time.monotonic()
        decision = clf.analyze("any text here for analysis")
        elapsed = time.monotonic() - start
    finally:
        object.__setattr__(settings, "onnx_inference_timeout_ms", original)

    assert elapsed < 2.0, f"analyze() blocked {elapsed:.2f}s — timeout did not fire"
    assert decision.verdict == Verdict.PASS
    assert decision.reason == "onnx_timeout"


def test_intent_classifier_falls_back_when_session_hangs():
    """A blocking InferenceSession.run must not hang classify(); fall back to NORMAL."""
    from gateway.classifier.intent import IntentClassifier, NORMAL

    clf = IntentClassifier()

    blocking = MagicMock()
    blocking.run.side_effect = lambda *a, **k: time.sleep(5.0)
    blocking.get_inputs.return_value = [MagicMock(name="prompt", shape=[1, 1])]
    # Make get_inputs()[0].name == "prompt" so the sklearn branch is taken
    inp_meta = MagicMock()
    inp_meta.name = "prompt"
    inp_meta.shape = [1, 1]
    blocking.get_inputs.return_value = [inp_meta]
    clf._onnx_session = blocking

    from gateway.config import get_settings
    settings = get_settings()
    original = settings.onnx_inference_timeout_ms
    object.__setattr__(settings, "onnx_inference_timeout_ms", 50)
    try:
        start = time.monotonic()
        # tier1_deterministic returns None for a plain prompt → tier2 runs
        result = clf._tier2_onnx("hello world how are you today")
        elapsed = time.monotonic() - start
    finally:
        object.__setattr__(settings, "onnx_inference_timeout_ms", original)

    assert elapsed < 2.0, f"_tier2_onnx blocked {elapsed:.2f}s — timeout did not fire"
    assert result.intent == NORMAL
    assert result.reason == "onnx_timeout"
