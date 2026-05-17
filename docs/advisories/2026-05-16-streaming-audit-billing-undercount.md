# INTEGRITY ADVISORY — streamed SSE audit-blank + budget undercount

**Severity:** HIGH (audit-integrity + financial/budget-enforcement).
Not user-facing. Pre-existing; found by the rigor, not introduced by it.

**Status:** fixed in THIS standalone PR (`fix/stream-sse-fragmentation`),
independently revertable, no schema-mapping-feature dependency.

**Surface to the audit/compliance owner AND the billing/budget owner
before deploy. A fix for a silent integrity + billing defect must not
ship silent.**

## Defect

OpenAI / Ollama / HuggingFace `parse_streamed_response` decoded the
buffered stream **per chunk** (`chunk.decode().splitlines()`). The
forwarder buffers raw `aiter_bytes()` chunks, which TCP fragments with no
respect for SSE line or UTF-8 codepoint boundaries. A `data:` line split
across two chunks failed the `startswith("data: ")` test in *both*
fragments and was silently discarded.

Consequence on streamed OpenAI/Ollama/HF traffic under normal TCP
fragmentation (common under load / large tokens / slow networks):

- **Audit integrity:** the lineage/execution record recorded **empty
  content** for responses that returned full text. For a tamper-evidence
  product the audit trail *is* the deliverable.
- **Budget enforcement (financial):** `_record_token_usage` consumes the
  same parsed `usage` → tokens recorded as **0** under fragmentation →
  **budgets/quotas under-counted, spend under-enforced** on the majority
  (streaming) traffic pattern. Direction: under-billing / over-serving.

User-facing delivery was NOT affected (the wire path yields raw chunks;
the broken parse runs post-EOF for audit/usage only).

## Fix

Shared `iter_sse_data_payloads` (openai.py) — `b"".join(chunks)` then
decode then split — applied to `parse_streamed_response` in
openai / ollama / huggingface. Join-before-decode also heals
mid-codepoint UTF-8 corruption. Mirrors Anthropic's already-correct
`_iter_sse_objects` discipline.

## Verification

`tests/production/test_stream_chunk_boundary_discovery.py` — permanent
truth-judged guard: 25 random byte-fragmentations (incl. mid-line and
mid-UTF-8) of a known stream, asserting `parse_streamed_response`
reproduces the KNOWN content AND usage. This is the **same truth oracle**
(byte-identical logic, scoped to `parse_streamed_response`) used by the
schema-mapping feature branch's version of this fix — so the two
necessarily-separate implementations cannot silently diverge.

## RELATED defect that is NOT fixed here (must be surfaced, not buried)

A second, distinct pre-existing audit-integrity defect was found in the
same investigation: **Anthropic translated-path (`/v1/chat/completions`)
drift/overflow audit-blindness** — the Anthropic→OpenAI bridge emits a
fixed-shape dict and drops unknown top-level provider fields, so
production (which audits the bridged body on that path) is blind to
Anthropic provider schema drift / overflow.

**This Anthropic defect's fix is structurally inseparable from the
schema-mapping feature** (the corrected mechanism — honest-audit signal
from the raw Anthropic body via a composition helper — *is* feature
infrastructure). It **cannot be fast-tracked** and **ships only when the
schema-mapping feature lands**.

**Audit/compliance owner action:** Anthropic translated-path
drift/overflow blindness is a confirmed pre-existing production defect on
a major provider's primary path that **will persist until the
schema-mapping feature ships**. That persistence window is a risk you may
need to act on (interim mitigation / disclosure) — that decision is
yours; it is surfaced here explicitly with the ship-gate stated, not
buried as "fixed later". Full technical detail is in the feature branch's
`docs/advisories/2026-05-16-anthropic-translated-path-audit-blind.md`
(commit `f9b0d4e`).

## Owner actions (this PR)

1. Billing/budget owner: assess whether historical streamed-traffic
   budget accounting needs reconciliation (under-count window = however
   long TCP fragmentation has affected these providers in production).
2. Deploy this PR on an expedited path independent of the schema-mapping
   initiative; it is self-contained and revertable.
