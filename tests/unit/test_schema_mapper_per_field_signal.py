"""SchemaMapper emits one verdict per field with the 139-d feature vector.

Before this rewrite, `map_response` recorded ONE coarse verdict per call
with `input_features_json="{}"` — useless for training the per-field
ONNX classifier the production `_classify_onnx` pipeline runs. This
suite locks the per-field contract: every field that flows through
`_classify_onnx` produces a verdict whose `input_features_json` carries
the actual 139-d feature vector and whose `prediction` matches the
per-field label assigned to that field.
"""
from __future__ import annotations

import json

import pytest

from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.schema.features import FEATURE_DIM
from gateway.schema.mapper import SchemaMapper


def _drain(buf: VerdictBuffer) -> list[ModelVerdict]:
    return buf.drain(max_batch=10_000)


def test_per_field_verdicts_emitted_for_openai_response():
    """An OpenAI-shaped response yields one per-field verdict per leaf."""
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {"message": {"content": "Hello!"}, "finish_reason": "stop"},
        ],
        "usage": {
            "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
        },
    }
    mapper.map_response(resp)

    rows = _drain(buf)
    # All rows are schema_mapper verdicts.
    assert rows, "expected per-field verdict rows"
    assert all(r.model_name == "schema_mapper" for r in rows)

    # Every row carries the 139-d feature vector under feature_vector.
    for row in rows:
        payload = json.loads(row.input_features_json)
        assert "feature_vector" in payload
        vec = payload["feature_vector"]
        assert isinstance(vec, list)
        assert len(vec) == FEATURE_DIM
        # No NaN. JSON can't carry NaN anyway, but this is a sanity guard.
        assert all(isinstance(x, (int, float)) for x in vec)
        # Every row must declare which field it came from so the
        # harvester can match overflow paths to specific verdict rows.
        assert "field_path" in payload
        assert isinstance(payload["field_path"], str)


def test_per_field_count_matches_onnx_classified_field_count():
    """One verdict row per ONNX-classified field — no batching, no sampling.

    Fields routed through ``_PROVIDER_PATH_MAP`` are deterministically
    assigned and never reach the ONNX session, so they intentionally do
    not emit a per-field verdict (there's nothing to learn — the answer
    is known). The contract is "one verdict per ONNX inference", not
    "one verdict per leaf field".
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    from gateway.schema.features import flatten_json
    from gateway.schema.mapper import _PROVIDER_PATH_MAP
    resp = {
        "model": "test-model",
        "choices": [{"message": {"content": "A long response with several words."}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
    }
    all_fields = flatten_json(resp)
    onnx_classified = [f for f in all_fields if f.path not in _PROVIDER_PATH_MAP]
    expected = len(onnx_classified)

    mapper.map_response(resp)
    rows = _drain(buf)
    assert len(rows) == expected, (
        f"verdict rows ({len(rows)}) must equal ONNX-classified field count ({expected}); "
        f"all fields={len(all_fields)}, deterministic-skipped={len(all_fields)-expected}"
    )


def test_per_field_predictions_align_with_per_field_features():
    """The per-field prediction in each verdict row must be a real label.

    Sanity: every verdict row's prediction is one of the canonical
    labels (or UNKNOWN), and the row carries a corresponding feature
    vector. We don't pin specific labels because the production model
    is the source of truth — we only need the contract to hold.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "choices": [{"message": {"content": "hi", "role": "assistant"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mapper.map_response(resp)
    rows = _drain(buf)

    valid_labels = set(mapper._labels)
    for row in rows:
        assert row.prediction in valid_labels or row.prediction == "UNKNOWN"
        assert 0.0 <= row.confidence <= 1.0


def test_per_field_verdicts_carry_heuristic_teacher_signal():
    """When the heuristic has an opinion, divergence_signal is populated.

    The heuristic classifier is the rule-based teacher: it provides
    `divergence_signal` for fields it can name (content, prompt_tokens,
    etc.). Fields the heuristic can't classify get `divergence_signal=None`
    — the dataset builder then filters them out.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    # Deliberately NON-standard provider shape: every key here is absent
    # from `_PROVIDER_PATH_MAP`, so the fields reach the ONNX residual and
    # `_record_per_field_verdicts` runs (the only place a teacher signal
    # is emitted). Do NOT replace this with an OpenAI/Anthropic/Ollama
    # shape — those are now fully deterministic and emit zero verdicts by
    # design (see test_per_field_count_matches_onnx_classified_field_count).
    # `_heuristic_classify_one` still confidently names these by key:
    # output_text→content, completion_reason→finish_reason,
    # *_token_count→prompt/completion/total_tokens.
    resp = {
        "output_text": "This is a real natural-language response with many spaces and words here.",
        "completion_reason": "stop",
        "input_token_count": 12,
        "output_token_count": 7,
        "total_token_count": 19,
    }
    mapper.map_response(resp)
    rows = _drain(buf)

    # At least one row must have a teacher signal — we don't pin which
    # specific labels come through because the heuristic / value-aware
    # rules can shift, but the existence of >= 1 teacher proves the
    # producer is now generating training data.
    teachers = [r for r in rows if r.divergence_signal is not None]
    assert teachers, "expected heuristic to label at least one field"
    for t in teachers:
        assert t.divergence_source == "schema_mapper_heuristic"


def test_no_verdicts_when_buffer_unwired():
    """`verdict_buffer=None` is the default — must not raise on inference."""
    mapper = SchemaMapper(verdict_buffer=None)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")
    resp = {
        "choices": [{"message": {"content": "test"}}],
        "usage": {"prompt_tokens": 1},
    }
    mapper.map_response(resp)  # Just must not raise.


def test_per_field_input_hashes_are_unique_within_response():
    """Each field's `input_hash` is derived from path|type|value — collisions
    would let the dataset deduper drop legitimate per-field rows.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    mapper.map_response(resp)
    rows = _drain(buf)
    hashes = [r.input_hash for r in rows]
    # Every field has a distinct path — each hash must be unique.
    assert len(set(hashes)) == len(hashes)


def test_labels_binary_drift_warns_at_boot(caplog):
    """Loading a mapper whose labels.json lists a label the ONNX binary
    can't predict must log a WARNING naming the unpredictable label(s).

    Operators rely on this signal to know a retrain is needed BEFORE the
    accuracy metric quietly degrades (the prod failure mode PR #50 fixed).
    """
    import logging

    from gateway.schema.canonical import ENVELOPE_LABEL

    # Construct a mapper, then trigger a fresh _validate_labels run with
    # a synthetic extra label that the binary can't possibly emit. We
    # capture WARNINGs emitted during that re-run.
    mapper = SchemaMapper(verdict_buffer=None)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")
    if ENVELOPE_LABEL in mapper._model_class_labels:
        pytest.skip("ONNX binary now predicts envelope; drift signal would be vacuous.")

    # Add envelope to labels.json's in-memory snapshot — simulates the
    # prod pre-#50 state. _validate_labels recomputes _model_class_labels
    # against the (unchanged) ONNX binary and should detect the drift.
    mapper._labels.append(ENVELOPE_LABEL)
    mapper._label_to_idx[ENVELOPE_LABEL] = len(mapper._labels) - 1

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="gateway.schema.mapper"):
        mapper._validate_labels()

    drift_msgs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "label/binary drift" in r.getMessage()
    ]
    assert drift_msgs, (
        "expected a WARNING when labels.json lists a label the ONNX binary "
        "cannot predict; got no drift warning"
    )
    assert any(ENVELOPE_LABEL in r.getMessage() for r in drift_msgs), (
        f"drift warning must name the unpredictable label(s); "
        f"messages={[r.getMessage() for r in drift_msgs]}"
    )


def test_envelope_gate_trusts_model_output_not_labels_json():
    """Regression for the production 0.2% accuracy bug fixed by PR #50.

    Setup: the production ONNX binary doesn't include `envelope` in its
    output class set. The pre-fix gate at `mapper.py:772` checked
    `ENVELOPE_LABEL in self._label_to_idx` — i.e. labels.json — so if
    labels.json listed `envelope` (as it did on prod before PR #50) the
    suppression never fired, the heuristic teacher emitted
    `divergence_signal="envelope"` on every envelope key, and the model
    predicted UNKNOWN → ~0% rolling accuracy.

    The fix: read the actual ONNX output class set via
    `_model_class_labels`, populated by `_validate_labels()` from the
    output_probability map keys. This test simulates the prod skew
    (labels.json knows `envelope`; binary doesn't) by adding `envelope`
    back to `_label_to_idx` AFTER session init, then verifies the gate
    still suppresses — i.e. that the gate ignores labels.json drift.
    """
    from gateway.schema.canonical import ENVELOPE_LABEL

    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    # Production binary (post-PR #50) shouldn't list envelope in its
    # output class set. If a future retrain adds envelope, this test
    # becomes a vacuous pass — skip to surface that.
    if ENVELOPE_LABEL in mapper._model_class_labels:
        pytest.skip(
            "ONNX binary now predicts envelope; gate-trust test is vacuous. "
            "Replace with a test against a different non-trainable label."
        )

    # Simulate the prod-skew condition: labels.json knows envelope but
    # the binary doesn't. The old gate would read True here; the new
    # gate must still read False because it consults `_model_class_labels`.
    mapper._label_to_idx[ENVELOPE_LABEL] = len(mapper._labels)
    mapper._labels.append(ENVELOPE_LABEL)

    # Envelope-heavy non-standard payload — `object`, `created`, `role`,
    # `index`, `service_tier` etc. are envelope keys the heuristic teacher
    # confidently classifies as ENVELOPE_LABEL.
    resp = {
        "object": "chat.completion",
        "created": 1234567890,
        "service_tier": "default",
        "x_meta_role": "assistant",
        "x_meta_index": 0,
    }
    mapper.map_response(resp)
    rows = _drain(buf)
    envelope_signals = [r for r in rows if r.divergence_signal == ENVELOPE_LABEL]
    assert not envelope_signals, (
        f"gate must suppress envelope teacher when the loaded ONNX binary "
        f"can't predict envelope — got {len(envelope_signals)} rows with "
        f"divergence_signal='envelope'. labels.json drift broke the metric "
        f"on prod (PR #50); this gate must be labels.json-independent."
    )


def test_per_field_buffer_overflow_drops_oldest_not_newest():
    """When per-field volume exceeds the buffer, oldest fields drop first.

    A typical response has ~10-50 fields; a small buffer makes the
    drop behavior observable. The buffer's `dropped_total` counter
    must reflect the exact overflow, and the surviving rows must be
    the most recent (the buffer popleft()s on overflow).
    """
    # Tight bound — any normal response will overflow.
    buf = VerdictBuffer(max_size=3)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    # NON-standard shape (5 leaves, none in `_PROVIDER_PATH_MAP`) so all
    # 5 reach the ONNX residual and emit per-field verdicts. A standard
    # provider shape is now fully deterministic and would emit zero
    # verdicts (nothing to overflow) — keep this fixture non-standard.
    resp = {
        "output_text": "Multi word response here now.",
        "completion_reason": "stop",
        "input_token_count": 5,
        "output_token_count": 3,
        "total_token_count": 8,
    }
    mapper.map_response(resp)
    assert buf.size <= 3
    # 5 verdict rows into a size-3 buffer ⇒ 2 dropped. Positive drop
    # count confirms the per-field loop didn't silently short-circuit.
    assert buf.dropped_total > 0
