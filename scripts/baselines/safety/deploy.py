#!/usr/bin/env python3
"""Promote the freshly-built safety artifacts into the source tree.

Reads:
    out/release/{model.onnx, tokenizer.json, model_labels.json, model_card.json}

Writes:
    src/gateway/content/safety_classifier.onnx
    src/gateway/content/safety_classifier_tokenizer.json
    src/gateway/content/safety_classifier_labels.json
    src/gateway/content/safety_classifier_card.json
    src/gateway/intelligence/baselines/manifest.json (updated)

Same gate discipline as build_safety.py — refuses to overwrite if
gates fail.
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
CONTENT_DIR = REPO_ROOT / "src" / "gateway" / "content"
MANIFEST_PATH = REPO_ROOT / "src" / "gateway" / "intelligence" / "baselines" / "manifest.json"

MIN_MACRO_F1 = 0.50
MAX_INT8_DELTA = 0.015
MIN_PER_CLASS_RECALL = 0.30
MAX_ECE = 0.12


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def gate_check(card: dict, allow_skip_int8: bool) -> list[str]:
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
    if not allow_skip_int8 and int8:
        delta = fp32.get("macro_f1", 0) - int8.get("macro_f1", 0)
        if delta > MAX_INT8_DELTA:
            fail.append(f"INT8 delta {delta} > {MAX_INT8_DELTA}")
    return fail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-dir", type=Path, default=RELEASE_DIR)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-int8-gate", action="store_true")
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
        fails = gate_check(card, args.skip_int8_gate)
        if fails:
            print("QUALITY GATES FAILED — refusing to promote:", file=sys.stderr)
            for f in fails:
                print(f"  - {f}", file=sys.stderr)
            return 3

    # Copy with safety_classifier_* prefix to match the legacy file naming
    # used by safety_classifier.py (which expects safety_classifier.onnx,
    # safety_classifier_labels.json next to it).
    rename_map = {
        "model.onnx":         "safety_classifier.onnx",
        "tokenizer.json":     "safety_classifier_tokenizer.json",
        "model_labels.json":  "safety_classifier_labels.json",
        "model_card.json":    "safety_classifier_card.json",
    }
    for src_name, dst_name in rename_map.items():
        src = release / src_name
        dst = CONTENT_DIR / dst_name
        shutil.copy2(src, dst)
        print(f"copied {src_name} -> {dst}")

    # Update manifest.json safety entry.
    new_sha = file_sha256(CONTENT_DIR / "safety_classifier.onnx")
    new_size = (CONTENT_DIR / "safety_classifier.onnx").stat().st_size
    manifest = json.loads(MANIFEST_PATH.read_text())
    safety_entry = manifest.setdefault("baselines", {}).setdefault("safety", {})
    safety_entry["version"] = card.get("baseline_version", "baseline-v2.0")
    safety_entry["source_path"] = "content/safety_classifier.onnx"
    safety_entry["sha256"] = new_sha
    safety_entry["size_bytes"] = new_size
    safety_entry["architecture"] = card.get("base_model", "MiniLM-L12-H384")
    safety_entry["parameters"] = "33M"
    safety_entry["task"] = "classification"
    safety_entry["labels_path"] = "content/safety_classifier_labels.json"
    safety_entry["trained_on"] = "civil_comments + toxigen + hate_speech_offensive + toxic-chat + prompt-injections + synthetic"
    fp32 = (card.get("metrics") or {}).get("fp32") or {}
    safety_entry["notes"] = (
        f"MiniLM-L12 fine-tuned on multi-source toxicity / jailbreak / synthetic "
        f"corpora. macro-F1={fp32.get('macro_f1', 0):.3f}, ECE={fp32.get('ece', 0):.3f}."
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"updated {MANIFEST_PATH}: sha256={new_sha[:12]}.. size={new_size}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
