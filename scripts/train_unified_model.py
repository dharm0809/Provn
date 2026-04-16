#!/usr/bin/env python3
"""Train the unified intent classifier on real Walacor execution records.

Usage:
    python scripts/train_unified_model.py [--data data/training_records.json]

Takes the exported Walacor records, labels them using the deterministic rules
(which are 100% accurate for detectable signals) plus heuristics for the rest,
then trains a TF-IDF + LogisticRegression pipeline and exports to ONNX.

The resulting model is saved to src/gateway/classifier/model.onnx.
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Intent labels
NORMAL = "normal"
WEB_SEARCH = "web_search"
RAG = "rag"
SYSTEM_TASK = "system_task"
REASONING = "reasoning"
ALL_INTENTS = [NORMAL, WEB_SEARCH, RAG, SYSTEM_TASK, REASONING]


def label_record(record: dict) -> str:
    """Label a record using deterministic rules + heuristics.

    These match the Tier 1 rules in the intent classifier exactly,
    plus additional heuristics based on observed data patterns.
    """
    meta = record.get("metadata", {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, ValueError):
            meta = {}

    prompt = record.get("prompt_text", "") or ""
    model_id = record.get("model_id", "") or ""

    # 1. Explicit intent from metadata (already classified records)
    existing_intent = meta.get("_intent", "")
    if existing_intent and existing_intent != "none" and existing_intent in ALL_INTENTS:
        return existing_intent

    # 2. System task detection
    request_type = meta.get("request_type", "")
    if isinstance(request_type, str) and request_type.startswith("system_task"):
        return SYSTEM_TASK
    if prompt.lstrip().startswith("### Task:"):
        return SYSTEM_TASK

    # 3. Reasoning model
    if model_id and any(model_id.startswith(p) for p in ("o1-", "o1", "o3-", "o3", "o4-", "o4")):
        return REASONING

    # 4. Web search — check for tool events or explicit feature toggle
    audit = meta.get("walacor_audit", {})
    body_meta = meta.get("_body_metadata", {})
    features = body_meta.get("features", {})
    if features.get("web_search") is True:
        return WEB_SEARCH

    # Heuristic: if the prompt asks about current/recent events
    web_patterns = [
        "what is the latest", "current news", "today's", "right now",
        "what happened", "search for", "find information about",
        "who won", "latest updates", "breaking news",
    ]
    prompt_lower = prompt.lower()
    if any(p in prompt_lower for p in web_patterns):
        return WEB_SEARCH

    # 5. RAG detection
    if audit.get("has_rag_context"):
        return RAG
    if audit.get("has_files") or audit.get("file_count", 0) > 0:
        return RAG

    # 6. Thinking model heuristic — models with thinking output
    thinking = record.get("thinking_content")
    if thinking and len(thinking) > 100:
        # Thinking models doing complex reasoning
        if any(kw in prompt_lower for kw in ("explain", "analyze", "compare", "reason", "think through")):
            return REASONING

    # Default
    return NORMAL


def build_training_set(records: list[dict]) -> tuple[list[str], list[str]]:
    """Build (prompts, labels) from exported records."""
    prompts = []
    labels = []
    skipped = 0

    for r in records:
        prompt = r.get("prompt_text", "") or ""
        if not prompt or len(prompt) < 5:
            skipped += 1
            continue

        label = label_record(r)
        prompts.append(prompt[:2000])  # cap prompt length for TF-IDF
        labels.append(label)

    log.info("Training set: %d records, %d skipped (empty prompt)", len(prompts), skipped)
    log.info("Label distribution: %s", dict(Counter(labels)))
    return prompts, labels


def augment_training_data(prompts: list[str], labels: list[str]) -> tuple[list[str], list[str]]:
    """Augment underrepresented classes with synthetic examples.

    Critical for classes with few examples (web_search, reasoning, system_task, rag).
    """
    augmented_prompts = list(prompts)
    augmented_labels = list(labels)

    # Synthetic examples for underrepresented classes
    synthetic = {
        WEB_SEARCH: [
            "What is the current weather in Tokyo?",
            "Search for the latest news about AI regulation",
            "What are today's stock market results?",
            "Find me information about recent SpaceX launches",
            "Who won the latest election?",
            "What happened in the world today?",
            "Current price of Bitcoin",
            "Latest research papers on quantum computing",
            "What are the trending topics on social media?",
            "Search for reviews of the new iPhone",
            "What is the exchange rate for USD to EUR today?",
            "Find recent developments in renewable energy",
            "Who is the current president of France?",
            "What are the top headlines right now?",
            "Search for upcoming concerts in Los Angeles",
            "What is the latest score of the World Cup?",
            "Find information about COVID-19 vaccination rates",
            "What are the newest movies in theaters?",
            "Search for restaurants near Central Park",
            "What is the current status of the Mars rover?",
        ],
        RAG: [
            "Based on the document I uploaded, what are the key findings?",
            "Summarize the attached PDF about machine learning",
            "What does the contract say about termination clauses?",
            "According to the data I provided, what trends do you see?",
            "Review the attached code and suggest improvements",
            "What are the main points from the research paper I shared?",
            "Analyze the spreadsheet data I uploaded",
            "Based on the provided context, answer the following question",
            "From the knowledge base article, explain the process",
            "Using the attached transcript, create meeting notes",
            "What does the legal document say about liability?",
            "Summarize the uploaded financial report",
            "Based on the reference material provided, explain the concept",
            "Review the attached resume and provide feedback",
            "What insights can you draw from this dataset?",
        ],
        SYSTEM_TASK: [
            "### Task: Generate a title for the conversation",
            "### Task: Generate tags for this conversation",
            "### Task: Generate a brief search query for the conversation",
            "### Task: Summarize the conversation in one sentence",
            "### Task: Extract keywords from this conversation",
            "### Task: Rate the sentiment of this conversation",
            "### Task: Classify the topic of this conversation",
            "### Task: Generate follow-up questions",
            "### Task: Create a summary title",
            "### Task: Generate a one-line description",
        ],
        REASONING: [
            "Walk me through solving this differential equation step by step",
            "Analyze the time complexity of this algorithm and explain your reasoning",
            "Think through the ethical implications of autonomous weapons",
            "Compare and contrast the economic theories of Keynes and Friedman",
            "Explain why this mathematical proof is correct or incorrect",
            "Reason about the consequences of climate change on biodiversity",
            "What are the logical fallacies in this argument?",
            "Derive the formula for compound interest from first principles",
            "Think about how quantum entanglement challenges classical physics",
            "Analyze the game theory behind the prisoner's dilemma",
        ],
        NORMAL: [
            "What is photosynthesis?",
            "Write a poem about the ocean",
            "How does a computer work?",
            "Tell me a joke",
            "What is the meaning of life?",
            "Explain quantum computing in simple terms",
            "Write a Python function to sort a list",
            "What is DNA and how does it work?",
            "Describe the water cycle",
            "How do airplanes fly?",
        ],
    }

    # Balance: ensure each class has at least 50 examples
    label_counts = Counter(augmented_labels)
    target_count = max(50, max(label_counts.values()))

    for intent, examples in synthetic.items():
        current = label_counts.get(intent, 0)
        needed = target_count - current
        if needed > 0:
            # Cycle through synthetic examples
            for i in range(needed):
                augmented_prompts.append(examples[i % len(examples)])
                augmented_labels.append(intent)
            log.info("Augmented %s: +%d synthetic (was %d, now %d)", intent, needed, current, current + needed)

    log.info("After augmentation: %d records", len(augmented_prompts))
    log.info("Label distribution: %s", dict(Counter(augmented_labels)))
    return augmented_prompts, augmented_labels


def train_and_export(
    prompts: list[str],
    labels: list[str],
    output_dir: str,
) -> dict:
    """Train TF-IDF + LogisticRegression, export to ONNX."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.pipeline import Pipeline

    # Build sklearn pipeline
    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            max_features=5000,
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=2,
            max_df=0.95,
        )),
        ("clf", LogisticRegression(
            C=1.0,
            max_iter=1000,
            solver="lbfgs",
            class_weight="balanced",
        )),
    ])

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(pipeline, prompts, labels, cv=cv, scoring="accuracy")
    log.info("Cross-validation accuracy: %.3f ± %.3f", scores.mean(), scores.std())

    # Train on full data
    pipeline.fit(prompts, labels)

    # Per-class accuracy
    from sklearn.metrics import classification_report
    y_pred = pipeline.predict(prompts)
    report = classification_report(labels, y_pred, output_dict=True)
    log.info("Training accuracy: %.3f", report["accuracy"])
    for cls in ALL_INTENTS:
        if cls in report:
            log.info("  %s: precision=%.3f recall=%.3f f1=%.3f support=%d",
                     cls, report[cls]["precision"], report[cls]["recall"],
                     report[cls]["f1-score"], report[cls]["support"])

    # Export to ONNX
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import StringTensorType

    onnx_model = convert_sklearn(
        pipeline,
        "intent_classifier",
        initial_types=[("prompt", StringTensorType([None, 1]))],
        target_opset=12,
    )

    onnx_path = output_path / "model.onnx"
    with open(onnx_path, "wb") as f:
        f.write(onnx_model.SerializeToString())
    log.info("ONNX model saved: %s (%.1f KB)", onnx_path, onnx_path.stat().st_size / 1024)

    # Save label map
    labels_path = output_path / "model_labels.json"
    unique_labels = sorted(set(labels))
    labels_path.write_text(json.dumps(unique_labels, indent=2))
    log.info("Labels saved: %s", labels_path)

    # Save training metadata
    meta_path = output_path / "model_params.json"
    meta_path.write_text(json.dumps({
        "model_type": "tfidf_logreg",
        "features": 5000,
        "ngrams": "1-2",
        "classes": unique_labels,
        "training_samples": len(prompts),
        "cv_accuracy": float(scores.mean()),
        "cv_std": float(scores.std()),
        "training_accuracy": float(report["accuracy"]),
    }, indent=2))

    # Verify ONNX model works
    from onnxruntime import InferenceSession
    session = InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    test_input = np.array([["What is AI?"]]).reshape(1, 1)
    test_out = session.run(None, {"prompt": test_input})
    log.info("ONNX verification: input='What is AI?' → intent=%s, confidence=%.3f",
             test_out[0][0], max(test_out[1][0].values()))

    return {
        "onnx_path": str(onnx_path),
        "cv_accuracy": float(scores.mean()),
        "training_accuracy": float(report["accuracy"]),
        "samples": len(prompts),
        "classes": unique_labels,
    }


def main():
    parser = argparse.ArgumentParser(description="Train unified intent classifier")
    parser.add_argument("--data", default="data/training_records.json")
    parser.add_argument("--output", default="src/gateway/classifier")
    args = parser.parse_args()

    # Load training data
    data_path = Path(args.data)
    if not data_path.exists():
        log.error("Training data not found: %s", data_path)
        log.error("Run: python scripts/export_training_data.py first")
        sys.exit(1)

    data = json.loads(data_path.read_text())
    records = data["executions"]
    log.info("Loaded %d execution records from %s", len(records), data_path)

    # Build training set from real data
    prompts, labels = build_training_set(records)

    # Augment with synthetic examples for class balance
    prompts, labels = augment_training_data(prompts, labels)

    # Train and export
    result = train_and_export(prompts, labels, args.output)

    log.info("\n=== Training Complete ===")
    log.info("Model: %s", result["onnx_path"])
    log.info("CV Accuracy: %.1f%%", result["cv_accuracy"] * 100)
    log.info("Training Accuracy: %.1f%%", result["training_accuracy"] * 100)
    log.info("Classes: %s", result["classes"])
    log.info("Samples: %d", result["samples"])


if __name__ == "__main__":
    main()
