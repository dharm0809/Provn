"""Tests for the gateway.schema.features v2 additions (Phase 3 Task 3.2).

The plan asserted FEATURE_DIM ≥ 228 against an assumed 200-dim baseline,
but the actual deployed baseline is 139-dim. The meaningful invariant is
the 28-dim DELTA (16 parent-path-hash + 4 sibling-cardinality + 8
value-stats), which is what these tests pin down.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / "src"))

from gateway.schema.features import (  # noqa: E402
    FEATURE_DIM,
    FEATURE_DIM_V2,
    FlatField,
    extract_batch_v2,
    extract_features,
    extract_features_v2,
    flatten_json,
)


def test_v2_dim_is_legacy_plus_28():
    assert FEATURE_DIM_V2 == FEATURE_DIM + 28


def test_legacy_dim_unchanged():
    """Legacy ONNX (200-dim placeholder) must still load — extract_features
    output dimensionality must NOT change with this PR."""
    f = FlatField(
        path="x", key="x", value=1, value_type="int",
        depth=0, parent_key="", sibling_keys=[], sibling_types=[], int_siblings=[],
    )
    assert len(extract_features(f)) == FEATURE_DIM


def test_v2_features_for_token_count_field():
    obj = {"usage": {"prompt_tokens": 64, "completion_tokens": 7, "total_tokens": 71}}
    fields = flatten_json(obj)
    pt = next(f for f in fields if f.path == "usage.prompt_tokens")
    v = extract_features_v2(pt)
    assert len(v) == FEATURE_DIM_V2


def test_v2_value_stats_quantile_thresholds():
    """The quantile-threshold block fires on numeric values."""
    big = FlatField(
        path="usage.total_tokens", key="total_tokens", value=15000, value_type="int",
        depth=1, parent_key="usage",
        sibling_keys=["prompt_tokens", "completion_tokens"],
        sibling_types=["int", "int"], int_siblings=[10, 20, 15000],
    )
    small = FlatField(
        path="x.y", key="y", value=3, value_type="int",
        depth=1, parent_key="x", sibling_keys=[], sibling_types=[], int_siblings=[],
    )
    big_v = extract_features_v2(big)
    small_v = extract_features_v2(small)
    # The last 8 dims are the value-stats block. For 15000:
    # ≥50, ≥100, ≥1000, ≥10000 all True; not power-of-two; not negative; not zero.
    big_stats = big_v[-8:]
    assert big_stats[0] == 1.0  # ≥50
    assert big_stats[1] == 1.0  # ≥100
    assert big_stats[2] == 1.0  # ≥1000
    assert big_stats[3] == 1.0  # ≥10000
    assert big_stats[5] == 0.0  # not negative
    assert big_stats[6] == 0.0  # not zero
    # For 3: only is-zero=0, others mostly false; log-mag positive
    small_stats = small_v[-8:]
    assert small_stats[0] == 0.0
    assert small_stats[3] == 0.0


def test_v2_sibling_cardinality_buckets():
    """The 4-dim block before value-stats. [0, 1, 2-5, 6+]."""
    none_sib = FlatField(
        path="x", key="x", value=1, value_type="int",
        depth=0, parent_key="", sibling_keys=[], sibling_types=[], int_siblings=[],
    )
    many_sib = FlatField(
        path="x", key="x", value=1, value_type="int",
        depth=0, parent_key="",
        sibling_keys=[f"s{i}" for i in range(7)],
        sibling_types=["int"] * 7, int_siblings=[],
    )
    # The 4-dim block ends at index FEATURE_DIM_V2 - 8 (right before value-stats).
    sib_block_none = extract_features_v2(none_sib)[FEATURE_DIM_V2 - 12 : FEATURE_DIM_V2 - 8]
    sib_block_many = extract_features_v2(many_sib)[FEATURE_DIM_V2 - 12 : FEATURE_DIM_V2 - 8]
    assert sib_block_none == [1.0, 0.0, 0.0, 0.0]  # 0 siblings → bucket 0
    assert sib_block_many == [0.0, 0.0, 0.0, 1.0]  # 7 siblings → bucket 6+


def test_extract_batch_v2_round_trip():
    obj = {"id": "abc", "usage": {"prompt_tokens": 10}}
    fields = flatten_json(obj)
    vecs = extract_batch_v2(fields)
    assert all(len(v) == FEATURE_DIM_V2 for v in vecs)
