"""DISCOVERY (not confirmation): does delivery parsing survive REAL chunk
boundaries, judged against TRUTH?

The forwarder buffers raw `aiter_bytes()` chunks. TCP does NOT respect
SSE line boundaries or UTF-8 codepoint boundaries. This takes a stream
whose correct decoded content/usage are KNOWN, re-splits the raw bytes at
random offsets (incl. inside a `data:` line and inside a multi-byte UTF-8
char), and asserts `parse_streamed_response` reproduces the KNOWN TRUTH —
content AND usage (the audit-blank + budget-undercount failure).

SHARED ORACLE: this is byte-identical in logic to the feature branch's
`test_stream_chunk_boundary_discovery.py`, scoped to `parse_streamed_response`
only (no reassembler dependency). Same truth oracle on both
implementations is what prevents silent divergence when the code cannot
be shared (the standalone fix vs the feature-branch fix). Permanent
guard — never weakened to go green.
"""
import random

import pytest

from gateway.adapters.openai import OpenAIAdapter

_TRUTH_CONTENT = "Café ☕ déjà vu — naïve façade 日本語"
_TRUTH_PROMPT, _TRUTH_COMPLETION = 7, 11

_STREAM = (
    'data: {"id":"c","model":"gpt-4o-mini","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Café ☕ "},"finish_reason":null}]}\n\n'
    'data: {"id":"c","model":"gpt-4o-mini","choices":[{"index":0,'
    '"delta":{"content":"déjà vu — "},"finish_reason":null}]}\n\n'
    'data: {"id":"c","model":"gpt-4o-mini","choices":[{"index":0,'
    '"delta":{"content":"naïve façade 日本語"},"finish_reason":null}]}\n\n'
    'data: {"id":"c","model":"gpt-4o-mini","choices":[{"index":0,'
    '"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,'
    '"completion_tokens":11,"total_tokens":18}}\n\n'
    "data: [DONE]\n\n"
).encode("utf-8")


def _resplit(raw: bytes, n: int, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    cuts = sorted(rng.sample(range(1, len(raw)), min(n, len(raw) - 1)))
    out, prev = [], 0
    for c in cuts:
        out.append(raw[prev:c])
        prev = c
    out.append(raw[prev:])
    return out


@pytest.mark.parametrize("seed", range(25))
def test_delivery_parse_matches_TRUTH_under_random_byte_splits(seed):
    a = OpenAIAdapter("http://x", "k")
    buf = _resplit(_STREAM, n=12, seed=seed)
    mr = a.parse_streamed_response(buf)
    assert mr.content == _TRUTH_CONTENT, (
        f"DELIVERY content corrupted by byte-split (seed={seed}): {mr.content!r}")
    usage = mr.usage or {}
    assert usage.get("prompt_tokens") == _TRUTH_PROMPT, (
        f"DELIVERY usage lost → budget undercount (seed={seed}): {usage}")
    assert usage.get("completion_tokens") == _TRUTH_COMPLETION, (seed, usage)
