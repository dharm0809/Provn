#!/usr/bin/env python3
"""Promote the freshly-built intent artifacts into the source tree.

Reads:
    out/release/{model.onnx, tokenizer.json, model_labels.json, model_card.json}

Writes:
    src/gateway/classifier/model.onnx
    src/gateway/classifier/tokenizer.json
    src/gateway/classifier/model_labels.json
    src/gateway/classifier/model_card.json
    src/gateway/intelligence/baselines/manifest.json   (updated sha256/size/version)

Refuses to overwrite if the model_card metrics fall below the same gates
the build pipeline enforces — duplicate gate so even a manual override
still has to pass them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = Path(__file__).parent / "out" / "release"
CLASSIFIER_DIR = REPO_ROOT / "src" / "gateway" / "classifier"
MANIFEST_PATH = REPO_ROOT / "src" / "gateway" / "intelligence" / "baselines" / "manifest.json"

# Same gates as build_intent.py — duplicate-and-don't-trust.
MIN_MACRO_F1 = 0.85
MAX_INT8_DELTA = 0.015
MIN_PER_CLASS_RECALL = 0.70
MAX_ECE = 0.08


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def gate_check(card: dict, allow_skip_int8_gate: bool) -> list[str]:
    fail = []
    metrics = card.get("metrics", {})
    fp32 = metrics.get("fp32") or {}
    int8 = metrics.get("int8") or {}
    if fp32.get("macro_f1", 0) < MIN_MACRO_F1:
        fail.append(f"FP32 macro-F1 {fp32.get('macro_f1')} < {MIN_MACRO_F1}")
    for pc in fp32.get("per_class", []):
        if pc.get("recall", 0) < MIN_PER_CLASS_RECALL:
            fail.append(f"class {pc['label']} recall {pc['recall']} < {MIN_PER_CLASS_RECALL}")
    if fp32.get("ece", 1) > MAX_ECE:
        fail.append(f"ECE {fp32.get('ece')} > {MAX_ECE}")
    if not allow_skip_int8_gate and int8:
        delta = fp32.get("macro_f1", 0) - int8.get("macro_f1", 0)
        if delta > MAX_INT8_DELTA:
            fail.append(f"INT8 delta {delta} > {MAX_INT8_DELTA}")
    return fail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-dir", type=Path, default=RELEASE_DIR)
    ap.add_argument("--force", action="store_true",
                    help="Skip quality-gate guard (use only when retiring an old baseline).")
    ap.add_argument("--skip-int8-gate", action="store_true",
                    help="Allow promoting FP32 even when INT8 delta exceeded the gate.")
    args = ap.parse_args()

    release = args.release_dir
    if not release.exists():
        print(f"release dir not found: {release}", file=sys.stderr)
        return 2
    needed = ("model.onnx", "tokenizer.json", "model_labels.json", "model_card.json")
    for n in needed:
        if not (release / n).exists():
            print(f"missing {n} in release dir", file=sys.stderr)
            return 2

    card = json.loads((release / "model_card.json").read_text())
    if not args.force:
        fails = gate_check(card, allow_skip_int8_gate=args.skip_int8_gate)
        if fails:
            print("QUALITY GATES FAILED — refusing to promote:", file=sys.stderr)
            for f in fails:
                print(f"  - {f}", file=sys.stderr)
            print("\nRe-run with --force only if you understand the risk.", file=sys.stderr)
            return 3

    # Copy artifacts.
    for n in needed:
        src = release / n
        dst = CLASSIFIER_DIR / n
        shutil.copy2(src, dst)
        print(f"copied {n} -> {dst}")

    # Update manifest.json.
    new_sha = file_sha256(CLASSIFIER_DIR / "model.onnx")
    new_size = (CLASSIFIER_DIR / "model.onnx").stat().st_size
    manifest = json.loads(MANIFEST_PATH.read_text())
    intent_entry = manifest.setdefault("baselines", {}).setdefault("intent", {})
    intent_entry["version"] = card.get("baseline_version", "baseline-v2.0")
    intent_entry["sha256"] = new_sha
    intent_entry["size_bytes"] = new_size
    intent_entry["architecture"] = card.get("base_model", "MiniLM-L12-H384")
    intent_entry["parameters"] = "33M"
    intent_entry["task"] = "classification"
    intent_entry["labels_path"] = "classifier/model_labels.json"
    intent_entry["trained_on"] = "clinc150 + banking77 + massive-en + synthetic"
    fp32 = (card.get("metrics") or {}).get("fp32") or {}
    intent_entry["notes"] = (
        f"MiniLM-L12 fine-tuned on hand-mapped public NLU corpora plus "
        f"synthetic gateway prompts. macro-F1={fp32.get('macro_f1', 0):.3f}, "
        f"ECE={fp32.get('ece', 0):.3f}."
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"updated {MANIFEST_PATH}: sha256={new_sha[:12]}.. size={new_size}")

    print("\nDone. Next:")
    print("  1. Local smoke: PYTHONPATH=src .venv/bin/python -c 'from gateway.classifier.unified import SchemaIntelligence; ...'")
    print("  2. rsync to EC2 + restart")
    print(
        f"  3. Existing deployments will see provenance flip to 'trained_local' "
        f"unless their on-disk hash matches the new manifest sha. "
        f"Delete /tmp/walacor-wal-*/models/production/intent.onnx + intent.baseline.json "
        f"on each box to force re-seeding from the new bundled baseline."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
