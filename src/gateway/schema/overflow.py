"""Self-healing schema — capture unknown fields, track frequency, promote candidates.

When the SchemaMapper classifies a field as UNKNOWN, it goes into the
overflow dict rather than being dropped. The FieldRegistry tracks how
often each overflow field appears per provider. Fields that appear
consistently get flagged as promotion candidates — they deserve
first-class canonical slots in future schema versions.

The system NEVER drops data. Unknown fields are preserved in
metadata_json under `_overflow_fields` with provenance.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Fields matching these patterns are meaningful (worth tracking).
# Everything else is transport noise.
_MEANINGFUL_PATTERNS = [
    re.compile(r".*token.*", re.I),
    re.compile(r".*count.*", re.I),
    re.compile(r".*_ms$", re.I),
    re.compile(r".*duration.*", re.I),
    re.compile(r".*latency.*", re.I),
    re.compile(r".*time.*", re.I),
    re.compile(r".*content.*", re.I),
    re.compile(r".*text.*", re.I),
    re.compile(r".*reason.*", re.I),
    re.compile(r".*status.*", re.I),
    re.compile(r".*model.*", re.I),
    re.compile(r".*version.*", re.I),
    re.compile(r".*cost.*", re.I),
    re.compile(r".*price.*", re.I),
    re.compile(r".*citation.*", re.I),
    re.compile(r".*source.*", re.I),
    re.compile(r".*safety.*", re.I),
    re.compile(r".*cache.*", re.I),
    re.compile(r".*thinking.*", re.I),
    re.compile(r".*reasoning.*", re.I),
]

# These are always noise — never track them.
_NOISE_PREFIXES = ("x-", "_", "x_", "__")
_NOISE_KEYS = frozenset({
    "object", "type", "role", "index", "logprobs", "refusal",
    "created", "created_at", "updated_at",
})


@dataclass
class FieldRecord:
    """Tracking record for an overflow field."""

    key: str
    value_type: str
    provider: str
    first_seen: float
    last_seen: float
    count: int = 1
    sample_value: Any = None
    promoted: bool = False


class FieldRegistry:
    """In-memory registry tracking overflow field frequency.

    Persisted to control plane DB on shutdown (if available).
    Fields seen > PROMOTION_THRESHOLD times are flagged as candidates.
    """

    PROMOTION_THRESHOLD = 10

    def __init__(self) -> None:
        # key = (field_path, provider), value = FieldRecord
        self._fields: dict[tuple[str, str], FieldRecord] = {}
        self._promotion_candidates: list[str] = []

    def record(self, field_path: str, value: Any, value_type: str, provider: str) -> None:
        """Record an overflow field observation."""
        # Filter noise
        leaf_key = field_path.split(".")[-1] if "." in field_path else field_path
        if leaf_key.lower() in _NOISE_KEYS:
            return
        if any(leaf_key.lower().startswith(p) for p in _NOISE_PREFIXES):
            return

        key = (field_path, provider)
        now = time.time()

        if key in self._fields:
            rec = self._fields[key]
            rec.count += 1
            rec.last_seen = now
            if rec.count == self.PROMOTION_THRESHOLD and not rec.promoted:
                rec.promoted = True
                self._promotion_candidates.append(field_path)
                logger.info(
                    "Field promotion candidate: %s (provider=%s, count=%d, type=%s)",
                    field_path, provider, rec.count, value_type,
                )
        else:
            self._fields[key] = FieldRecord(
                key=field_path,
                value_type=value_type,
                provider=provider,
                first_seen=now,
                last_seen=now,
                sample_value=_safe_sample(value),
            )

    def is_meaningful(self, field_path: str) -> bool:
        """Check if a field name matches meaningful patterns."""
        leaf = field_path.split(".")[-1] if "." in field_path else field_path
        return any(p.match(leaf) for p in _MEANINGFUL_PATTERNS)

    def get_promotion_candidates(self) -> list[str]:
        """Return field paths that have been seen enough times to promote."""
        return list(self._promotion_candidates)

    def get_stats(self) -> dict[str, Any]:
        """Return registry statistics for health/status endpoints."""
        return {
            "tracked_fields": len(self._fields),
            "promotion_candidates": len(self._promotion_candidates),
            "top_fields": [
                {"path": rec.key, "provider": rec.provider, "count": rec.count, "type": rec.value_type}
                for rec in sorted(self._fields.values(), key=lambda r: -r.count)[:10]
            ],
        }

    def to_dict(self) -> list[dict]:
        """Serialize for persistence."""
        return [
            {
                "path": rec.key,
                "provider": rec.provider,
                "type": rec.value_type,
                "count": rec.count,
                "first_seen": rec.first_seen,
                "last_seen": rec.last_seen,
                "promoted": rec.promoted,
                "sample": str(rec.sample_value)[:100] if rec.sample_value else None,
            }
            for rec in self._fields.values()
        ]


def build_overflow_envelope(
    overflow: dict[str, Any],
    provider: str,
    registry: FieldRegistry | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Build a structured overflow envelope for metadata_json.

    Wraps raw overflow fields with provenance metadata so they can
    be interpreted by future schema versions.
    """
    if not overflow:
        return {}

    envelope: dict[str, Any] = {
        "_schema_version": schema_version,
        "_overflow_fields": {},
    }

    for path, value in overflow.items():
        vtype = _type_name(value)
        entry = {
            "value": _safe_sample(value),
            "type": vtype,
            "provider": provider,
        }
        envelope["_overflow_fields"][path] = entry

        # Track in registry
        if registry:
            registry.record(path, value, vtype, provider)

    return envelope


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _safe_sample(value: Any, max_len: int = 100) -> Any:
    """Truncate value for storage as a sample."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    if isinstance(value, (dict, list)):
        s = str(value)
        return s[:max_len] + "..." if len(s) > max_len else s
    return value
