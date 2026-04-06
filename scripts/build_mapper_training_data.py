#!/usr/bin/env python3
"""Build training data for the Schema Mapper ONNX model.

Parses real LLM API response examples from all 22 provider formats,
flattens them, labels each field with its canonical name, and augments
with synthetic variations. Outputs a training-ready JSON file.

Usage:
    python scripts/build_mapper_training_data.py

Output:
    data/schema_mapper_training.json
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.gateway.schema.features import flatten_json, extract_features, FlatField

# ── Real provider response examples with labels ─────────────────────────────
# Each example: (provider_name, response_json, {path: canonical_label})

PROVIDER_EXAMPLES = [
    # ── 1. OpenAI Chat Completions ──────────────────────────────────────
    ("openai", {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1677858242,
        "model": "gpt-4o-2024-08-06",
        "system_fingerprint": "fp_44709d6fcb",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello! How can I help you today? I am ready to assist with whatever you need.",
                "refusal": None,
            },
            "logprobs": None,
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 12,
            "total_tokens": 21,
        }
    }, {
        "id": "response_id", "object": "UNKNOWN", "created": "UNKNOWN",
        "model": "model", "system_fingerprint": "model_hash",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN",
        "choices.0.message.content": "content",
        "choices.0.message.refusal": "UNKNOWN",
        "choices.0.logprobs": "UNKNOWN",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── 2. OpenAI with tool calls ───────────────────────────────────────
    ("openai_tools", {
        "id": "chatcmpl-tool123",
        "object": "chat.completion",
        "model": "gpt-4o",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Boston"}'
                    }
                }]
            },
            "finish_reason": "tool_calls"
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}
    }, {
        "id": "response_id", "object": "UNKNOWN", "model": "model",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN", "choices.0.message.content": "content",
        "choices.0.message.tool_calls": "UNKNOWN",
        "choices.0.message.tool_calls.0.id": "tool_call_id",
        "choices.0.message.tool_calls.0.type": "tool_call_type",
        "choices.0.message.tool_calls.0.function": "UNKNOWN",
        "choices.0.message.tool_calls.0.function.name": "tool_call_name",
        "choices.0.message.tool_calls.0.function.arguments": "tool_call_arguments",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── 3. Anthropic Messages API ───────────────────────────────────────
    ("anthropic", {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "content": [{
            "type": "text",
            "text": "Here is a thoughtful response to your question about machine learning."
        }],
        "usage": {
            "input_tokens": 25,
            "output_tokens": 150,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    }, {
        "id": "response_id", "type": "UNKNOWN", "role": "UNKNOWN",
        "model": "model", "stop_reason": "finish_reason", "stop_sequence": "UNKNOWN",
        "content": "UNKNOWN",
        "content.0.type": "UNKNOWN", "content.0.text": "content",
        "usage": "UNKNOWN",
        "usage.input_tokens": "prompt_tokens",
        "usage.output_tokens": "completion_tokens",
        "usage.cache_creation_input_tokens": "cache_creation_tokens",
        "usage.cache_read_input_tokens": "cached_tokens",
    }),

    # ── 4. Google Gemini ────────────────────────────────────────────────
    ("gemini", {
        "candidates": [{
            "content": {
                "parts": [{"text": "The answer to your question about physics is fascinating. Let me explain in detail."}],
                "role": "model"
            },
            "finishReason": "STOP",
            "safetyRatings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "NEGLIGIBLE"}]
        }],
        "usageMetadata": {
            "promptTokenCount": 14,
            "candidatesTokenCount": 234,
            "totalTokenCount": 248,
        },
        "modelVersion": "gemini-1.5-flash"
    }, {
        "candidates": "UNKNOWN",
        "candidates.0.content": "UNKNOWN",
        "candidates.0.content.parts": "UNKNOWN",
        "candidates.0.content.parts.0.text": "content",
        "candidates.0.content.role": "UNKNOWN",
        "candidates.0.finishReason": "finish_reason",
        "candidates.0.safetyRatings": "safety_category",
        "usageMetadata": "UNKNOWN",
        "usageMetadata.promptTokenCount": "prompt_tokens",
        "usageMetadata.candidatesTokenCount": "completion_tokens",
        "usageMetadata.totalTokenCount": "total_tokens",
        "modelVersion": "model",
    }),

    # ── 5. Ollama native /api/chat ──────────────────────────────────────
    ("ollama_native", {
        "model": "qwen3:4b",
        "created_at": "2024-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "Python is a versatile programming language known for its readability and extensive libraries."
        },
        "done": True,
        "done_reason": "stop",
        "total_duration": 5000000000,
        "load_duration": 100000000,
        "prompt_eval_count": 18,
        "prompt_eval_duration": 500000000,
        "eval_count": 45,
        "eval_duration": 4000000000,
    }, {
        "model": "model", "created_at": "UNKNOWN",
        "message": "UNKNOWN",
        "message.role": "UNKNOWN", "message.content": "content",
        "done": "UNKNOWN", "done_reason": "finish_reason",
        "total_duration": "timing_value", "load_duration": "timing_value",
        "prompt_eval_count": "prompt_tokens",
        "prompt_eval_duration": "timing_value",
        "eval_count": "completion_tokens",
        "eval_duration": "timing_value",
    }),

    # ── 6. Ollama OpenAI-compat ─────────────────────────────────────────
    ("ollama_compat", {
        "id": "chatcmpl-913",
        "object": "chat.completion",
        "created": 1775228794,
        "model": "qwen3:4b",
        "system_fingerprint": "fp_ollama",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Paris is the capital of France.",
                "reasoning": "The user is asking about the capital of France. I should give a direct answer."
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 22, "completion_tokens": 167, "total_tokens": 189}
    }, {
        "id": "response_id", "object": "UNKNOWN", "created": "UNKNOWN",
        "model": "model", "system_fingerprint": "model_hash",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN",
        "choices.0.message.content": "content",
        "choices.0.message.reasoning": "thinking_content",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── 7. Cohere v2 ───────────────────────────────────────────────────
    ("cohere", {
        "id": "cmpl-abc",
        "finish_reason": "COMPLETE",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Cohere models can help with various NLP tasks."}],
            "tool_plan": "I should search for the latest information before answering."
        },
        "usage": {
            "billed_units": {"input_tokens": 20, "output_tokens": 40},
            "tokens": {"input_tokens": 25, "output_tokens": 45}
        }
    }, {
        "id": "response_id", "finish_reason": "finish_reason",
        "message": "UNKNOWN",
        "message.role": "UNKNOWN",
        "message.content": "UNKNOWN",
        "message.content.0.type": "UNKNOWN", "message.content.0.text": "content",
        "message.tool_plan": "thinking_content",
        "usage": "UNKNOWN",
        "usage.billed_units": "UNKNOWN",
        "usage.billed_units.input_tokens": "UNKNOWN",
        "usage.billed_units.output_tokens": "UNKNOWN",
        "usage.tokens": "UNKNOWN",
        "usage.tokens.input_tokens": "prompt_tokens",
        "usage.tokens.output_tokens": "completion_tokens",
    }),

    # ── 8. DeepSeek ─────────────────────────────────────────────────────
    ("deepseek", {
        "id": "chatcmpl-ds123",
        "object": "chat.completion",
        "model": "deepseek-reasoner",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "The answer is 42.",
                "reasoning_content": "Let me think step by step. First, I need to analyze the question..."
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 200,
            "total_tokens": 230,
            "prompt_cache_hit_tokens": 10,
        }
    }, {
        "id": "response_id", "object": "UNKNOWN", "model": "model",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN",
        "choices.0.message.content": "content",
        "choices.0.message.reasoning_content": "thinking_content",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "usage.prompt_cache_hit_tokens": "cached_tokens",
    }),

    # ── 9. HuggingFace TGI ─────────────────────────────────────────────
    ("huggingface", {
        "generated_text": "Machine learning involves training models on data to make predictions and decisions.",
        "details": {
            "finish_reason": "eos_token",
            "generated_tokens": 42,
            "seed": None,
        }
    }, {
        "generated_text": "content",
        "details": "UNKNOWN",
        "details.finish_reason": "finish_reason",
        "details.generated_tokens": "completion_tokens",
        "details.seed": "UNKNOWN",
    }),

    # ── 10. AWS Bedrock Titan ───────────────────────────────────────────
    ("bedrock_titan", {
        "inputTextTokenCount": 15,
        "results": [{
            "tokenCount": 88,
            "outputText": "Amazon Titan is a family of foundation models available through AWS Bedrock.",
            "completionReason": "FINISHED"
        }]
    }, {
        "inputTextTokenCount": "prompt_tokens",
        "results": "UNKNOWN",
        "results.0.tokenCount": "completion_tokens",
        "results.0.outputText": "content",
        "results.0.completionReason": "finish_reason",
    }),

    # ── 11. Replicate ───────────────────────────────────────────────────
    ("replicate", {
        "id": "pred-abc123",
        "model": "meta/llama-2-70b-chat",
        "version": "sha256:abc123",
        "output": "Llama 2 is a large language model developed by Meta AI for research and commercial use.",
        "status": "succeeded",
        "metrics": {"predict_time": 4.2},
    }, {
        "id": "response_id", "model": "model", "version": "model_hash",
        "output": "content", "status": "finish_reason",
        "metrics": "UNKNOWN", "metrics.predict_time": "timing_value",
    }),

    # ── 12. Perplexity ──────────────────────────────────────────────────
    ("perplexity", {
        "id": "pplx-abc",
        "model": "sonar-pro",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "According to recent research, quantum computing has made significant advances."},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 50, "total_tokens": 70},
        "citations": ["https://arxiv.org/abs/2024.001", "https://nature.com/articles/quantum-2024"],
    }, {
        "id": "response_id", "model": "model", "object": "UNKNOWN",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "citations": "citation_url",
    }),

    # ── 13. Groq ────────────────────────────────────────────────────────
    ("groq", {
        "id": "chatcmpl-groq123",
        "object": "chat.completion",
        "model": "llama-3.1-70b-versatile",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Groq provides ultra-fast inference using custom LPU hardware."},
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40,
            "queue_time": 0.001, "prompt_time": 0.05, "completion_time": 0.15,
        },
        "x_groq": {"id": "req_groq_abc"},
    }, {
        "id": "response_id", "object": "UNKNOWN", "model": "model",
        "choices": "UNKNOWN",
        "choices.0.index": "UNKNOWN", "choices.0.message": "UNKNOWN",
        "choices.0.message.role": "UNKNOWN",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage": "UNKNOWN",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "usage.queue_time": "timing_value",
        "usage.prompt_time": "timing_value",
        "usage.completion_time": "timing_value",
        "x_groq": "UNKNOWN", "x_groq.id": "UNKNOWN",
    }),
]

# ── Augmentation ─────────────────────────────────────────────────────────────

_RANDOM_KEY_PARTS = [
    "response", "answer", "reply", "result", "output", "generated",
    "input", "prompt", "query", "request",
    "token", "count", "num", "total", "sum",
    "text", "content", "message", "data",
    "reason", "status", "state", "done", "finish", "complete",
    "time", "duration", "latency", "elapsed",
    "model", "version", "id", "name",
    "cache", "cached", "hit",
    "think", "reason", "chain", "thought", "plan",
]


def _random_rename(key: str) -> str:
    """Generate a plausible alternative field name."""
    tokens = key.replace("_", " ").split()
    # Randomly apply transformations
    r = random.random()
    if r < 0.3:
        # camelCase
        return tokens[0] + "".join(t.title() for t in tokens[1:])
    elif r < 0.5:
        # Add random prefix/suffix
        extra = random.choice(_RANDOM_KEY_PARTS)
        return f"{extra}_{key}" if random.random() < 0.5 else f"{key}_{extra}"
    elif r < 0.7:
        # Swap synonyms
        synonyms = {
            "prompt": ["input", "query", "request"],
            "completion": ["output", "generated", "response"],
            "tokens": ["count", "num", "toks"],
            "content": ["text", "message", "body", "answer"],
            "reason": ["status", "cause", "state"],
        }
        result = []
        for t in tokens:
            if t in synonyms:
                result.append(random.choice(synonyms[t]))
            else:
                result.append(t)
        return "_".join(result)
    return key


def augment_example(response: dict, labels: dict, n_variants: int = 5) -> list[tuple[dict, dict]]:
    """Generate synthetic variants of a provider response."""
    variants = []
    for _ in range(n_variants):
        new_resp = {}
        new_labels = {}
        for key, value in response.items():
            if random.random() < 0.3:
                new_key = _random_rename(key)
            else:
                new_key = key
            new_resp[new_key] = _mutate_value(value)
            # Map labels: original path → new path
            for orig_path, label in labels.items():
                if orig_path == key or orig_path.startswith(key + "."):
                    new_path = orig_path.replace(key, new_key, 1)
                    new_labels[new_path] = label
        variants.append((new_resp, new_labels))
    return variants


def _mutate_value(value):
    """Slightly mutate a value while preserving type and semantics."""
    if isinstance(value, int):
        return max(0, value + random.randint(-5, 50))
    if isinstance(value, float):
        return max(0.0, value + random.uniform(-1, 5))
    if isinstance(value, str) and len(value) > 20:
        # Vary length of long strings
        cut = random.randint(len(value) // 2, len(value))
        return value[:cut] + "..."
    if isinstance(value, dict):
        return {k: _mutate_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mutate_value(v) for v in value]
    return value


# ── Main ─────────────────────────────────────────────────────────────────────

def build_training_data() -> list[dict]:
    """Build labeled training samples from all provider examples + augmentation."""
    samples = []

    for provider, response, labels in PROVIDER_EXAMPLES:
        # Original example
        samples.extend(_extract_samples(provider, response, labels))

        # Augmented variants
        for aug_resp, aug_labels in augment_example(response, labels, n_variants=8):
            samples.extend(_extract_samples(f"{provider}_aug", aug_resp, aug_labels))

    random.shuffle(samples)
    return samples


def _extract_samples(provider: str, response: dict, labels: dict) -> list[dict]:
    """Flatten a response and create labeled samples."""
    fields = flatten_json(response)
    samples = []
    for f in fields:
        label = labels.get(f.path, "UNKNOWN")
        features = extract_features(f)
        samples.append({
            "provider": provider,
            "path": f.path,
            "key": f.key,
            "value_type": f.value_type,
            "label": label,
            "features": features,
        })
    return samples


if __name__ == "__main__":
    random.seed(42)
    os.makedirs("data", exist_ok=True)

    samples = build_training_data()
    print(f"Total samples: {len(samples)}")

    # Label distribution
    label_counts = {}
    for s in samples:
        label_counts[s["label"]] = label_counts.get(s["label"], 0) + 1
    print("\nLabel distribution:")
    for label, count in sorted(label_counts.items(), key=lambda x: -x[1]):
        print(f"  {label:30s} {count:5d}")

    # Save
    output_path = "data/schema_mapper_training.json"
    with open(output_path, "w") as f:
        json.dump(samples, f, indent=2)
    print(f"\nSaved to {output_path}")
    print(f"Feature dim: {len(samples[0]['features'])}")
