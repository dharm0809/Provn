"""Phase 10: Built-in toxicity/harmful content detector. Deny-list based. analyzer_id: walacor.toxicity.v1"""

from __future__ import annotations

import re

from gateway.content.base import ContentAnalyzer, Decision, Verdict

# Default deny-list patterns (word-boundary matched, case-insensitive).
# These are deliberately generic category headers — deployments configure specifics via
# WALACOR_TOXICITY_DENY_TERMS or control plane policy sync.
_DEFAULT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("self_harm_indicator", [
        r"\bkill\s+(my)?self\b", r"\bsuicid(e|al)\b", r"\bend\s+my\s+life\b",
    ]),
    ("violence_instruction", [
        r"\bhow\s+to\s+(make|build|create)\s+(a\s+)?(bomb|weapon|explosive)\b",
        r"\bstep[s]?\s+(by\s+step|by\s+step\s+guide)\s+to\s+(hurt|harm|kill|attack)\b",
    ]),
    ("child_safety", [
        r"\bcsam\b", r"\bchild\s+(sexual|porn|exploit)",
    ]),
]


def _compile_category(terms: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(terms), re.IGNORECASE)


class ToxicityDetector(ContentAnalyzer):
    """
    Keyword/pattern deny-list based toxicity detector.
    Returns WARN by default; upgrades to BLOCK for child safety.
    Deny terms can be extended at runtime via set_extra_terms().
    No content stored or logged.
    """

    _analyzer_id = "walacor.toxicity.v1"

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    def __init__(self, extra_terms: list[str] | None = None) -> None:
        self._categories: list[tuple[str, re.Pattern[str]]] = [
            (name, _compile_category(terms)) for name, terms in _DEFAULT_CATEGORIES
        ]
        if extra_terms:
            self._categories.append(
                ("custom_deny_list", _compile_category(extra_terms))
            )
        self._block_categories: set[str] = {"child_safety"}
        self._warn_categories: set[str] = {"self_harm_indicator", "violence_instruction"}

    def configure(self, policies: list[dict]) -> None:
        """Reconfigure block/warn category sets from control plane content policies."""
        if not policies:
            return
        self._block_categories = {p["category"] for p in policies if p.get("action") == "block"}
        self._warn_categories = {p["category"] for p in policies if p.get("action") == "warn"}

    def set_extra_terms(self, terms: list[str]) -> None:
        """Replace the custom deny list at runtime (e.g. after policy sync)."""
        self._categories = [c for c in self._categories if c[0] != "custom_deny_list"]
        if terms:
            self._categories.append(("custom_deny_list", _compile_category(terms)))

    @property
    def timeout_ms(self) -> int:
        return 20

    async def analyze(self, text: str) -> Decision:
        for category_name, pattern in self._categories:
            if pattern.search(text):
                if category_name in self._block_categories:
                    verdict = Verdict.BLOCK
                elif category_name in self._warn_categories:
                    verdict = Verdict.WARN
                else:
                    verdict = Verdict.WARN
                return Decision(
                    verdict=verdict,
                    confidence=0.90,
                    analyzer_id=self.analyzer_id,
                    category="toxicity",
                    reason=category_name,
                )
        return Decision(
            verdict=Verdict.PASS,
            confidence=1.0,
            analyzer_id=self.analyzer_id,
            category="toxicity",
            reason="no_toxicity_detected",
        )
