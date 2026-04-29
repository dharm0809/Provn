"""Background intelligence worker — async LLM enrichment via local Ollama.

Runs as a background asyncio task. The pipeline enqueues lightweight job
dicts after each response is sent to the user (zero added latency).
The worker pulls jobs and runs each task sequentially against Ollama.

Tasks (in priority order):
1. Intent reclassification (only if ONNX confidence < 0.7)
2. Topic extraction (2-3 key topics)
3. Compliance flagging (financial/medical/legal)
4. Summarization (multi-turn > 3 turns only)

Results are stored back in the WAL record metadata.
The distillation loop exports high-confidence LLM labels for ONNX retraining.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from gateway.util.errors import classify_exception

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_MODEL = "gemma3:1b"  # Smallest/fastest model for background tasks
MAX_QUEUE_SIZE = 500
CACHE_MAX_SIZE = 1000
DEFAULT_DISTILLATION_BUFFER_MAX = 10_000  # Bounded distillation buffer; overridden by WALACOR_DISTILLATION_BUFFER_MAX
NUM_PREDICT_CLASSIFY = 30
NUM_PREDICT_TOPICS = 50
NUM_PREDICT_COMPLIANCE = 50
NUM_PREDICT_SUMMARY = 150


@dataclass
class IntelligenceJob:
    """A job for the background intelligence worker."""

    execution_id: str
    prompt_text: str
    response_content: str
    model_id: str
    session_id: str
    intent: str
    intent_confidence: float
    conversation_turns: int
    enqueued_at: float = field(default_factory=time.time)


@dataclass
class IntelligenceResult:
    """Result from LLM intelligence processing."""

    execution_id: str
    topics: list[str] | None = None
    reclassified_intent: str | None = None
    reclassified_confidence: float | None = None
    compliance_flags: list[str] | None = None
    summary: str | None = None
    processing_time_ms: float = 0


# ── Prompt templates ─────────────────────────────────────────────────────────
#
# IMPORTANT: these strings are consumed by `str.format(...)`, which
# treats `{...}` as a placeholder. Literal braces from the example JSON
# body MUST be doubled (`{{` / `}}`) or `.format()` raises
# `KeyError: '"topics"'` when it tries to resolve `{"topics"}` as a
# format field name. Silent breakage of the intelligence worker: every
# job failed before this fix because three of these four templates
# used single braces.

_CLASSIFY_PROMPT = """Classify this user prompt into exactly one category.
Categories: normal, web_search, code_generation, reasoning, creative_writing, analysis, system_task
Respond ONLY with JSON: {{"category": "...", "confidence": 0.0-1.0}}

User prompt: {prompt}"""

_TOPICS_PROMPT = """Extract 2-3 key topics from this conversation.
Respond ONLY with JSON: {{"topics": ["topic1", "topic2"]}}

User: {prompt}
Assistant: {response}"""

_COMPLIANCE_PROMPT = """Does this conversation contain any of these sensitive categories?
- financial_advice: investment recommendations, trading guidance
- medical_guidance: health diagnoses, treatment recommendations
- legal_counsel: legal advice, contract interpretation
- pii_discussion: discussion of personal data handling

Respond ONLY with JSON: {{"flags": ["category1"] or [] if none}}

User: {prompt}
Assistant: {response}"""

_SUMMARY_PROMPT = """Summarize this conversation in one sentence (max 30 words).
Respond ONLY with JSON: {{"summary": "..."}}

Conversation: {prompt}"""


# ── Cache ────────────────────────────────────────────────────────────────────

class _LRUCache:
    """Simple bounded LRU cache for deduplication."""

    def __init__(self, max_size: int = CACHE_MAX_SIZE):
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Any | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
        self._cache[key] = value


def _prompt_hash(text: str) -> str:
    """Hash first 200 chars of prompt for cache key."""
    return hashlib.sha256(text[:200].encode()).hexdigest()[:16]


# ── Worker ───────────────────────────────────────────────────────────────────

class IntelligenceWorker:
    """Background async worker for LLM-powered enrichment.

    Usage:
        worker = IntelligenceWorker(ollama_url="http://localhost:11434")
        task = asyncio.create_task(worker.run())
        # ... later ...
        await worker.enqueue(job)
        # ... shutdown ...
        await worker.stop()
    """

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = DEFAULT_MODEL,
        enabled: bool = True,
        distillation_buffer_max: int = DEFAULT_DISTILLATION_BUFFER_MAX,
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._enabled = enabled
        self._queue: asyncio.Queue[IntelligenceJob | None] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._cache = _LRUCache()
        self._running = False
        self._processed = 0
        self._errors = 0
        self._results_callback: Any = None  # Set by pipeline to write back to WAL
        # Distillation buffer: (prompt, label, confidence) for ONNX retraining.
        # Bounded deque — oldest samples evicted when capacity reached.
        # Distillation cares about current traffic patterns, so dropping the
        # oldest samples is the correct eviction policy. Without the bound
        # this list grew without limit and was a slow memory leak under
        # sustained load.
        self._distillation_buffer: deque[dict] = deque(
            maxlen=max(1, int(distillation_buffer_max))
        )
        self._last_error: tuple[float, str] | None = None

    def _record_error(self, detail: str) -> None:
        cleaned = (detail or "").strip() or "Exception"
        self._last_error = (time.time(), cleaned)

    def snapshot(self) -> dict:
        from gateway.util.time import iso8601_utc
        q = getattr(self, "_queue", None)
        queue_depth = q.qsize() if q is not None else 0
        now = time.time()
        last_error = None
        if self._last_error is not None:
            ts, detail = self._last_error
            if now - ts <= 60.0:
                last_error = {"ts": iso8601_utc(ts), "detail": detail}
        return {
            "running": bool(self._running),
            "queue_depth": queue_depth,
            "oldest_job_age_s": 0.0,
            "last_error": last_error,
        }

    async def enqueue(self, job: IntelligenceJob) -> bool:
        """Enqueue a job for background processing. Returns False if queue full."""
        if not self._enabled:
            return False
        try:
            self._queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            logger.debug("Intelligence queue full, dropping job %s", job.execution_id)
            return False

    async def run(self) -> None:
        """Main worker loop. Call as asyncio.create_task(worker.run())."""
        self._running = True
        logger.info("Intelligence worker started (model=%s)", self._model)

        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            if job is None:  # Shutdown signal
                break

            try:
                result = await self._process(job)
                self._processed += 1
                if result and self._results_callback:
                    await self._results_callback(result)
            except Exception as e:
                self._errors += 1
                self._record_error(classify_exception(e))
                logger.warning("Intelligence worker error: %s", e, exc_info=True)

        logger.info("Intelligence worker stopped (processed=%d, errors=%d)", self._processed, self._errors)

    async def stop(self) -> None:
        """Signal the worker to stop."""
        self._running = False
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def _process(self, job: IntelligenceJob) -> IntelligenceResult | None:
        """Process a single job through all applicable tasks."""
        t_start = time.perf_counter()
        result = IntelligenceResult(execution_id=job.execution_id)

        # Check cache
        cache_key = _prompt_hash(job.prompt_text)
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # Skip system tasks
        if job.intent == "system_task":
            return None

        # Task 1: Reclassify intent (only if ONNX was unsure)
        if job.intent_confidence < 0.7:
            classify_result = await self._call_ollama(
                _CLASSIFY_PROMPT.format(prompt=job.prompt_text[:500]),
                NUM_PREDICT_CLASSIFY,
            )
            if classify_result:
                try:
                    parsed = json.loads(classify_result)
                    result.reclassified_intent = parsed.get("category")
                    result.reclassified_confidence = parsed.get("confidence")
                    # Distillation: save high-confidence reclassifications
                    if result.reclassified_confidence and result.reclassified_confidence > 0.85:
                        self._distillation_buffer.append({
                            "prompt": job.prompt_text[:500],
                            "label": result.reclassified_intent,
                            "confidence": result.reclassified_confidence,
                            "source": "llm_reclassify",
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

        # Task 2: Topic extraction
        topics_result = await self._call_ollama(
            _TOPICS_PROMPT.format(
                prompt=job.prompt_text[:300],
                response=(job.response_content or "")[:300],
            ),
            NUM_PREDICT_TOPICS,
        )
        if topics_result:
            try:
                parsed = json.loads(topics_result)
                result.topics = parsed.get("topics", [])
            except (json.JSONDecodeError, TypeError):
                pass

        # Task 3: Compliance flagging
        compliance_result = await self._call_ollama(
            _COMPLIANCE_PROMPT.format(
                prompt=job.prompt_text[:300],
                response=(job.response_content or "")[:300],
            ),
            NUM_PREDICT_COMPLIANCE,
        )
        if compliance_result:
            try:
                parsed = json.loads(compliance_result)
                flags = parsed.get("flags", [])
                if flags:
                    result.compliance_flags = flags
            except (json.JSONDecodeError, TypeError):
                pass

        # Task 4: Summarization (only for multi-turn conversations)
        if job.conversation_turns >= 3:
            summary_result = await self._call_ollama(
                _SUMMARY_PROMPT.format(prompt=job.prompt_text[:500]),
                NUM_PREDICT_SUMMARY,
            )
            if summary_result:
                try:
                    parsed = json.loads(summary_result)
                    result.summary = parsed.get("summary")
                except (json.JSONDecodeError, TypeError):
                    result.summary = summary_result[:200]

        result.processing_time_ms = (time.perf_counter() - t_start) * 1000
        self._cache.put(cache_key, result)

        logger.debug(
            "Intelligence processed %s: topics=%s compliance=%s time=%.0fms",
            job.execution_id[:8], result.topics, result.compliance_flags, result.processing_time_ms,
        )
        return result

    async def _call_ollama(self, prompt: str, num_predict: int) -> str | None:
        """Call local Ollama for a single inference."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/chat",
                    json={
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"num_predict": num_predict},
                        "format": "json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("message", {}).get("content", "")
        except Exception as e:
            logger.debug("Ollama call failed (non-fatal): %s", e)
        return None

    def get_distillation_buffer(self) -> list[dict]:
        """Return accumulated distillation samples for ONNX retraining."""
        return list(self._distillation_buffer)

    def clear_distillation_buffer(self) -> None:
        self._distillation_buffer.clear()

    def get_stats(self) -> dict[str, Any]:
        """Return worker stats for health endpoint."""
        return {
            "enabled": self._enabled,
            "running": self._running,
            "model": self._model,
            "queue_size": self._queue.qsize(),
            "processed": self._processed,
            "errors": self._errors,
            "cache_size": len(self._cache._cache),
            "distillation_samples": len(self._distillation_buffer),
        }
