"""Phase 17: Llama Guard 3 content analyzer.

Uses Meta's Llama Guard 3 model (available via `ollama pull llama-guard3`) to classify
text against 14 safety categories. Runs locally via Ollama — zero cloud dependency.

Category mapping (S1–S14):
  S1  → violent_crimes
  S2  → nonviolent_crimes
  S3  → sex_crimes
  S4  → child_safety        (BLOCK)
  S5  → defamation
  S6  → specialized_advice
  S7  → privacy_pii
  S8  → intellectual_property
  S9  → indiscriminate_weapons
  S10 → hate_discrimination
  S11 → self_harm
  S12 → sexual_content
  S13 → elections
  S14 → code_interpreter_abuse
"""

from __future__ import annotations

import logging

import httpx

from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)

# S-code to category string mapping (S4 child_safety → BLOCK; all others → WARN)
_CATEGORY_MAP: dict[str, str] = {
    "S1": "violent_crimes",
    "S2": "nonviolent_crimes",
    "S3": "sex_crimes",
    "S4": "child_safety",
    "S5": "defamation",
    "S6": "specialized_advice",
    "S7": "privacy_pii",
    "S8": "intellectual_property",
    "S9": "indiscriminate_weapons",
    "S10": "hate_discrimination",
    "S11": "self_harm",
    "S12": "sexual_content",
    "S13": "elections",
    "S14": "code_interpreter_abuse",
}

_BLOCK_CATEGORIES = {"child_safety"}

_LLAMA_GUARD_TEMPLATE = """\
[INST] Task: Check if there is unsafe content in the '{role}' messages in conversations \
according to our safety policy with the below categories.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes.
S2: Non-Violent Crimes.
S3: Sex-Related Crimes.
S4: Child Sexual Exploitation.
S5: Defamation.
S6: Specialized Advice.
S7: Privacy.
S8: Intellectual Property.
S9: Indiscriminate Weapons.
S10: Hate.
S11: Suicide & Self-Harm.
S12: Sexual Content.
S13: Elections.
S14: Code Interpreter Abuse.
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>
{role}: {content}
<END CONVERSATION>

Provide your safety assessment for the '{role}' in the above conversation:
- First line must read 'safe' or 'unsafe'.
- If unsafe, a second line must include a comma-separated list of violated categories. [/INST]"""


class LlamaGuardAnalyzer(ContentAnalyzer):
    """Llama Guard 3 safety classifier via local Ollama.

    Fail-open: if Ollama is unavailable or times out, returns PASS with confidence=0.0
    so the pipeline is not blocked by infrastructure issues.
    """

    _analyzer_id = "walacor.llama_guard.v3"

    def __init__(
        self,
        ollama_url: str,
        model: str = "llama-guard3:1b",
        timeout_ms: int = 1500,
        http_client: httpx.AsyncClient | None = None,
        role: str = "Agent",
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._timeout_ms = timeout_ms
        self._http_client = http_client
        self._role = role
        self._category_actions: dict[str, str] = {"S4": "block"}

    def configure(self, policies: list[dict]) -> None:
        """Reconfigure category actions from control plane content policies."""
        if not policies:
            return
        self._category_actions = {p["category"]: p["action"] for p in policies}

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    def _build_prompt(self, text: str) -> str:
        return _LLAMA_GUARD_TEMPLATE.format(role=self._role, content=text)

    def _parse_response(self, response_text: str) -> Decision:
        """Parse 'safe' or 'unsafe\\nS7,S10' from Llama Guard output."""
        lines = response_text.strip().splitlines()
        first = lines[0].strip().lower() if lines else ""

        if first == "safe":
            return Decision(
                verdict=Verdict.PASS,
                confidence=1.0,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="safe",
            )

        if first == "unsafe":
            # Parse category codes from second line
            categories: list[str] = []
            if len(lines) > 1:
                codes = [c.strip().upper() for c in lines[1].split(",") if c.strip()]
                categories = [_CATEGORY_MAP[c] for c in codes if c in _CATEGORY_MAP]

            if not categories:
                categories = ["unknown"]

            category = categories[0]
            # Check category actions from configure(), fall back to _BLOCK_CATEGORIES
            # Map S-codes back to check _category_actions (e.g., "S4" -> "block")
            action = None
            for code, cat_name in _CATEGORY_MAP.items():
                if cat_name == category and code in self._category_actions:
                    action = self._category_actions[code]
                    break
            if action == "block":
                verdict = Verdict.BLOCK
            elif action == "warn":
                verdict = Verdict.WARN
            elif action == "pass":
                verdict = Verdict.PASS
            else:
                verdict = Verdict.BLOCK if category in _BLOCK_CATEGORIES else Verdict.WARN
            reason = ",".join(categories)
            return Decision(
                verdict=verdict,
                confidence=0.95,
                analyzer_id=self.analyzer_id,
                category=category,
                reason=reason,
            )

        # Unexpected format — fail-open
        logger.warning("LlamaGuard unexpected response format: %r", response_text[:100])
        return Decision(
            verdict=Verdict.PASS,
            confidence=0.0,
            analyzer_id=self.analyzer_id,
            category="safety",
            reason="parse_error",
        )

    async def analyze(self, text: str) -> Decision:
        """Call Llama Guard via Ollama and return a safety Decision.

        Fail-open on httpx errors and timeouts.
        """
        prompt = self._build_prompt(text)
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        url = f"{self._ollama_url}/api/chat"
        timeout = self._timeout_ms / 1000.0

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload, timeout=timeout)
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, timeout=timeout)

            resp.raise_for_status()
            data = resp.json()
            content = (data.get("message") or {}).get("content") or ""
            return self._parse_response(content)

        except httpx.TimeoutException:
            logger.warning("LlamaGuard timeout after %.1fs: model=%s", timeout, self._model)
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="timeout",
            )
        except Exception as exc:
            logger.warning("LlamaGuard unavailable (fail-open): %s", exc)
            return Decision(
                verdict=Verdict.PASS,
                confidence=0.0,
                analyzer_id=self.analyzer_id,
                category="safety",
                reason="unavailable",
            )
