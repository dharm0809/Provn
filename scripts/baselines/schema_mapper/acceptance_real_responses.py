"""Live acceptance test for schema_mapper baseline-v2.

Runs the exported INT8 ONNX over real provider responses captured from
hitting OpenAI / Anthropic / Ollama directly. Prints per-field predictions
side-by-side with whatever gold spec we have for that provider.

This complements the gate suite (which measures macro-F1 on
synthetic-rename test variants) with the question that actually matters:
"on a fresh response from a live API, does the model assign the right
canonical label to each field?"

CLI:
    python acceptance_real_responses.py
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
from paths import FlatField as BuildFlat  # noqa: E402
from train import _runtime_path_to_build_path, _runtime_to_build_field  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
RESP_DIR = HERE / "out" / "realresp"
ONNX_PATH = HERE / "out" / "onnx" / "schema_mapper.onnx"
TOK_DIR = HERE / "out" / "onnx" / "schema_mapper_tokenizer"
SPECS_DIR = HERE / "data" / "provider_specs"

# Real-response file → which gold spec to compare against
RESPONSE_TO_SPEC = {
    "openai_gpt4omini.json": "openai_chat",
    "anthropic_claude.json": "anthropic_messages",
    "ollama_qwen3.json": "ollama_chat",
    "ollama_native.json": "ollama_generate",
}


def _gold_label(spec_path: str, field_path: str) -> str | None:
    """Best-effort lookup. Real-response paths may differ from gold spec
    paths slightly (e.g. choices[0] indices differ). Try exact match,
    then path with `[0]` indices stripped, then last-segment match."""
    spec_file = SPECS_DIR / f"{spec_path}.json"
    if not spec_file.exists():
        return None
    spec = json.loads(spec_file.read_text())
    for ex in spec.get("examples", []):
        labels = ex.get("expected_labels", {})
        if field_path in labels:
            return labels[field_path]
        # Strip array indices (foo[0].bar → foo.bar) and retry
        import re
        normalized = re.sub(r"\[\d+\]", "", field_path)
        for gp, gl in labels.items():
            if re.sub(r"\[\d+\]", "", gp) == normalized:
                return gl
    return None


def _classify_response(sess, tokenizer, raw: dict) -> list[tuple[str, str, float]]:
    """Returns [(path, predicted_label, confidence), ...] for each field."""
    fields = rt_flatten(raw)
    results = []
    for f in fields:
        bf = _runtime_to_build_field(f)
        text = linearize_field(bf)
        feats = np.asarray(extract_features_v2(f), dtype=np.float32)[None, :]
        enc = tokenizer(text, truncation=True, max_length=MAX_SEQ_LEN,
                        padding="max_length", return_tensors="np")
        out = sess.run(None, {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64),
            "features": feats,
        })
        logits = out[0][0]
        # Softmax for confidence
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        idx = int(probs.argmax())
        results.append((f.path, CANONICAL_LABELS[idx], float(probs[idx])))
    return results


def main() -> None:
    import onnxruntime as ort
    from transformers import AutoTokenizer

    sess = ort.InferenceSession(str(ONNX_PATH), providers=["CPUExecutionProvider"])
    tok = AutoTokenizer.from_pretrained(str(TOK_DIR))

    for resp_file, spec_name in RESPONSE_TO_SPEC.items():
        resp_path = RESP_DIR / resp_file
        if not resp_path.exists():
            print(f"\n--- {resp_file} (MISSING) ---")
            continue
        try:
            raw = json.loads(resp_path.read_text())
        except json.JSONDecodeError as e:
            print(f"\n--- {resp_file} (INVALID JSON: {e}) ---")
            continue

        print(f"\n=== {resp_file} (compare vs gold: {spec_name}) ===")
        preds = _classify_response(sess, tok, raw)
        # Also fetch gold for each path
        match_count = 0
        unknown_match = 0
        labeled_match = 0
        labeled_total = 0
        rows = []
        for path, pred, conf in preds:
            gold = _gold_label(spec_name, path)
            ok = "  " if gold is None else ("✓ " if gold == pred else "✗ ")
            rows.append((ok, path, pred, conf, gold))
            if gold is not None:
                if gold == pred:
                    match_count += 1
                    if gold == "UNKNOWN":
                        unknown_match += 1
                    else:
                        labeled_match += 1
                if gold != "UNKNOWN":
                    labeled_total += 1
        # Print
        for ok, path, pred, conf, gold in rows:
            gold_str = f"gold={gold}" if gold is not None else "gold=N/A"
            print(f"  {ok}{path:55s}  pred={pred:24s}  conf={conf:.3f}  {gold_str}")
        labeled_acc = (labeled_match / labeled_total) if labeled_total else float('nan')
        print(f"\n  fields:           {len(preds)}")
        print(f"  gold-overlap:     {sum(1 for r in rows if r[4] is not None)}")
        print(f"  matches (overall): {match_count}")
        if labeled_total:
            print(f"  labeled-acc (excluding gold=UNKNOWN): {labeled_match}/{labeled_total} = {labeled_acc:.3f}")


if __name__ == "__main__":
    main()
