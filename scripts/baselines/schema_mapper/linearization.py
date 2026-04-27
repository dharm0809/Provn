"""Per-field linearization for the MiniLM-L6 encoder input.

Produces ONE string per FlatField in a fixed schema (DODUO SIGMOD-2022
recipe adapted to single-JSON-dict labelling):

    path:<dotted_path> [SEP] key:<leaf_key> [SEP] siblings:<a,b,c>
    [SEP] type:<value_type> [SEP] value:<value_summary>

The string is consumed by the MiniLM tokenizer at training and inference
time. Every transformation here is the SINGLE source of truth — runtime
gateway code imports `linearize_field` directly so the form stays
identical across the build/runtime boundary.

Conventions (chosen for determinism, not brevity):
- siblings list is sorted alphabetically (paths.flatten_json already
  does this; we re-sort defensively for old callers)
- value summary is at most 32 chars
- numeric values stringified verbatim (truncated to 32 chars)
- string values truncated to first 32 chars
- list values rendered as `list[N]` (N = element count)
- dict values rendered as `dict[k1,k2,k3]` with up to 3 sorted keys
- None → `null`
- bool → `true`/`false`
- The full output is hard-capped at 256 chars (well under MiniLM's
  128-token limit even in the worst case after subword tokenization)
"""
from __future__ import annotations

from typing import Any

# Re-export so callers can import a single module
from paths import FlatField, flatten_json, parent_object_path  # noqa: F401

_VALUE_MAX = 32
_TOTAL_MAX = 256
_SEP = " [SEP] "


def _summarize_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        s = str(v)
        return s if len(s) <= _VALUE_MAX else s[:_VALUE_MAX]
    if isinstance(v, str):
        return v if len(v) <= _VALUE_MAX else v[:_VALUE_MAX]
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, dict):
        keys = sorted(v.keys())[:3]
        return "dict[" + ",".join(keys) + "]"
    # Fallback for unexpected types
    s = repr(v)
    return s if len(s) <= _VALUE_MAX else s[:_VALUE_MAX]


def _value_type_tag(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "other"


def linearize_field(f: FlatField) -> str:
    """Linearize a FlatField to a string suitable for the encoder tokenizer.

    The output is deterministic, capped at 256 characters, and
    contains five [SEP]-separated regions: path, key, siblings, type,
    value. See module docstring for the exact format.
    """
    sibs = ",".join(sorted(f.siblings))
    value_summary = _summarize_value(f.value)
    type_tag = _value_type_tag(f.value)
    parts = [
        f"path:{f.path}",
        f"key:{f.key}",
        f"siblings:{sibs}",
        f"type:{type_tag}",
        f"value:{value_summary}",
    ]
    s = _SEP.join(parts)
    if len(s) <= _TOTAL_MAX:
        return s
    # Hard cap: truncate the path region first (it's the longest in the
    # adversarial cases — deeply-nested arrays). Re-assemble.
    overflow = len(s) - _TOTAL_MAX
    new_path = f.path[:-overflow] if overflow < len(f.path) else f.path[:1]
    parts[0] = f"path:{new_path}"
    s = _SEP.join(parts)
    return s if len(s) <= _TOTAL_MAX else s[:_TOTAL_MAX]


def linearize_dict(obj: dict) -> list[tuple[str, str]]:
    """Convenience: flatten + linearize a JSON dict in one pass.

    Returns a list of (path, linearized_string) pairs in the order
    flatten_json emits them (depth-first, dict-iteration order).
    """
    return [(f.path, linearize_field(f)) for f in flatten_json(obj)]
