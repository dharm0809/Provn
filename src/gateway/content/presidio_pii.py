"""Optional Presidio NER-based PII detector. Fail-open on missing deps.

Uses Named Entity Recognition for higher-accuracy PII detection vs regex.
Install with: pip install 'walacor-gateway[presidio]'
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)

# High-risk PII types → BLOCK; low-risk → WARN
_BLOCK_ENTITIES = {"CREDIT_CARD", "US_SSN", "US_BANK_NUMBER", "IBAN_CODE", "CRYPTO"}
_WARN_ENTITIES = {
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION", "DATE_TIME",
    "NRP", "MEDICAL_LICENSE", "US_DRIVER_LICENSE", "US_PASSPORT", "UK_NHS",
    "IP_ADDRESS",
}


class PresidioPIIAnalyzer(ContentAnalyzer):
    """Presidio-based PII detector implementing ContentAnalyzer ABC.

    Uses asyncio.to_thread for CPU-bound NER inference.
    Fail-open: returns PASS if Presidio is unavailable.
    """

    _analyzer_id = "walacor.presidio_pii.v1"

    def __init__(self) -> None:
        self._engine: Any = None
        self._available = False
        self._block_entities: set[str] = set(_BLOCK_ENTITIES)
        self._warn_entities: set[str] = set(_WARN_ENTITIES)
        try:
            from presidio_analyzer import AnalyzerEngine
            self._engine = AnalyzerEngine()
            self._available = True
            logger.info("Presidio PII analyzer initialized")
        except ImportError:
            logger.warning(
                "presidio-analyzer not installed — PresidioPIIAnalyzer disabled. "
                "Install with: pip install 'walacor-gateway[presidio]'"
            )
        except Exception as e:
            logger.warning("Presidio initialization failed (fail-open): %s", e)

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    @property
    def timeout_ms(self) -> int:
        return 200  # NER inference is slower than regex

    def configure(self, policies: list[dict]) -> None:
        """Hot-reload content policies (updates block/warn entity sets)."""
        if not policies:
            return
        self._block_entities = {
            p["category"].upper() for p in policies if p.get("action") == "block"
        }
        self._warn_entities = {
            p["category"].upper() for p in policies if p.get("action") == "warn"
        }

    def _analyze_sync(self, text: str) -> Decision:
        """Synchronous Presidio analysis — run via to_thread."""
        results = self._engine.analyze(
            text=text,
            language="en",
            score_threshold=0.5,
        )

        if not results:
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="pii",
                reason="no_pii_detected",
            )

        # Find highest-confidence result and determine verdict
        top = max(results, key=lambda r: r.score)
        entity_type = top.entity_type

        if entity_type in self._block_entities:
            verdict = Verdict.BLOCK
        elif entity_type in self._warn_entities:
            verdict = Verdict.WARN
        else:
            verdict = Verdict.WARN  # unknown entity types default to warn

        entity_summary = ",".join(r.entity_type for r in results)

        return Decision(
            verdict=verdict,
            confidence=top.score,
            analyzer_id=self.analyzer_id,
            category="pii",
            reason=f"presidio:{entity_type.lower()}:{len(results)}_entities:[{entity_summary}]",
        )

    async def analyze(self, text: str) -> Decision:
        """Analyze text for PII entities using Presidio NER."""
        if not self._available or not self._engine:
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="pii",
                reason="unavailable",
            )

        try:
            return await asyncio.to_thread(self._analyze_sync, text)
        except Exception as e:
            logger.warning("Presidio analysis failed (fail-open): %s", e)
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="pii",
                reason="error",
            )
