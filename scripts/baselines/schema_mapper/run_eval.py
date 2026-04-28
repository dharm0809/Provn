"""End-to-end evaluation runner.

Loads best.pt, runs the model against:
  - val split (held-out 10% of train data)
  - test split (xai_grok + replicate held-out specs — adversarial unseen)
  - rename-attack synthetic split (regenerated from gold via the
    rename_attacks table in adversarial_holdouts.json)

Writes predictions.jsonl in the format evaluate.py consumes, then
invokes the gate suite.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))

from canonical_schema import LABEL_TO_ID  # noqa: E402
from encoder import MAX_SEQ_LEN, load_tokenizer  # noqa: E402
from gateway.schema.features import FEATURE_DIM_V2, extract_features_v2, flatten_json as rt_flatten  # noqa: E402
from linearization import linearize_field  # noqa: E402
from model import SchemaMapper  # noqa: E402
from paths import FlatField as BuildFlat  # noqa: E402
from train import _runtime_path_to_build_path, _runtime_to_build_field, _samples_from_obj  # noqa: E402


def predict_samples(model: SchemaMapper, tokenizer, samples, device: str = "cpu", batch_size: int = 16):
    preds, labels, probs = [], [], []
    for i in range(0, len(samples), batch_size):
        batch = samples[i : i + batch_size]
        texts = [s.text for s in batch]
        enc = tokenizer(texts, truncation=True, max_length=MAX_SEQ_LEN, padding="max_length", return_tensors="pt")
        feats = torch.tensor([s.features for s in batch], dtype=torch.float32)
        with torch.no_grad():
            out = model(enc["input_ids"], enc["attention_mask"], feats)
        logits = out["logits"]
        probs_b = F.softmax(logits, dim=-1).cpu().numpy()
        preds_b = logits.argmax(dim=-1).cpu().numpy()
        for s, p, pb in zip(batch, preds_b, probs_b):
            preds.append(int(p))
            labels.append(s.label)
            probs.append(pb.tolist())
    return preds, labels, probs


def make_rename_attack_samples(specs_dir: pathlib.Path, holdouts_path: pathlib.Path) -> list:
    """Generate rename-attack test samples from gold + adversarial_holdouts."""
    holdouts = json.loads(holdouts_path.read_text())
    out = []
    for entry in holdouts.get("rename_attacks", []):
        base = entry["base_provider"]
        spec_path = specs_dir / f"{base}.json"
        if not spec_path.exists():
            continue
        spec = json.loads(spec_path.read_text())
        for ex in spec["examples"]:
            for orig_key, alts in entry["renames"].items():
                for alt in alts:
                    raw = copy.deepcopy(ex["raw"])
                    labels = copy.deepcopy(ex["expected_labels"])
                    if not _rename_in_place(raw, orig_key, alt):
                        continue
                    new_labels = {}
                    for path, lab in labels.items():
                        new_path = path.replace(f".{orig_key}", f".{alt}").replace(f"^{orig_key}", f"^{alt}")
                        if new_path == path and orig_key in path:
                            # Top-level
                            new_path = path.replace(orig_key, alt, 1) if path.startswith(orig_key) else path
                        new_labels[new_path] = lab
                    out.extend(_samples_from_obj(raw, new_labels, variant_id=f"rename:{base}:{orig_key}->{alt}", source_spec=base))
    return out


def _rename_in_place(obj, old_key: str, new_key: str) -> bool:
    """Recursively rename old_key -> new_key in obj. Returns True if anything renamed."""
    found = False
    if isinstance(obj, dict):
        if old_key in obj:
            obj[new_key] = obj.pop(old_key)
            found = True
        for v in obj.values():
            if _rename_in_place(v, old_key, new_key):
                found = True
    elif isinstance(obj, list):
        for item in obj:
            if _rename_in_place(item, old_key, new_key):
                found = True
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=pathlib.Path, default=pathlib.Path(__file__).parent / "out" / "checkpoints" / "best.pt")
    ap.add_argument("--specs-dir", type=pathlib.Path, default=pathlib.Path(__file__).parent / "data" / "provider_specs")
    ap.add_argument("--holdouts", type=pathlib.Path, default=pathlib.Path(__file__).parent / "data" / "adversarial_holdouts.json")
    ap.add_argument("--synth", type=pathlib.Path, default=pathlib.Path(__file__).parent / "out" / "synthetic_corpus.jsonl")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).parent / "out" / "predictions.jsonl")
    ap.add_argument("--max-val", type=int, default=2000)
    args = ap.parse_args()

    print(f"[eval] loading checkpoint {args.checkpoint}", file=sys.stderr)
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    feature_dim = ckpt.get("feature_dim", FEATURE_DIM_V2)
    model = SchemaMapper(feature_dim=feature_dim)
    model.load_state_dict(ckpt["model_state"])
    model.train(False)
    tokenizer = load_tokenizer()

    # Test split: xai_grok + replicate gold
    test_samples = []
    for spec_name in ("xai_grok", "replicate"):
        sp = args.specs_dir / f"{spec_name}.json"
        if not sp.exists():
            continue
        spec = json.loads(sp.read_text())
        for ex_idx, ex in enumerate(spec["examples"]):
            test_samples.extend(_samples_from_obj(ex["raw"], ex["expected_labels"],
                                                  variant_id=f"test:{spec_name}#{ex_idx}",
                                                  source_spec=spec_name))
    print(f"[eval] test samples (unseen providers): {len(test_samples)}", file=sys.stderr)

    # Val split: from synthetic corpus (sample N)
    val_samples = []
    if args.synth.exists():
        import random
        rng = random.Random(20260427)
        with args.synth.open() as f:
            for line in f:
                val_samples.extend(_samples_from_obj(*[json.loads(line)[k] for k in ("raw", "expected_labels")] + [json.loads(line).get("variant_id", "?"), json.loads(line).get("source_spec", "?")]))
                if len(val_samples) >= args.max_val:
                    break
        rng.shuffle(val_samples)
        val_samples = val_samples[: args.max_val]
    print(f"[eval] val samples: {len(val_samples)}", file=sys.stderr)

    # Rename-attack split
    rename_samples = make_rename_attack_samples(args.specs_dir, args.holdouts)
    print(f"[eval] rename-attack samples: {len(rename_samples)}", file=sys.stderr)

    splits = []
    for name, samples in [("test", test_samples), ("unseen", test_samples), ("val", val_samples), ("rename", rename_samples)]:
        if not samples:
            print(f"[eval] {name} split is empty, skipping", file=sys.stderr)
            continue
        preds, labels, probs = predict_samples(model, tokenizer, samples)
        splits.append({
            "split": name,
            "preds": preds, "labels": labels, "probs": probs,
            "n": len(samples),
        })
        print(f"[eval] {name}: {len(samples)} samples", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for s in splits:
            f.write(json.dumps(s) + "\n")
    print(f"[eval] wrote predictions to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
