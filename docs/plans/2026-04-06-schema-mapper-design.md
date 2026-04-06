# SchemaMapper Design — ML-Powered JSON Response Understanding

**Date**: 2026-04-06  
**Status**: Approved  
**Branch**: feature/data-integrity-engine

## Problem

The gateway has hardcoded provider-specific logic across 6 adapters and a static `_PROVIDER_USAGE_MAPS` dict. Adding a new provider or handling a format change requires code updates. We need a system that genuinely understands JSON structure and maps any LLM response to a canonical schema — without code changes.

## Key Insight

Field names are unreliable. Field values are universal. Three integers where one equals the sum of the other two are ALWAYS token counts, regardless of naming convention. A long natural-language string nested in the response is ALWAYS the content. The model learns value semantics, not name matching.

## Canonical Schema

Every LLM response maps to:

```python
CANONICAL_RESPONSE = {
    "content": str,
    "thinking_content": str | None,
    "finish_reason": str,          # stop | length | tool_calls | content_filter | error
    "response_id": str | None,
    "model": str | None,
    "model_hash": str | None,

    "usage": {
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int,
        "reasoning_tokens": int | None,
        "cached_tokens": int | None,
        "cache_creation_tokens": int | None,
        "cost_usd": float | None,
    },

    "tool_calls": [{
        "id": str,
        "name": str,
        "arguments": dict | str,
        "type": str,
    }] | None,

    "citations": [{
        "url": str,
        "title": str | None,
    }] | None,

    "timing": {
        "total_ms": float | None,
        "prompt_ms": float | None,
        "completion_ms": float | None,
        "queue_ms": float | None,
    } | None,

    "safety": {
        "blocked": bool,
        "categories": dict,
    } | None,

    "overflow": dict,      # Unknown fields preserved, never dropped
}
```

## Feature Engineering (~200 dimensions)

For each field in a JSON response:

### Name features (~64 dims)
- Key path tokens via HashingVectorizer (split on `_`, `.`, camelCase)
- Nesting depth (1 dim)

### Value features (~80 dims) — THE INNOVATION
- **Type**: one-hot (string, int, float, bool, array, object, null) — 7 dims
- **Int fields**: magnitude bucket (5 bins), is_sum_of_siblings, ratio_to_max_sibling, is_zero — 8 dims
- **String fields**: length bucket (6 bins), is_natural_language, is_identifier (UUID/hex), is_enum (< 20 chars, no spaces), is_url, has_json_structure — 11 dims
- **Array fields**: element count bucket (4 bins), element_type, has_name_key_in_elements, has_arguments_key, has_text_key — 8 dims
- **Object fields**: child count, child_type_distribution (6 dims) — 7 dims

### Structural features (~40 dims)
- Sibling count and type distribution (8 dims)
- Parent key name tokens (HashingVectorizer, 16 dims)
- Int siblings count in same object (1 dim)
- String siblings count (1 dim)
- Has enum-like sibling (1 dim)
- Position: is_top_level, is_in_array, depth (3 dims)
- Response-level: total_field_count, max_depth (2 dims)
- Remaining contextual (8 dims)

### Relationship features (~16 dims)
- Int group pattern: count of int siblings, sum_match_exists (2 dims)
- Adjacent types (4 dims)
- Structural signature of parent object (10 dims via hashing)

## Model

- **Architecture**: GradientBoosting (sklearn) or LightGBM
- **Classes**: 14 canonical field types (content, thinking_content, prompt_tokens, completion_tokens, total_tokens, reasoning_tokens, cached_tokens, finish_reason, response_id, model, tool_calls, citations, timing_value, UNKNOWN)
- **Training data**: 22 real provider formats → flatten to (path, value) pairs → label with canonical field → augment with synthetic variations (rename, restructure, noise) → ~3000+ samples
- **Export**: ONNX via skl2onnx (~100KB)
- **Inference**: < 5ms for full response (20-30 fields)

## Architecture

```
Response JSON arrives
    │
    ├─ Flatten to list of (path, value, siblings, parent) tuples
    ├─ Extract ~200-dim feature vector per field
    ├─ ONNX batch inference: classify all fields at once
    ├─ Post-processing:
    │    ├─ Validate: exactly 1 content, usage ints that sum correctly
    │    ├─ Resolve conflicts: if 2 fields both map to "content", pick higher confidence
    │    └─ Everything classified as UNKNOWN → goes to overflow dict
    └─ Return CanonicalResponse
```

## Files

```
src/gateway/schema/
  ├── __init__.py
  ├── mapper.py              # SchemaMapper class — main entry point
  ├── features.py            # Value-aware feature extraction
  ├── canonical.py           # Canonical schema dataclass + validation
  ├── postprocess.py         # Conflict resolution, sum validation
  └── schema_mapper.onnx     # Trained model

scripts/
  ├── build_mapper_training_data.py   # Parse provider formats → training JSON
  └── train_schema_mapper.py          # Train + export ONNX model
```

## Integration

SchemaMapper sits between raw HTTP response and the existing ModelResponse. The adapters call `SchemaMapper.map_response(raw_json)` instead of hand-parsing. For known provider adapters, SchemaMapper is an additional validation layer. For unknown providers, it's the primary parser.

## Self-Healing (Overflow)

Fields classified as UNKNOWN are preserved in `overflow` dict. The system never drops data. A field registry tracks overflow field frequency — fields appearing consistently get flagged for promotion to canonical schema in future versions.

## Training Pipeline

1. `build_mapper_training_data.py` reads `docs/LLM_API_RESPONSE_FORMATS.md`
2. Flattens each provider's example JSON to (path, value) pairs
3. Labels each pair with canonical field name
4. Augments: rename fields (random tokens), shuffle structure, mutate values
5. Extracts features → writes training matrix
6. `train_schema_mapper.py` trains GradientBoosting, cross-validates, exports ONNX
