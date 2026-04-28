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

    audit_state, model_version, model_sha512, label_schema_version and
    drift_signals are populated by SchemaMapper so every Walacor execution
    record knows which model classified its fields and whether the result
    is verified-canonical or audit-flagged.
    """

    confidence: float = 1.0
    incomplete: bool = False
    mapped_fields: list[str] = field(default_factory=list)
    unmapped_fields: list[str] = field(default_factory=list)
    # Confidence-gated audit (gap 2): one of "verified", "unverified",
    # "rejected". "verified" iff confidence >= audit_threshold.
    audit_state: str = "verified"
    audit_threshold: float = 0.85
    # Model provenance (gap 3): pinned at SchemaMapper init.
    # Reproducibility property — a future auditor can re-run the same
    # model on the same input and verify identical labels.
    model_version: str | None = None
    model_sha512: str | None = None
    label_schema_version: str | None = None
    # Drift detection (gap 4): non-empty when this response surfaced a
    # field path we hadn't seen before for this provider+model.
    drift_signals: list[str] = field(default_factory=list)


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
    "UNKNOWN",               # Does not map to any canonical field → overflow
]

LABEL_TO_IDX = {label: i for i, label in enumerate(CANONICAL_LABELS)}
IDX_TO_LABEL = {i: label for i, label in enumerate(CANONICAL_LABELS)}

# Fields where we expect exactly one value per response
SINGLETON_FIELDS = {"content", "thinking_content", "finish_reason", "response_id", "model", "model_hash"}

# Fields that are part of the usage group (ints that may sum)
USAGE_FIELDS = {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                "cached_tokens", "cache_creation_tokens", "cost_usd"}


# ── Walacor record-field bindings (gap 1) ───────────────────────────────────
#
# Schema version bumps when LABEL_BINDINGS changes shape. Pinned into every
# MappingReport so audit consumers can detect schema-level drift between
# the records they're reviewing and their current canonical definitions.
LABEL_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class LabelBinding:
    """Maps a canonical label to its Walacor record field + value contract.

    Fields:
      walacor_field_path  Where this label's value is written in the
                          Walacor execution record (ETId 9000031). Dotted
                          path; "executions.usage.prompt_tokens" lands at
                          the same column in the lineage UI regardless of
                          which provider produced the field.
      json_type           Expected JSON-typed value. Records can validate
                          before write; a type-mismatched value is a
                          stronger signal than a low-confidence label.
      constraint          Optional human-readable validity rule
                          ("int >= 0", "ISO-8601 UTC", "regex:^msg_").
                          Documentation; not enforced by the model.
      compliance_refs     Compliance frameworks that reference this
                          field. Used by audit views to filter records
                          that satisfy specific regulator queries.
      description         One-line summary visible in lineage UI tooltips
                          and `/v1/control/labels` responses.
    """

    canonical_label: str
    walacor_field_path: str
    json_type: str
    constraint: str | None
    compliance_refs: tuple[str, ...]
    description: str


# Bindings for every entry in CANONICAL_LABELS. The compliance refs are
# best-effort regulator-mapped (EU AI Act sections, SOC2 trust criteria);
# audit teams should validate the specific clause numbers against current
# rule text — these are starting points, not legal opinions.
LABEL_BINDINGS: dict[str, LabelBinding] = {
    "content": LabelBinding(
        canonical_label="content",
        walacor_field_path="executions.response.content",
        json_type="string",
        constraint="non-empty for completed responses",
        compliance_refs=("eu-ai-act:50(2)",),
        description="LLM assistant text reply",
    ),
    "thinking_content": LabelBinding(
        canonical_label="thinking_content",
        walacor_field_path="executions.response.thinking_content",
        json_type="string",
        constraint=None,
        compliance_refs=("eu-ai-act:13",),
        description="Reasoning/chain-of-thought trace, if exposed by provider",
    ),
    "finish_reason": LabelBinding(
        canonical_label="finish_reason",
        walacor_field_path="executions.response.finish_reason",
        json_type="string",
        constraint="enum: stop|length|tool_calls|content_filter|...",
        compliance_refs=(),
        description="Why generation halted",
    ),
    "response_id": LabelBinding(
        canonical_label="response_id",
        walacor_field_path="executions.response_id",
        json_type="string",
        constraint="provider-unique identifier",
        compliance_refs=("soc2:cc7.3",),
        description="Provider-assigned response identifier; primary correlation key",
    ),
    "model": LabelBinding(
        canonical_label="model",
        walacor_field_path="executions.model_id",
        json_type="string",
        constraint="non-empty",
        compliance_refs=("eu-ai-act:53(1)(a)",),
        description="Provider model identifier (e.g. gpt-4o-mini, claude-haiku-4-5)",
    ),
    "model_hash": LabelBinding(
        canonical_label="model_hash",
        walacor_field_path="executions.model_hash",
        json_type="string",
        constraint="provider-issued fingerprint",
        compliance_refs=("eu-ai-act:53(1)(a)",),
        description="Model fingerprint / system_fingerprint for attestation chain",
    ),
    "prompt_tokens": LabelBinding(
        canonical_label="prompt_tokens",
        walacor_field_path="executions.usage.prompt_tokens",
        json_type="int",
        constraint=">= 0",
        compliance_refs=("eu-ai-act:53(1)(b)", "soc2:cc7.2"),
        description="Token count of input to the LLM",
    ),
    "completion_tokens": LabelBinding(
        canonical_label="completion_tokens",
        walacor_field_path="executions.usage.completion_tokens",
        json_type="int",
        constraint=">= 0",
        compliance_refs=("eu-ai-act:53(1)(b)", "soc2:cc7.2"),
        description="Token count of model output",
    ),
    "total_tokens": LabelBinding(
        canonical_label="total_tokens",
        walacor_field_path="executions.usage.total_tokens",
        json_type="int",
        constraint=">= prompt_tokens + completion_tokens",
        compliance_refs=("eu-ai-act:53(1)(b)",),
        description="Sum of prompt and completion tokens",
    ),
    "reasoning_tokens": LabelBinding(
        canonical_label="reasoning_tokens",
        walacor_field_path="executions.usage.reasoning_tokens",
        json_type="int",
        constraint=">= 0",
        compliance_refs=(),
        description="Thinking-phase token count (subset of completion_tokens)",
    ),
    "cached_tokens": LabelBinding(
        canonical_label="cached_tokens",
        walacor_field_path="executions.usage.cached_tokens",
        json_type="int",
        constraint=">= 0",
        compliance_refs=(),
        description="Tokens served from provider prompt cache",
    ),
    "cache_creation_tokens": LabelBinding(
        canonical_label="cache_creation_tokens",
        walacor_field_path="executions.usage.cache_creation_tokens",
        json_type="int",
        constraint=">= 0",
        compliance_refs=(),
        description="Tokens written to provider prompt cache",
    ),
    "cost_usd": LabelBinding(
        canonical_label="cost_usd",
        walacor_field_path="executions.usage.cost_usd",
        json_type="float",
        constraint=">= 0",
        compliance_refs=("soc2:cc7.2",),
        description="Per-request cost in USD (when provider returns it)",
    ),
    "tool_call_id": LabelBinding(
        canonical_label="tool_call_id",
        walacor_field_path="executions.tool_calls[].id",
        json_type="string",
        constraint="non-empty",
        compliance_refs=(),
        description="Tool call identifier",
    ),
    "tool_call_name": LabelBinding(
        canonical_label="tool_call_name",
        walacor_field_path="executions.tool_calls[].name",
        json_type="string",
        constraint="non-empty",
        compliance_refs=("eu-ai-act:13",),
        description="Function or tool name invoked",
    ),
    "tool_call_arguments": LabelBinding(
        canonical_label="tool_call_arguments",
        walacor_field_path="executions.tool_calls[].arguments",
        json_type="string|object",
        constraint="parsable JSON when provider returns string",
        compliance_refs=("eu-ai-act:13",),
        description="Tool call arguments",
    ),
    "tool_call_type": LabelBinding(
        canonical_label="tool_call_type",
        walacor_field_path="executions.tool_calls[].type",
        json_type="string",
        constraint="enum: function|...",
        compliance_refs=(),
        description="Tool call type discriminator",
    ),
    "citation_url": LabelBinding(
        canonical_label="citation_url",
        walacor_field_path="executions.citations[].url",
        json_type="string",
        constraint="absolute http(s) URL",
        compliance_refs=("eu-ai-act:50(1)",),
        description="Source URL referenced by the response",
    ),
    "citation_title": LabelBinding(
        canonical_label="citation_title",
        walacor_field_path="executions.citations[].title",
        json_type="string",
        constraint=None,
        compliance_refs=("eu-ai-act:50(1)",),
        description="Source title for a citation",
    ),
    "timing_value": LabelBinding(
        canonical_label="timing_value",
        walacor_field_path="executions.timing.*",
        json_type="float",
        constraint=">= 0 (ms or ns; provider-specific)",
        compliance_refs=("soc2:cc7.2",),
        description="Inference timing measurement",
    ),
    "safety_blocked": LabelBinding(
        canonical_label="safety_blocked",
        walacor_field_path="executions.safety.blocked",
        json_type="bool",
        constraint=None,
        compliance_refs=("eu-ai-act:50(2)",),
        description="Provider-reported content-block flag",
    ),
    "safety_category": LabelBinding(
        canonical_label="safety_category",
        walacor_field_path="executions.safety.categories",
        json_type="string|float",
        constraint=None,
        compliance_refs=("eu-ai-act:50(2)",),
        description="Provider safety category score or label",
    ),
    "UNKNOWN": LabelBinding(
        canonical_label="UNKNOWN",
        walacor_field_path="executions.overflow.<original_path>",
        json_type="any",
        constraint=None,
        compliance_refs=(),
        description="Field not recognized — preserved as-is in overflow envelope",
    ),
}


def get_label_binding(label: str) -> LabelBinding | None:
    """Return the Walacor binding for a canonical label, or None if unknown."""
    return LABEL_BINDINGS.get(label)
