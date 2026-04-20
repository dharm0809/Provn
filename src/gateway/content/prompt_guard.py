"""Prompt injection detection via Meta Prompt Guard 2.

Uses a tiny DeBERTa-xsmall classifier (22M params) that runs on CPU in 2-5ms.
Three-class output: benign (0), injection (1), jailbreak (2).

Install with: pip install 'walacor-gateway[guard]'
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)

_CLASS_NAMES = {0: "benign", 1: "injection", 2: "jailbreak"}


def _load_model(model_id: str) -> tuple[Any, Any]:
    """Load tokenizer and model. Raises ImportError if transformers/torch not installed."""
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id)
    return tokenizer, model


class PromptGuardAnalyzer(ContentAnalyzer):
    """Prompt Guard 2 injection/jailbreak classifier.

    Fail-open: if model not installed or inference fails, returns PASS with confidence=0.0.
    """

    _analyzer_id = "walacor.prompt_guard.v2"

    def __init__(
        self,
        model_id: str = "meta-llama/Prompt-Guard-2-22M",
        threshold: float = 0.9,
    ) -> None:
        self._model_id = model_id
        self._threshold = threshold
        self._tokenizer: Any = None
        self._model: Any = None
        self._available = True
        try:
            self._tokenizer, self._model = _load_model(model_id)
            logger.info("Prompt Guard 2 loaded: %s", model_id)
        except ImportError:
            logger.warning(
                "Prompt Guard 2 unavailable: transformers/torch not installed. "
                "Install with: pip install 'walacor-gateway[guard]'"
            )
            self._available = False
        except Exception as e:
            logger.warning("Prompt Guard 2 init failed (fail-open): %s", e)
            self._available = False

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    @property
    def timeout_ms(self) -> int:
        return 20

    def _classify_sync(self, text: str) -> Decision:
        """Synchronous classification — runs on CPU."""
        import torch

        inputs = self._tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512
        )
        with torch.no_grad():
            outputs = self._model(**inputs)
        probs = torch.softmax(outputs.logits, dim=-1)[0]
        predicted_class = int(torch.argmax(probs))
        confidence = float(probs[predicted_class])
        class_name = _CLASS_NAMES.get(predicted_class, "unknown")

        if predicted_class == 0:
            return Decision(
                verdict=Verdict.PASS,
                confidence=confidence,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="benign",
            )
        if predicted_class == 1 and confidence >= self._threshold:
            return Decision(
                verdict=Verdict.BLOCK,
                confidence=confidence,
                analyzer_id=self.analyzer_id,
                category="injection",
                reason=f"injection:{confidence:.3f}",
            )
        if predicted_class == 2 and confidence >= self._threshold:
            return Decision(
                verdict=Verdict.WARN,
                confidence=confidence,
                analyzer_id=self.analyzer_id,
                category="jailbreak",
                reason=f"jailbreak:{confidence:.3f}",
            )
        return Decision(
            verdict=Verdict.PASS,
            confidence=1.0 - confidence,
            analyzer_id=self.analyzer_id,
            category=class_name,
            reason=f"{class_name}:{confidence:.3f}:below_threshold",
        )

    def configure(self, policies: list[dict]) -> None:
        """No-op: threshold is model-card-driven, not policy-driven.

        Present for ContentAnalyzer protocol parity with PII/toxicity/LlamaGuard.
        The 0.9 confidence threshold comes from the Prompt Guard 2 model card;
        overriding it per-tenant via the control plane would silently weaken
        injection detection across the fleet, so we reject that surface area.
        """
        return None

    async def analyze(self, text: str) -> Decision:
        if not self._available:
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="unavailable",
            )
        try:
            return await asyncio.to_thread(self._classify_sync, text)
        except Exception as e:
            logger.warning("Prompt Guard 2 analysis failed (fail-open): %s", e)
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="error",
            )
