#!/usr/bin/env python3
"""Train the Schema Mapper ONNX model.

Reads training data from data/schema_mapper_training.json,
trains a GradientBoosting classifier, cross-validates, and
exports to ONNX.

Usage:
    python scripts/train_schema_mapper.py

Output:
    src/gateway/schema/schema_mapper.onnx
    src/gateway/schema/schema_mapper_labels.json
"""

import json
import os
import sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    # ── Load training data ───────────────────────────────────────────
    data_path = "data/schema_mapper_training.json"
    if not os.path.exists(data_path):
        print(f"ERROR: {data_path} not found. Run build_mapper_training_data.py first.")
        sys.exit(1)

    with open(data_path) as f:
        samples = json.load(f)

    print(f"Loaded {len(samples)} samples")

    # Extract features and labels
    X = np.array([s["features"] for s in samples], dtype=np.float32)
    labels = [s["label"] for s in samples]

    # Build label encoder
    unique_labels = sorted(set(labels))
    label_to_idx = {l: i for i, l in enumerate(unique_labels)}
    y = np.array([label_to_idx[l] for l in labels])

    print(f"Features: {X.shape[1]} dimensions")
    print(f"Classes: {len(unique_labels)}")
    for i, label in enumerate(unique_labels):
        count = (y == i).sum()
        print(f"  {label:30s} {count:5d} ({100*count/len(y):.1f}%)")

    # ── Handle NaN/Inf ───────────────────────────────────────────────
    X = np.nan_to_num(X, nan=0.0, posinf=1.0, neginf=-1.0)

    # ── Train/test split ─────────────────────────────────────────────
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.ensemble import GradientBoostingClassifier

    clf = GradientBoostingClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.1,
        min_samples_split=5,
        min_samples_leaf=2,
        subsample=0.8,
        random_state=42,
    )

    # ── Cross-validation ─────────────────────────────────────────────
    print("\n=== Cross-Validation ===")
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(clf, X, y, cv=cv, scoring="accuracy")
    print(f"CV Accuracy: {scores.mean():.4f} ± {scores.std():.4f}")
    print(f"Per-fold: {[f'{s:.4f}' for s in scores]}")

    # ── Train on full data ───────────────────────────────────────────
    print("\n=== Training on full dataset ===")
    clf.fit(X, y)
    train_acc = clf.score(X, y)
    print(f"Training accuracy: {train_acc:.4f}")

    # ── Feature importance (top 20) ──────────────────────────────────
    importances = clf.feature_importances_
    top_indices = np.argsort(importances)[::-1][:20]
    print("\nTop 20 features by importance:")
    for i, idx in enumerate(top_indices):
        print(f"  {i+1:2d}. feature_{idx:3d}  importance={importances[idx]:.4f}")

    # ── Per-class accuracy ───────────────────────────────────────────
    from sklearn.metrics import classification_report
    y_pred = clf.predict(X)
    print("\n=== Per-class Performance ===")
    print(classification_report(y, y_pred, target_names=unique_labels, digits=3))

    # ── Export to ONNX ───────────────────────────────────────────────
    print("=== Exporting to ONNX ===")
    try:
        from skl2onnx import convert_sklearn
        from skl2onnx.common.data_types import FloatTensorType

        onnx_model = convert_sklearn(
            clf,
            "schema_mapper",
            initial_types=[("features", FloatTensorType([None, X.shape[1]]))],
            target_opset=12,
        )

        output_dir = os.path.join("src", "gateway", "schema")
        os.makedirs(output_dir, exist_ok=True)

        onnx_path = os.path.join(output_dir, "schema_mapper.onnx")
        with open(onnx_path, "wb") as f:
            f.write(onnx_model.SerializeToString())
        print(f"ONNX model saved: {onnx_path} ({os.path.getsize(onnx_path) / 1024:.1f} KB)")

        # Save labels
        labels_path = os.path.join(output_dir, "schema_mapper_labels.json")
        with open(labels_path, "w") as f:
            json.dump(unique_labels, f, indent=2)
        print(f"Labels saved: {labels_path}")

        # ── Verify ONNX inference ────────────────────────────────────
        import onnxruntime as ort
        session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        input_name = session.get_inputs()[0].name

        # Test on a few samples
        test_X = X[:10]
        onnx_preds = session.run(None, {input_name: test_X})[0]
        sklearn_preds = clf.predict(test_X)

        match = (onnx_preds == sklearn_preds).all()
        print(f"ONNX vs sklearn match: {'PASS' if match else 'FAIL'}")

        # Benchmark inference speed
        import time
        n_iters = 100
        start = time.perf_counter()
        for _ in range(n_iters):
            session.run(None, {input_name: X[:30]})  # 30 fields = typical response
        elapsed = (time.perf_counter() - start) / n_iters * 1000
        print(f"Inference speed: {elapsed:.2f}ms per response (30 fields)")

    except ImportError as e:
        print(f"ONNX export skipped (missing dependency): {e}")
        print("Install: pip install skl2onnx onnxruntime")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
