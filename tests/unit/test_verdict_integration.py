"""Phase 25 Task 7: Hot-path verdict recording integration.

These tests prove each ONNX client records into a VerdictBuffer when one
is injected, stays silent when not, and survives a broken buffer without
breaking the inference path.

The ONNX sessions themselves are mocked — the tests care about wiring,
not model outputs. Real model inference is covered elsewhere.

SchemaMapper and SafetyClassifier import numpy at module scope. In minimal
envs without numpy installed, those tests skip cleanly; IntentClassifier
tests always run because intent.py has no top-level numpy dependency.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from gateway.classifier.intent import IntentClassifier, NORMAL
from gateway.intelligence.verdict_buffer import VerdictBuffer

# Tests that require numpy-backed ONNX clients (SchemaMapper, SafetyClassifier)
# are decorated with `requires_numpy` so they skip cleanly in minimal test envs
# without numpy installed. IntentClassifier has no top-level numpy dependency,
# so its tests always run.
try:
    import numpy  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

requires_numpy = pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not installed")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# IntentClassifier — no numpy dependency at module scope
# ─────────────────────────────────────────────────────────────────────────


def test_intent_records_verdict_with_buffer():
    """classify() must append exactly one verdict per call with correct fields."""
    buf = VerdictBuffer(max_size=10)
    # No ONNX model path → deterministic tier + no_ml_model_default fallback.
    clf = IntentClassifier(onnx_model_path=None, verdict_buffer=buf)

    prompt = "hello world"
    result = clf.classify(prompt=prompt, metadata={}, model_id="llama3")

    assert result.intent == NORMAL  # safe default
    assert buf.size == 1
    drained = buf.drain()
    assert len(drained) == 1
    v = drained[0]
    assert v.model_name == "intent"
    assert v.prediction == NORMAL
    assert v.input_hash == _sha256(prompt)
    assert v.request_id is None
    assert 0.0 <= v.confidence <= 1.0


def test_intent_no_buffer_does_not_record():
    """verdict_buffer=None path must not raise and must still classify."""
    clf = IntentClassifier(onnx_model_path=None, verdict_buffer=None)
    result = clf.classify(prompt="hi", metadata={}, model_id="m")
    assert result.intent == NORMAL  # deterministic default when no ML model


def test_intent_broken_buffer_does_not_break_inference():
    """A buffer whose record() raises must not propagate — inference path is sacred."""
    broken = MagicMock()
    broken.record.side_effect = RuntimeError("simulated buffer failure")
    clf = IntentClassifier(onnx_model_path=None, verdict_buffer=broken)
    # Must not raise.
    result = clf.classify(prompt="hello", metadata={}, model_id="m")
    assert result.intent == NORMAL
    # And the buffer WAS attempted.
    assert broken.record.called


# ─────────────────────────────────────────────────────────────────────────
# SchemaMapper — requires numpy (see importorskip above)
# ─────────────────────────────────────────────────────────────────────────


@requires_numpy
def test_schema_mapper_records_verdict_with_buffer():
    """map_response() must record one verdict per call using the raw JSON as input."""
    from gateway.schema.mapper import SchemaMapper

    buf = VerdictBuffer(max_size=10)
    mapper = SchemaMapper(verdict_buffer=buf)

    raw = {"choices": [{"message": {"content": "hello"}}]}
    mapper.map_response(raw)

    assert buf.size == 1
    drained = buf.drain()
    assert len(drained) == 1
    v = drained[0]
    assert v.model_name == "schema_mapper"
    # prediction is a canonical completion flag; confidence is mapping confidence.
    assert v.prediction in {"complete", "incomplete"}
    assert v.request_id is None
    assert 0.0 <= v.confidence <= 1.0


@requires_numpy
def test_schema_mapper_records_incomplete_for_non_dict_input():
    """Non-dict input takes the early-return path; verdict still fires with prediction=incomplete."""
    from gateway.schema.mapper import SchemaMapper

    buf = VerdictBuffer(max_size=10)
    mapper = SchemaMapper(verdict_buffer=buf)

    mapper.map_response("not a dict")  # type: ignore[arg-type]

    assert buf.size == 1
    v = buf.drain()[0]
    assert v.model_name == "schema_mapper"
    assert v.prediction == "incomplete"


@requires_numpy
def test_schema_mapper_no_buffer_does_not_record():
    """verdict_buffer=None must not raise and must still map."""
    from gateway.schema.mapper import SchemaMapper

    mapper = SchemaMapper(verdict_buffer=None)
    raw = {"choices": [{"message": {"content": "hi"}}]}
    out = mapper.map_response(raw)
    assert out is not None


@requires_numpy
def test_schema_mapper_broken_buffer_does_not_break_inference():
    from gateway.schema.mapper import SchemaMapper

    broken = MagicMock()
    broken.record.side_effect = RuntimeError("boom")
    mapper = SchemaMapper(verdict_buffer=broken)
    out = mapper.map_response({"foo": "bar"})
    assert out is not None
    assert broken.record.called


# ─────────────────────────────────────────────────────────────────────────
# SafetyClassifier — requires numpy
# ─────────────────────────────────────────────────────────────────────────


def _stub_safety_classifier(verdict_buffer: VerdictBuffer | None):
    """Build a SafetyClassifier with a fake ONNX session that always returns 'safe'.

    Avoids loading the real 1.5MB ONNX + TF-IDF vocab on every test while still
    exercising the happy-path recording branch. Only callable when numpy is
    installed (SafetyClassifier imports numpy at module scope).
    """
    import numpy as np
    from gateway.content.safety_classifier import SafetyClassifier

    clf = SafetyClassifier.__new__(SafetyClassifier)
    clf._session = None
    clf._input_name = "features"
    clf._labels = ["safe", "violence", "sexual", "criminal", "self_harm",
                   "hate_speech", "dangerous", "child_safety"]
    clf._vocab = {}
    clf._idf = None
    clf._svd_components = None
    clf._ngram_range = (3, 5)
    clf._loaded = True
    clf._verdict_buffer = verdict_buffer

    # Mock ONNX session: returns label index 0 ("safe") with probability dict.
    mock_session = MagicMock()
    mock_session.run.return_value = (
        [[0]],
        [[{0: 0.95, 1: 0.01, 2: 0.01, 3: 0.01, 4: 0.005, 5: 0.005, 6: 0.005, 7: 0.005}]],
    )
    clf._session = mock_session

    # Mock _featurize to avoid TF-IDF work.
    clf._featurize = lambda text: np.zeros((1, 10), dtype=np.float32)  # type: ignore[assignment]
    return clf


@requires_numpy
def test_safety_records_verdict_with_buffer():
    from gateway.content.base import Verdict

    buf = VerdictBuffer(max_size=10)
    clf = _stub_safety_classifier(verdict_buffer=buf)

    text = "a perfectly normal sentence"
    decision = clf.analyze(text)

    assert decision.verdict == Verdict.PASS
    assert buf.size == 1
    v = buf.drain()[0]
    assert v.model_name == "safety"
    assert v.prediction == "safe"
    assert v.input_hash == _sha256(text)
    assert v.request_id is None
    # Mocked probs had max 0.95.
    assert abs(v.confidence - 0.95) < 1e-6


@requires_numpy
def test_safety_records_verdict_when_unavailable():
    """When the classifier is not loaded, we still want a fail-open verdict entry."""
    from gateway.content.base import Verdict
    from gateway.content.safety_classifier import SafetyClassifier

    buf = VerdictBuffer(max_size=10)
    clf = SafetyClassifier.__new__(SafetyClassifier)
    clf._session = None
    clf._input_name = ""
    clf._labels = []
    clf._vocab = {}
    clf._idf = None
    clf._svd_components = None
    clf._ngram_range = (3, 5)
    clf._loaded = False  # unavailable
    clf._verdict_buffer = buf

    decision = clf.analyze("anything")
    assert decision.verdict == Verdict.PASS
    assert buf.size == 1
    v = buf.drain()[0]
    assert v.model_name == "safety"
    assert v.prediction == "safe"
    assert v.confidence == 0.0


@requires_numpy
def test_safety_no_buffer_does_not_record():
    from gateway.content.base import Verdict

    clf = _stub_safety_classifier(verdict_buffer=None)
    decision = clf.analyze("hello")
    assert decision.verdict == Verdict.PASS  # no error


@requires_numpy
def test_safety_broken_buffer_does_not_break_inference():
    from gateway.content.base import Verdict

    broken = MagicMock()
    broken.record.side_effect = RuntimeError("boom")
    clf = _stub_safety_classifier(verdict_buffer=broken)
    decision = clf.analyze("hello")
    assert decision.verdict == Verdict.PASS
    assert broken.record.called


# ─────────────────────────────────────────────────────────────────────────
# SchemaIntelligence.classify_intent — the live production intent path
# ─────────────────────────────────────────────────────────────────────────


def test_schema_intelligence_records_intent_verdict_tier1():
    """Tier-1 deterministic paths must also record a verdict via SchemaIntelligence."""
    from gateway.classifier.unified import SchemaIntelligence, SYSTEM_TASK

    buf = VerdictBuffer(max_size=10)
    si = SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False, verdict_buffer=buf)

    # ### Task: prefix triggers tier-1 SYSTEM_TASK branch.
    prompt = "### Task: summarize this"
    result = si.classify_intent(prompt=prompt, metadata={}, model_id="llama3")

    assert result.intent == SYSTEM_TASK
    assert buf.size == 1
    v = buf.drain()[0]
    assert v.model_name == "intent"
    assert v.prediction == SYSTEM_TASK
    assert v.input_hash == _sha256(prompt)
    assert v.request_id is None  # ContextVar unset → None
    assert 0.0 <= v.confidence <= 1.0


def test_schema_intelligence_records_intent_verdict_default_branch():
    """No ONNX + no tier-1 match → NORMAL default, verdict still recorded."""
    from gateway.classifier.unified import SchemaIntelligence, NORMAL

    buf = VerdictBuffer(max_size=10)
    si = SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False, verdict_buffer=buf)

    result = si.classify_intent(prompt="a bland question", metadata={}, model_id="llama3")

    assert result.intent == NORMAL
    assert buf.size == 1
    v = buf.drain()[0]
    assert v.model_name == "intent"
    assert v.prediction == NORMAL


def test_schema_intelligence_no_buffer_does_not_record():
    """verdict_buffer=None must not raise and must still classify."""
    from gateway.classifier.unified import SchemaIntelligence, NORMAL

    si = SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False, verdict_buffer=None)
    result = si.classify_intent(prompt="hello", metadata={}, model_id="m")
    assert result.intent == NORMAL


def test_schema_intelligence_broken_buffer_does_not_break_inference():
    from gateway.classifier.unified import SchemaIntelligence, NORMAL

    broken = MagicMock()
    broken.record.side_effect = RuntimeError("boom")
    si = SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False, verdict_buffer=broken)
    result = si.classify_intent(prompt="hello", metadata={}, model_id="m")
    assert result.intent == NORMAL
    assert broken.record.called


# ─────────────────────────────────────────────────────────────────────────
# request_id correlation via ContextVar — harvesters join verdicts to WAL
# ─────────────────────────────────────────────────────────────────────────


def test_intent_verdict_carries_request_id_from_contextvar():
    """When request_id_var is set (as the completeness middleware does),
    the recorded verdict must carry it so Task 13-16 harvesters can join."""
    from gateway.util.request_context import request_id_var

    buf = VerdictBuffer(max_size=10)
    clf = IntentClassifier(onnx_model_path=None, verdict_buffer=buf)

    token = request_id_var.set("test-req-123")
    try:
        clf.classify(prompt="hello", metadata={}, model_id="m")
    finally:
        request_id_var.reset(token)

    v = buf.drain()[0]
    assert v.request_id == "test-req-123"


def test_intent_verdict_request_id_is_none_when_contextvar_unset():
    """Default ContextVar value is empty string; the recording stanza must
    normalize that to None so downstream consumers don't get an empty id."""
    buf = VerdictBuffer(max_size=10)
    clf = IntentClassifier(onnx_model_path=None, verdict_buffer=buf)

    # Do NOT set request_id_var — default is "".
    clf.classify(prompt="hi", metadata={}, model_id="m")

    v = buf.drain()[0]
    assert v.request_id is None


def test_schema_intelligence_verdict_carries_request_id_from_contextvar():
    """SchemaIntelligence — the live production path — must also pick up
    the request id from the ContextVar for cross-stream correlation."""
    from gateway.classifier.unified import SchemaIntelligence
    from gateway.util.request_context import request_id_var

    buf = VerdictBuffer(max_size=10)
    si = SchemaIntelligence(onnx_model_path=None, has_mcp_tools=False, verdict_buffer=buf)

    token = request_id_var.set("test-req-xyz")
    try:
        si.classify_intent(prompt="hello", metadata={}, model_id="m")
    finally:
        request_id_var.reset(token)

    v = buf.drain()[0]
    assert v.request_id == "test-req-xyz"
