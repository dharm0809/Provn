"""Safety harvester — LlamaGuard as teacher.

When both the ONNX `SafetyClassifier` (student) and `LlamaGuardAnalyzer`
(teacher) ran on the same response, compare their emitted categories. If
they disagree, back-write LlamaGuard's label (normalized into the
classifier's 8-label vocabulary) onto the safety verdict row. Agreement
contributes nothing — the classifier was already right.

Design notes
------------
* LlamaGuard's categories (`walacor.llama_guard.v3`, see `content/llama_guard.py`
  `_CATEGORY_MAP`) are finer-grained than the classifier's. A mapping
  table narrows them to the 8 labels SafetyClassifier can predict; unmappable
  labels (elections, privacy_pii, etc.) yield no training signal.
* A `confidence <= 0.0` LlamaGuard decision means fail-open (timeout /
  parse error) and is not a real teacher verdict — skip those.
* The UPDATE plan calls for `WHERE model_name='safety' AND request_id=?`
  WITHOUT a "latest-only" subquery — per-row signals are OK here because
  the classifier's verdict row is already 1:1 with an `analyze()` call,
  and the teacher signal applies to all such rows in the same request
  (streaming chunks, retries).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters.base import Harvester, HarvesterSignal

logger = logging.getLogger(__name__)

_SAFETY_ANALYZER_ID = "truzenai.safety.v1"
_LLAMA_GUARD_ANALYZER_ID = "walacor.llama_guard.v3"


# Maps LlamaGuard `category` values (see `content/llama_guard._CATEGORY_MAP`
# plus the literal "safety" pass label) into SafetyClassifier's label
# vocabulary (see `content/safety_classifier._labels`):
#   safe, violence, sexual, criminal, self_harm, hate_speech,
#   dangerous, child_safety.
#
# Entries NOT present → the teacher label has no equivalent in the
# classifier's output space, so we skip the training signal for that row.
LLAMA_GUARD_TO_SAFETY_LABEL: dict[str, str] = {
    # LlamaGuard safe pass → classifier "safe"
    "safety": "safe",
    # Direct / near-direct mappings
    "violent_crimes": "violence",
    "nonviolent_crimes": "criminal",
    "sex_crimes": "sexual",
    "sexual_content": "sexual",
    "self_harm": "self_harm",
    "child_safety": "child_safety",
    "hate_discrimination": "hate_speech",
    "indiscriminate_weapons": "dangerous",
    # NOT mapped (intentionally left out): defamation, specialized_advice,
    # privacy_pii, intellectual_property, elections, code_interpreter_abuse.
    # These don't correspond to any of the classifier's 8 labels and would
    # just inject noise into the training pool.
}


def _find_decision(decisions: list[Any], analyzer_id: str) -> dict | None:
    for d in decisions:
        if isinstance(d, dict) and d.get("analyzer_id") == analyzer_id:
            return d
    return None


def _normalize_safety_category(category: Any) -> str | None:
    """Collapse the classifier's pass category (`"safety"`) to `"safe"`.

    SafetyClassifier emits `category="safety"` on PASS and
    `category=<label>` on flagged results. Normalizing to the training
    label space keeps the agreement check honest.
    """
    if not isinstance(category, str) or not category:
        return None
    if category == "safety":
        return "safe"
    return category


class SafetyHarvester(Harvester):
    target_model = "safety"

    def __init__(self, db: IntelligenceDB) -> None:
        self._db = db

    async def process(self, signal: HarvesterSignal) -> None:
        import asyncio

        if signal.request_id is None:
            return

        decisions = _extract_decisions(signal.response_payload)
        if not decisions:
            return

        safety_d = _find_decision(decisions, _SAFETY_ANALYZER_ID)
        llama_d = _find_decision(decisions, _LLAMA_GUARD_ANALYZER_ID)
        if safety_d is None or llama_d is None:
            # Need BOTH to have run — no teacher means no training signal
            # and no student means no row to update either.
            return

        # LlamaGuard fail-open yields confidence 0.0 (timeout / parse
        # error). Treating that as a teacher verdict would pollute the
        # signal pool with "PASS because we couldn't check", which would
        # actively mis-train the classifier.
        try:
            llama_conf = float(llama_d.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            llama_conf = 0.0
        if llama_conf <= 0.0:
            return

        safety_norm = _normalize_safety_category(safety_d.get("category"))
        if safety_norm is None:
            return

        llama_raw = llama_d.get("category")
        if not isinstance(llama_raw, str):
            return
        teacher_label = LLAMA_GUARD_TO_SAFETY_LABEL.get(llama_raw)
        if teacher_label is None:
            # Unmappable LlamaGuard category — no usable training signal.
            return

        if safety_norm == teacher_label:
            # Agreement post-normalization. Nothing to learn here.
            return

        # The analyzed text is the MODEL RESPONSE (SafetyClassifier and
        # LlamaGuard both analyze the response, not the prompt). Pull it
        # from context if the orchestrator populated it.
        training_text = ""
        if isinstance(signal.context, dict):
            resp = signal.context.get("response")
            if isinstance(resp, str):
                training_text = resp
        await asyncio.to_thread(
            self._update_divergence, signal.request_id, teacher_label, training_text,
        )

    def _update_divergence(
        self, request_id: str, teacher_label: str, training_text: str,
    ) -> None:
        text_to_write = training_text if training_text else None
        conn = sqlite3.connect(self._db.path)
        try:
            conn.execute(
                """
                UPDATE onnx_verdicts
                SET divergence_signal = ?,
                    divergence_source = 'llama_guard_disagreement',
                    training_text = COALESCE(?, training_text)
                WHERE model_name = 'safety' AND request_id = ?
                """,
                (teacher_label, text_to_write, request_id),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "SafetyHarvester UPDATE failed request_id=%r label=%r",
                request_id, teacher_label, exc_info=True,
            )
        finally:
            conn.close()


def _extract_decisions(payload: Any) -> list[Any]:
    """Pull `analyzer_decisions` out of the orchestrator's metadata dict.

    Matches the defensive shape handling in the SchemaMapper harvester —
    mistyped branches return [] so `process` falls through to its no-op.
    """
    if not isinstance(payload, dict):
        return []
    decisions = payload.get("analyzer_decisions")
    if not isinstance(decisions, list):
        return []
    return decisions
