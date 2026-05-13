"""Canonical schema — the universal format every LLM response maps to.

Regardless of provider (OpenAI, Anthropic, Gemini, Ollama, Cohere, etc.),
the SchemaMapper normalizes responses into this structure. Fields classified
as UNKNOWN go into `overflow` — never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CanonicalUsage:
    """Token usage in canonical form."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        self.compute_total()

    def compute_total(self) -> None:
        """Fill total_tokens if missing but components are present."""
        if not self.total_tokens and (self.prompt_tokens or self.completion_tokens):
            self.total_tokens = (self.prompt_tokens or 0) + (self.completion_tokens or 0)


@dataclass
class CanonicalToolCall:
    """A single tool/function call from the LLM response."""

    id: str = ""
    name: str = ""
    arguments: dict | str = field(default_factory=dict)
    type: str = "function"


@dataclass
class CanonicalCitation:
    """A source citation (Perplexity, Cohere, Gemini)."""

    url: str = ""
    title: str | None = None
    snippet: str | None = None


@dataclass
class CanonicalTiming:
    """Inference timing breakdown (Ollama, Groq, Cerebras)."""

    total_ms: float | None = None
    prompt_ms: float | None = None
    completion_ms: float | None = None
    queue_ms: float | None = None


@dataclass
class CanonicalSafety:
    """Content safety results (Gemini, Azure)."""

    blocked: bool = False
    categories: dict = field(default_factory=dict)


@dataclass
class MappingReport:
    """Metadata produced by SchemaMapper describing how well the mapping succeeded.

    ``confidence`` reports coverage-weighted classification quality:
    ``sum(mapped_confidences) / len(all_classified_fields)`` where
    ``all_classified_fields`` excludes ``envelope``-labelled keys (provider
    response-shape boilerplate like ``object``/``created``/``role`` that
    have no canonical class). Returns 0.0 when no field is classifiable.

    ``confidence_on_mapped`` keeps the legacy "average over only-MAPPED
    fields" semantic for downstream consumers that interpret confidence as
    "how sure were we about the classifications we did make". Defaults to
    the same value as ``confidence`` for backward compatibility.

    Why coverage-weighted is the operator-visible metric:
    on a real OpenAI response with 7 mapped + 8 UNKNOWN the previous
    formula returned ``1.0`` even though only 47% of fields had a
    canonical class. Operators reading ``schema_mapper_confidence=1.0``
    assumed the mapper was perfect; the coverage-weighted form
    correctly reports ``0.47`` and is honest about the gap.
    """

    confidence: float = 1.0
    confidence_on_mapped: float = 1.0
    incomplete: bool = False
    mapped_fields: list[str] = field(default_factory=list)
    unmapped_fields: list[str] = field(default_factory=list)


@dataclass
class CanonicalResponse:
    """Universal LLM response representation.

    Every provider response, regardless of format, maps to this structure.
    The SchemaMapper's ONNX model classifies each field in the raw JSON
    by understanding value semantics (not just field names), then assembles
    this canonical form.
    """

    # Core content
    content: str = ""
    thinking_content: str | None = None
    finish_reason: str = "stop"

    # Identity
    response_id: str | None = None
    model: str | None = None
    model_hash: str | None = None

    # Token usage
    usage: CanonicalUsage = field(default_factory=CanonicalUsage)

    # Tool calls
    tool_calls: list[CanonicalToolCall] = field(default_factory=list)

    # Citations
    citations: list[CanonicalCitation] = field(default_factory=list)

    # Timing
    timing: CanonicalTiming | None = None

    # Safety
    safety: CanonicalSafety | None = None

    # Self-healing: unknown fields preserved here, never dropped
    overflow: dict[str, Any] = field(default_factory=dict)

    # Mapping metadata (populated by SchemaMapper after assembly)
    mapping: MappingReport = field(default_factory=MappingReport)

    def is_complete(self) -> bool:
        """Check if the mapping captured the essential fields."""
        return bool(self.content or self.thinking_content) and not self.mapping.incomplete


# ── Canonical field labels for the ONNX classifier ──────────────────────────
#
# IMPORTANT: ``CANONICAL_LABELS`` is the broader operator-visible vocabulary.
# The PRODUCTION ONNX model's class set is the (smaller) list in
# ``schema_mapper_labels.json`` — that file is the source of truth for
# whatever the deployed classifier actually emits. ``SchemaMapper`` loads
# ``schema_mapper_labels.json`` and asserts the count matches the ONNX
# class count at startup (see ``mapper.SchemaMapper._validate_labels``);
# adding a new label here that the model wasn't trained on does NOT change
# what the model predicts — it's only useful as a target for post-hoc
# rewrites (the ``envelope`` tag below, applied after ONNX classification)
# or as a documentation list of "labels we eventually want trained in".

# ENVELOPE_LABEL — synthetic, post-classification tag for provider response-
# shape boilerplate (``object``, ``created``, ``role``, ``index``, …). NOT a
# Walacor schema field; emitted by the mapper to exclude such fields from
# both ``unmapped`` and ``overflow_keys``. Adding a new label requires
# retraining the ONNX model AND updating ``schema_mapper_labels.json`` —
# the ``envelope`` tag is the one exception (post-hoc rewrite, never the
# model's direct output).
ENVELOPE_LABEL = "envelope"

CANONICAL_LABELS = [
    "content",               # LLM text answer
    "thinking_content",      # Reasoning / chain-of-thought
    "finish_reason",         # stop/length/tool_calls/etc.
    "response_id",           # Provider response ID
    "model",                 # Model name/ID
    "model_hash",            # Model fingerprint / system_fingerprint
    "prompt_tokens",         # Input token count
    "completion_tokens",     # Output token count
    "total_tokens",          # Sum of prompt + completion
    "reasoning_tokens",      # Thinking/reasoning token count
    "cached_tokens",         # Cache hit tokens
    "cache_creation_tokens", # Cache write tokens
    "cost_usd",              # Per-request cost
    "tool_call_id",          # Tool call identifier
    "tool_call_name",        # Function/tool name
    "tool_call_arguments",   # Function arguments
    "tool_call_type",        # Tool type (function, etc.)
    "citation_url",          # Source URL
    "citation_title",        # Source title
    "timing_value",          # Timing measurement (ms/ns)
    "safety_blocked",        # Content blocked flag
    "safety_category",       # Safety category score
    ENVELOPE_LABEL,          # Provider response-shape boilerplate (synthetic)
    "UNKNOWN",               # Does not map to any canonical field → overflow
]

LABEL_TO_IDX = {label: i for i, label in enumerate(CANONICAL_LABELS)}
IDX_TO_LABEL = {i: label for i, label in enumerate(CANONICAL_LABELS)}

# ── Envelope key set ────────────────────────────────────────────────────────
#
# Provider response-shape keys with no canonical semantic. Verified against
# real OpenAI / Anthropic / Ollama responses (see
# ``tests/unit/test_schema_mapper_envelope.py``). When a leaf field's key
# matches one of these AND the field would otherwise be UNKNOWN, the mapper
# tags it ``envelope`` instead — keeping it out of the operator-visible
# ``schema_mapper_unmapped`` / ``schema_mapper_overflow_keys`` accounting.
#
# Why this is a closed set (not a regex / heuristic): the goal is precisely
# "things that are not actionable for the mapper to improve". Adding entries
# here is a deliberate decision; broadening it accidentally hides legitimate
# unmapped fields. Each entry below is documented with the provider(s) that
# emit it and a one-line "why no canonical class" justification.
# Path-token substrings that, if present in a field's dotted path,
# disqualify it from envelope tagging — these are user-data parent
# scopes where a leaf key colliding with an envelope name might
# legitimately carry semantic data (a tool call's `arguments.role`
# could be a user-defined string, a prompt's `input.type` an input
# discriminator the user cares about). Verified against current
# tool-use and structured-output schemas in OpenAI / Anthropic.
ENVELOPE_PATH_DISQUALIFIERS: frozenset[str] = frozenset({
    "arguments",  # tool_call function arguments
    "input",      # tool_use input payload
    "parameters", # function-schema parameters
})


ENVELOPE_KEYS: frozenset[str] = frozenset({
    # Top-level response-type discriminator. OpenAI: "chat.completion",
    # Anthropic: "message", "completion". No semantic content.
    "object",
    # Unix epoch creation timestamp. Per-request timestamps without a
    # semantic role — the gateway already records wall-clock time.
    "created",
    # Choice/content-block ordinal within an array. Structural.
    "index",
    # Message role ("assistant", "user", …). Conversation framing, not a
    # field of the response's canonical content.
    "role",
    # OpenAI structured-refusal flag. The refusal payload itself is `content`;
    # this is only the type discriminator.
    "refusal",
    # OpenAI per-token logprob payloads. Telemetry; no canonical slot.
    "logprobs",
    # OpenAI service tier ("default", "scale", …). Routing tier, not content.
    "service_tier",
    # OpenAI runtime fingerprint. Distinct from `model_hash` (which is the
    # canonical "what model produced this" identifier) — the fingerprint is
    # operationally noisier and the production classifier doesn't have a
    # canonical slot for it.
    "system_fingerprint",
    # Anthropic content-block type ("text", "tool_use", "thinking",
    # "server_tool_use", "web_search_tool_result"). Discriminator.
    "type",
    # Anthropic stop sequence value (string, when stop_reason is
    # "stop_sequence"). Telemetry; the canonical "why did this end" is
    # `finish_reason`.
    "stop_sequence",
})

# Fields where we expect exactly one value per response
SINGLETON_FIELDS = {"content", "thinking_content", "finish_reason", "response_id", "model", "model_hash"}

# Fields that are part of the usage group (ints that may sum)
USAGE_FIELDS = {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                "cached_tokens", "cache_creation_tokens", "cost_usd"}
