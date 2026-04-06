"""AuditLLM-inspired consistency checker for LLM responses.

Adapted from "AuditLLM: A Tool for Auditing Large Language Models Using
Multiprobe Approach" (Amirizaniani et al., 2402.09334).

Key adaptation: instead of generating probe questions, we passively observe
the gateway's traffic. When semantically similar questions arrive from
different sessions/users, we compare the model's responses. This gives us
free consistency auditing with zero additional LLM calls for the common case.

For active probing, the background intelligence worker periodically generates
5 probe variants of recent questions and tests model consistency.

Components:
  1. ConsistencyTracker — passive inline fingerprinting + similarity tracking
  2. ProbeGenerator — background active consistency probing via Ollama
  3. ReliabilityScorer — per-model reliability scores for dashboard

Similarity: TF-IDF + cosine similarity (lightweight, no torch/sentence-transformers
dependency). Optionally upgrade to sentence-transformers if installed.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.60   # AuditLLM uses 60% for flagging
HIGH_SIMILARITY = 0.85        # Prompts this similar should produce very similar responses
MAX_HISTORY_PER_MODEL = 200   # Max stored prompt-response pairs per model
MAX_PROBE_BATCH = 5           # AuditLLM generates 5 probes per question
CONSISTENCY_WINDOW = 3600     # Compare within last hour of traffic


# ── Lightweight TF-IDF + Cosine Similarity ───────────────────────────────────
# No external deps — pure Python. Matches AuditLLM's approach but without
# requiring sentence-transformers (~500MB). Accuracy is lower but sufficient
# for detecting gross inconsistencies.

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "i", "me", "my", "you", "your", "he", "she", "it", "we", "they",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "if", "then", "than", "too", "very", "just", "about", "how",
})

_WORD_RE = re.compile(r"\b[a-z]{2,}\b")


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words, removing stop words."""
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS]


def _term_freq(tokens: list[str]) -> dict[str, float]:
    """Compute term frequency vector."""
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    total = len(tokens) or 1
    return {t: c / total for t, c in counts.items()}


def cosine_similarity(text_a: str, text_b: str) -> float:
    """Compute cosine similarity between two texts using TF vectors.

    Returns 0.0-1.0. Uses term frequency (not TF-IDF) for simplicity
    since we're comparing within the same domain (LLM responses).
    """
    if not text_a or not text_b:
        return 0.0

    tf_a = _term_freq(_tokenize(text_a))
    tf_b = _term_freq(_tokenize(text_b))

    # Cosine similarity
    all_terms = set(tf_a) | set(tf_b)
    if not all_terms:
        return 0.0

    dot = sum(tf_a.get(t, 0) * tf_b.get(t, 0) for t in all_terms)
    mag_a = math.sqrt(sum(v * v for v in tf_a.values()))
    mag_b = math.sqrt(sum(v * v for v in tf_b.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0

    return dot / (mag_a * mag_b)


def prompt_fingerprint(text: str) -> str:
    """Generate a semantic fingerprint for a prompt.

    Uses sorted token set hash — prompts with the same words (regardless
    of order) get the same fingerprint. This catches simple rephrasings.
    """
    tokens = sorted(set(_tokenize(text)))
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()[:16]


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class PromptResponsePair:
    """A stored prompt-response pair for consistency comparison."""

    prompt: str
    response: str
    model_id: str
    execution_id: str
    session_id: str
    user: str
    timestamp: float
    fingerprint: str
    prompt_tokens: list[str] = field(default_factory=list)


@dataclass
class ConsistencyResult:
    """Result of comparing two responses to similar prompts."""

    execution_id_a: str
    execution_id_b: str
    prompt_similarity: float
    response_similarity: float
    consistent: bool  # True if response_similarity >= threshold when prompts are similar
    model_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ModelReliability:
    """Per-model reliability score based on consistency tracking."""

    model_id: str
    total_comparisons: int = 0
    consistent_count: int = 0
    inconsistent_count: int = 0
    avg_response_similarity: float = 0.0
    reliability_score: float = 1.0  # 0.0-1.0, starts at 1.0
    last_updated: float = field(default_factory=time.time)

    def update(self, consistent: bool, response_sim: float) -> None:
        self.total_comparisons += 1
        if consistent:
            self.consistent_count += 1
        else:
            self.inconsistent_count += 1
        # Rolling average
        alpha = 0.1
        self.avg_response_similarity = (
            alpha * response_sim + (1 - alpha) * self.avg_response_similarity
        )
        # Reliability = fraction of consistent responses
        self.reliability_score = (
            self.consistent_count / self.total_comparisons
            if self.total_comparisons > 0 else 1.0
        )
        self.last_updated = time.time()


# ── Consistency Tracker (Passive, Inline) ────────────────────────────────────

class ConsistencyTracker:
    """Passively tracks prompt-response pairs and detects inconsistencies.

    Every request is fingerprinted and stored. When a semantically similar
    prompt arrives (same fingerprint or high cosine similarity), responses
    are compared. Inconsistencies are flagged in the audit trail.

    Usage:
        tracker = ConsistencyTracker()
        # On each request:
        result = tracker.check(prompt, response, model_id, execution_id, ...)
        if result and not result.consistent:
            record["metadata"]["consistency_flag"] = True
    """

    def __init__(self) -> None:
        # Per-model history: {model_id: deque of PromptResponsePair}
        self._history: dict[str, deque[PromptResponsePair]] = defaultdict(
            lambda: deque(maxlen=MAX_HISTORY_PER_MODEL)
        )
        # Per-model reliability scores
        self._reliability: dict[str, ModelReliability] = {}
        # Recent consistency results for dashboard
        self._recent_results: deque[ConsistencyResult] = deque(maxlen=100)
        # Fingerprint index for fast lookup
        self._fingerprint_index: dict[str, list[str]] = defaultdict(list)  # fp → [model_id:idx, ...]

    def check(
        self,
        prompt: str,
        response: str,
        model_id: str,
        execution_id: str,
        session_id: str = "",
        user: str = "",
    ) -> ConsistencyResult | None:
        """Check consistency against previous similar prompts.

        Returns ConsistencyResult if a similar prompt was found, None otherwise.
        Always stores the current prompt-response for future comparisons.
        """
        if not prompt or not response or len(prompt) < 10:
            return None

        fp = prompt_fingerprint(prompt)
        tokens = _tokenize(prompt)
        now = time.time()

        pair = PromptResponsePair(
            prompt=prompt[:500],  # Cap storage
            response=response[:1000],
            model_id=model_id,
            execution_id=execution_id,
            session_id=session_id,
            user=user,
            timestamp=now,
            fingerprint=fp,
            prompt_tokens=tokens,
        )

        result = None
        history = self._history[model_id]

        # Search for similar prompts in history
        best_match: PromptResponsePair | None = None
        best_prompt_sim = 0.0

        for prev in history:
            # Skip same session (same conversation)
            if prev.session_id == session_id and session_id:
                continue
            # Skip old entries
            if now - prev.timestamp > CONSISTENCY_WINDOW:
                continue

            # Fast check: exact fingerprint match
            if prev.fingerprint == fp:
                prompt_sim = 1.0
            else:
                # Compute cosine similarity
                prompt_sim = cosine_similarity(prev.prompt, prompt)

            if prompt_sim >= SIMILARITY_THRESHOLD and prompt_sim > best_prompt_sim:
                best_match = prev
                best_prompt_sim = prompt_sim

        if best_match:
            response_sim = cosine_similarity(best_match.response, response)

            # AuditLLM 45° slope rule adapted for TF-IDF:
            # TF-IDF produces lower similarity on longer texts (responses are verbose),
            # so we use a more forgiving threshold than the paper's 60%.
            # Consistent = response similarity > 40% of prompt similarity, OR > 0.35 absolute
            consistent = response_sim >= min(best_prompt_sim * 0.5, 0.5) or response_sim >= 0.35

            result = ConsistencyResult(
                execution_id_a=best_match.execution_id,
                execution_id_b=execution_id,
                prompt_similarity=round(best_prompt_sim, 3),
                response_similarity=round(response_sim, 3),
                consistent=consistent,
                model_id=model_id,
            )

            self._recent_results.append(result)

            # Update reliability
            if model_id not in self._reliability:
                self._reliability[model_id] = ModelReliability(model_id=model_id)
            self._reliability[model_id].update(consistent, response_sim)

            if not consistent:
                logger.info(
                    "Consistency flag: model=%s prompt_sim=%.2f response_sim=%.2f "
                    "exec_a=%s exec_b=%s",
                    model_id, best_prompt_sim, response_sim,
                    best_match.execution_id[:8], execution_id[:8],
                )

        # Store current pair for future comparisons
        history.append(pair)

        return result

    def get_reliability(self, model_id: str) -> ModelReliability | None:
        """Get reliability score for a specific model."""
        return self._reliability.get(model_id)

    def get_all_reliability(self) -> dict[str, ModelReliability]:
        """Get reliability scores for all tracked models."""
        return dict(self._reliability)

    def get_recent_results(self, limit: int = 20) -> list[ConsistencyResult]:
        """Get recent consistency comparison results."""
        return list(self._recent_results)[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Return tracker stats for health/status endpoints."""
        return {
            "models_tracked": len(self._history),
            "total_pairs_stored": sum(len(h) for h in self._history.values()),
            "total_comparisons": sum(
                r.total_comparisons for r in self._reliability.values()
            ),
            "recent_inconsistencies": sum(
                1 for r in self._recent_results if not r.consistent
            ),
            "model_reliability": {
                model_id: {
                    "score": round(rel.reliability_score, 3),
                    "comparisons": rel.total_comparisons,
                    "consistent": rel.consistent_count,
                    "inconsistent": rel.inconsistent_count,
                    "avg_similarity": round(rel.avg_response_similarity, 3),
                }
                for model_id, rel in self._reliability.items()
            },
        }


# ── Probe Generator (Background, Active) ────────────────────────────────────

PROBE_PROMPT = """Generate exactly 5 different ways to ask this same question.
Each rephrasing must ask for the same information but use different words.
Respond ONLY with JSON: {{"probes": ["q1", "q2", "q3", "q4", "q5"]}}

Original question: {question}"""


@dataclass
class ProbeResult:
    """Result of active consistency probing."""

    original_prompt: str
    model_id: str
    probes: list[str]
    responses: list[str]
    similarities: list[float]  # Cosine similarity of each response to probe[0]'s response
    avg_similarity: float
    min_similarity: float
    consistent: bool  # All responses above threshold
    timestamp: float = field(default_factory=time.time)


class ProbeGenerator:
    """Active consistency probing using the AuditLLM multiprobe approach.

    Uses the background intelligence worker's Ollama access to:
    1. Generate 5 probe variants of a question (using LLM1)
    2. Send all 5 to the target model (using LLM2 via gateway)
    3. Compare response consistency using cosine similarity

    This runs in background — zero impact on user requests.
    """

    def __init__(self, ollama_url: str = "http://localhost:11434") -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._results: deque[ProbeResult] = deque(maxlen=50)
        self._probes_run = 0

    async def probe_model(
        self, question: str, model_id: str, probe_model: str = "gemma3:1b"
    ) -> ProbeResult | None:
        """Run a full multiprobe consistency check on a model.

        1. Generate 5 probe variants using probe_model
        2. Send each to model_id
        3. Compare response consistency

        Returns ProbeResult or None if probing failed.
        """
        import httpx

        # Step 1: Generate probes
        probes = await self._generate_probes(question, probe_model)
        if not probes or len(probes) < 3:
            return None

        # Step 2: Get responses from target model
        responses = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            for probe in probes:
                try:
                    resp = await client.post(
                        f"{self._ollama_url}/api/chat",
                        json={
                            "model": model_id,
                            "messages": [{"role": "user", "content": probe}],
                            "stream": False,
                            "options": {"num_predict": 200},
                        },
                    )
                    if resp.status_code == 200:
                        content = resp.json().get("message", {}).get("content", "")
                        responses.append(content)
                    else:
                        responses.append("")
                except Exception:
                    responses.append("")

        if not any(responses):
            return None

        # Step 3: Compare consistency
        # Use first response as reference (AuditLLM approach)
        ref_response = responses[0]
        similarities = []
        for r in responses[1:]:
            sim = cosine_similarity(ref_response, r) if r else 0.0
            similarities.append(round(sim, 3))

        avg_sim = sum(similarities) / len(similarities) if similarities else 0.0
        min_sim = min(similarities) if similarities else 0.0
        consistent = min_sim >= SIMILARITY_THRESHOLD

        result = ProbeResult(
            original_prompt=question[:200],
            model_id=model_id,
            probes=probes,
            responses=[r[:300] for r in responses],
            similarities=similarities,
            avg_similarity=round(avg_sim, 3),
            min_similarity=round(min_sim, 3),
            consistent=consistent,
        )

        self._results.append(result)
        self._probes_run += 1

        logger.info(
            "Probe result: model=%s avg_sim=%.2f min_sim=%.2f consistent=%s probes=%d",
            model_id, avg_sim, min_sim, consistent, len(probes),
        )

        return result

    async def _generate_probes(self, question: str, probe_model: str) -> list[str]:
        """Generate probe variants using a local LLM (AuditLLM's LLM1 role)."""
        import json
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/chat",
                    json={
                        "model": probe_model,
                        "messages": [{"role": "user", "content": PROBE_PROMPT.format(question=question[:300])}],
                        "stream": False,
                        "options": {"num_predict": 200},
                        "format": "json",
                    },
                )
                if resp.status_code == 200:
                    content = resp.json().get("message", {}).get("content", "")
                    parsed = json.loads(content)
                    probes = parsed.get("probes", [])
                    # Include original question as first probe
                    return [question] + probes[:MAX_PROBE_BATCH - 1]
        except Exception as e:
            logger.debug("Probe generation failed: %s", e)

        return []

    def get_results(self, limit: int = 20) -> list[ProbeResult]:
        """Get recent probe results."""
        return list(self._results)[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Return probe stats."""
        return {
            "probes_run": self._probes_run,
            "results_stored": len(self._results),
            "recent_consistent": sum(1 for r in self._results if r.consistent),
            "recent_inconsistent": sum(1 for r in self._results if not r.consistent),
        }
