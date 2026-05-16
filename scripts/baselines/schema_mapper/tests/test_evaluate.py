"""Each evaluate.py gate function has a passing-case + failing-case unit
test, exercised with synthetic logits/labels (no real model needed)."""
from __future__ import annotations

import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import LABEL_TO_ID  # noqa: E402
from evaluate import (  # noqa: E402
    expected_calibration_error,
    gate1_macro_f1,
    gate2_per_class_precision,
    gate3_per_class_recall,
    gate4_int8_delta,
    gate5_ece,
    gate6_adversarial_unseen,
    gate7_latency,
    gate8_bundle_size,
    gate9_rename_attacks,
    gate10_reproducibility,
    macro_f1,
    per_class_prf1,
)


def _perfect_predictions():
    labels = np.arange(20)  # one per canonical label
    return labels.copy(), labels


def _wrong_predictions():
    labels = np.arange(20)
    preds = (labels + 1) % 20
    return preds, labels


def test_macro_f1_perfect_is_one():
    preds, labels = _perfect_predictions()
    assert macro_f1(preds, labels) == 1.0


def test_macro_f1_wrong_is_zero():
    preds, labels = _wrong_predictions()
    assert macro_f1(preds, labels) == 0.0


def test_per_class_prf1_shapes():
    preds, labels = _perfect_predictions()
    out = per_class_prf1(preds, labels)
    for k in ("precision", "recall", "f1", "support"):
        assert out[k].shape == (20,)


def test_gate1_pass_and_fail():
    preds, labels = _perfect_predictions()
    g = gate1_macro_f1(preds, labels)
    assert g.passed and g.value == 1.0
    preds, labels = _wrong_predictions()
    g = gate1_macro_f1(preds, labels)
    assert not g.passed


def test_gate2_pass_and_fail():
    preds, labels = _perfect_predictions()
    assert gate2_per_class_precision(preds, labels).passed
    preds, labels = _wrong_predictions()
    assert not gate2_per_class_precision(preds, labels).passed


def test_gate3_pass_and_fail():
    preds, labels = _perfect_predictions()
    assert gate3_per_class_recall(preds, labels).passed
    preds, labels = _wrong_predictions()
    assert not gate3_per_class_recall(preds, labels).passed


def test_gate4_int8_delta_pass_and_fail():
    assert gate4_int8_delta(0.95, 0.945).passed   # 0.5pt drop
    assert not gate4_int8_delta(0.95, 0.93).passed  # 2pt drop


def test_gate5_ece_pass_and_fail():
    # Perfectly calibrated: confidence == accuracy
    K = 20
    n = 200
    preds = np.zeros(n, dtype=np.int64)
    labels = np.zeros(n, dtype=np.int64)
    probs = np.full((n, K), 1.0 / K)
    probs[:, 0] = 1.0  # all confident in class 0; accuracy = 100%
    g = gate5_ece(probs, labels)
    assert g.passed
    # Mis-calibrated: confidence high but accuracy 0
    labels_wrong = np.full(n, 1, dtype=np.int64)
    g_bad = gate5_ece(probs, labels_wrong)
    assert not g_bad.passed


def test_gate6_unseen_pass_and_fail():
    preds, labels = _perfect_predictions()
    assert gate6_adversarial_unseen(preds, labels).passed
    preds, labels = _wrong_predictions()
    assert not gate6_adversarial_unseen(preds, labels).passed


def test_gate7_latency_pass_and_fail():
    assert gate7_latency(3.5).passed
    assert not gate7_latency(7.0).passed


def test_gate8_bundle_size_pass_and_fail(tmp_path: pathlib.Path):
    small = tmp_path / "a.bin"
    small.write_bytes(b"x" * 1024)
    g = gate8_bundle_size([small])
    assert g.passed
    big = tmp_path / "b.bin"
    big.write_bytes(b"x" * (60 * 1024 * 1024))
    g = gate8_bundle_size([big])
    assert not g.passed


def test_gate9_rename_attacks_pass_and_fail():
    preds, labels = _perfect_predictions()
    assert gate9_rename_attacks(preds, labels).passed
    preds, labels = _wrong_predictions()
    assert not gate9_rename_attacks(preds, labels).passed


def test_gate10_reproducibility_first_build_passes(tmp_path: pathlib.Path):
    p = tmp_path / "ckpt.pt"
    p.write_bytes(b"abcd")
    g = gate10_reproducibility(p, baseline_sha256=None)
    assert g.passed


def test_gate10_reproducibility_mismatch_fails(tmp_path: pathlib.Path):
    p = tmp_path / "ckpt.pt"
    p.write_bytes(b"abcd")
    g = gate10_reproducibility(p, baseline_sha256="zzz")
    assert not g.passed
