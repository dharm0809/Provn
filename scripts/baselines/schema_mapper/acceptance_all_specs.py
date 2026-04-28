"""All-providers acceptance sweep for schema_mapper baseline-v2.

Runs the exported INT8 ONNX over every static gold envelope in
data/provider_specs/ and reports per-provider labeled accuracy.
This complements:
  - the gate suite (synthetic rename variants)
  - acceptance_real_responses.py (4 live API calls)

with a third measurement: every provider gold response, all at once.
"""
from __future__ import annotations

import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))

from canonical_schema import CANONICAL_LABELS  # noqa: E402
from encoder import MAX_SEQ_LEN  # noqa: E402
from gateway.schema.features import extract_features_v2, flatten_json as rt_flatten  # noqa: E402
from linearization import linearize_field  # noqa: E402
from train import _runtime_to_build_field  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
ONNX_PATH = HERE / "out" / "onnx" / "schema_mapper.onnx"
TOK_DIR = HERE / "out" / "onnx" / "schema_mapper_tokenizer"
SPECS_DIR = HERE / "data" / "provider_specs"


def _classify(sess, tok, raw: dict) -> list[tuple[str, str, float]]:
    fields = rt_flatten(raw)
    out_rows = []
    for f in fields:
        bf = _runtime_to_build_field(f)
        text = linearize_field(bf)
        feats = np.asarray(extract_features_v2(f), dtype=np.float32)[None, :]
        enc = tok(text, truncation=True, max_length=MAX_SEQ_LEN,
                  padding="max_length", return_tensors="np")
        out = sess.run(None, {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64),
            "features": feats,
        })
        logits = out[0][0]
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        idx = int(probs.argmax())
        out_rows.append((f.path, CANONICAL_LABELS[idx], float(probs[idx])))
    return out_rows


def main() -> None:
    import onnxruntime as ort
    from transformers import AutoTokenizer

    sess = ort.InferenceSession(str(ONNX_PATH), providers=["CPUExecutionProvider"])
    tok = AutoTokenizer.from_pretrained(str(TOK_DIR))

    spec_files = sorted(SPECS_DIR.glob("*.json"))
    if not spec_files:
        print("No specs found.", file=sys.stderr)
        return

    print(f"{'Provider':<28} {'fields':>8} {'labeled':>8} {'unk-acc':>8} {'lab-acc':>8} {'overall':>8}")
    print("-" * 74)

    totals = {"labeled_match": 0, "labeled_total": 0,
              "unknown_match": 0, "unknown_total": 0,
              "any_match": 0, "any_total": 0}
    failures = []
    for spec_file in spec_files:
        spec = json.loads(spec_file.read_text())
        provider = spec_file.stem
        per_spec = {"labeled_match": 0, "labeled_total": 0,
                    "unknown_match": 0, "unknown_total": 0,
                    "any_match": 0, "any_total": 0,
                    "fields_seen": 0}
        for ex in spec.get("examples", []):
            raw = ex.get("raw")
            labels = ex.get("expected_labels", {})
            preds = _classify(sess, tok, raw)
            for path, pred, conf in preds:
                per_spec["fields_seen"] += 1
                if path not in labels:
                    continue
                gold = labels[path]
                per_spec["any_total"] += 1
                if gold == pred:
                    per_spec["any_match"] += 1
                if gold == "UNKNOWN":
                    per_spec["unknown_total"] += 1
                    if pred == gold:
                        per_spec["unknown_match"] += 1
                else:
                    per_spec["labeled_total"] += 1
                    if pred == gold:
                        per_spec["labeled_match"] += 1
                    else:
                        failures.append((provider, path, pred, gold, conf))

        for k in totals:
            totals[k] += per_spec[k]

        unk_acc = per_spec["unknown_match"] / per_spec["unknown_total"] if per_spec["unknown_total"] else float("nan")
        lab_acc = per_spec["labeled_match"] / per_spec["labeled_total"] if per_spec["labeled_total"] else float("nan")
        any_acc = per_spec["any_match"] / per_spec["any_total"] if per_spec["any_total"] else float("nan")
        print(f"{provider:<28} {per_spec['fields_seen']:>8} "
              f"{per_spec['any_total']:>8} "
              f"{unk_acc:>8.3f} {lab_acc:>8.3f} {any_acc:>8.3f}")

    print("-" * 74)
    unk_acc = totals["unknown_match"] / totals["unknown_total"] if totals["unknown_total"] else float("nan")
    lab_acc = totals["labeled_match"] / totals["labeled_total"] if totals["labeled_total"] else float("nan")
    any_acc = totals["any_match"] / totals["any_total"] if totals["any_total"] else float("nan")
    print(f"{'TOTAL':<28} {'':>8} {totals['any_total']:>8} "
          f"{unk_acc:>8.3f} {lab_acc:>8.3f} {any_acc:>8.3f}")

    if failures:
        print(f"\n{len(failures)} mispredictions:")
        for provider, path, pred, gold, conf in failures:
            print(f"  [{provider}] {path:55s} pred={pred:24s} gold={gold:24s} conf={conf:.3f}")


if __name__ == "__main__":
    main()
