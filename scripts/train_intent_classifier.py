"""Train the intent classifier model and export to ONNX.

Usage:
    python scripts/train_intent_classifier.py [--real-data /tmp/training_data.json]

Generates synthetic training data, optionally merges real labeled data,
trains a TF-IDF + LogisticRegression pipeline, converts to ONNX, and
saves to src/gateway/classifier/model.onnx

Requires: scikit-learn, skl2onnx, onnxruntime
    pip install scikit-learn skl2onnx onnxruntime
"""

import argparse
import json
import random
import sys
from pathlib import Path

# ── Synthetic training data ──────────────────────────────────────────

SYNTHETIC = {
    "normal": [
        "What is Python?",
        "Explain quantum computing",
        "Write a poem about love",
        "How do I sort a list in JavaScript?",
        "What is the capital of France?",
        "Tell me a joke",
        "Write a function to calculate fibonacci",
        "What is machine learning?",
        "Explain the difference between TCP and UDP",
        "How does DNS work?",
        "Write a haiku about spring",
        "What is Docker?",
        "Explain REST APIs",
        "How do I use git rebase?",
        "What are design patterns?",
        "Write me a cover letter",
        "Summarize this text",
        "Translate this to Spanish",
        "What is Kubernetes?",
        "How do I center a div in CSS?",
        "Explain recursion to a beginner",
        "What is OAuth?",
        "Write a SQL query to find duplicates",
        "What is the meaning of life?",
        "Help me debug this code",
        "What is React?",
        "Explain microservices architecture",
        "How do async await work in Python?",
        "What is a neural network?",
        "Write a regex to match email addresses",
        "What is Redis used for?",
        "Explain ACID properties in databases",
        "How do I deploy to AWS?",
        "What is GraphQL?",
        "Write a bash script to backup files",
        "Explain the CAP theorem",
        "What is a blockchain?",
        "How do I optimize database queries?",
        "What is WebSocket?",
        "Explain CI/CD pipelines",
        "What is the difference between Python 2 and 3?",
        "How do I handle errors in Go?",
        "What is CORS?",
        "Write a unit test for this function",
        "What is an API gateway?",
        "How do you implement rate limiting?",
        "What is HTTPS?",
        "Explain event-driven architecture",
        "What is serverless computing?",
        "How do I use environment variables?",
    ],
    "web_search": [
        "Search the web for latest Python release",
        "What is the current price of Bitcoin?",
        "Look up the latest news today",
        "Find out the weather in New York right now",
        "What happened in the world today?",
        "Search for FC Barcelona next match",
        "What is the current stock price of Apple?",
        "Find the latest sports scores",
        "Who won the election?",
        "What are today's top headlines?",
        "Search the web for recent AI breakthroughs",
        "What is trending on Twitter right now?",
        "Look up the current exchange rate USD to EUR",
        "Find the latest COVID statistics",
        "What is the current population of Earth?",
        "Search for reviews of iPhone 16",
        "What movies are playing near me?",
        "Find restaurants open now in San Francisco",
        "What is the traffic like right now?",
        "Search for flight prices to London",
        "What is the latest version of Node.js?",
        "Look up recent earthquakes",
        "What events are happening this weekend?",
        "Find the score of last night's game",
        "What is the current interest rate?",
        "Search for the newest Tesla model",
        "What is happening in Ukraine right now?",
        "Find the current gas prices",
        "What are today's deals on Amazon?",
        "Search for upcoming concerts in my area",
        "What is the current time in Tokyo?",
        "Find the latest research on quantum computing",
        "What are the newest features in iOS 20?",
        "Look up the schedule for the World Cup",
        "Search for the best rated restaurants nearby",
        "What is the current temperature outside?",
        "Find out when the next SpaceX launch is",
        "What happened at the Fed meeting today?",
        "Search for real-time crypto market data",
        "What is the current unemployment rate?",
        "Find the latest updates on the Mars mission",
        "What are the trending topics on Reddit?",
        "Search for breaking news",
        "What is the live score of the cricket match?",
        "Find the current air quality index",
        "What movies are coming out this month?",
        "Search for the latest software vulnerabilities",
        "What is the status of my flight?",
        "Find real-time train schedules",
        "What happened in tech news this week?",
    ],
    "system_task": [
        "### Task: Suggest 3-5 relevant follow-up questions or prompts that the user might naturally ask next",
        "### Task: Generate 1-3 broad tags categorizing the main themes of the chat history",
        "### Task: Generate a concise, 3-5 word title with an emoji summarizing the chat topic",
        "### Task: Summarize the conversation in one paragraph",
        "### Task: Extract key topics from the conversation",
        "### Task: Rate the quality of the response",
        "### Task:\nSuggest 3-5 relevant follow-up questions",
        "### Task:\nGenerate 1-3 broad tags categorizing the themes",
        "### Task:\nGenerate a concise title for this conversation",
        "Generate 3 follow-up questions based on the conversation",
        "Suggest tags for this conversation",
        "Generate a title for this chat",
        "### Task: Classify the sentiment of this conversation",
        "### Task: Extract action items from the conversation",
        "### Task: Generate a summary of key decisions",
        "### Task: Identify the main topic discussed",
        "### Task: Rate the helpfulness of the response",
        "### Task: Suggest related topics the user might be interested in",
        "### Task: Generate alternative phrasings for the question",
        "### Task: Identify any unresolved questions in the conversation",
    ],
    "rag": [
        "Based on the following internal documentation, what does the API return?",
        "According to the provided context, how does authentication work?",
        "Given the following document excerpt, summarize the key points",
        "Based on the uploaded PDF, what are the main findings?",
        "Using the reference material below, explain the architecture",
        "From the knowledge base: what is the deployment process?",
        "According to the retrieved documents, what is the policy?",
        "Based on the following context from our internal wiki",
        "Here is the relevant documentation. What does section 3.2 say?",
        "Using the provided search results, answer the question",
        "Based on the following internal documentation:\n---\nARCHITECTURE:",
        "The user uploaded quarterly_report.pdf. Contents: Revenue grew 15%",
        "Reference the following knowledge base articles to answer",
        "Given the context from the retrieved chunks below",
        "Based on the following source documents",
        "According to the internal documentation provided",
        "Using the context window below, answer the question",
        "From the uploaded file, extract the key metrics",
        "Based on the RAG context provided, what is the answer?",
        "Here are the relevant search results from our database",
    ],
    "reasoning": [
        "Think step by step about this math problem",
        "Reason through this logic puzzle carefully",
        "Show your reasoning for each step",
        "Let's think about this carefully before answering",
        "Break down the problem step by step",
        "What is 25 * 47? Show your reasoning",
        "Solve this optimization problem with detailed reasoning",
        "Analyze this argument and identify logical fallacies",
        "Think through the implications of this decision",
        "Reason about the best approach to solve this",
    ],
}


def generate_training_data(samples_per_class: int = 200) -> list[dict]:
    """Generate balanced synthetic training data with augmentation."""
    data = []
    for label, examples in SYNTHETIC.items():
        # Use all examples
        for ex in examples:
            data.append({"prompt": ex, "label": label})

        # Augment to reach target count
        while len([d for d in data if d["label"] == label]) < samples_per_class:
            ex = random.choice(examples)
            # Simple augmentation: lowercase, add prefix, add suffix
            augmented = random.choice([
                ex.lower(),
                f"Please {ex.lower()}",
                f"Can you {ex.lower()}",
                f"I need you to {ex.lower()}",
                f"{ex} Please be concise.",
                f"{ex} Explain in detail.",
                f"Hey, {ex.lower()}",
                f"{ex} Thanks!",
            ])
            data.append({"prompt": augmented, "label": label})

    return data


def merge_real_data(synthetic: list[dict], real_path: str) -> list[dict]:
    """Merge real labeled data from Walacor export."""
    if not Path(real_path).exists():
        return synthetic
    with open(real_path) as f:
        real = json.load(f)
    merged = synthetic + [{"prompt": r["prompt"], "label": r["label"]} for r in real]
    print(f"Merged {len(real)} real records with {len(synthetic)} synthetic")
    return merged


def train_and_export(data: list[dict], output_dir: str):
    """Train TF-IDF + LogisticRegression, export to ONNX."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import classification_report
    import numpy as np

    prompts = [d["prompt"] for d in data]
    labels = [d["label"] for d in data]
    classes = sorted(set(labels))

    print(f"\nTraining on {len(data)} samples, {len(classes)} classes")
    from collections import Counter
    for label, count in Counter(labels).most_common():
        print(f"  {label}: {count}")

    # Build pipeline
    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            max_features=10000,
            ngram_range=(1, 3),
            sublinear_tf=True,
            min_df=2,
        )),
        ("clf", LogisticRegression(
            max_iter=1000,
            C=5.0,
            class_weight="balanced",
            solver="lbfgs",
        )),
    ])

    # Cross-validation
    scores = cross_val_score(pipeline, prompts, labels, cv=5, scoring="accuracy")
    print(f"\nCross-validation accuracy: {scores.mean():.3f} (+/- {scores.std():.3f})")

    # Train on full dataset
    pipeline.fit(prompts, labels)

    # Full report
    preds = pipeline.predict(prompts)
    print(f"\nTraining set report:")
    print(classification_report(labels, preds))

    # Export to ONNX
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from skl2onnx import convert_sklearn
        from skl2onnx.common.data_types import StringTensorType

        onnx_model = convert_sklearn(
            pipeline,
            "intent_classifier",
            initial_types=[("prompt", StringTensorType([None, 1]))],
        )
        onnx_path = str(out / "model.onnx")
        with open(onnx_path, "wb") as f:
            f.write(onnx_model.SerializeToString())
        print(f"\nONNX model saved: {onnx_path} ({Path(onnx_path).stat().st_size / 1024:.0f} KB)")

        # Verify ONNX model
        from onnxruntime import InferenceSession
        sess = InferenceSession(onnx_path)
        test_input = np.array([["What is Python?"]]).reshape(1, 1)
        result = sess.run(None, {"prompt": test_input})
        print(f"ONNX verification: input='What is Python?' → label={result[0][0]}, probs={result[1][0]}")

    except ImportError:
        print("\nskl2onnx not installed — saving as JSON model instead")

    # Save label map
    label_path = str(out / "model_labels.json")
    with open(label_path, "w") as f:
        json.dump(classes, f)
    print(f"Labels saved: {label_path}")

    # Save model params as JSON (portable fallback)
    tfidf = pipeline.named_steps["tfidf"]
    clf = pipeline.named_steps["clf"]
    model_json = {
        "vocabulary": {str(k): int(v) for k, v in tfidf.vocabulary_.items()},
        "idf": tfidf.idf_.tolist(),
        "coefficients": clf.coef_.tolist(),
        "intercept": clf.intercept_.tolist(),
        "classes": classes,
    }
    json_path = str(out / "model_params.json")
    with open(json_path, "w") as f:
        json.dump(model_json, f)
    print(f"JSON model params saved: {json_path} ({Path(json_path).stat().st_size / 1024:.0f} KB)")


def main():
    parser = argparse.ArgumentParser(description="Train intent classifier")
    parser.add_argument("--real-data", default="/tmp/training_data.json",
                        help="Path to real labeled data from Walacor")
    parser.add_argument("--output", default="src/gateway/classifier",
                        help="Output directory for model files")
    parser.add_argument("--samples-per-class", type=int, default=200,
                        help="Minimum synthetic samples per class")
    args = parser.parse_args()

    print("=== Intent Classifier Training ===\n")

    # Generate synthetic + merge real
    synthetic = generate_training_data(args.samples_per_class)
    data = merge_real_data(synthetic, args.real_data)

    random.shuffle(data)

    train_and_export(data, args.output)
    print("\nDone!")


if __name__ == "__main__":
    main()
