# src/gateway/adaptive/request_classifier.py
"""Smart request classification — body > headers > prompt.

Detects OpenWebUI background tasks, synthetic traffic (curl/k6/etc.),
and falls back to prompt-based regex for backward compatibility.
"""
from __future__ import annotations

import re
import logging
from typing import Any

from gateway.adaptive.interfaces import RequestClassifier

logger = logging.getLogger(__name__)


class DefaultRequestClassifier(RequestClassifier):
    """Multi-source request classifier with priority: body > headers > prompt."""

    _BODY_TASK_TYPES = frozenset({
        "title_generation", "tags_generation", "query_generation",
        "emoji_generation", "follow_up_generation",
    })

    _SYNTHETIC_UA = (
        "curl/", "httpie/", "python-requests/", "python-httpx/",
        "k6/", "artillery/", "wrk", "ab/", "siege/", "vegeta",
    )

    _PROMPT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("title_generation", re.compile(
            r"generate a (?:concise|brief|short).*?title", re.IGNORECASE)),
        ("autocomplete", re.compile(
            r"### Task:.*?autocompletion system", re.IGNORECASE | re.DOTALL)),
        ("follow_up", re.compile(
            r"generate (?:\d+ )?(?:follow[- ]?up|suggested|relevant).*?question",
            re.IGNORECASE)),
        ("tag_generation", re.compile(
            r"generate (?:\d+ )?(?:concise )?tags?\b", re.IGNORECASE)),
        ("emoji_generation", re.compile(
            r"generate (?:a single |an? )?emoji", re.IGNORECASE)),
        ("search_query", re.compile(
            r"generate (?:a )?search query", re.IGNORECASE)),
    ]

    def classify(self, prompt: str, headers: dict[str, str],
                 body: dict[str, Any]) -> str:
        """Classify a request using priority: body > headers > prompt.

        Args:
            prompt: The user's prompt text (may be empty).
            headers: HTTP request headers (lowercased keys).
            body: Parsed JSON request body.

        Returns:
            Classification label: ``"system_task:<type>"``, ``"synthetic"``,
            or ``"user_message"``.
        """
        # Priority 1: explicit task field in request body
        task = body.get("task")
        if task and task in self._BODY_TASK_TYPES:
            return f"system_task:{task}"

        # Also check metadata.task (some OpenWebUI versions nest it)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            meta_task = metadata.get("task")
            if meta_task and meta_task in self._BODY_TASK_TYPES:
                return f"system_task:{meta_task}"

        # Priority 2: synthetic traffic detection via user-agent
        ua = headers.get("user-agent", "").lower()
        if any(s in ua for s in self._SYNTHETIC_UA):
            return "synthetic"

        # Priority 3: prompt-based fallback (regex)
        if prompt:
            text = prompt[:1000]
            for task_type, pattern in self._PROMPT_PATTERNS:
                if pattern.search(text):
                    return f"system_task:{task_type}"
            if text.lstrip().startswith("### Task:"):
                return "system_task"

        return "user_message"

