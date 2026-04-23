"""Phase 10: Semantic plugin interface — stable contract for third-party content analyzers."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import Enum

from gateway.walacor.client import _iso8601


class Verdict(str, Enum):
    """Result of content analysis."""

    PASS = "pass"   # content is clean, proceed
    WARN = "warn"   # content has findings, flag but do not block
    BLOCK = "block"  # content violates policy, block the request/response


@dataclass(frozen=True)
class Decision:
    """Single analyzer result. Required for all analyzers."""

    verdict: Verdict
    confidence: float  # 0.0 – 1.0; required for all verdicts
    analyzer_id: str   # unique, stable identifier for this analyzer
    category: str      # "pii" | "toxicity" | "injection" | "secrets" | custom
    reason: str        # short, human-readable (no content); e.g. "email_pattern_matched"


class ContentAnalyzer(ABC):
    """
    Plugin contract for semantic firewalls (Protect AI, Lakera Guard, custom NLP).
    Implementations MUST NOT store or log the analyzed text.
    """

    def _ensure_fail_open_log(self) -> deque:
        """Lazy-init the bounded fail-open log.

        Subclasses do not all call super().__init__(); this helper avoids
        forcing a change to every analyzer constructor.
        """
        log = getattr(self, "_fail_open_log", None)
        if log is None:
            log = deque(maxlen=50)
            self._fail_open_log = log
        return log

    def _record_fail_open(self, reason: str) -> None:
        self._ensure_fail_open_log().append((time.time(), reason))

    def fail_open_snapshot(self) -> dict:
        log = self._ensure_fail_open_log()
        now = time.time()
        recent = [e for e in log if now - e[0] <= 60.0]
        last = log[-1] if log else None
        return {
            "fail_opens_60s": len(recent),
            "last_fail_open": {"ts": _iso8601(last[0]), "reason": last[1]} if last else None,
        }

    @property
    @abstractmethod
    def analyzer_id(self) -> str:
        """Stable unique identifier, e.g. 'walacor.pii.v1', 'protectai.scanv2'."""
        ...

    @property
    def timeout_ms(self) -> int:
        """Max time this analyzer may take. Default 50ms. Slow analyzers don't block the pipeline."""
        return 50

    @abstractmethod
    async def analyze(self, text: str) -> Decision:
        """
        Analyze text and return a Decision.
        Contract:
        - MUST NOT store or log the text
        - MUST return within timeout_ms or be cancelled
        - MUST return Decision with confidence 0.0 if analysis is inconclusive
        """
        ...
