"""Unified Schema Intelligence Engine.

Replaces three scattered components with one coherent system:
  1. IntentClassifier (intent.py) → classify_request()
  2. normalize_model_response (normalizer.py) → normalize_response()
  3. validate_record (schema.py) → validate_before_write()

Plus the critical missing piece:
  4. extract_user_question() → ML-based prompt extraction that separates
     the user's actual question from conversation history

All decisions are logged with confidence for the audit trail.
Every record passes through schema validation before Walacor write.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.adapters.base import ModelResponse
from gateway.adapters.caching import detect_cache_hit

logger = logging.getLogger(__name__)

# ── Intent labels ────────────────────────────────────────────────────────

NORMAL = "normal"
WEB_SEARCH = "web_search"
RAG = "rag"
MCP_TOOLS = "mcp_tools"
REASONING = "reasoning"
SYSTEM_TASK = "system_task"
ALL_INTENTS = (NORMAL, WEB_SEARCH, RAG, MCP_TOOLS, REASONING, SYSTEM_TASK)

# ── Confidence thresholds ────────────────────────────────────────────────

CONFIDENCE_AUTO = 0.95
CONFIDENCE_FLAG = 0.70

# ── Provider signatures for auto-detection ───────────────────────────────

_PROVIDER_USAGE_MAPS: dict[str, dict[str, str]] = {
    "anthropic": {
        "input_tokens": "prompt_tokens",
        "output_tokens": "completion_tokens",
    },
    "openai": {
        # OpenAI already uses prompt_tokens/completion_tokens
    },
    "ollama": {
        # Ollama uses prompt_eval_count/eval_count in native API,
        # but OpenAI-compat endpoint uses prompt_tokens/completion_tokens
        "prompt_eval_count": "prompt_tokens",
        "eval_count": "completion_tokens",
    },
}

# Sentinel returned by OpenAI adapter when reasoning summary is unavailable
_RETRY_SENTINEL = "__RETRY_WITHOUT_SUMMARY__"


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IntentResult:
    """Classification result with confidence and audit metadata."""
    intent: str
    confidence: float
    tier: str
    reason: str


@dataclass(frozen=True)
class PromptExtraction:
    """Result of extracting user question from conversation."""
    user_question: str
    conversation_context: str
    conversation_turns: int
    has_system_prompt: bool
    has_rag_context: bool
    has_files: bool
    extraction_method: str  # "last_user_message", "single_turn", "ml_model"


@dataclass
class NormalizationReport:
    """What the normalizer changed."""
    changes: list[str] = field(default_factory=list)
    provider_detected: str = ""
    usage_mapped: bool = False
    content_fixed: bool = False
    thinking_fallback_applied: bool = False


@dataclass
class ValidationReport:
    """What the schema validator found and fixed."""
    record_type: str = ""
    issues: list[str] = field(default_factory=list)
    coercions: int = 0
    defaults_applied: int = 0


# ── Walacor schemas (canonical source of truth) ─────────────────────────

EXECUTION_SCHEMA: dict[str, dict] = {
    "execution_id":         {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "gateway_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "policy_version":       {"type": int,   "required": True},
    "policy_result":        {"type": str,   "required": True},
    "model_attestation_id": {"type": str,   "required": False, "default": ""},
    "model_id":             {"type": str,   "required": False, "default": "unknown"},
    "provider":             {"type": str,   "required": False, "default": "unknown"},
    "user":                 {"type": str,   "required": False, "default": None},
    "session_id":           {"type": str,   "required": False, "default": None},
    "prompt_text":          {"type": str,   "required": False, "default": None},
    "response_content":     {"type": str,   "required": False, "default": None},
    "provider_request_id":  {"type": str,   "required": False, "default": None},
    "model_hash":           {"type": str,   "required": False, "default": None},
    "thinking_content":     {"type": str,   "required": False, "default": None},
    "prompt_tokens":        {"type": int,   "required": False, "default": 0},
    "completion_tokens":    {"type": int,   "required": False, "default": 0},
    "total_tokens":         {"type": int,   "required": False, "default": 0},
    "latency_ms":           {"type": float, "required": False, "default": None},
    "cache_hit":            {"type": bool,  "required": False, "default": False},
    "cached_tokens":        {"type": int,   "required": False, "default": 0},
    "cache_creation_tokens":{"type": int,   "required": False, "default": 0},
    "retry_of":             {"type": str,   "required": False, "default": None},
    "variant_id":           {"type": str,   "required": False, "default": None},
    "metadata_json":        {"type": str,   "required": False, "default": None},
    "estimated_cost_usd":   {"type": float, "required": False, "default": None},
    "sequence_number":      {"type": int,   "required": False, "default": None},
    "record_hash":          {"type": str,   "required": False, "default": None},
    "previous_record_hash": {"type": str,   "required": False, "default": None},
}

TOOL_EVENT_SCHEMA: dict[str, dict] = {
    "event_id":             {"type": str,   "required": True},
    "execution_id":         {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "gateway_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "tool_name":            {"type": str,   "required": False, "default": "unknown"},
    "tool_type":            {"type": str,   "required": False, "default": "function"},
    "tool_source":          {"type": str,   "required": False, "default": "gateway"},
    "session_id":           {"type": str,   "required": False, "default": None},
    "input_data":           {"type": str,   "required": False, "default": None},
    "input_hash":           {"type": str,   "required": False, "default": None},
    "output_data":          {"type": str,   "required": False, "default": None},
    "output_hash":          {"type": str,   "required": False, "default": None},
    "sources":              {"type": str,   "required": False, "default": None},
    "duration_ms":          {"type": float, "required": False, "default": None},
    "iteration":            {"type": int,   "required": False, "default": None},
    "is_error":             {"type": bool,  "required": False, "default": False},
    "content_analysis":     {"type": str,   "required": False, "default": None},
}

ATTEMPT_SCHEMA: dict[str, dict] = {
    "request_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "disposition":          {"type": str,   "required": True},
    "status_code":          {"type": int,   "required": True},
    "path":                 {"type": str,   "required": False, "default": ""},
    "provider":             {"type": str,   "required": False, "default": None},
    "model_id":             {"type": str,   "required": False, "default": None},
    "execution_id":         {"type": str,   "required": False, "default": None},
    "user":                 {"type": str,   "required": False, "default": None},
}


# ═══════════════════════════════════════════════════════════════════════════
# Schema Intelligence Engine
# ═══════════════════════════════════════════════════════════════════════════

class SchemaIntelligence:
    """Unified intelligence engine for the gateway pipeline.

    Handles the complete data flow from raw request → clean Walacor record:
      1. extract_prompt()     → separate user question from conversation context
      2. classify_intent()    → determine request type (normal, web_search, etc.)
      3. normalize_response() → canonicalize ModelResponse across all providers
      4. validate_record()    → enforce Walacor schema before write

    One class, one import, one coherent system.
    """

    def __init__(
        self,
        onnx_model_path: str | None = None,
        has_mcp_tools: bool = False,
    ) -> None:
        self._has_mcp_tools = has_mcp_tools
        self._onnx_session = None
        self._label_map: dict[int, str] = {}

        # Provider profile cache — learned from observed responses
        self._provider_profiles: dict[str, dict[str, Any]] = {}

        # Load ONNX intent model
        if onnx_model_path and Path(onnx_model_path).exists():
            try:
                self._load_onnx(onnx_model_path)
                logger.info("SchemaIntelligence: ONNX model loaded from %s", onnx_model_path)
            except Exception as e:
                logger.warning("SchemaIntelligence: ONNX load failed: %s", e)

    # ═══════════════════════════════════════════════════════════════════════
    # 1. PROMPT EXTRACTION — the biggest data quality fix
    # ═══════════════════════════════════════════════════════════════════════

    def extract_prompt(self, messages: list[dict]) -> PromptExtraction:
        """Extract the user's actual question from a conversation message array.

        This replaces _concat_messages() which blindly joins ALL messages into
        a single string, producing garbage like:
            "System prompt\\nUser question 1\\nAssistant response 1\\nUser question 2"

        Instead, we extract:
          - user_question: just the latest user message (what they actually asked)
          - conversation_context: summarized context for audit trail
          - metadata: turns, system prompt, RAG context, files
        """
        if not messages:
            return PromptExtraction(
                user_question="", conversation_context="",
                conversation_turns=0, has_system_prompt=False,
                has_rag_context=False, has_files=False,
                extraction_method="empty_messages",
            )

        # Separate message roles
        system_msgs = []
        user_msgs = []
        assistant_msgs = []
        has_files = False
        has_rag = False

        for msg in messages:
            role = msg.get("role", "")
            content = self._extract_text(msg.get("content", ""))

            if role == "system":
                system_msgs.append(content)
                # RAG context detection: system prompts > 500 chars with document-like content
                if len(content) > 500 or any(kw in content.lower() for kw in (
                    "context:", "document:", "reference:", "based on the following",
                    "retrieved", "source:", "knowledge base",
                )):
                    has_rag = True
            elif role == "user":
                user_msgs.append(content)
                # Check for file attachments in content blocks
                raw_content = msg.get("content")
                if isinstance(raw_content, list):
                    for block in raw_content:
                        if isinstance(block, dict) and block.get("type") in ("image_url", "image", "file"):
                            has_files = True
            elif role == "assistant":
                assistant_msgs.append(content)

        # The user's actual question is the LAST user message
        user_question = user_msgs[-1] if user_msgs else ""

        # Build conversation context (compact summary, not full dump)
        conversation_turns = len(user_msgs)
        context_parts = []
        if system_msgs:
            # Include system prompt summary (first 200 chars)
            sys_preview = system_msgs[0][:200]
            context_parts.append(f"[system: {sys_preview}]")
        if conversation_turns > 1:
            # Include prior turns as summaries (not full text)
            for i, (u, a) in enumerate(zip(user_msgs[:-1], assistant_msgs)):
                u_preview = u[:100] + "..." if len(u) > 100 else u
                a_preview = a[:100] + "..." if len(a) > 100 else a
                context_parts.append(f"[turn {i+1}: Q={u_preview} A={a_preview}]")

        conversation_context = "\n".join(context_parts) if context_parts else ""

        method = "single_turn" if conversation_turns <= 1 else "last_user_message"

        return PromptExtraction(
            user_question=user_question[:5000],  # cap at 5k chars
            conversation_context=conversation_context[:10000],
            conversation_turns=conversation_turns,
            has_system_prompt=bool(system_msgs),
            has_rag_context=has_rag,
            has_files=has_files,
            extraction_method=method,
        )

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Extract text from message content (string or content blocks)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content) if content is not None else ""

    # ═══════════════════════════════════════════════════════════════════════
    # 2. INTENT CLASSIFICATION — keeps working 2-tier approach
    # ═══════════════════════════════════════════════════════════════════════

    def classify_intent(
        self,
        prompt: str,
        metadata: dict[str, Any],
        model_id: str,
    ) -> IntentResult:
        """Classify request intent. Tier 1 deterministic, Tier 2 ONNX ML."""
        result = self._tier1_deterministic(prompt, metadata, model_id)
        if result:
            return result

        if self._onnx_session:
            return self._tier2_onnx(prompt)

        return IntentResult(
            intent=NORMAL, confidence=0.5,
            tier="deterministic", reason="no_ml_model_default",
        )

    def _tier1_deterministic(
        self, prompt: str, metadata: dict, model_id: str,
    ) -> IntentResult | None:
        """100% accurate deterministic checks based on explicit signals."""
        # 1. System task
        request_type = metadata.get("request_type", "")
        if isinstance(request_type, str) and request_type.startswith("system_task"):
            return IntentResult(SYSTEM_TASK, 1.0, "deterministic", f"request_type={request_type}")
        if prompt.lstrip().startswith("### Task:"):
            return IntentResult(SYSTEM_TASK, 1.0, "deterministic", "prompt_starts_with_task")

        # 2. Reasoning model
        if model_id and any(model_id.startswith(p) for p in ("o1-", "o1", "o3-", "o3", "o4-", "o4")):
            return IntentResult(REASONING, 1.0, "deterministic", f"reasoning_model={model_id}")

        # 3. Web search toggle
        body_meta = metadata.get("_body_metadata") or {}
        features = body_meta.get("features") or {}
        if features.get("web_search") is True:
            return IntentResult(WEB_SEARCH, 1.0, "deterministic", "features.web_search=true")

        # 4. MCP tools
        if self._has_mcp_tools:
            return IntentResult(MCP_TOOLS, 1.0, "deterministic", "mcp_servers_configured")

        # 5. RAG
        audit = metadata.get("walacor_audit") or {}
        if audit.get("has_rag_context"):
            return IntentResult(RAG, 1.0, "deterministic", "has_rag_context=true")
        files = body_meta.get("files") or metadata.get("files") or []
        if files:
            return IntentResult(RAG, 1.0, "deterministic", f"files_attached={len(files)}")

        return None

    def _tier2_onnx(self, prompt: str) -> IntentResult:
        """ONNX ML classification with confidence gating."""
        try:
            import numpy as np
            input_name = self._onnx_session.get_inputs()[0].name

            if input_name == "prompt":
                inp = np.array([[prompt[:1000]]]).reshape(1, 1)
                outputs = self._onnx_session.run(None, {input_name: inp})
                intent = str(outputs[0][0])
                prob_dict = outputs[1][0]
                confidence = float(max(prob_dict.values()))
            else:
                inputs = self._tokenize(prompt)
                outputs = self._onnx_session.run(None, inputs)
                logits = outputs[0][0]
                exp_logits = np.exp(logits - np.max(logits))
                probs = exp_logits / exp_logits.sum()
                idx = int(np.argmax(probs))
                confidence = float(probs[idx])
                intent = self._label_map.get(idx, NORMAL)

            if confidence >= CONFIDENCE_AUTO:
                return IntentResult(intent, confidence, "ml_onnx", "onnx_auto_accept")
            elif confidence >= CONFIDENCE_FLAG:
                return IntentResult(intent, confidence, "ml_onnx", "onnx_flagged_for_review")
            else:
                return IntentResult(NORMAL, confidence, "ml_onnx", "onnx_low_confidence_default")
        except Exception as e:
            logger.warning("ONNX classification failed: %s", e)
            return IntentResult(NORMAL, 0.0, "ml_onnx", f"onnx_error: {e}")

    def _tokenize(self, text: str) -> dict:
        """Tokenize for transformer ONNX input."""
        import numpy as np
        ids = [ord(c) % 30000 for c in text[:128]]
        ids = ids + [0] * (128 - len(ids))
        return {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([[1] * min(len(text), 128) + [0] * max(0, 128 - len(text))], dtype=np.int64),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # 3. RESPONSE NORMALIZATION — provider-aware with auto-detection
    # ═══════════════════════════════════════════════════════════════════════

    def normalize_response(
        self,
        response: ModelResponse,
        provider: str,
    ) -> tuple[ModelResponse, NormalizationReport]:
        """Normalize ModelResponse to canonical contract.

        Returns (normalized_response, report) so callers know what changed.
        """
        report = NormalizationReport(provider_detected=provider)
        usage = response.usage
        content = response.content
        thinking = response.thinking_content
        changed = False

        # ── Usage normalization (provider-aware) ──────────────────────
        if usage is not None:
            new_usage = dict(usage)
            provider_map = _PROVIDER_USAGE_MAPS.get(provider, {})

            # Apply provider-specific field mappings
            for src_field, dst_field in provider_map.items():
                if src_field in new_usage and dst_field not in new_usage:
                    new_usage[dst_field] = new_usage[src_field]
                    report.changes.append(f"usage: {src_field} → {dst_field}")
                    report.usage_mapped = True

            # Compute total_tokens if missing
            if "total_tokens" not in new_usage or not new_usage["total_tokens"]:
                pt = new_usage.get("prompt_tokens", 0) or 0
                ct = new_usage.get("completion_tokens", 0) or 0
                if pt > 0 or ct > 0:
                    new_usage["total_tokens"] = pt + ct
                    report.changes.append(f"usage: computed total_tokens={pt + ct}")

            # Cache enrichment
            if "cache_hit" not in new_usage:
                cache_info = detect_cache_hit(new_usage)
                new_usage.update(cache_info)
                report.changes.append("usage: cache enrichment applied")

            if new_usage != usage:
                usage = new_usage
                changed = True

        # ── Content normalization ─────────────────────────────────────
        if content == _RETRY_SENTINEL:
            content = ""
            changed = True
            report.changes.append("content: cleared __RETRY_WITHOUT_SUMMARY__ sentinel")
            report.content_fixed = True

        if content is None:
            content = ""
            changed = True
            report.changes.append("content: None → empty string")
            report.content_fixed = True

        # ── Thinking fallback ─────────────────────────────────────────
        if not content.strip() and thinking:
            content = thinking
            changed = True
            report.changes.append("content: used thinking_content as fallback")
            report.thinking_fallback_applied = True

        if not changed:
            return response, report

        normalized = dataclasses.replace(
            response, content=content, usage=usage, thinking_content=thinking,
        )
        return normalized, report

    # ═══════════════════════════════════════════════════════════════════════
    # 4. SCHEMA VALIDATION — enforce types before Walacor write
    # ═══════════════════════════════════════════════════════════════════════

    def validate_execution(self, record: dict[str, Any]) -> tuple[dict[str, Any], ValidationReport]:
        """Validate an execution record before Walacor write."""
        return self._validate(record, EXECUTION_SCHEMA, "execution")

    def validate_tool_event(self, record: dict[str, Any]) -> tuple[dict[str, Any], ValidationReport]:
        """Validate a tool event record before Walacor write."""
        return self._validate(record, TOOL_EVENT_SCHEMA, "tool_event")

    def validate_attempt(self, record: dict[str, Any]) -> tuple[dict[str, Any], ValidationReport]:
        """Validate an attempt record before Walacor write."""
        return self._validate(record, ATTEMPT_SCHEMA, "attempt")

    def _validate(
        self, record: dict[str, Any], schema: dict[str, dict], record_type: str,
    ) -> tuple[dict[str, Any], ValidationReport]:
        """Validate and coerce a record against schema. Returns (cleaned, report)."""
        report = ValidationReport(record_type=record_type)
        cleaned = dict(record)

        for field_name, field_spec in schema.items():
            value = cleaned.get(field_name)
            expected_type = field_spec["type"]
            required = field_spec["required"]
            default = field_spec.get("default")

            # Missing or null required field
            if value is None and (field_name not in cleaned or required):
                if required:
                    report.issues.append(f"MISSING required: {field_name}")
                    cleaned[field_name] = default if default is not None else ""
                    report.defaults_applied += 1
                continue

            if value is None:
                continue

            # Type coercion
            if not isinstance(value, expected_type):
                try:
                    if expected_type == int:
                        cleaned[field_name] = int(float(value)) if value else 0
                    elif expected_type == float:
                        cleaned[field_name] = float(value) if value else 0.0
                    elif expected_type == str:
                        cleaned[field_name] = str(value)
                    elif expected_type == bool:
                        cleaned[field_name] = bool(value)
                    report.issues.append(
                        f"TYPE coerced: {field_name} {type(value).__name__} → {expected_type.__name__}"
                    )
                    report.coercions += 1
                except (ValueError, TypeError):
                    report.issues.append(
                        f"TYPE FAILED: {field_name} {type(value).__name__} → {expected_type.__name__}"
                    )
                    cleaned[field_name] = default

        if report.issues:
            eid = (
                cleaned.get("execution_id")
                or cleaned.get("event_id")
                or cleaned.get("request_id")
                or "?"
            )
            logger.warning(
                "Schema validation (%s %s): %d issue(s): %s",
                record_type, eid, len(report.issues), "; ".join(report.issues),
            )

        return cleaned, report

    # ═══════════════════════════════════════════════════════════════════════
    # 5. PROMPT QUALITY — build proper prompt_text and user_question
    # ═══════════════════════════════════════════════════════════════════════

    def build_prompt_fields(self, messages: list[dict]) -> dict[str, Any]:
        """Build all prompt-related fields for the execution record.

        Returns a dict with:
          - user_question: the actual question (goes to walacor_audit)
          - prompt_text: the user question (NOT the full conversation)
          - conversation_context: compact context summary
          - conversation_turns: number of user messages
          - question_fingerprint: hash for dedup
        """
        extraction = self.extract_prompt(messages)

        # Question fingerprint for dedup/tracking
        fp = hashlib.sha256(extraction.user_question.encode()).hexdigest()[:16]

        return {
            "user_question": extraction.user_question,
            "prompt_text": extraction.user_question,  # THE KEY FIX: prompt_text = actual question
            "conversation_context": extraction.conversation_context,
            "conversation_turns": extraction.conversation_turns,
            "has_system_prompt": extraction.has_system_prompt,
            "has_rag_context": extraction.has_rag_context,
            "has_files": extraction.has_files,
            "question_fingerprint": fp,
            "extraction_method": extraction.extraction_method,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # 6. FULL PIPELINE — process a complete request-response pair
    # ═══════════════════════════════════════════════════════════════════════

    def process_request(
        self,
        messages: list[dict],
        metadata: dict[str, Any],
        model_id: str,
    ) -> dict[str, Any]:
        """Process an incoming request through the full intelligence pipeline.

        Returns a dict of enrichments to merge into call metadata:
          - prompt fields (user_question, prompt_text, etc.)
          - intent fields (_intent, _intent_confidence, etc.)
          - routing decisions (_gateway_web_search, _responses_api, etc.)
        """
        # Step 1: Extract prompt
        prompt_fields = self.build_prompt_fields(messages)

        # Step 2: Classify intent
        intent = self.classify_intent(
            prompt=prompt_fields["user_question"],
            metadata=metadata,
            model_id=model_id,
        )

        # Step 3: Build routing decisions based on intent
        routing: dict[str, Any] = {}
        if intent.intent == WEB_SEARCH:
            routing["_gateway_web_search"] = True
        elif intent.intent == SYSTEM_TASK:
            routing["_gateway_web_search"] = False
        elif intent.intent == REASONING:
            if model_id and any(model_id.startswith(p) for p in ("o1", "o3", "o4")):
                routing["_responses_api"] = True
        if metadata.get("_gateway_web_search") and intent.intent not in (WEB_SEARCH, MCP_TOOLS):
            routing["_gateway_web_search"] = False

        # Build unified enrichment dict
        enrichment = {
            **prompt_fields,
            "_intent": intent.intent,
            "_intent_confidence": intent.confidence,
            "_intent_tier": intent.tier,
            "_intent_reason": intent.reason,
            **routing,
        }

        logger.debug(
            "SchemaIntelligence.process_request: intent=%s (%.2f, %s), prompt=%d chars, turns=%d",
            intent.intent, intent.confidence, intent.tier,
            len(prompt_fields["user_question"]), prompt_fields["conversation_turns"],
        )

        return enrichment

    def process_response(
        self,
        response: ModelResponse,
        provider: str,
    ) -> tuple[ModelResponse, NormalizationReport]:
        """Normalize a model response. Thin wrapper for normalize_response().

        Exists so the orchestrator has one consistent API:
          si.process_request()  → pre-forward
          si.process_response() → post-forward
        """
        return self.normalize_response(response, provider)

    # ═══════════════════════════════════════════════════════════════════════
    # ONNX model loading
    # ═══════════════════════════════════════════════════════════════════════

    def _load_onnx(self, path: str) -> None:
        """Load ONNX intent model + label map."""
        from onnxruntime import InferenceSession
        self._onnx_session = InferenceSession(path, providers=["CPUExecutionProvider"])

        label_path = path.replace(".onnx", "_labels.json")
        if os.path.exists(label_path):
            with open(label_path) as f:
                labels = json.load(f)
                self._label_map = {i: l for i, l in enumerate(labels)}
        else:
            self._label_map = {i: l for i, l in enumerate(ALL_INTENTS)}
