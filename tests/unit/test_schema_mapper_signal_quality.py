"""End-to-end signal quality: per-field producer rows train a usable candidate.

The promotion gate's accuracy floor exists because the training corpus
must actually carry signal. With per-response verdicts (the previous
shape) the trainer fit on a handful of degenerate rows and produced a
candidate that couldn't classify anything. With per-field rows the
candidate should at least achieve > 50% per-class accuracy on its OWN
training set — not because we expect production-quality fits from
synthetic data, but because anything below that means the producer is
emitting garbage signal.

This is the smoking-gun gate: if it goes red after a refactor, we know
the producer or the trainer have drifted apart again.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

from gateway.intelligence.distillation.trainers.schema_trainer import (
    SchemaMapperTrainer,
    _featurize_row,
    _load_production_featurizer,
)
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.schema.mapper import SchemaMapper


def _per_field_corpus(n: int = 20) -> tuple[list[str], list[str]]:
    """Run SchemaMapper on `n` synthetic responses, harvest per-field rows.

    The producer-side change records one verdict row per field with the
    actual 139-d feature vector and a heuristic `divergence_signal`
    teacher label. We drain the buffer and build (X, y) lists in the
    shape the trainer + dataset builder expect.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    fixtures = [
        # OpenAI-shaped — exercises content + token labels.
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"content": "A natural-language reply with several words."},
                    "finish_reason": "stop",
                },
            ],
            "usage": {
                "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
            },
        },
        {
            "id": "chatcmpl-2",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"content": "Another well-formed response with multiple words."},
                    "finish_reason": "stop",
                },
            ],
            "usage": {
                "prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19,
            },
        },
        # Anthropic-shaped.
        {
            "id": "msg_3",
            "model": "claude-3",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Anthropic-shape response with words."}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        # Ollama-shaped.
        {
            "model": "qwen3:4b",
            "message": {"content": "Ollama response carrying real text payload."},
            "done_reason": "stop",
            "prompt_eval_count": 8,
            "eval_count": 6,
        },
    ]
    # Cycle the fixtures so the corpus has the requested size — each
    # fixture flattens to ~6-10 fields, giving 24-40 per-field rows
    # per response.
    for i in range(n):
        mapper.map_response(fixtures[i % len(fixtures)])

    rows = buf.drain(max_batch=10_000)
    X: list[str] = []
    y: list[str] = []
    for row in rows:
        if row.divergence_signal is None:
            continue
        X.append(row.input_features_json)
        y.append(row.divergence_signal)
    return X, y


def test_per_field_rows_carry_enough_signal_to_train(tmp_path):
    """The trainer fits on per-field rows and the candidate predicts > 50%.

    We hold out NO test set — fitting the corpus to itself is a self-
    consistency test, not a generalization test. The point is that
    the trainer can ACTUALLY USE the per-field signal: if X were
    `"{}"` for every row (the pre-rewrite shape), the candidate
    would learn nothing and self-prediction would still be poor.
    """
    X, y = _per_field_corpus(n=24)
    # Need at least 2 distinct classes to fit anything at all.
    if len(set(y)) < 2:
        pytest.skip("not enough class diversity in synthetic corpus")

    trainer = SchemaMapperTrainer()
    trainer.train(X, y, version="quality", candidates_dir=tmp_path)

    candidate_onnx = tmp_path / "schema_mapper-quality.onnx"
    assert candidate_onnx.exists()

    # Score the candidate on its own training rows.
    from onnxruntime import InferenceSession

    session = InferenceSession(
        str(candidate_onnx), providers=["CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name
    feat = _load_production_featurizer()
    labels = feat.labels

    correct_per_class: dict[str, int] = {}
    total_per_class: dict[str, int] = {}
    for x_item, y_label in zip(X, y):
        vec = _featurize_row(x_item, feat.feature_dim).reshape(1, -1).astype(np.float32)
        vec = np.nan_to_num(vec, nan=0.0, posinf=1.0, neginf=-1.0)
        outputs = session.run(None, {input_name: vec})
        idx = int(np.asarray(outputs[0])[0])
        predicted = labels[idx] if 0 <= idx < len(labels) else "UNKNOWN"
        total_per_class[y_label] = total_per_class.get(y_label, 0) + 1
        if predicted == y_label:
            correct_per_class[y_label] = correct_per_class.get(y_label, 0) + 1

    # Per-class accuracy floor — anything below this means the per-field
    # signal didn't actually feed the model, and the gate should
    # legitimately block. With per-field 139-d features and a
    # heuristic teacher signal, we expect >= 0.5 even on the noisiest
    # class.
    for cls, total in total_per_class.items():
        acc = correct_per_class.get(cls, 0) / total
        assert acc >= 0.5, (
            f"per-class accuracy too low: {cls}={acc:.2f} "
            f"({correct_per_class.get(cls, 0)}/{total}). "
            "Indicates per-field signal is degenerate."
        )


def test_legacy_per_response_rows_skip_trainer(tmp_path):
    """A purely-legacy corpus (input_features_json='{}') still trains a
    candidate but produces zero-feature rows; we just want NO crash and
    a non-empty ONNX file.

    This documents that the trainer can ingest legacy rows without
    raising — the dataset builder and the empty-JSON gate inside the
    builder are the actual filters that keep degenerate rows from
    promotion.
    """
    legacy_X = ["{}"] * 6
    legacy_y = ["content", "content", "content",
                "prompt_tokens", "prompt_tokens", "prompt_tokens"]
    SchemaMapperTrainer().train(
        legacy_X, legacy_y, version="legacy", candidates_dir=tmp_path,
    )
    assert (tmp_path / "schema_mapper-legacy.onnx").exists()
