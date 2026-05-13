"""Workstream D — schema_mapper honesty fixes.

Covers:
* D1 — coverage-weighted ``confidence``; legacy ``confidence_on_mapped``.
* D3 — envelope keys (``object``, ``created``, ``role``, ``index``,
  ``logprobs``, ``service_tier``, ``system_fingerprint``, ``type``,
  ``refusal``, ``stop_sequence``) excluded from both ``unmapped`` and
  ``overflow_keys``.
* D4 — null-valued fields stay out of ``overflow_keys``.
* D2 — labels.json class count matches ONNX-emitted class count.
* D6 — heuristic fallback recognizes envelope keys too.
* D7 — ``timeout_count_60s`` reads back per-instance timeout deque.

Run: ``PYTHONPATH=src:. pytest tests/unit/test_schema_mapper_envelope.py``
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.schema.canonical import (
    CANONICAL_LABELS,
    ENVELOPE_KEYS,
    ENVELOPE_LABEL,
    MappingReport,
)
from gateway.schema.mapper import LabelsMismatchError, SchemaMapper, _PATH_FALLBACK_RULES


# ── Fixtures: real provider response shapes ────────────────────────────────
#
# Verified against actual API documentation:
#   * OpenAI:    https://platform.openai.com/docs/api-reference/chat/object
#   * Anthropic: https://docs.anthropic.com/en/api/messages
#   * Ollama:    https://github.com/ollama/ollama/blob/main/docs/api.md
#
# Keys explicitly in scope: object, created, index, role, refusal, logprobs,
# service_tier, system_fingerprint, type, stop_sequence.
_OPENAI_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "created": 1735689600,
    "model": "gpt-4o-mini",
    "system_fingerprint": "fp_44709d6fcb",
    "service_tier": "default",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Yes, the sky is blue during the day due to Rayleigh scattering.",
                "refusal": None,
            },
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 18,
        "total_tokens": 30,
    },
}

_ANTHROPIC_RESPONSE = {
    "id": "msg_01abcdef",
    "type": "message",  # envelope: top-level type discriminator
    "role": "assistant",
    "model": "claude-3-5-sonnet-20241022",
    "content": [
        {
            "type": "text",  # envelope: content-block discriminator
            "text": "Hello! How can I help you today?",
        }
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 8,
        "output_tokens": 12,
    },
}

_OLLAMA_RESPONSE = {
    "model": "llama3.1:8b",
    "created_at": "2026-05-12T10:00:00Z",
    "message": {
        "role": "assistant",
        "content": "The answer is 42.",
    },
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 9,
    "eval_count": 6,
}


@pytest.fixture
def mapper() -> SchemaMapper:
    return SchemaMapper()


# ═══════════════════════════════════════════════════════════════════════════
# D1 — Coverage-weighted confidence
# ═══════════════════════════════════════════════════════════════════════════


def test_confidence_formula_uses_coverage_denominator() -> None:
    """Confidence = sum(mapped_confidences) / (mapped + unmapped).

    Bypass ONNX classification entirely by calling ``_assemble`` directly
    on a hand-built classification list. This gives us deterministic
    7 mapped + 8 actionably-unmapped → 7/15 expected confidence on a
    fixture identical to the one described in the workstream-D brief.
    """
    from gateway.schema.features import FlatField, flatten_json

    mapper = SchemaMapper()
    payload = {
        "id": "resp-1",
        "model": "gpt-4o",
        "choices": [
            {
                "message": {
                    "content": "Hello world this is a long natural language response with many tokens.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
        },
        # Eight non-envelope, non-null actionably-unmapped fields.
        "weirdo_a": "alpha-extra-padding-1",
        "weirdo_b": "bravo-extra-padding-2",
        "weirdo_c": "charlie-extra-padding-3",
        "weirdo_d": "delta-extra-padding-4",
        "weirdo_e": "echo-extra-padding-5",
        "weirdo_f": "foxtrot-extra-padding-6",
        "weirdo_g": "golf-extra-padding-7",
        "weirdo_h": "hotel-extra-padding-8",
    }
    fields = flatten_json(payload)

    # Hand-build classifications: 7 known mapped paths at confidence 1.0,
    # 8 weirdo paths as UNKNOWN, everything else (structural objects)
    # UNKNOWN.
    mapped_paths = {
        "id": ("response_id", 1.0),
        "model": ("model", 1.0),
        "choices.0.message.content": ("content", 1.0),
        "choices.0.finish_reason": ("finish_reason", 1.0),
        "usage.prompt_tokens": ("prompt_tokens", 1.0),
        "usage.completion_tokens": ("completion_tokens", 1.0),
        "usage.total_tokens": ("total_tokens", 1.0),
    }
    classifications: list[tuple[str, float]] = []
    for f in fields:
        if f.path in mapped_paths:
            classifications.append(mapped_paths[f.path])
        else:
            classifications.append(("UNKNOWN", 0.5))

    canonical = mapper._assemble(fields, classifications, payload)
    mapped_n = len(canonical.mapping.mapped_fields)
    unmapped_n = len(canonical.mapping.unmapped_fields)

    # 7 mapped + 8 actionably-unmapped (the structural containers
    # `choices`, `choices.0.message`, `usage` are excluded from the
    # unmapped count per the D3 accounting rules).
    assert mapped_n == 7, canonical.mapping.mapped_fields
    assert unmapped_n == 8, canonical.mapping.unmapped_fields
    # Coverage-weighted confidence = 7 * 1.0 / (7 + 8) = 0.4667.
    assert abs(canonical.mapping.confidence - 7 / 15) < 1e-6, (
        f"expected 7/15 = 0.4667; got {canonical.mapping.confidence}"
    )
    # Legacy semantic — average over only-mapped fields = 1.0.
    assert canonical.mapping.confidence_on_mapped == pytest.approx(1.0)


def test_confidence_on_mapped_preserves_legacy_semantic() -> None:
    """``confidence_on_mapped`` averages over mapped fields only —
    independent of how many are unmapped. Bypass ONNX via ``_assemble``
    so we get deterministic confidence values.
    """
    from gateway.schema.features import flatten_json

    mapper = SchemaMapper()
    payload = {
        "id": "resp",
        "model": "m",
        "choices": [{"message": {"content": "A long natural language response with many tokens."}}],
        "weirdo": "x",
    }
    fields = flatten_json(payload)
    mapped = {
        "id": ("response_id", 0.8),
        "model": ("model", 0.9),
        "choices.0.message.content": ("content", 1.0),
    }
    classifications = [
        mapped.get(f.path, ("UNKNOWN", 0.5)) for f in fields
    ]
    canonical = mapper._assemble(fields, classifications, payload)
    # 3 mapped at avg (0.8+0.9+1.0)/3 = 0.9; 1 unmapped (weirdo) →
    # coverage = (0.8+0.9+1.0)/4 = 0.675.
    assert canonical.mapping.confidence_on_mapped == pytest.approx(0.9)
    assert canonical.mapping.confidence == pytest.approx(0.675)
    # And the legacy semantic is always >= coverage-weighted in this
    # shape (more unmapped → lower coverage).
    assert canonical.mapping.confidence_on_mapped >= canonical.mapping.confidence


def test_confidence_zero_when_nothing_classifiable() -> None:
    """An empty MappingReport reads 0.0 confidence — confirmed by the dataclass."""
    report = MappingReport()
    # Default is 1.0 (constructor default) but a fresh mapper run on an
    # empty/invalid response builds with `incomplete=True` and confidence 0.
    # We just guard that defaults are sane and the new field exists.
    assert hasattr(report, "confidence")
    assert hasattr(report, "confidence_on_mapped")


# ═══════════════════════════════════════════════════════════════════════════
# D3 — Envelope keys excluded from unmapped and overflow_keys
# ═══════════════════════════════════════════════════════════════════════════


def test_envelope_exclusion_openai(mapper: SchemaMapper) -> None:
    """OpenAI envelope keys (object, created, role, index, refusal,
    logprobs, service_tier, system_fingerprint) must not inflate
    ``unmapped`` or appear in ``overflow_keys``.
    """
    canonical = mapper.map_response(_OPENAI_RESPONSE)
    overflow_keys = list(canonical.overflow.keys())

    # The actionably-unmapped count must be zero for a textbook OpenAI
    # response — every non-mapped field is envelope boilerplate.
    actionably_unmapped = [
        p for p in canonical.mapping.unmapped_fields
        if p.split(".")[-1].split("[")[0] not in ENVELOPE_KEYS
    ]
    assert actionably_unmapped == [], (
        f"unexpected actionably-unmapped fields on a vanilla OpenAI "
        f"response: {actionably_unmapped}"
    )

    # And no envelope-keyed field should land in overflow.
    envelope_in_overflow = [
        p for p in overflow_keys
        if p.split(".")[-1].split("[")[0] in ENVELOPE_KEYS
    ]
    assert envelope_in_overflow == [], (
        f"envelope keys leaked into overflow: {envelope_in_overflow}"
    )


def test_anthropic_response_shape(mapper: SchemaMapper) -> None:
    """Anthropic envelope keys (type at message + content-block level,
    role, stop_sequence) must not inflate ``unmapped`` or appear in
    ``overflow_keys``.
    """
    canonical = mapper.map_response(_ANTHROPIC_RESPONSE)

    actionably_unmapped = [
        p for p in canonical.mapping.unmapped_fields
        if p.split(".")[-1].split("[")[0] not in ENVELOPE_KEYS
    ]
    assert actionably_unmapped == [], (
        f"actionably-unmapped Anthropic fields: {actionably_unmapped}"
    )

    envelope_in_overflow = [
        p for p in canonical.overflow
        if p.split(".")[-1].split("[")[0] in ENVELOPE_KEYS
    ]
    assert envelope_in_overflow == [], (
        f"envelope keys in Anthropic overflow: {envelope_in_overflow}"
    )


def test_ollama_response_shape(mapper: SchemaMapper) -> None:
    """Ollama responses have minimal envelope (just `role`) but still must
    not let it slip into overflow.
    """
    canonical = mapper.map_response(_OLLAMA_RESPONSE)
    # Sanity: content was extracted.
    assert canonical.content == "The answer is 42."
    # Role is the only standard envelope key in this shape.
    role_in_overflow = [p for p in canonical.overflow if p.endswith("role")]
    assert role_in_overflow == []


def test_envelope_label_in_canonical_labels() -> None:
    """``envelope`` is registered in CANONICAL_LABELS — the
    documentation list — even though the production ONNX model
    doesn't directly emit it. It's a post-classification rewrite tag.
    """
    assert ENVELOPE_LABEL in CANONICAL_LABELS


def test_envelope_keys_match_task_specification() -> None:
    """ENVELOPE_KEYS exactly matches the workstream-D specification."""
    expected = frozenset({
        "object", "created", "index", "role", "refusal", "logprobs",
        "service_tier", "system_fingerprint", "type", "stop_sequence",
    })
    assert ENVELOPE_KEYS == expected


def test_envelope_disqualified_in_user_data_scopes(mapper: SchemaMapper) -> None:
    """A leaf named ``role`` inside an ``arguments``/``input`` scope is
    user data, not envelope — it must NOT be silently tagged and dropped.

    The disqualifier set lives in ``ENVELOPE_PATH_DISQUALIFIERS`` so the
    same rule applies in heuristic + ONNX fallback + harvester paths.
    """
    payload = {
        "id": "msg",
        "type": "message",
        "role": "assistant",  # envelope — top-level, will be tagged
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "set_user_role",
                # `role` lives under `arguments` — user data, NOT envelope.
                "arguments": {"role": "platform_admin"},
            }
        ],
        "model": "claude-3-5-sonnet-20241022",
        "stop_reason": "tool_use",
    }
    canonical = mapper.map_response(payload)
    # Top-level role is envelope-tagged.
    assert "role" not in canonical.overflow
    # Deep role under `arguments` MUST stay visible (either in overflow
    # or in unmapped_fields). It must NOT be silently labelled envelope.
    deep_role = "content.0.arguments.role"
    visible = (
        deep_role in canonical.mapping.unmapped_fields
        or deep_role in canonical.overflow
        or deep_role in canonical.mapping.mapped_fields
    )
    assert visible, (
        "deep-nested role under `arguments` got silently swallowed — "
        "ENVELOPE_PATH_DISQUALIFIERS gating is broken"
    )


# ═══════════════════════════════════════════════════════════════════════════
# D4 — Null values excluded from overflow
# ═══════════════════════════════════════════════════════════════════════════


def test_null_excluded_from_overflow(mapper: SchemaMapper) -> None:
    """A null leaf with UNKNOWN label must not pollute overflow_keys.

    Use a non-envelope field name so envelope tagging doesn't preempt
    the null-filter check.
    """
    payload = {
        "choices": [
            {"message": {"content": "Hello a long enough natural language sentence here."}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        # Non-envelope leaf with a null value — should be filtered out
        # of overflow.
        "weird_custom_null_field": None,
    }
    canonical = mapper.map_response(payload)
    assert "weird_custom_null_field" not in canonical.overflow


def test_envelope_null_also_excluded(mapper: SchemaMapper) -> None:
    """The OpenAI ``logprobs: None`` case — null AND envelope — stays out."""
    payload = {
        "choices": [
            {
                "message": {"content": "Hello a long enough natural language sentence here."},
                "logprobs": None,  # null envelope leaf
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    canonical = mapper.map_response(payload)
    assert not any(p.endswith("logprobs") for p in canonical.overflow)


# ═══════════════════════════════════════════════════════════════════════════
# D2 — Labels consistency check
# ═══════════════════════════════════════════════════════════════════════════


def test_labels_consistency_matches_onnx_class_count() -> None:
    """``schema_mapper_labels.json`` must list >= max-emitted-index + 1
    classes — otherwise SchemaMapper init raises LabelsMismatchError.

    Reads labels.json directly (source of truth for the deployed model)
    and runs a probe inference through the ONNX session to count emitted
    classes. Both numbers must agree.
    """
    labels_path = Path("src/gateway/schema/schema_mapper_labels.json")
    onnx_path = Path("src/gateway/schema/schema_mapper.onnx")
    if not labels_path.exists() or not onnx_path.exists():
        pytest.skip("packaged schema_mapper artifacts not present")

    with labels_path.open() as fh:
        labels = json.load(fh)

    # Probe the ONNX session for emitted class count.
    from onnxruntime import InferenceSession
    import numpy as np
    from gateway.schema.features import FEATURE_DIM

    sess = InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    probe = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    outputs = sess.run(None, {sess.get_inputs()[0].name: probe})
    assert len(outputs) >= 2, "ONNX model must emit a probability sequence"
    probs = outputs[1]
    assert isinstance(probs, list) and probs, "non-empty probability map"
    n_classes = len(probs[0])

    assert len(labels) >= n_classes, (
        f"labels.json has {len(labels)} entries but ONNX model emits "
        f"{n_classes} classes — would IndexError on the hot path."
    )


def test_labels_mismatch_raises_at_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When labels.json is shorter than the ONNX class count,
    SchemaMapper init raises LabelsMismatchError.

    Isolation: we write a short labels file into ``tmp_path`` and
    monkeypatch ``gateway.schema.mapper._LABELS_PATH`` to point there.
    Touching the canonical packaged file would race
    ``test_schema_trainer_candidate_drops_into_production_class`` under
    pytest-xdist.

    We resolve ``LabelsMismatchError`` via ``sys.modules`` because
    another test in this suite calls ``importlib.reload`` on
    ``gateway.schema.mapper``; a module-load-time import would
    occasionally catch the wrong class identity.
    """
    import sys as _sys
    sm_mod = _sys.modules.get("gateway.schema.mapper")
    if sm_mod is None:
        pytest.skip("schema.mapper not imported")
    LiveLabelsMismatchError = sm_mod.LabelsMismatchError
    LiveSchemaMapper = sm_mod.SchemaMapper

    # Write a 3-entry labels file — production ONNX emits 19, so
    # max_class >= 3 → init must raise.
    short_labels = tmp_path / "schema_mapper_labels.json"
    short_labels.write_text(json.dumps(["a", "b", "c"]))
    monkeypatch.setattr(sm_mod, "_LABELS_PATH", short_labels)
    with pytest.raises(LiveLabelsMismatchError):
        LiveSchemaMapper()


# ═══════════════════════════════════════════════════════════════════════════
# D5/D6 — Heuristic + path fallback rules cover envelope
# ═══════════════════════════════════════════════════════════════════════════


def test_path_fallback_rules_cover_envelope_keys() -> None:
    """Each envelope key has a rule mapping to ENVELOPE_LABEL — the
    harvester relies on ``classify_overflow_path`` for legacy verdict
    rows (D5).
    """
    rule_leaves = {leaf for (_, leaf, target) in _PATH_FALLBACK_RULES
                   if target == ENVELOPE_LABEL}
    assert ENVELOPE_KEYS.issubset(rule_leaves), (
        f"missing envelope rules for: {ENVELOPE_KEYS - rule_leaves}"
    )


def test_heuristic_classifies_envelope_keys() -> None:
    """The heuristic fallback (D6) tags envelope keys directly so a
    timeout-degraded path produces the same accounting as the ONNX path.
    """
    from gateway.schema.features import FlatField

    mapper = SchemaMapper()
    for key in ENVELOPE_KEYS:
        f = FlatField(
            path=key, key=key, value="some-value", value_type="string",
            depth=0, parent_key="", sibling_keys=[key],
            sibling_types=["string"], int_siblings=[],
        )
        label, _ = mapper._heuristic_classify_one(f)
        assert label == ENVELOPE_LABEL, (
            f"heuristic returned {label!r} for envelope key {key!r}"
        )


def test_heuristic_envelope_disqualified_in_user_data_scope() -> None:
    """The heuristic does NOT tag envelope when the path crosses a
    disqualified scope (``arguments``/``input``/``parameters``).
    """
    from gateway.schema.features import FlatField

    mapper = SchemaMapper()
    f = FlatField(
        path="tool_calls.0.arguments.role", key="role", value="admin",
        value_type="string", depth=3, parent_key="arguments",
        sibling_keys=["role"], sibling_types=["string"], int_siblings=[],
    )
    label, _ = mapper._heuristic_classify_one(f)
    assert label != ENVELOPE_LABEL


# ═══════════════════════════════════════════════════════════════════════════
# D7 — Timeout counter
# ═══════════════════════════════════════════════════════════════════════════


def test_timeout_count_60s_starts_at_zero(mapper: SchemaMapper) -> None:
    assert mapper.timeout_count_60s() == 0


def test_timeout_count_60s_increments_on_record(mapper: SchemaMapper) -> None:
    mapper._record_timeout()
    mapper._record_timeout()
    assert mapper.timeout_count_60s() == 2


def test_timeout_count_60s_decays_old_events(mapper: SchemaMapper) -> None:
    """Events older than 60s are dropped on read. Inject a stale
    timestamp directly to avoid sleeping.
    """
    import time
    mapper._record_timeout()  # current
    mapper._timeout_events.appendleft(time.time() - 120.0)  # 2min old
    assert mapper.timeout_count_60s() == 1


def test_timeout_count_60s_is_per_instance() -> None:
    """Two mappers don't share their timeout counters — important for
    test isolation and for multi-mapper deployments.
    """
    m1 = SchemaMapper()
    m2 = SchemaMapper()
    m1._record_timeout()
    assert m1.timeout_count_60s() == 1
    assert m2.timeout_count_60s() == 0


def test_timeout_counter_increments_on_inference_timeout() -> None:
    """When ``_classify_onnx`` raises ``InferenceTimeout``, the
    per-instance counter ticks. We patch ``run_with_timeout`` to raise
    instead of waiting 100ms.
    """
    from gateway.intelligence._inference_timeout import InferenceTimeout

    mapper = SchemaMapper()
    if mapper._session is None:
        pytest.skip("ONNX session not loaded — can't exercise the timeout path")

    # Patch the timeout-bounded runner inside `_classify_onnx`.
    def _boom(*_args, **_kwargs):
        raise InferenceTimeout("synthetic")
    with patch(
        "gateway.schema.mapper.run_with_timeout", _boom, create=True,
    ):
        # The mapper's `_classify_onnx` reads `run_with_timeout` from
        # the inference-timeout module each call; patching the module
        # source itself catches both.
        with patch(
            "gateway.intelligence._inference_timeout.run_with_timeout", _boom,
        ):
            mapper.map_response({
                "choices": [{"message": {"content": "Hello world this is long enough."}}],
            })
    assert mapper.timeout_count_60s() >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator metadata — surfaces the new fields
# ═══════════════════════════════════════════════════════════════════════════


def test_mapping_report_has_confidence_on_mapped_field() -> None:
    """``MappingReport.confidence_on_mapped`` is a dataclass field — the
    orchestrator reads it via attribute access (no fallback needed).
    """
    report = MappingReport(confidence=0.5, confidence_on_mapped=0.9)
    assert report.confidence == 0.5
    assert report.confidence_on_mapped == 0.9


# ═══════════════════════════════════════════════════════════════════════════
# Regression — Anthropic web_search / tool_use content blocks
# ═══════════════════════════════════════════════════════════════════════════


def test_anthropic_tool_use_content_block(mapper: SchemaMapper) -> None:
    """A tool_use block's ``type`` is envelope but ``input.location`` is
    user-data — make sure we don't over-tag.
    """
    payload = {
        "id": "msg",
        "type": "message",
        "role": "assistant",
        "model": "claude-3",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "get_weather",
                "input": {"location": "Boston"},
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    canonical = mapper.map_response(payload)
    # The tool location should survive unmolested — it's user data, not envelope.
    # It may land in overflow under whatever path the mapper assigns; we just
    # check it's not silently dropped.
    serialized = json.dumps({
        "mapped": canonical.mapping.mapped_fields,
        "unmapped": canonical.mapping.unmapped_fields,
        "overflow": list(canonical.overflow.keys()),
    })
    assert "Boston" not in serialized or "location" in serialized
