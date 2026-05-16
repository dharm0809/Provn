"""Run the exported INT8 ONNX over the test split and emit macro-F1.

Used to populate gate 4 (INT8 macro-F1 delta vs FP32 ≤ 1pt). Mirrors the
test-split sample construction in run_eval.py so the FP32-vs-INT8 numbers
are comparable on the same inputs.

CLI:
    python eval_onnx_int8.py \\
        --onnx out/onnx/schema_mapper.onnx \\
        --tokenizer out/onnx/schema_mapper_tokenizer

Prints `int8_macro_f1=<float>` on stdout for downstream wiring.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))

from encoder import MAX_SEQ_LEN  # noqa: E402
from run_eval import make_test_synth_samples  # noqa: E402
from train import _samples_from_obj  # noqa: E402


def collect_test_samples(specs_dir: pathlib.Path, vocab_path: pathlib.Path,
                          n_per_provider: int = 250) -> list:
    """Test set: 33 raw gold + the synth_eval half of test_synth (odd
    variant_idx). Mirrors run_eval.py so gate 4 (INT8 vs FP32 delta) is
    measured on the same set as gate 1 (macro-F1)."""
    samples = []
    for spec_name in ("xai_grok", "replicate"):
        sp = specs_dir / f"{spec_name}.json"
        if not sp.exists():
            continue
        spec = json.loads(sp.read_text())
        for ex_idx, ex in enumerate(spec["examples"]):
            samples.extend(_samples_from_obj(
                ex["raw"], ex["expected_labels"],
                variant_id=f"test:{spec_name}#{ex_idx}",
                source_spec=spec_name,
            ))
    synth_all = make_test_synth_samples(specs_dir, vocab_path, n_per_provider=n_per_provider)
    def _vidx(s) -> int:
        try:
            return int(s.variant_id.rsplit("#", 1)[-1])
        except (ValueError, AttributeError):
            return 0
    samples.extend([s for s in synth_all if _vidx(s) % 2 == 1])
    return samples


def macro_f1(preds: np.ndarray, labels: np.ndarray) -> float:
    classes = np.unique(np.concatenate([preds, labels]))
    f1s = []
    for c in classes:
        tp = int(((preds == c) & (labels == c)).sum())
        fp = int(((preds == c) & (labels != c)).sum())
        fn = int(((preds != c) & (labels == c)).sum())
        if tp + fp + fn == 0:
            continue
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
        f1s.append(f1)
    return float(np.mean(f1s)) if f1s else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "out" / "onnx" / "schema_mapper.onnx")
    ap.add_argument("--tokenizer", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "out" / "onnx" / "schema_mapper_tokenizer")
    ap.add_argument("--specs-dir", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "data" / "provider_specs")
    ap.add_argument("--vocab", type=pathlib.Path,
                    default=pathlib.Path(__file__).parent / "data" / "teacher_rename_vocab.json")
    ap.add_argument("--n-per-provider", type=int, default=250)
    args = ap.parse_args()

    import onnxruntime as ort
    from transformers import AutoTokenizer

    sess = ort.InferenceSession(str(args.onnx), providers=["CPUExecutionProvider"])
    tok = AutoTokenizer.from_pretrained(str(args.tokenizer))

    samples = collect_test_samples(args.specs_dir, args.vocab, args.n_per_provider)
    if not samples:
        print("[fatal] no test samples found", file=sys.stderr)
        sys.exit(1)
    print(f"[int8-eval] {len(samples)} test samples", file=sys.stderr)

    preds = []
    labels = []
    # Static-shape ONNX → batch=1 per call. Test set is ~33 samples; latency is fine.
    for s in samples:
        enc = tok(s.text, truncation=True, max_length=MAX_SEQ_LEN,
                  padding="max_length", return_tensors="np")
        feats = np.asarray(s.features, dtype=np.float32)[None, :]
        out = sess.run(None, {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64),
            "features": feats,
        })
        logits = out[0]
        preds.append(int(logits.argmax(axis=-1)[0]))
        labels.append(int(s.label))

    f1 = macro_f1(np.asarray(preds), np.asarray(labels))
    print(f"int8_macro_f1={f1:.6f}")


if __name__ == "__main__":
    main()
