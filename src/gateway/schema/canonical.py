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
    """Metadata produced by SchemaMapper describing how well the mapping succeeded."""

    confidence: float = 1.0
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
