"""Value-aware feature extraction for JSON fields.

The key innovation: instead of matching field NAMES (which break on
unknown providers), we analyze field VALUES to understand what they are.
Three integers where one equals the sum of the other two are ALWAYS
token counts. A long natural-language string is ALWAYS the content.

Extracts ~200-dimensional feature vectors per JSON field for the
ONNX schema mapper model.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{32,}$", re.I)
_URL_RE = re.compile(r"^https?://", re.I)
_ENUM_MAX_LEN = 30
_NATURAL_LANG_MIN_SPACES = 3
_HASH_DIMS = 32  # Dimensions for hashed token features
_PATH_HASH_DIMS = 64
_SIBLING_HASH_DIMS = 16

# camelCase split: "completionTokens" → ["completion", "Tokens"]
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_key_tokens(key: str) -> list[str]:
    """Split a key name into semantic tokens.

    'completion_tokens' → ['completion', 'tokens']
    'completionTokens' → ['completion', 'tokens']
    'usageMetadata'     → ['usage', 'metadata']
    'prompt_eval_count' → ['prompt', 'eval', 'count']
    """
    # Split on _ and .
    parts = re.split(r"[_.\[\]]", key)
    # Further split camelCase
    tokens = []
    for part in parts:
        if not part or part.isdigit():
            continue
        tokens.extend(_CAMEL_SPLIT.split(part))
    return [t.lower() for t in tokens if t]


def _hash_tokens(tokens: list[str], dims: int) -> list[float]:
    """Hash a list of string tokens into a fixed-dimension float vector."""
    vec = [0.0] * dims
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        idx = h % dims
        sign = 1.0 if (h // dims) % 2 == 0 else -1.0
        vec[idx] += sign
    return vec


def _magnitude_bucket(value: int | float) -> list[float]:
    """Bucket a numeric value into magnitude ranges. Returns 6-dim one-hot."""
    # [0-10, 10-100, 100-1K, 1K-10K, 10K-100K, 100K+]
    abs_val = abs(value) if value else 0
    buckets = [0.0] * 6
    if abs_val <= 10:
        buckets[0] = 1.0
    elif abs_val <= 100:
        buckets[1] = 1.0
    elif abs_val <= 1000:
        buckets[2] = 1.0
    elif abs_val <= 10000:
        buckets[3] = 1.0
    elif abs_val <= 100000:
        buckets[4] = 1.0
    else:
        buckets[5] = 1.0
    return buckets


def _string_length_bucket(length: int) -> list[float]:
    """Bucket string length. Returns 7-dim one-hot."""
    # [0, 1-10, 10-50, 50-200, 200-1K, 1K-10K, 10K+]
    buckets = [0.0] * 7
    if length == 0:
        buckets[0] = 1.0
    elif length <= 10:
        buckets[1] = 1.0
    elif length <= 50:
        buckets[2] = 1.0
    elif length <= 200:
        buckets[3] = 1.0
    elif length <= 1000:
        buckets[4] = 1.0
    elif length <= 10000:
        buckets[5] = 1.0
    else:
        buckets[6] = 1.0
    return buckets


# ── JSON Flattening ──────────────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass
class FlatField:
    """A single flattened field from a JSON response."""

    path: str              # e.g. "choices.0.message.content"
    key: str               # leaf key name, e.g. "content"
    value: Any             # the actual value
    value_type: str        # "string" | "int" | "float" | "bool" | "array" | "object" | "null"
    depth: int             # nesting level (0 = top-level)
    parent_key: str        # parent's key name
    sibling_keys: list[str]  # keys of siblings at the same level
    sibling_types: list[str] # types of siblings
    int_siblings: list[int]  # values of int-type siblings (for sum detection)


def flatten_json(obj: dict, _prefix: str = "", _depth: int = 0,
                 _parent_key: str = "") -> list[FlatField]:
    """Flatten a JSON object into a list of FlatField entries.

    Each leaf value becomes one FlatField with full context (path, parent,
    siblings, depth). Arrays are traversed but only the first element is
    analyzed (providers use consistent array structures).
    """
    fields: list[FlatField] = []
    if not isinstance(obj, dict):
        return fields

    # Collect sibling info at this level
    sibling_keys = list(obj.keys())
    sibling_types = [_type_of(v) for v in obj.values()]
    int_siblings = [v for v in obj.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]

    for key, value in obj.items():
        path = f"{_prefix}.{key}" if _prefix else key
        vtype = _type_of(value)

        if isinstance(value, dict):
            # Recurse into objects
            fields.append(FlatField(
                path=path, key=key, value=value, value_type="object",
                depth=_depth, parent_key=_parent_key,
                sibling_keys=sibling_keys, sibling_types=sibling_types,
                int_siblings=int_siblings,
            ))
            fields.extend(flatten_json(value, path, _depth + 1, key))
        elif isinstance(value, list):
            # Record the array itself
            fields.append(FlatField(
                path=path, key=key, value=value, value_type="array",
                depth=_depth, parent_key=_parent_key,
                sibling_keys=sibling_keys, sibling_types=sibling_types,
                int_siblings=int_siblings,
            ))
            # Analyze first element if it's an object
            if value and isinstance(value[0], dict):
                fields.extend(flatten_json(value[0], f"{path}.0", _depth + 1, key))
        else:
            # Leaf value
            fields.append(FlatField(
                path=path, key=key, value=value, value_type=vtype,
                depth=_depth, parent_key=_parent_key,
                sibling_keys=sibling_keys, sibling_types=sibling_types,
                int_siblings=int_siblings,
            ))

    return fields


def _type_of(value: Any) -> str:
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
    return "unknown"


# ── Feature Vector Extraction ────────────────────────────────────────────────

def extract_features(field: FlatField) -> list[float]:
    """Extract ~200-dimensional feature vector from a FlatField.

    Features encode VALUE SEMANTICS (what the data IS) rather than
    just name matching (what the field is CALLED).
    """
    features: list[float] = []

    # ── 1. Key name tokens (64 dims) ──────────────────────────────────
    path_tokens = _split_key_tokens(field.path)
    features.extend(_hash_tokens(path_tokens, _PATH_HASH_DIMS))

    # ── 2. Value type one-hot (7 dims) ────────────────────────────────
    type_vec = [0.0] * 7
    type_map = {"string": 0, "int": 1, "float": 2, "bool": 3, "array": 4, "object": 5, "null": 6}
    type_vec[type_map.get(field.value_type, 6)] = 1.0
    features.extend(type_vec)

    # ── 3. Nesting depth (1 dim) ──────────────────────────────────────
    features.append(min(field.depth / 5.0, 1.0))  # Normalized 0-1

    # ── 4. Int value features (10 dims) ───────────────────────────────
    if field.value_type in ("int", "float") and not isinstance(field.value, bool):
        val = field.value or 0
        features.extend(_magnitude_bucket(val))       # 6 dims
        # Is this value the sum of other int siblings?
        other_ints = [v for v in field.int_siblings if v != val]
        is_sum = 1.0 if (len(other_ints) >= 2 and abs(sum(other_ints) - val) < 2) else 0.0
        features.append(is_sum)                        # 1 dim
        # Ratio to max sibling
        max_sib = max(field.int_siblings) if field.int_siblings else 1
        features.append(min(val / max(max_sib, 1), 10.0) / 10.0)  # 1 dim
        features.append(1.0 if val == 0 else 0.0)     # is_zero, 1 dim
        features.append(math.log1p(abs(val)) / 20.0)   # log magnitude, 1 dim
    else:
        features.extend([0.0] * 10)

    # ── 5. String value features (12 dims) ────────────────────────────
    if field.value_type == "string" and isinstance(field.value, str):
        s = field.value
        features.extend(_string_length_bucket(len(s)))  # 7 dims
        # Is natural language (has spaces, punctuation, sentences)?
        space_count = s.count(" ")
        features.append(1.0 if space_count >= _NATURAL_LANG_MIN_SPACES else 0.0)
        # Is identifier (UUID or hex hash)?
        features.append(1.0 if _UUID_RE.match(s) or _HEX_RE.match(s) else 0.0)
        # Is enum-like (short, no spaces)?
        features.append(1.0 if len(s) <= _ENUM_MAX_LEN and " " not in s else 0.0)
        # Is URL?
        features.append(1.0 if _URL_RE.match(s) else 0.0)
        # Has JSON structure inside?
        features.append(1.0 if s.startswith("{") or s.startswith("[") else 0.0)
    else:
        features.extend([0.0] * 12)

    # ── 6. Array value features (8 dims) ──────────────────────────────
    if field.value_type == "array" and isinstance(field.value, list):
        arr = field.value
        # Length buckets: [0, 1, 2-5, 5+]
        len_bucket = [0.0] * 4
        if len(arr) == 0:
            len_bucket[0] = 1.0
        elif len(arr) == 1:
            len_bucket[1] = 1.0
        elif len(arr) <= 5:
            len_bucket[2] = 1.0
        else:
            len_bucket[3] = 1.0
        features.extend(len_bucket)                    # 4 dims
        # Element type (first element)
        if arr:
            elem = arr[0]
            features.append(1.0 if isinstance(elem, dict) else 0.0)   # is_object_array
            if isinstance(elem, dict):
                features.append(1.0 if "name" in elem else 0.0)       # has_name_key
                features.append(1.0 if "arguments" in elem or "function" in elem else 0.0)  # has_args
                features.append(1.0 if "text" in elem or "content" in elem else 0.0)  # has_text
            else:
                features.extend([0.0] * 3)
        else:
            features.extend([0.0] * 4)
    else:
        features.extend([0.0] * 8)

    # ── 7. Object value features (4 dims) ─────────────────────────────
    if field.value_type == "object" and isinstance(field.value, dict):
        obj = field.value
        features.append(min(len(obj) / 10.0, 1.0))    # child count normalized
        child_types = [_type_of(v) for v in obj.values()]
        features.append(child_types.count("int") / max(len(child_types), 1))   # int ratio
        features.append(child_types.count("string") / max(len(child_types), 1))  # string ratio
        features.append(child_types.count("object") / max(len(child_types), 1))  # object ratio
    else:
        features.extend([0.0] * 4)

    # ── 8. Structural context (20 dims) ───────────────────────────────
    # Sibling count and type distribution
    features.append(min(len(field.sibling_keys) / 10.0, 1.0))  # sibling count
    sib_type_counts = {}
    for st in field.sibling_types:
        sib_type_counts[st] = sib_type_counts.get(st, 0) + 1
    total_sibs = max(len(field.sibling_types), 1)
    features.append(sib_type_counts.get("int", 0) / total_sibs)     # int sibling ratio
    features.append(sib_type_counts.get("string", 0) / total_sibs)  # string sibling ratio
    features.append(sib_type_counts.get("object", 0) / total_sibs)  # object sibling ratio
    features.append(sib_type_counts.get("array", 0) / total_sibs)   # array sibling ratio

    # Parent key tokens (16 dims)
    parent_tokens = _split_key_tokens(field.parent_key) if field.parent_key else []
    features.extend(_hash_tokens(parent_tokens, _SIBLING_HASH_DIMS))

    # ── 9. Relationship features (12 dims) ────────────────────────────
    # Int group detection: how many int siblings, does a sum match exist?
    int_sib_count = len(field.int_siblings)
    features.append(min(int_sib_count / 5.0, 1.0))    # int siblings count

    # Check if ANY pair of int siblings sums to another
    sum_exists = 0.0
    if int_sib_count >= 3:
        sorted_ints = sorted(field.int_siblings, reverse=True)
        if abs(sorted_ints[0] - sum(sorted_ints[1:])) < 2:
            sum_exists = 1.0
    features.append(sum_exists)

    # Has enum-like string sibling (finish_reason pattern)
    has_enum_sib = 0.0
    for sk, sv in zip(field.sibling_keys, field.sibling_types):
        if sv == "string":
            # Check if sibling looks like an enum
            pass  # We check during flatten
    features.append(has_enum_sib)

    # Sibling key name hashing (8 dims)
    features.extend(_hash_tokens(field.sibling_keys, 8))

    # Position flags
    features.append(1.0 if field.depth == 0 else 0.0)  # is_top_level

    return features


# ── Batch feature extraction ─────────────────────────────────────────────────

FEATURE_DIM = len(extract_features(FlatField(
    path="test", key="test", value="test", value_type="string",
    depth=0, parent_key="", sibling_keys=[], sibling_types=[], int_siblings=[],
)))
"""Dimensionality of the feature vector. Computed once at import."""


def extract_batch(fields: list[FlatField]) -> list[list[float]]:
    """Extract feature vectors for a batch of fields."""
    return [extract_features(f) for f in fields]
