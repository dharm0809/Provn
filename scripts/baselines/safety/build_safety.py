#!/usr/bin/env python3
"""Reproducible build pipeline for the safety baseline.

Same shape as scripts/baselines/intent/build_intent.py — different data
sources, label space, and synthetic corpus. See mapping.py for the
class-mapping discipline + dataset thresholds.

Quality gates (build fails on violation):
    - macro-F1 on test >= 0.80   (lower than intent because public
                                  toxicity labels are noisier; we want
                                  high precision more than F1)
    - per-class recall >= 0.65
    - INT8 macro-F1 within 1.5pp of FP32
    - ECE <= 0.10
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from datasets import Dataset, load_dataset
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    set_seed,
)

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from mapping import (  # noqa: E402
    CIVIL_AUX_FIELDS,
    CIVIL_PRIMARY_FIELD,
    CIVIL_THRESH,
    CLASSES,
    HSO_MAPPING,
    JAILBREAK_AS,
    LABEL_TO_ID,
    SYNTHETIC,
    TOXIC_CHAT_MAX_SAFE_PER_RUN,
    TOXIGEN_HATE_THRESH,
    TOXIGEN_SAFE_THRESH,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
log = logging.getLogger("build_safety")

SEED = 20260427
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
set_seed(SEED)

DEFAULT_BASE_MODEL = "microsoft/MiniLM-L12-H384-uncased"
MAX_LENGTH = 128
DEFAULT_OUT = HERE / "out"
DEFAULT_BATCH_SIZE = 32
DEFAULT_EPOCHS = 3
DEFAULT_LR = 5e-5

CIVIL_MAX_PER_CLASS = 5000
CIVIL_MAX_SAFE = 4000

MIN_MACRO_F1 = 0.80
MAX_INT8_DELTA = 0.015
MIN_PER_CLASS_RECALL = 0.65
MAX_ECE = 0.10


@dataclass
class LabelledRow:
    text: str
    label: str
    source: str


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_civil_comments(max_rows: int = 200_000) -> list[LabelledRow]:
    log.info("loading google/civil_comments (capped at %d)", max_rows)
    ds = load_dataset("google/civil_comments", split=f"train[:{max_rows}]")
    out: list[LabelledRow] = []
    per_class: dict[str, int] = {c: 0 for c in CLASSES}
    for ex in ds:
        text = (ex.get("text") or "").strip()
        if not text:
            continue
        scores: dict[str, float] = {}
        for field, cls in CIVIL_PRIMARY_FIELD.items():
            scores[cls] = max(scores.get(cls, 0.0), _to_float(ex.get(field, 0.0)))
        best_cls, best_score = max(scores.items(), key=lambda kv: kv[1])
        aux_max = max(_to_float(ex.get(f, 0.0)) for f in CIVIL_AUX_FIELDS)
        chosen: str | None = None
        if best_score >= CIVIL_THRESH:
            chosen = best_cls
        elif aux_max < CIVIL_THRESH:
            chosen = "safe"
        if chosen is None:
            continue
        cap = CIVIL_MAX_SAFE if chosen == "safe" else CIVIL_MAX_PER_CLASS
        if per_class[chosen] >= cap:
            continue
        per_class[chosen] += 1
        out.append(LabelledRow(text=text, label=chosen, source=f"civil:{best_cls if best_score>=CIVIL_THRESH else 'safe'}"))
    log.info("  civil_comments rows: %d (per-class: %s)", len(out), per_class)
    return out


def load_toxigen() -> list[LabelledRow]:
    log.info("loading toxigen/toxigen-data")
    try:
        ds = load_dataset("toxigen/toxigen-data", split="train")
    except Exception as e:
        log.warning("toxigen load failed (%s); skipping", e)
        return []
    out: list[LabelledRow] = []
    for ex in ds:
        text = (ex.get("text") or "").strip()
        score = _to_float(ex.get("toxicity_human") or ex.get("toxicity_ai") or 0.0)
        if not text:
            continue
        if score >= TOXIGEN_HATE_THRESH:
            out.append(LabelledRow(text, "hate_speech", "toxigen:tox"))
        elif score <= TOXIGEN_SAFE_THRESH:
            out.append(LabelledRow(text, "safe", "toxigen:safe"))
    log.info("  toxigen rows: %d", len(out))
    return out


def load_hate_speech_offensive() -> list[LabelledRow]:
    log.info("loading hate_speech_offensive")
    try:
        ds = load_dataset("hate_speech_offensive", split="train")
    except Exception as e:
        log.warning("hate_speech_offensive load failed (%s); skipping", e)
        return []
    out: list[LabelledRow] = []
    for ex in ds:
        text = (ex.get("tweet") or "").strip()
        cls_id = ex.get("class")
        try:
            cls_int = int(cls_id)
        except (TypeError, ValueError):
            continue
        target = HSO_MAPPING.get(cls_int)
        if target is None or not text:
            continue
        out.append(LabelledRow(text, target, "hso"))
    log.info("  hate_speech_offensive rows: %d", len(out))
    return out


def load_toxic_chat() -> list[LabelledRow]:
    log.info("loading lmsys/toxic-chat (toxicchat0124)")
    try:
        ds = load_dataset("lmsys/toxic-chat", "toxicchat0124")
    except Exception as e:
        log.warning("toxic-chat load failed (%s); skipping", e)
        return []
    out: list[LabelledRow] = []
    safe_used = 0
    for split in ("train", "test"):
        if split not in ds:
            continue
        for ex in ds[split]:
            text = (ex.get("user_input") or "").strip()
            if not text:
                continue
            tox = int(_to_float(ex.get("toxicity") or 0))
            jail = int(_to_float(ex.get("jailbreaking") or 0))
            if jail == 1:
                out.append(LabelledRow(text, "dangerous", "toxic-chat:jail"))
            elif tox == 1:
                continue
            else:
                if safe_used >= TOXIC_CHAT_MAX_SAFE_PER_RUN:
                    continue
                safe_used += 1
                out.append(LabelledRow(text, "safe", "toxic-chat:safe"))
    log.info("  toxic-chat rows: %d", len(out))
    return out


def load_prompt_injections() -> list[LabelledRow]:
    out: list[LabelledRow] = []
    for name in ("deepset/prompt-injections", "jackhhao/jailbreak-classification"):
        log.info("loading %s", name)
        try:
            ds = load_dataset(name)
        except Exception as e:
            log.warning("%s load failed (%s); skipping", name, e)
            continue
        for split in ("train", "test"):
            if split not in ds:
                continue
            for ex in ds[split]:
                text = (ex.get("text") or ex.get("prompt") or "").strip()
                if not text:
                    continue
                lbl = ex.get("label") if "label" in ex else ex.get("type")
                pos = lbl in (1, "1", "jailbreak", "injection", True)
                if pos:
                    out.append(LabelledRow(text, JAILBREAK_AS, f"{name}:pos"))
                else:
                    out.append(LabelledRow(text, "safe", f"{name}:neg"))
    log.info("  prompt-injection rows: %d", len(out))
    return out


def load_synthetic() -> list[LabelledRow]:
    return [LabelledRow(t, lbl, "synthetic:gateway") for t, lbl in SYNTHETIC]


def dedupe_and_clean(rows: list[LabelledRow]) -> list[LabelledRow]:
    seen: dict[str, LabelledRow] = {}
    conflict_keys: set[str] = set()
    cleaned = 0
    for r in rows:
        text = (r.text or "").strip()
        if not text:
            continue
        key = text.lower()
        existing = seen.get(key)
        if existing is None:
            seen[key] = LabelledRow(text=text, label=r.label, source=r.source)
        elif existing.label != r.label:
            conflict_keys.add(key)
        else:
            cleaned += 1
    for k in conflict_keys:
        seen.pop(k, None)
    log.info("dedupe: %d unique kept (%d dups merged, %d label-conflict dropped)",
             len(seen), cleaned, len(conflict_keys))
    return list(seen.values())


def balance_to_targets(rows, target_floor: int = 200, target_ceiling: int = 2000) -> list[LabelledRow]:
    """Balance via downsampling big classes + oversampling tiny classes.

    Public toxicity datasets have wildly different per-class densities
    (civil_comments has tens of thousands of safe/hate; we have ~80
    synthetic for criminal/self_harm/child_safety). A relative cap
    collapses the dataset to the smallest class — a fixed floor + ceiling
    keeps all the natural variety in big classes AND ensures every class
    has enough samples to learn from.

    Strategy:
      • class with > target_ceiling rows → random subsample to target_ceiling
      • class with target_floor ≤ rows ≤ target_ceiling → keep as-is
      • class with < target_floor rows → oversample (with replacement) to target_floor

    Oversampling teaches the model the duplicated synthetic strings
    repeatedly — necessary tradeoff for classes with no public-data
    coverage (criminal, self_harm, child_safety).
    """
    rng = random.Random(SEED)
    by_label: dict[str, list[LabelledRow]] = {c: [] for c in CLASSES}
    for r in rows:
        by_label[r.label].append(r)
    out: list[LabelledRow] = []
    for cls, items in by_label.items():
        n = len(items)
        if n == 0:
            log.warning("class-balance: %s has 0 rows; check data sources / mapping",
                        cls)
            continue
        if n > target_ceiling:
            rng.shuffle(items)
            keep = items[:target_ceiling]
            out.extend(keep)
            log.info("class-balance: %s downsampled %d -> %d", cls, n, len(keep))
        elif n < target_floor:
            # Sample WITH replacement until we hit the floor.
            mult = (target_floor + n - 1) // n
            grown = (items * mult)[:target_floor]
            out.extend(grown)
            log.info("class-balance: %s oversampled %d -> %d (factor %.1fx)",
                     cls, n, len(grown), target_floor / n)
        else:
            out.extend(items)
            log.info("class-balance: %s kept %d as-is", cls, n)
    rng.shuffle(out)
    return out


def stratified_split(rows, val_frac=0.1, test_frac=0.1):
    rng = random.Random(SEED)
    by_label: dict[str, list[LabelledRow]] = {c: [] for c in CLASSES}
    for r in rows:
        by_label[r.label].append(r)
    train, val, test = [], [], []
    for cls, items in by_label.items():
        rng.shuffle(items)
        n = len(items)
        n_test = max(1, int(n * test_frac))
        n_val = max(1, int(n * val_frac))
        test.extend(items[:n_test])
        val.extend(items[n_test : n_test + n_val])
        train.extend(items[n_test + n_val :])
    rng.shuffle(train); rng.shuffle(val); rng.shuffle(test)
    return train, val, test


def to_hf_dataset(rows, tokenizer):
    texts = [r.text for r in rows]
    labels = [LABEL_TO_ID[r.label] for r in rows]
    ds = Dataset.from_dict({"text": texts, "label": labels})
    def tok(batch):
        return tokenizer(batch["text"], truncation=True, padding="max_length", max_length=MAX_LENGTH)
    return ds.map(tok, batched=True, desc="tokenizing")


def softmax(logits):
    e = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def expected_calibration_error(probs, labels, n_bins: int = 10) -> float:
    confidences = probs.max(axis=-1)
    predictions = probs.argmax(axis=-1)
    accuracies = (predictions == labels).astype(np.float32)
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        if i < n_bins - 1:
            mask = (confidences >= bins[i]) & (confidences < bins[i + 1])
        else:
            mask = (confidences >= bins[i]) & (confidences <= bins[i + 1])
        if mask.sum() == 0:
            continue
        bin_acc = accuracies[mask].mean()
        bin_conf = confidences[mask].mean()
        ece += (mask.sum() / len(labels)) * abs(bin_acc - bin_conf)
    return float(ece)


def score_predictions(probs, labels) -> dict[str, Any]:
    preds = probs.argmax(axis=-1)
    macro_f1 = f1_score(labels, preds, average="macro")
    micro_f1 = f1_score(labels, preds, average="micro")
    p, r, f1, _ = precision_recall_fscore_support(labels, preds, labels=range(len(CLASSES)), zero_division=0)
    cm = confusion_matrix(labels, preds, labels=range(len(CLASSES))).tolist()
    ece = expected_calibration_error(probs, labels)
    return {
        "macro_f1": float(macro_f1),
        "micro_f1": float(micro_f1),
        "ece": ece,
        "per_class": [
            {"label": CLASSES[i], "precision": float(p[i]), "recall": float(r[i]), "f1": float(f1[i])}
            for i in range(len(CLASSES))
        ],
        "confusion_matrix": cm,
        "report": classification_report(labels, preds, target_names=CLASSES, zero_division=0),
    }


def predict_in_batches(model, tokenizer, rows, device, batch_size=64):
    model.to(device)
    all_probs = []; all_labels = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        enc = tokenizer([r.text for r in chunk], truncation=True, padding="max_length",
                       max_length=MAX_LENGTH, return_tensors="pt").to(device)
        with torch.no_grad():
            logits = model(**enc).logits.cpu().numpy()
        all_probs.append(softmax(logits))
        all_labels.extend(LABEL_TO_ID[r.label] for r in chunk)
    return np.concatenate(all_probs, axis=0), np.array(all_labels)


def export_onnx(ckpt_dir: Path, onnx_dir: Path) -> None:
    from optimum.onnxruntime import ORTModelForSequenceClassification
    log.info("exporting ONNX FP32 -> %s", onnx_dir)
    ort_model = ORTModelForSequenceClassification.from_pretrained(ckpt_dir, export=True)
    ort_model.save_pretrained(onnx_dir)
    AutoTokenizer.from_pretrained(ckpt_dir).save_pretrained(onnx_dir)


def quantize_int8(onnx_dir: Path, int8_dir: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    import shutil
    log.info("quantizing INT8 -> %s", int8_dir)
    quantize_dynamic(model_input=str(onnx_dir / "model.onnx"),
                     model_output=str(int8_dir / "model.onnx"),
                     weight_type=QuantType.QInt8, per_channel=False, reduce_range=False)
    for name in ("tokenizer.json", "tokenizer_config.json", "vocab.txt",
                 "special_tokens_map.json", "config.json"):
        s = onnx_dir / name
        if s.exists():
            shutil.copy2(s, int8_dir / name)


def predict_onnx_in_batches(onnx_path: Path, tokenizer, rows, batch_size=32):
    import onnxruntime as ort
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_names = {inp.name for inp in sess.get_inputs()}
    all_probs = []; all_labels = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        enc = tokenizer([r.text for r in chunk], truncation=True, padding="max_length",
                       max_length=MAX_LENGTH, return_tensors="np")
        feed = {k: v.astype(np.int64) for k, v in enc.items() if k in input_names}
        logits = sess.run(None, feed)[0]
        all_probs.append(softmax(logits))
        all_labels.extend(LABEL_TO_ID[r.label] for r in chunk)
    return np.concatenate(all_probs, axis=0), np.array(all_labels)


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_artifacts(out_dir, onnx_path, ckpt_dir, fp32_metrics, int8_metrics, args, sha) -> None:
    import shutil
    artifacts_dir = out_dir / "release"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, artifacts_dir / "model.onnx")
    src_tok = ckpt_dir / "tokenizer.json"
    if src_tok.exists():
        shutil.copy2(src_tok, artifacts_dir / "tokenizer.json")
    labels = list(CLASSES)
    (artifacts_dir / "model_labels.json").write_text(json.dumps(labels, indent=2))
    card = {
        "model_name": "safety",
        "baseline_version": "baseline-v2.0",
        "produced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_model": args.base_model,
        "torch_version": torch.__version__,
        "device": ("mps" if torch.backends.mps.is_available()
                   else ("cuda" if torch.cuda.is_available() else "cpu")),
        "training": {
            "epochs": args.epochs, "batch_size": args.batch_size,
            "lr": args.lr, "max_length": MAX_LENGTH,
            "label_smoothing": 0.1, "weight_decay": 0.01, "seed": SEED,
        },
        "datasets": [
            {"id": "google/civil_comments", "license": "CC0"},
            {"id": "toxigen/toxigen-data", "license": "MIT"},
            {"id": "hate_speech_offensive", "license": "MIT"},
            {"id": "lmsys/toxic-chat (toxicchat0124)", "license": "CC-BY-NC-4.0"},
            {"id": "deepset/prompt-injections", "license": "Apache-2.0"},
            {"id": "jackhhao/jailbreak-classification", "license": "MIT"},
            {"id": "synthetic:gateway", "license": "internal"},
        ],
        "metrics": {"fp32": fp32_metrics, "int8": int8_metrics},
        "labels": labels,
        "max_length": MAX_LENGTH,
        "model_sha256": sha,
        "model_size_bytes": onnx_path.stat().st_size,
    }
    (artifacts_dir / "model_card.json").write_text(json.dumps(card, indent=2))
    log.info("artifacts written -> %s", artifacts_dir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--lr", type=float, default=DEFAULT_LR)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--no-quantize", action="store_true")
    ap.add_argument("--max-train-rows", type=int, default=0)
    ap.add_argument("--civil-rows", type=int, default=200_000)
    args = ap.parse_args()

    if args.device == "auto":
        if torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"
    else:
        device = args.device
    log.info("device: %s, base model: %s", device, args.base_model)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dir = args.out_dir / "ckpt"
    onnx_dir = args.out_dir / "onnx"
    int8_dir = args.out_dir / "onnx-int8"

    rows: list[LabelledRow] = []
    rows += load_civil_comments(args.civil_rows)
    rows += load_toxigen()
    rows += load_hate_speech_offensive()
    rows += load_toxic_chat()
    rows += load_prompt_injections()
    rows += load_synthetic()
    rows = dedupe_and_clean(rows)
    rows = balance_to_targets(rows, target_floor=400, target_ceiling=2500)
    log.info("post-balance: %d rows total", len(rows))
    if args.max_train_rows:
        random.Random(SEED).shuffle(rows)
        rows = rows[: args.max_train_rows]
        log.info("DEV CAP applied: %d rows", len(rows))
    train, val, test = stratified_split(rows)
    log.info("split: train=%d val=%d test=%d", len(train), len(val), len(test))

    train_class_counts = {c: 0 for c in CLASSES}
    for r in train:
        train_class_counts[r.label] += 1
    log.info("train class balance: %s", train_class_counts)

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    train_ds = to_hf_dataset(train, tokenizer)
    val_ds = to_hf_dataset(val, tokenizer)

    label_id_to_str = {i: c for c, i in LABEL_TO_ID.items()}
    if args.skip_train and (ckpt_dir / "config.json").exists():
        log.info("skip-train: loading existing checkpoint at %s", ckpt_dir)
        model = AutoModelForSequenceClassification.from_pretrained(ckpt_dir)
    else:
        model = AutoModelForSequenceClassification.from_pretrained(
            args.base_model, num_labels=len(CLASSES),
            id2label=label_id_to_str, label2id=LABEL_TO_ID,
        )
        targs = TrainingArguments(
            output_dir=str(ckpt_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            per_device_eval_batch_size=args.batch_size,
            learning_rate=args.lr,
            weight_decay=0.01, warmup_ratio=0.06,
            label_smoothing_factor=0.1,
            eval_strategy="epoch", save_strategy="epoch",
            load_best_model_at_end=True,
            metric_for_best_model="macro_f1", greater_is_better=True,
            logging_steps=50, seed=SEED, data_seed=SEED,
            report_to=[],
            use_mps_device=(device == "mps"),
        )

        def compute_metrics(pred_tuple):
            logits, labels = pred_tuple
            probs = softmax(logits)
            preds = probs.argmax(-1)
            return {"macro_f1": float(f1_score(labels, preds, average="macro"))}

        trainer = Trainer(
            model=model, args=targs,
            train_dataset=train_ds, eval_dataset=val_ds,
            tokenizer=tokenizer, compute_metrics=compute_metrics,
        )
        t0 = time.time()
        trainer.train()
        log.info("training done in %.1f s", time.time() - t0)
        trainer.save_model(str(ckpt_dir))
        tokenizer.save_pretrained(str(ckpt_dir))

    # switch to inference mode (no dropout / batchnorm updates)
    model.train(False)
    log.info("scoring FP32 on test set")
    fp32_probs, fp32_labels = predict_in_batches(model, tokenizer, test, device)
    fp32_metrics = score_predictions(fp32_probs, fp32_labels)
    log.info("FP32 macro-F1=%.4f  ECE=%.4f", fp32_metrics["macro_f1"], fp32_metrics["ece"])
    log.info("\n%s", fp32_metrics["report"])

    onnx_dir.mkdir(parents=True, exist_ok=True)
    int8_dir.mkdir(parents=True, exist_ok=True)
    export_onnx(ckpt_dir, onnx_dir)

    if args.no_quantize:
        final_dir = onnx_dir
        int8_metrics = None
    else:
        quantize_int8(onnx_dir, int8_dir)
        log.info("scoring INT8 on test set")
        int8_probs, int8_labels = predict_onnx_in_batches(int8_dir / "model.onnx", tokenizer, test)
        int8_metrics = score_predictions(int8_probs, int8_labels)
        log.info("INT8 macro-F1=%.4f  ECE=%.4f", int8_metrics["macro_f1"], int8_metrics["ece"])
        final_dir = int8_dir

    fail = []
    if fp32_metrics["macro_f1"] < MIN_MACRO_F1:
        fail.append(f"FP32 macro-F1 {fp32_metrics['macro_f1']:.4f} < {MIN_MACRO_F1}")
    for pc in fp32_metrics["per_class"]:
        if pc["recall"] < MIN_PER_CLASS_RECALL:
            fail.append(f"class {pc['label']} recall {pc['recall']:.4f} < {MIN_PER_CLASS_RECALL}")
    if fp32_metrics["ece"] > MAX_ECE:
        fail.append(f"ECE {fp32_metrics['ece']:.4f} > {MAX_ECE}")
    if int8_metrics is not None:
        delta = fp32_metrics["macro_f1"] - int8_metrics["macro_f1"]
        if delta > MAX_INT8_DELTA:
            fail.append(f"INT8 delta {delta:.4f} > {MAX_INT8_DELTA}")

    final_onnx = final_dir / "model.onnx"
    sha = file_sha256(final_onnx)
    write_artifacts(args.out_dir, final_onnx, ckpt_dir, fp32_metrics, int8_metrics, args, sha)

    if fail:
        log.error("QUALITY GATES FAILED:\n  - %s", "\n  - ".join(fail))
        return 2
    log.info("OK all quality gates passed; artifacts in %s", args.out_dir / "release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
