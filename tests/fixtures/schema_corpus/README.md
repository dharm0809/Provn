# Schema corpus

Fixture cases for the Phase 4 perfect-coverage CI gate
(`tests/production/test_schema_coverage_gate.py`).

Each case lives at `<target>/<variant>.json` with this shape:

```json
{
  "target": "openai",
  "variant": "nonstream_basic",
  "raw": { "...": "the exact provider response JSON" },
  "expected": {
    "content": true,
    "finish_reason": true,
    "usage.prompt_tokens": true
  }
}
```

`raw` is fed verbatim to `SchemaMapper.map_response(...)`. `expected` lists
the canonical field paths that MUST be populated for this case.

## Authoring rules

1. **Real shapes only.** Synthesize-the-shape-from-memory drifts from real
   provider responses silently. Capture from a real call (Phase 8) or copy
   a verified example from this provider's docs.
2. **Conservative `expected` to start.** When adding a case, list ONLY the
   fields you're confident the deterministic map produces today. Use the
   gate failures to drive map extensions in Phase 5, not the other way
   around (don't pre-emptively promise fields the map can't yet produce
   and then xfail them — that just hides regressions).
3. **No zero-default token counts.** `_is_populated` treats `0` as "not
   populated" because every `CanonicalUsage` field defaults to 0. If a
   case asserts `usage.prompt_tokens` is expected, its `raw` payload MUST
   carry a non-zero count — otherwise the assertion passes for the wrong
   reason.
4. **Skip envelope keys in `expected`.** Envelope keys (`object`,
   `created`, `role`, `index`, …) are tagged via `_apply_path_fallbacks`
   and excluded from `overflow` upstream — they're not canonical fields.

## What the gate enforces

For every case: `coverage_pct == 100.0` AND `overflow_keys == []`. Either
failure breaks the build. Do not xfail/skip individual cases to make this
pass — extend `_PROVIDER_PATH_MAP` (`src/gateway/schema/mapper.py:146`)
instead.

## Closed target set

See `docs/plans/2026-05-16-schema-mapping-perfect-score.md`. Adding a new
target requires a new plan, not a new fixture.
