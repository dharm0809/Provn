# Provider response specs — gold curation format

Each `<provider>_<endpoint>.json` is hand-curated by reading the provider's
public API documentation and (where available) inspecting real responses
from the gateway WAL. Format:

```jsonc
{
  "provider": "openai",
  "endpoint": "chat.completions",
  "doc_url": "https://platform.openai.com/docs/api-reference/chat/object",
  "captured_at": "2026-04-27T15:00:00Z",
  "captured_from": "Public API docs + N anonymised WAL samples (provider=openai, status=200)",
  "examples": [
    {
      "raw": { /* minimal real response shape, scrubbed of any user data */ },
      "expected_labels": {
        "id": "response_id",
        "model": "model",
        "choices[0].message.content": "content",
        "choices[0].finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "object": "UNKNOWN",
        "created": "UNKNOWN",
        "system_fingerprint": "model_hash"
      },
      "notes": "Plain non-streaming chat-completions response."
    }
  ]
}
```

## Path syntax

Leaf paths use a dotted notation with bracket indexing:
- Object members: `usage.prompt_tokens`
- Array elements: `choices[0].message.content`
- Nested arrays: `choices[0].message.tool_calls[0].function.name`

The encoder reads paths in this exact form (see `linearization.py`).

## Rules for curation

- Every leaf path in `raw` must appear as a key in `expected_labels`.
- Every value in `expected_labels` must be one of the 19 canonical labels
  from `canonical_schema.py:CANONICAL_LABELS`.
- Ambiguous fields → `UNKNOWN`. Do NOT guess.
- Tool-call sub-fields are labelled with their canonical class
  (`tool_call_id`, `tool_call_name`, etc.), not a parent type.
- Streaming chunks live in their own `examples[]` entry with
  `notes: "streaming chunk shape"`.
- Coverage target per provider: ≥ 5 examples across normal /
  streaming / tool-calls / safety-flagged / error states.

## Anonymisation

Real WAL samples MUST be passed through
`src/gateway/content/pii_sanitizer.py` before being committed. In
practice, response IDs, model hashes, and timing values are kept
verbatim; user-visible content is replaced with short synthetic
placeholders. Tool-call argument JSON is replaced with a structurally
similar but content-free string.

## Adding a new provider

1. Create `<provider>_<endpoint>.json` with the schema above.
2. Read the official API docs and capture the documented shape.
3. Where the gateway has live traffic for the provider, capture 5
   real-shape samples from `wal_records.record_json` (NOT
   `gateway_attempts` — that table doesn't carry response bodies).
4. Run `pytest tests/test_provider_fixtures.py -v` to verify every
   leaf path is labelled and every label is in the canonical set.
5. Commit one provider per commit:
   `data(schema_mapper): provider spec for <provider>`

## Holdouts

`xai_grok.json` and `replicate.json` are designated **adversarial
holdouts** — never included in train/val. They live alongside the
others but are excluded by name in `train.py` and surfaced as the
"unseen provider" generalisation gate in `evaluate.py`.
