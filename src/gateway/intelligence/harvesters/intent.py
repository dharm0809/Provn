"""Phase 25 Task 16: Intent harvester.

Three signal sources feed divergence labels onto the intent verdict row:

1. **Immediate action check.** Some intents imply a tool call in the same
   turn (`web_search` → a web_search tool). When the classifier picked
   such a label and `tool_events_detail` shows no matching activity,
   the divergence signal is `"normal"` with source
   `immediate_action_mismatch`.

2. **Next-turn contradiction.** The harvester keeps an in-memory LRU of
   the last classification per session. When a new turn arrives on that
   session, it scans the follow-up prompt for explicit action keywords
   (`search for`, `look it up`, etc.). If a keyword hit points at an
   intent different from the prior turn's label, the PRIOR row is
   back-written with the corrected label and source
   `next_turn_contradiction`. The in-memory store is bounded and evicts
   oldest entries — a restart loses the short-horizon deferred signals,
   which is acceptable given the 10% cap on per-session training
   contribution (Task 17).

3. **Sampled teacher LLM.** At `teacher_sample_rate` probability per
   call, the harvester POSTs the prompt to an OpenAI-compatible chat
   endpoint (`teacher_url`) asking for a single-label classification.
   When the teacher's label disagrees with the classifier's, that label
   becomes the divergence signal with source `teacher_llm`. Every
   attempt increments a per-outcome Prometheus counter so operators can
   reason about teacher cost vs. harvest value.

All three are additive — a single call may emit any combination, each
wrapped so a failure in one doesn't block the others.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters.base import Harvester, HarvesterSignal
from gateway.metrics.prometheus import intent_teacher_samples_total

logger = logging.getLogger(__name__)


_INTENT_LABELS: frozenset[str] = frozenset({
    "normal", "web_search", "rag", "mcp_tools", "reasoning", "system_task",
})

# Keyword cues for next-turn contradiction. The list is deliberately
# small and focused on high-signal phrasings — a vague "tell me more"
# doesn't disambiguate prior classification.
_NEXT_TURN_WEB_SEARCH = re.compile(
    r"\b("
    r"search for|search online|web search|google (?:it|for)|"
    r"look (?:it|this) up|look up (?:the|a|some)|find online|fetch|browse|"
    r"actually,?\s+search|look online"
    r")\b",
    re.I,
)

# Web-search tool names across builtin + MCP + provider-native integrations.
_WEB_SEARCH_TOOL_NAMES = frozenset({
    "web_search",
    "search",          # generic MCP search tools
    "browse",          # Anthropic/OpenAI native
    "web_search_20250305",  # Anthropic beta
})


@dataclass
class _PendingIntent:
    request_id: str
    intent: str
    prompt: str


class IntentHarvester(Harvester):
    target_model = "intent"

    def __init__(
        self,
        db: IntelligenceDB,
        *,
        teacher_url: str | None = None,
        teacher_sample_rate: float = 0.0,
        http_client: Any | None = None,
        max_pending: int = 2000,
        rand: random.Random | None = None,
    ) -> None:
        self._db = db
        self._teacher_url = (teacher_url or "").strip()
        self._teacher_sample_rate = max(0.0, min(1.0, float(teacher_sample_rate or 0.0)))
        self._http = http_client
        self._pending: "OrderedDict[str, _PendingIntent]" = OrderedDict()
        self._max_pending = max(1, int(max_pending))
        # Injectable random source for deterministic tests. When None we
        # use the module-level `random` singleton — fine for production
        # since the outcome is purely statistical (1% sample).
        self._rand = rand or random

    async def process(self, signal: HarvesterSignal) -> None:
        if signal.request_id is None:
            return
        prompt = _stringify(signal.context.get("prompt") if isinstance(signal.context, dict) else "")
        session_id = signal.context.get("session_id") if isinstance(signal.context, dict) else None

        # 1. Immediate: web_search label + no matching tool = mis-classified normal.
        immediate = _check_immediate(signal.prediction, signal.response_payload)
        if immediate is not None:
            await asyncio.to_thread(
                _write_divergence, self._db.path, signal.request_id,
                immediate, "immediate_action_mismatch", prompt,
            )

        # 2. Deferred: check prior turn in this session against current prompt.
        if session_id:
            deferred = self._check_next_turn_contradiction(str(session_id), prompt)
            if deferred is not None:
                prior_rid, prior_prompt, teacher_label = deferred
                # Back-write uses the PRIOR turn's prompt as training_text
                # (the prior row is what's being relabeled, not the current).
                await asyncio.to_thread(
                    _write_divergence, self._db.path, prior_rid,
                    teacher_label, "next_turn_contradiction", prior_prompt,
                )
            # Store the current verdict as the new pending for this session.
            self._remember_pending(str(session_id), signal.request_id, signal.prediction, prompt)

        # 3. Sampled teacher LLM.
        await self._maybe_sample_teacher(signal)

    # ── Immediate check ─────────────────────────────────────────────────

    # (static helper `_check_immediate` below — isolated for testability.)

    # ── Next-turn contradiction ─────────────────────────────────────────

    def _check_next_turn_contradiction(
        self, session_id: str, current_prompt: str,
    ) -> tuple[str, str, str] | None:
        """Return (prior_request_id, prior_prompt, corrected_label) when the
        new turn contradicts the prior classification; None otherwise.
        """
        prior = self._pending.get(session_id)
        if prior is None:
            return None
        # Only flag when the NEW prompt carries an explicit action cue
        # AND the prior label didn't already match it.
        if _NEXT_TURN_WEB_SEARCH.search(current_prompt) and prior.intent != "web_search":
            return prior.request_id, prior.prompt, "web_search"
        return None

    def _remember_pending(
        self, session_id: str, request_id: str, intent: str, prompt: str,
    ) -> None:
        # OrderedDict move-to-end gives us LRU eviction on insert.
        self._pending[session_id] = _PendingIntent(
            request_id=request_id, intent=intent, prompt=prompt,
        )
        self._pending.move_to_end(session_id)
        while len(self._pending) > self._max_pending:
            self._pending.popitem(last=False)

    # ── Teacher LLM sample ──────────────────────────────────────────────

    async def _maybe_sample_teacher(self, signal: HarvesterSignal) -> None:
        if not self._teacher_url or self._teacher_sample_rate <= 0.0:
            return
        if self._http is None:
            return
        if self._rand.random() >= self._teacher_sample_rate:
            intent_teacher_samples_total.labels(outcome="skipped").inc()
            return

        prompt = _stringify(signal.context.get("prompt") if isinstance(signal.context, dict) else "")
        if not prompt:
            intent_teacher_samples_total.labels(outcome="skipped").inc()
            return

        try:
            label = await self._call_teacher(prompt)
        except Exception:
            intent_teacher_samples_total.labels(outcome="failed").inc()
            logger.debug("Intent teacher LLM call failed (non-fatal)", exc_info=True)
            return

        if label is None or label not in _INTENT_LABELS:
            intent_teacher_samples_total.labels(outcome="failed").inc()
            return

        intent_teacher_samples_total.labels(outcome="called").inc()
        if label == signal.prediction:
            # Agreement — nothing to learn.
            return
        await asyncio.to_thread(
            _write_divergence, self._db.path, signal.request_id,
            label, "teacher_llm", prompt,
        )

    async def _call_teacher(self, prompt: str) -> str | None:
        """POST an OpenAI-compatible chat completion request to the teacher.

        Returns the parsed first-token intent label, or None when the
        teacher's response doesn't match the known label vocabulary.
        """
        body = {
            "model": "teacher",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Classify the user message into exactly one of: "
                        "normal, web_search, rag, mcp_tools, reasoning, system_task. "
                        "Respond with the single label only — no punctuation, no "
                        "explanation."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 16,
        }
        resp = await self._http.post(self._teacher_url, json=body, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        # OpenAI shape: choices[0].message.content.
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        if not isinstance(content, str):
            return None
        # First token only, lowercased, trimmed.
        token = content.strip().split()[0].strip(" .,:;!?\"'").lower() if content.strip() else ""
        return token or None


# ── Module-level helpers (kept stateless so they're testable in isolation) ──


def _check_immediate(prediction: str, payload: Any) -> str | None:
    """Return a divergence label when the immediate-action check fires.

    Currently limited to the web_search case (see plan Task 16). Any
    other `prediction` returns None — the remaining intent labels don't
    have a clean same-turn ground truth inside the response payload.
    """
    if prediction != "web_search":
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("tool_events_detail")
    if not isinstance(events, list):
        return None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        name = ev.get("tool_name")
        if isinstance(name, str) and name.lower() in _WEB_SEARCH_TOOL_NAMES:
            return None  # Actually searched — no divergence.
    # Classified web_search but no search tool activity — mis-labeled as
    # web_search when it should have been normal.
    return "normal"


def _write_divergence(
    db_path: str, request_id: str, label: str, source: str,
    training_text: str = "",
) -> None:
    """Back-write a divergence label onto the latest intent row for `request_id`.

    `training_text` — when non-empty — is stored on the verdict row so the
    Task 17 dataset builder can train on the actual prompt. Stored only on
    rows that have a divergence signal (i.e. training candidates); normal
    verdicts remain text-free.
    """
    text_to_write = training_text if training_text else None
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE onnx_verdicts
            SET divergence_signal = ?,
                divergence_source = ?,
                training_text = COALESCE(?, training_text)
            WHERE id = (
                SELECT id FROM onnx_verdicts
                WHERE request_id = ? AND model_name = 'intent'
                ORDER BY timestamp DESC LIMIT 1
            )
            """,
            (label, source, text_to_write, request_id),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "IntentHarvester UPDATE failed request_id=%r label=%r source=%r",
            request_id, label, source, exc_info=True,
        )
    finally:
        conn.close()


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else ""
