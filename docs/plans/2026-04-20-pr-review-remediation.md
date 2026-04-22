# PR Review Remediation + Identity Sovereignty Migration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve all 11 PR-review findings AND migrate the gateway from a SHA3-512 Merkle chain to an ID-pointer chain. Under the new architecture:

- **Gateway** = identity authority + observer. Generates every primary key and chain pointer as its own UUIDs. Does NOT compute hashes over record contents, prompts, responses, or tool I/O.
- **Walacor** = trust layer. Computes `DH` (data hash) on ingest, seals records into blockchain envelopes (`BlockId`, `TransId`, `BL`). The gateway surfaces these as the authoritative tamper-evidence at read time; it does not verify them.
- **Ed25519 signing stays** but retargets from `record_hash` to a canonical ID+metadata string — the signature proves gateway authorship of a record *before* it reaches Walacor, separate from Walacor's blockchain provenance.
- **SchemaMapper** continues to normalize provider-varied shapes (content, usage, IDs) into canonical form; provider-native IDs (Anthropic `msg_...`, OpenAI `chatcmpl-...`, Ollama `model_hash`) are evidence in `metadata`, never keys.

**Architecture sequencing:** Phase A (2 independent critical bugs) ships first, then Phase 0 (the big identity migration), then Phases B–F (silent failures, test coverage, type hardening, hygiene). Each phase lands green and independently revertable.

**Tech stack:** Python 3.12, pytest+anyio (`@pytest.mark.anyio`, NEVER `pytest.mark.asyncio`), pydantic, httpx, starlette, React/js-sha3 (removed in Phase 0), Ed25519.

**Non-goals:**
- Not replacing Walacor ingest API.
- Not rewriting SchemaMapper (just adding an ID-normalization hook).
- Not adding any new feature — strictly remediation + architectural migration.

**Load-bearing invariants (from CLAUDE.md + research findings — must hold at every commit):**
1. Gateway never hashes prompt/response text (Walacor does on ingest). Existing.
2. **NEW:** Gateway never hashes record contents or tool I/O either (replaces the current SHA3 chain).
3. Dual-write to BOTH Walacor AND WAL must stay (if/if, never if/elif) — StorageRouter fans out, both succeed or the write is considered degraded.
4. Async tests: `@pytest.mark.anyio` with `anyio_backend` fixture pinned to `["asyncio"]`. Never `pytest.mark.asyncio`.
5. `get_settings.cache_clear()` in teardown when monkeypatching env.
6. ML/ONNX verdicts observe+log, never act — acting goes through declarative policies. `SchemaMapper`, intent classifier, shadow model all fit this rule.
7. `aiter_bytes` stream mocks use `MagicMock(return_value=aiter([...]))`, not `AsyncMock`.
8. React: every hook runs BEFORE any `if (...) return` in a component. Phase 24 hit a blank-dashboard regression from this.
9. The completeness invariant holds: every request gets an attempt record via `completeness_middleware` `finally` block; execution records only post-forward.

---

## Phase A — Critical bugs independent of the architecture migration

These survive unchanged in the new architecture. Ship them first so they don't get caught in Phase 0's rebase.

### Task A.1: Fix silent empty `ModelResponse` on Anthropic parse failure

**Intent:** When Anthropic returns HTML/truncated body on an upstream incident, `parse_response` currently returns `ModelResponse(content="", usage=None, ...)` with zero logging. Orchestrator then writes an empty audit record as if the model replied. Audit integrity is non-negotiable — raise with context so the provider_error path runs.

**Files:**
- Modify: `src/gateway/adapters/anthropic.py:1099-1103`
- Test: `tests/unit/test_anthropic_parse_response.py` (new)

**Step 1 — Write the failing test**

```python
"""parse_response must log + surface parse failures, not silently return empty."""
from __future__ import annotations
import logging
import httpx
import pytest
from gateway.adapters.anthropic import AnthropicAdapter


def test_parse_response_raises_on_non_json(caplog: pytest.LogCaptureFixture) -> None:
    adapter = AnthropicAdapter(base_url="https://api.anthropic.com")
    caplog.set_level(logging.WARNING, logger="gateway.adapters.anthropic")
    resp = httpx.Response(
        status_code=502,
        content=b"<html><body>Bad Gateway</body></html>",
        headers={"content-type": "text/html"},
    )
    with pytest.raises(ValueError, match="Anthropic response body is not valid JSON"):
        adapter.parse_response(resp)
    assert any("parse_response" in r.getMessage() for r in caplog.records)
```

**Step 2 — Run, verify it fails**

```bash
pytest tests/unit/test_anthropic_parse_response.py -v
```

Expected: FAIL (current code returns empty ModelResponse, no raise).

**Step 3 — Fix `parse_response`**

```python
def parse_response(self, response: httpx.Response) -> ModelResponse:
    try:
        data = response.json()
    except Exception as exc:
        preview = (response.content or b"")[:256]
        logger.warning(
            "Anthropic parse_response JSON decode failed "
            "(status=%s len=%d preview=%r): %s",
            response.status_code, len(response.content or b""), preview, exc,
        )
        raise ValueError("Anthropic response body is not valid JSON") from exc
    ...
```

**Step 4 — Verify orchestrator's existing error path handles the raise**

```bash
grep -n "parse_response" src/gateway/pipeline/orchestrator.py
```

Confirm the call site is inside a try/except that converts to `disposition=provider_error`. If not, widen the except first in the same commit.

**Step 5 — Run the test + anthropic-adjacent suites**

```bash
pytest tests/unit/test_anthropic_parse_response.py tests/integration/test_gateway_torture.py -v
```

**Step 6 — Commit**

```bash
git add src/gateway/adapters/anthropic.py tests/unit/test_anthropic_parse_response.py
git commit -m "fix(anthropic): surface parse failures instead of writing empty audit record

parse_response silently returned an empty ModelResponse on non-JSON
upstream bodies. The orchestrator then wrote that empty response to the
audit trail as if the model had replied — corrupting compliance records
under upstream incidents. Log with status + content preview and raise
ValueError so the orchestrator's provider_error path writes the correct
disposition."
```

---

### Task A.2: Retain strong references to in-flight shadow tasks

**Intent:** `asyncio.create_task()` without a retained reference lets the GC collect pending tasks mid-run. Other workers in this codebase use a module-level set + `add_done_callback(set.discard)`; shadow must too. Silently-lost verdicts corrupt the self-learning training signal.

**Files:**
- Modify: `src/gateway/intelligence/shadow.py:175-191`
- Test: `tests/unit/test_shadow_task_retention.py` (new)

**Step 1 — Write the failing test**

```python
"""maybe_fire_shadow must retain a strong reference to the created task."""
from __future__ import annotations
import asyncio
import gc
from unittest.mock import MagicMock
import pytest
import gateway.intelligence.shadow as shadow_mod


@pytest.mark.anyio
async def test_maybe_fire_shadow_retains_task_reference(monkeypatch) -> None:
    runner = MagicMock(); runner.is_enabled = True
    registry = MagicMock()
    candidate = MagicMock(); candidate.version = "v1"
    registry.active_candidate.return_value = candidate

    async def slow_shadow(*a, **kw):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(shadow_mod, "fire_shadow_text", lambda *a, **kw: slow_shadow())
    before = len(shadow_mod._IN_FLIGHT_TASKS)
    shadow_mod.maybe_fire_shadow(
        runner=runner, registry=registry, model_name="intent",
        input_text="hello", production_prediction="normal",
        production_confidence=1.0, infer_on_session=MagicMock(),
    )
    gc.collect()
    assert len(shadow_mod._IN_FLIGHT_TASKS) == before + 1
    await asyncio.sleep(0.1)
    assert len(shadow_mod._IN_FLIGHT_TASKS) == before
```

**Step 2 — Run, verify it fails**

Expected: `AttributeError: module 'gateway.intelligence.shadow' has no attribute '_IN_FLIGHT_TASKS'`.

**Step 3 — Add the strong-ref set + callback**

In `src/gateway/intelligence/shadow.py`, add at module scope:

```python
# Strong references to in-flight shadow tasks. Without this, the GC can
# collect a pending Task whose coroutine is still running, silently
# dropping the shadow verdict and emitting "Exception was never retrieved"
# warnings. Mirror the pattern in completeness._pending_attempt_writes
# and intelligence.api._retrain_tasks.
_IN_FLIGHT_TASKS: set[asyncio.Task[Any]] = set()
```

Change `maybe_fire_shadow` around line 175:

```python
try:
    task = asyncio.create_task(
        fire_shadow_text(...),
        name=f"shadow-{model_name}-{cand.version}",
    )
    _IN_FLIGHT_TASKS.add(task)
    task.add_done_callback(_IN_FLIGHT_TASKS.discard)
except RuntimeError:
    logger.debug("no running event loop; shadow fire skipped")
```

**Step 4 — Run test + existing shadow tests, commit**

```bash
pytest tests/unit/test_shadow_task_retention.py tests/unit/test_shadow_*.py -v
git add src/gateway/intelligence/shadow.py tests/unit/test_shadow_task_retention.py
git commit -m "fix(shadow): retain strong refs to in-flight shadow tasks"
```

---

## Phase 0 — Identity Sovereignty Migration

This is the big architectural change. Sequenced so every commit leaves the suite green and the gateway functioning.

**High-level sequence:**
1. Tasks 0.1–0.4: **additive** — introduce `record_id`, `previous_record_id`, `walacor_block_id`, `walacor_trans_id`, `walacor_dh` fields alongside existing `record_hash`/`previous_record_hash`. Both chains present.
2. Tasks 0.5–0.9: **switch consumers** — verify endpoint, dashboard, compliance export, chain-resume SQL, Ed25519 signing all move to IDs + Walacor envelope fields.
3. Tasks 0.10–0.13: **delete** — remove `compute_sha3_512_string` from session chain + tool executor, remove the hash fields from schemas, remove `js-sha3` from dashboard.
4. Tasks 0.14–0.15: **clean up tests + docs** — rewrite/delete hash-dependent tests, update CLAUDE.md + FLOW-AND-SOUNDNESS.md + README.

### Task 0.1: Add UUIDv7 utility + `record_id` / `previous_record_id` fields (additive)

**Intent:** Introduce the new ID-pointer chain alongside the existing hash chain. Python 3.12 does not ship `uuid.uuid7()` (that's 3.14), so create a small inline generator — zero new deps, ~15 lines — and use it for sovereign ID generation. At this step, both chains are written; nothing consumes the new fields yet. Reversible.

**Important data-shape fact (verified against `src/gateway/wal/writer.py:29-121`):** `record_hash` and `previous_record_hash` are **NOT flat columns** in `wal_records` — they live inside the `record_json` TEXT blob and are accessed via `json_extract`. This means:

- No `ALTER TABLE DROP COLUMN` is ever needed for the WAL migration.
- No VACUUM, no downtime, no schema migration script.
- Legacy record_json blobs keep their hash fields; new blobs don't write them. Readers normalize old vs. new (Task 0.2).

**Files:**
- Create: `src/gateway/util/ids.py` (new — UUIDv7 util)
- Modify: `src/gateway/pipeline/hasher.py:37`
- Modify: `src/gateway/pipeline/session_chain.py:69-100` (in-memory tracker) + Redis tracker equivalent
- Modify: `src/gateway/walacor/client.py:199-228` (`_EXECUTION_SCHEMA_FIELDS`)
- Modify: `src/gateway/pipeline/orchestrator.py:545-585` (`_apply_session_chain`)
- Test: `tests/unit/test_ids.py` (new)
- Test: `tests/unit/test_record_id_chain.py` (new)

**Step 0 — Create the UUIDv7 utility**

`src/gateway/util/ids.py`:

```python
"""Gateway-sovereign ID generation.

UUIDv7 (RFC 9562) gives us time-sortable primary keys — records written
in order have IDs that sort in order, which simplifies pagination,
chain-resume queries, and debugging. Python 3.14 will ship uuid.uuid7();
until this project bumps its floor past 3.12, we generate it inline.
"""
from __future__ import annotations
import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)   # version 7
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)   # variant 10
    b[9:16] = rand[3:10]
    return uuid.UUID(bytes=bytes(b))


def uuid7_str() -> str:
    return str(uuid7())
```

Tests for the util itself (`tests/unit/test_ids.py`):

```python
import time, uuid
from gateway.util.ids import uuid7, uuid7_str

def test_uuid7_version_and_variant() -> None:
    u = uuid7()
    assert u.version == 7
    assert (u.bytes[8] & 0xC0) == 0x80  # variant 10

def test_uuid7_is_time_sortable() -> None:
    a = uuid7_str()
    time.sleep(0.002)
    b = uuid7_str()
    assert a < b

def test_uuid7_is_unique_at_high_rate() -> None:
    ids = {uuid7_str() for _ in range(10_000)}
    assert len(ids) == 10_000
```

**Step 1 — Write the failing test for the chain**

```python
"""record_id + previous_record_id form a valid ID chain per session."""
from __future__ import annotations
import pytest
from gateway.pipeline.hasher import build_execution_record
from gateway.pipeline.session_chain import SessionChainTracker


def test_build_execution_record_sets_record_id() -> None:
    rec = build_execution_record(...)
    # Time-sortable UUIDv7, so two records in order sort in order:
    rec2 = build_execution_record(...)
    assert rec["record_id"] < rec2["record_id"]
    assert len(rec["record_id"]) == 36  # UUID string form


@pytest.mark.anyio
async def test_session_chain_produces_id_pointer() -> None:
    tracker = SessionChainTracker(ttl_seconds=60, max_sessions=100)
    cv1 = await tracker.next_chain_values("s1")
    assert cv1.previous_record_id is None
    await tracker.update("s1", sequence_number=0, record_id="rec-1")
    cv2 = await tracker.next_chain_values("s1")
    assert cv2.previous_record_id == "rec-1"
```

**Step 2 — Implement additively**

- `build_execution_record`: set `record["record_id"] = uuid7_str()` alongside the existing `execution_id`. Don't remove `record_hash` yet.
- `SessionChainTracker.next_chain_values`: return a `ChainValues(sequence_number, previous_record_id, previous_record_hash)` dataclass. Tracker stores both `last_record_id` and `last_record_hash` on `SessionState` during the transition.
- `SessionChainTracker.update(session_id, sequence_number, record_id=None, record_hash=None)`: accept both; deprecate `record_hash` path with a debug log once all call sites migrate.
- `_EXECUTION_SCHEMA_FIELDS` whitelist: add `"record_id"`, `"previous_record_id"`. Keep the hash fields for now.
- `_apply_session_chain`: populate `record["record_id"]` and `record["previous_record_id"]` from the tracker. Still compute + populate the hash fields in this task so the old chain keeps verifying.

**Step 3 — Verify, commit**

```bash
pytest tests/unit/test_ids.py tests/unit/test_record_id_chain.py tests/unit/test_session_chain_*.py -v
git add src/gateway/util/ids.py src/gateway/pipeline/hasher.py src/gateway/pipeline/session_chain.py src/gateway/walacor/client.py src/gateway/pipeline/orchestrator.py tests/unit/test_ids.py tests/unit/test_record_id_chain.py
git commit -m "feat(chain): add UUIDv7 util + record_id/previous_record_id fields (additive)

Adds the sovereign ID chain alongside the existing SHA3 chain. Both
chains are populated at write time; consumers still read the hash chain.
Later Phase 0 tasks will switch consumers to the ID chain, then delete
the hash chain.

- src/gateway/util/ids.py: inline UUIDv7 generator (RFC 9562). Python
  3.14 will ship uuid.uuid7(); replace then.
- record_id, previous_record_id fields on every execution record.
- SessionChainTracker.next_chain_values returns a ChainValues dataclass
  carrying both the new ID pointer and the legacy hash pointer during
  the transition."
```

**Step 3 — Verify, commit**

```bash
pytest tests/unit/test_record_id_chain.py tests/unit/test_session_chain_*.py -v
git commit -m "feat(chain): introduce record_id + previous_record_id fields (additive)

Adds the new ID-pointer chain alongside the existing SHA3 chain. Both
chains are populated at write time; consumers still read the hash chain.
Phase 0 commits will progressively switch consumers to the ID chain,
then delete the hash chain once every reader has migrated.

Adds: record_id (UUID v7, time-sortable), previous_record_id on every
execution record; WAL schema columns; Walacor schema fields."
```

---

### Task 0.2: Surface Walacor envelope + synthesize `record_id` for legacy records at read time

**Intent:** Two jobs, one commit:

**(a)** Walacor already returns `DH`, `BlockId`, `TransId`, `BL` via the `envelopes` $lookup (`walacor_reader.py:290-316`). Today those live in an `_envelope` sub-dict. Promote them to top-level response fields so the dashboard, compliance export, and verify endpoint can display them consistently.

**(b)** Historical records (WAL and Walacor) were written before `record_id` existed. The reader normalizes on read: if `record_id` is missing but `record_hash` is present, synthesize `record_id = f"legacy:{record_hash[:32]}"` deterministically. Same for `previous_record_id`. Downstream code only ever sees the new schema; the translation is one-line and hash-agnostic (it's a string slice, not a hash computation).

**Why deterministic legacy IDs:** every time a legacy record is read, the same synthesized `record_id` comes out. So chain walks across a mix of legacy + new records still link correctly — the first new record's `previous_record_id` literally is `f"legacy:{last_old_hash[:32]}"`, written that way at migration time (Task 0.1 bridge code).

**Files:**
- Modify: `src/gateway/lineage/walacor_reader.py:22-316` (`_deserialize_record` + `get_session_timeline` + `get_execution`)
- Modify: `src/gateway/lineage/reader.py` (WAL reader — same normalizer; emits `None` for envelope fields since WAL-only lineage has no Walacor envelope)
- Modify: `src/gateway/lineage/api.py:_enrich_execution_record`
- Test: `tests/unit/test_lineage_walacor_envelope.py` (new)
- Test: `tests/unit/test_legacy_record_normalization.py` (new)

**Step 1 — Write the failing tests**

```python
def test_execution_response_exposes_walacor_envelope() -> None:
    # Seed a record with a fake envelope lookup result; assert top-level
    # walacor_block_id, walacor_trans_id, walacor_dh, walacor_block_level,
    # walacor_created_at are present.

def test_wal_reader_emits_none_for_envelope_fields() -> None:
    # WAL-only records have no Walacor envelope; response still has all
    # five keys, set to None.

def test_legacy_record_gets_synthesized_record_id() -> None:
    raw = {"record_hash": "a" * 128, "previous_record_hash": "b" * 128, ...}
    normalized = _normalize_record(raw)
    assert normalized["record_id"] == "legacy:" + "a" * 32
    assert normalized["previous_record_id"] == "legacy:" + "b" * 32

def test_legacy_synthesis_is_deterministic() -> None:
    raw = {"record_hash": "c" * 128, ...}
    assert _normalize_record(raw)["record_id"] == _normalize_record(raw)["record_id"]

def test_new_record_passes_through_unchanged() -> None:
    raw = {"record_id": "rec-1", "previous_record_id": "rec-0", ...}
    normalized = _normalize_record(raw)
    assert normalized["record_id"] == "rec-1"
    assert normalized["previous_record_id"] == "rec-0"

def test_first_record_in_session_has_no_previous() -> None:
    raw = {"record_hash": "d" * 128}  # no previous_record_hash
    assert _normalize_record(raw)["previous_record_id"] is None
```

**Step 2 — Implement the normalizer**

Extract a shared helper in `src/gateway/lineage/_normalize.py` (new) so both readers call it:

```python
from __future__ import annotations
from typing import Any

def _legacy_id(hash_str: str | None) -> str | None:
    if not hash_str:
        return None
    return f"legacy:{hash_str[:32]}"

def normalize_record(r: dict[str, Any]) -> dict[str, Any]:
    """Promote Walacor envelope fields to top-level and synthesize record_id
    for legacy records. Safe to call on already-new records (no-op)."""
    if r.get("record_id") is None and r.get("record_hash"):
        r["record_id"] = _legacy_id(r["record_hash"])
    if r.get("previous_record_id") is None and r.get("previous_record_hash"):
        r["previous_record_id"] = _legacy_id(r["previous_record_hash"])
    env_list = r.pop("env", None) or []
    env = env_list[0] if env_list else {}
    r["walacor_block_id"]    = env.get("BlockId")
    r["walacor_trans_id"]    = env.get("TransId")
    r["walacor_dh"]          = env.get("DH")
    r["walacor_block_level"] = env.get("BL")
    r["walacor_created_at"]  = env.get("CreatedAt")
    return r
```

Wire into both readers at the end of their deserialization path. WAL reader doesn't have an envelope join, so the Walacor fields end up as `None` — fine.

**Step 3 — Run tests, commit**

```bash
pytest tests/unit/test_lineage_walacor_envelope.py tests/unit/test_legacy_record_normalization.py tests/unit/test_lineage_reader.py -v
git add src/gateway/lineage/_normalize.py src/gateway/lineage/walacor_reader.py src/gateway/lineage/reader.py src/gateway/lineage/api.py tests/unit/test_lineage_walacor_envelope.py tests/unit/test_legacy_record_normalization.py
git commit -m "feat(lineage): expose Walacor envelope + synthesize legacy record_id on read

(a) Promote DH, BlockId, TransId, BL, CreatedAt from _envelope sub-dict
    to top-level response fields so dashboard and compliance export can
    display Walacor's blockchain attestation directly.
(b) Legacy records (written before record_id existed) get a deterministic
    record_id = 'legacy:{record_hash[:32]}' synthesized at read time, so
    downstream code only ever sees the new ID-pointer schema.
    Synthesis is a string slice, not a hash — no SHA3 work on the read
    path."
```

---

### Task 0.3: Retarget Ed25519 signing from `record_hash` to a canonical ID string

**Intent:** Signing proves gateway authorship separate from Walacor's provenance. Under the new model, sign a canonical string of IDs and stable metadata — no SHA3 input needed. The signature lives in `record_signature` as before; format changes.

**Files:**
- Modify: `src/gateway/crypto/signing.py:38-56` — add `sign_canonical(record_id, previous_record_id, sequence_number, execution_id, timestamp) -> signature_bytes`
- Modify: `src/gateway/pipeline/orchestrator.py:_apply_session_chain` — call new signer with ID fields
- Modify: `tests/unit/test_signing.py:71-74` — migrate to canonical string input
- Test: `tests/unit/test_signing_canonical.py` (new)

**Step 1 — Test**

```python
def test_sign_canonical_and_verify_round_trip() -> None:
    priv, pub = generate_keypair()
    sig = sign_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        private_key=priv,
    )
    assert verify_canonical(
        record_id="rec-1", previous_record_id=None,
        sequence_number=0, execution_id="exec-1",
        timestamp="2026-04-20T00:00:00Z",
        signature=sig, public_key=pub,
    ) is True

def test_sign_canonical_is_sensitive_to_each_field() -> None:
    # Change any field → signature no longer verifies
    ...
```

**Step 2 — Implement**

```python
def _canonical_bytes(record_id, previous_record_id, sequence_number, execution_id, timestamp) -> bytes:
    return "|".join([
        record_id or "",
        previous_record_id or "",
        str(sequence_number),
        execution_id,
        timestamp,
    ]).encode("utf-8")

def sign_canonical(*, record_id, previous_record_id, sequence_number, execution_id, timestamp, private_key) -> bytes:
    return _sign(_canonical_bytes(record_id, previous_record_id, sequence_number, execution_id, timestamp), private_key)

def verify_canonical(*, record_id, previous_record_id, sequence_number, execution_id, timestamp, signature, public_key) -> bool:
    return _verify(_canonical_bytes(...), signature, public_key)
```

Keep the legacy `sign_hash` / `verify_signature` exported for one release cycle; mark `@deprecated` with a log message on first call.

**Step 3 — Wire into `_apply_session_chain`**

Replace the call that feeds `record_hash` with the canonical signer. `record_signature` field stays; payload format changes.

**Step 4 — Run, commit**

```bash
pytest tests/unit/test_signing*.py tests/unit/test_session_chain_*.py -v
git commit -m "feat(signing): retarget Ed25519 to canonical ID string instead of record_hash"
```

---

### Task 0.4: Chain verify server endpoint + dashboard UI switch to ID walk + Walacor envelope

**Intent:** `/v1/lineage/verify/{id}` today recomputes SHA3 chain. Replace with: walk by `previous_record_id` pointers, assert sequence numbers are contiguous, surface each record's Walacor `BlockId`/`TransId`/`DH`. Dashboard does the same — no `js-sha3` recompute.

**Files:**
- Modify: `src/gateway/lineage/reader.py:696-730` (WAL `verify_chain`)
- Modify: `src/gateway/lineage/walacor_reader.py:542-583` (Walacor `verify_chain`)
- Modify: `src/gateway/lineage/api.py:377` (endpoint handler — response shape)
- Modify: `src/gateway/lineage/dashboard/src/views/Timeline.jsx` — remove js-sha3 import, switch verify button to ID walk (client calls server endpoint)
- Modify: `src/gateway/lineage/dashboard/src/views/Sessions.jsx` — update verify card labels from "SHA3-512 · ED25519" to "ID chain · ED25519 · Walacor sealed"
- Modify: `src/gateway/lineage/dashboard/src/views/Execution.jsx:167-179` — replace "Record Hash"/"Previous Hash" rows with "Record ID"/"Previous ID" + "Walacor Block"/"Walacor Trans" rows
- Test: `tests/unit/test_verify_chain_by_id.py` (new)

**Step 1 — Tests**

```python
def test_verify_chain_walks_id_pointers() -> None:
    # Seed 3 records with record_id and previous_record_id forming a chain.
    # verify_chain returns valid=True.

def test_verify_chain_detects_broken_pointer() -> None:
    # Tamper: record 2's previous_record_id doesn't match record 1's record_id.
    # verify_chain returns valid=False, error mentions which record broke.

def test_verify_chain_detects_sequence_gap() -> None:
    # Records with sequence_number 0, 1, 3. verify_chain returns valid=False.

def test_verify_response_includes_walacor_envelope_per_record() -> None:
    # Each record in the verify response carries its walacor_block_id etc.
```

**Step 2 — Implement server-side**

Both `LineageReader.verify_chain` and `WalacorLineageReader.verify_chain`:

```python
def verify_chain(self, session_id: str) -> dict:
    records = self.get_session_timeline(session_id)  # sorted by sequence_number asc
    errors = []
    expected_prev = None
    for i, r in enumerate(records):
        if r["sequence_number"] != i:
            errors.append(f"sequence gap at record {i}: expected {i}, got {r['sequence_number']}")
        if r.get("previous_record_id") != expected_prev:
            errors.append(f"id pointer mismatch at sequence {i}")
        expected_prev = r["record_id"]
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "records_checked": len(records),
        "walacor_attestation": [
            {"record_id": r["record_id"],
             "walacor_block_id": r.get("walacor_block_id"),
             "walacor_trans_id": r.get("walacor_trans_id"),
             "walacor_dh": r.get("walacor_dh")}
            for r in records
        ],
    }
```

**Step 3 — Dashboard**

Remove `import { sha3_512 } from 'js-sha3'` from `Timeline.jsx`. Delete the client-side recompute loop. Replace the verify button handler with a call to the server endpoint; render the returned `walacor_attestation` list.

Update `Execution.jsx` DetailRow labels + source fields: `record_hash` → `record_id`; `previous_record_hash` → `previous_record_id`; add rows for `walacor_block_id`, `walacor_trans_id`, `walacor_dh` with copy buttons.

**Step 4 — Dashboard build + smoke test**

```bash
cd src/gateway/lineage/dashboard && npm run build
# Start gateway, navigate to /lineage/, verify the Sessions view verify-chain UI works,
# Execution detail shows new ID + Walacor fields.
```

**Step 5 — Commit per surface (server first, then dashboard — two commits)**

```bash
git add src/gateway/lineage/reader.py src/gateway/lineage/walacor_reader.py src/gateway/lineage/api.py tests/unit/test_verify_chain_by_id.py
git commit -m "feat(lineage): verify chain by ID pointers + surface Walacor attestation"

git add src/gateway/lineage/dashboard/ src/gateway/lineage/static/
git commit -m "feat(dashboard): drop js-sha3, show Walacor envelope per record, switch verify UI"
```

---

### Task 0.5: Remove `js-sha3` npm dependency

**Files:**
- Modify: `src/gateway/lineage/dashboard/package.json`
- Modify: `src/gateway/lineage/dashboard/package-lock.json`

**Step 1:** `cd src/gateway/lineage/dashboard && npm uninstall js-sha3`

**Step 2:** grep the tree — must be zero remaining imports:

```bash
grep -r "js-sha3\|sha3_512\|keccak" src/gateway/lineage/dashboard/src/
```

**Step 3:** Rebuild + smoke + commit.

---

### Task 0.6: Migrate chain-resume on gateway restart

**Intent:** `wal/writer.py:508-517` reads `record_hash` from WAL to restore in-memory `SessionChainTracker` on startup. Switch to `record_id`.

**Files:**
- Modify: `src/gateway/wal/writer.py:508-517` (chain-resume SQL)
- Modify: `src/gateway/pipeline/session_chain.py` — `warm()` accepts `(session_id, seq, record_id)` instead of `(session_id, seq, record_hash)`
- Test: `tests/unit/test_chain_resume.py` (new or extend existing)

**Step 1 — Test: write records with record_id, restart tracker via warm(), assert next_chain_values returns the right previous_record_id**

**Step 2 — SQL migration**

```python
cur.execute("""
  SELECT session_id,
         MAX(sequence_number) AS seq,
         json_extract(record_json, '$.record_id') AS record_id
  FROM wal_records
  WHERE session_id IS NOT NULL
    AND json_extract(record_json, '$.record_id') IS NOT NULL
  GROUP BY session_id
""")
```

**Step 3 — Commit**

```bash
git commit -m "feat(chain): resume session chain from record_id on gateway restart"
```

---

### Task 0.7: Migrate compliance CSV/JSON exports

**Files:**
- Modify: `src/gateway/compliance/api.py:17-22` (CSV columns) + `99-111` (JSON shape)
- Modify: `src/gateway/compliance/pdf_report.py` (if it renders hashes, which the research said it doesn't — but verify)
- Test: `tests/unit/test_compliance_export.py` (new or extend)

**Step 1 — Update `_CSV_COLUMNS`:**

Remove `"record_hash"`. Add `"record_id"`, `"previous_record_id"`, `"walacor_block_id"`, `"walacor_trans_id"`, `"walacor_dh"`.

**Step 2 — Update JSON `chain_integrity` shape:**

Replace SHA3 chain report with ID chain report + per-record Walacor attestation.

**Step 3 — Test**

```python
def test_csv_export_contains_record_id_and_walacor_fields() -> None:
    ...

def test_csv_export_no_longer_has_record_hash_column() -> None:
    ...
```

**Step 4 — Commit**

```bash
git commit -m "feat(compliance): replace record_hash CSV column with record_id + Walacor envelope"
```

---

### Task 0.8: Delete SHA3 computation from session chain + route Merkle checkpoint to Walacor DH

**Intent:** Once every consumer of `record_hash` has migrated (Tasks 0.4, 0.6, 0.7), remove the write-side computation entirely. Because `record_hash` is NOT a flat WAL column — it lives inside the `record_json` TEXT blob — there's no `ALTER TABLE DROP COLUMN`, no VACUUM, no downtime. New records simply don't carry the field; legacy records keep theirs and are normalized on read by Task 0.2.

The `main.py` Merkle checkpoint task (`main.py:1326-1344`) reads `$.record_hash` from `record_json` to build a Merkle tree for logging. Under the new architecture, the meaningful Merkle leaves are Walacor's `DH` values (the real blockchain-attested hashes, not our own). Retarget the task to build the tree over `DH` from the envelope join — same `build_merkle_tree` function, new leaf source.

**Keep `compute_sha3_512_string` in `src/gateway/core/`** — the attachment tracker (`attachment_tracker.py:123-125`) hard-validates `hash_sha3_512` in the attachment POST body. That hash is computed by the OpenWebUI filter client-side, not the gateway — we just validate the shape. That path is unrelated to the record chain.

**Files:**
- Modify: `src/gateway/pipeline/session_chain.py` — delete `compute_record_hash`; tracker stops tracking `last_record_hash`; `ChainValues` drops the `previous_record_hash` field
- Modify: `src/gateway/pipeline/hasher.py` — drop `record_hash` and `previous_record_hash` from the output dict
- Modify: `src/gateway/pipeline/orchestrator.py:_apply_session_chain` — stop populating hash fields
- Modify: `src/gateway/walacor/client.py:_EXECUTION_SCHEMA_FIELDS` — remove `"record_hash"`, `"previous_record_hash"` from the whitelist
- Modify: `src/gateway/main.py:1326-1344` — retarget Merkle checkpoint to read `$.walacor_dh` (or fetch via envelope join if DH isn't persisted into record_json)
- Modify: `src/gateway/schema/anomaly.py:97` — drop the `chain_hash_missing` warning rule (it's now expected, not an anomaly)
- Modify: `src/gateway/wal/writer.py:508-517` — chain-resume SQL already migrated in Task 0.6 to prefer `$.record_id`; confirm the legacy `$.record_hash` fallback has a `COALESCE` that tolerates NULL
- Test: `tests/unit/test_session_chain_no_hash.py` (new)
- Test: `tests/unit/test_merkle_checkpoint_uses_dh.py` (new)

**Step 1 — Grep to confirm zero remaining readers of the hash fields in production code**

```bash
grep -rn "record_hash\|previous_record_hash" src/gateway/ \
  | grep -v "# legacy" \
  | grep -v "_normalize" \
  | grep -v "test"
```

Expected: hits only in (a) `lineage/_normalize.py` legacy-synthesis path, (b) Merkle checkpoint task which this commit is about to update. If anything else shows up, migrate it first — don't delete on top of a live reader.

**Step 2 — Write the failing tests**

```python
def test_build_execution_record_has_no_hash_fields() -> None:
    rec = build_execution_record(...)
    assert "record_hash" not in rec
    assert "previous_record_hash" not in rec
    assert rec["record_id"]  # still present

def test_chain_values_dataclass_has_only_id_pointer() -> None:
    cv = ChainValues(sequence_number=3, previous_record_id="rec-prev")
    # The previous_record_hash field no longer exists on ChainValues

def test_walacor_schema_whitelist_excludes_hashes() -> None:
    from gateway.walacor.client import _EXECUTION_SCHEMA_FIELDS
    assert "record_hash" not in _EXECUTION_SCHEMA_FIELDS
    assert "previous_record_hash" not in _EXECUTION_SCHEMA_FIELDS

def test_merkle_checkpoint_builds_tree_from_walacor_dh() -> None:
    # Seed WAL with records carrying walacor_dh in record_json; the
    # checkpoint task logs a root derived from those DH values.
```

**Step 3 — Delete SHA3 computation, retarget Merkle, run suite**

- Remove `compute_record_hash` from `session_chain.py` entirely.
- Drop `last_record_hash` field from `SessionState`; tracker only stores `last_record_id` now.
- Remove `record_hash`/`previous_record_hash` assignments in `_apply_session_chain`.
- Rewrite `main.py` Merkle SQL:

```python
cur = conn.execute(
    "SELECT json_extract(record_json, '$.walacor_dh') FROM wal_records "
    "WHERE json_extract(record_json, '$.walacor_dh') IS NOT NULL "
    "ORDER BY created_at DESC LIMIT 1000"
)
```

(If `walacor_dh` isn't persisted into `record_json` by the writer — which it shouldn't be, since DH is Walacor-assigned — the Merkle checkpoint needs to query Walacor directly via the envelope lookup. Confirm during implementation; may require a small extension in `walacor_reader` to expose a bulk-DH-fetch.)

- Drop the `chain_hash_missing` rule from `anomaly.py:97`.

**Step 4 — Run full suite**

```bash
pytest tests/ -x -q 2>&1 | tail -30
```

Expected: failures only in tests that still assert on `record_hash`. Those migrate in Task 0.11.

**Step 5 — Commit**

```bash
git add src/gateway/pipeline/session_chain.py src/gateway/pipeline/hasher.py src/gateway/pipeline/orchestrator.py src/gateway/walacor/client.py src/gateway/main.py src/gateway/schema/anomaly.py tests/unit/test_session_chain_no_hash.py tests/unit/test_merkle_checkpoint_uses_dh.py
git commit -m "feat(chain): stop computing SHA3 record_hash — Walacor DH is now authoritative

- Remove compute_record_hash from session_chain; tracker stores only
  last_record_id going forward.
- Drop record_hash / previous_record_hash from the execution record
  output and from the Walacor schema whitelist.
- Retarget the Merkle checkpoint task to use Walacor DH values as leaves
  (the real blockchain-attested hashes) instead of gateway-computed SHA3.
- Drop the chain_hash_missing anomaly rule (expected state now).

WAL record_hash/previous_record_hash data: lives inside the record_json
TEXT blob, not a flat column — no ALTER TABLE, no VACUUM, no downtime.
Legacy records keep their hash fields in blob form and are normalized
on read by lineage._normalize.normalize_record (Task 0.2)."
```

---

### Task 0.9: Delete SHA3 computation from tool_executor (subsumes original Critical #1)

**Intent:** `tool_executor.py:614-619` currently calls `compute_sha3_512_string` (and crashes because the import is missing). Under the new architecture, these lines are **deleted**, not fixed. Tool I/O raw data still goes to Walacor; Walacor hashes on ingest.

**Files:**
- Modify: `src/gateway/pipeline/tool_executor.py:598-625` — delete `input_hash`/`output_hash` computation
- Modify: `src/gateway/walacor/client.py:_TOOL_EVENT_SCHEMA_FIELDS` — remove `input_hash`, `output_hash`

**Step 1 — Test: tool events are written without `input_hash`/`output_hash` fields**

```python
def test_tool_event_record_has_no_gateway_hashes() -> None:
    ti = ToolInteraction(name="web_search", source="builtin",
                         input_data={"query": "hi"}, output_data={"r": [1]},
                         sources=None, duration_ms=1, iteration=1)
    rec = _build_tool_event_record(ti, execution_id="e1", session_id="s1", tenant_id="t1")
    assert "input_hash" not in rec
    assert "output_hash" not in rec
    # Raw data is preserved — Walacor will hash on ingest
    assert rec["input_data"] == {"query": "hi"}
    assert rec["output_data"] == {"r": [1]}
```

**Step 2 — Delete the three hash-computing lines and the import that was never added**

**Step 3 — Run, commit**

```bash
pytest tests/unit/test_tool_executor_hashes.py tests/integration/test_property_tool_audit.py -v
git commit -m "feat(tool_events): stop computing input/output hashes — Walacor DH is authoritative"
```

---

### Task 0.10: Deprecate legacy `sign_hash`/`verify_signature`

**Files:**
- Modify: `src/gateway/crypto/signing.py` — add deprecation warning, schedule removal in next release

**Step 1 — Add `@deprecated` decorator with removal timeline + log at call site**

**Step 2 — Grep for callers in production code, confirm none remain**

**Step 3 — Commit**

---

### Task 0.11: Migrate / delete tests asserting on `record_hash`

**Files (per research report):**
- `tests/integration/test_stateful_gateway.py:196-609` — rewrite to assert ID chain
- `tests/integration/test_gateway_torture.py:425-600` — rewrite chain-recompute to ID walk
- `tests/integration/test_property_session_chain.py:18-228` — rewrite properties for ID chain (uniqueness, monotonicity, pointer linkage)
- `tests/integration/test_property_redis_parity.py` — same
- `tests/integration/test_property_tool_audit.py:60-102` — delete input_hash/output_hash tests; add input_data/output_data preservation tests
- `tests/integration/test_live_llama.py:654-880` — rewrite 128-char hash assertions to record_id UUID assertions
- `tests/unit/test_lineage_reader.py:53-294` — tamper tests become "tamper previous_record_id"
- `tests/unit/test_compliance_queries.py` — update fixtures
- `tests/unit/test_compliance_api.py:38` — update fixtures
- `tests/unit/test_redis_trackers.py:562-629` — update chain shape
- `tests/unit/test_session_chain_serialization.py` — migrate
- `tests/unit/test_signing.py:71-74` — migrate to canonical string
- `tests/unit/test_wal_writer_perf.py:36-37` — update fixtures

**Step 1 — Migrate one file at a time.** Each commit: one file, tests green. The suite stays green throughout.

**Step 2 — After all migrations, full suite run**

```bash
pytest tests/ -x -q 2>&1 | tail -30
```

**Step 3 — Commit per file. ~12 commits expected.**

---

### Task 0.12: Tier 6b production gate — update hash check to ID check

**Files:**
- `tests/production/tier6_mcp.py:220-283`

**Step 1 — Replace `len(input_hash) == 128 and all(c in hexdigits for c in input_hash)` with `record_id` UUID check (36 chars, valid UUID format)**

**Step 2 — Commit**

---

### Task 0.13: Update CLAUDE.md + FLOW-AND-SOUNDNESS.md + README

**Intent:** Codify the new architecture. Strike "Gateway computes session chain record_hash" and "Tool input/output hashes ARE computed by the gateway" from CLAUDE.md. Add "Gateway is sovereign over identity; Walacor provides tamper-evidence via blockchain envelope."

**Files:**
- `CLAUDE.md` — Key Architectural Facts section
- `docs/FLOW-AND-SOUNDNESS.md` — soundness analysis
- `docs/HOW-IT-WORKS.md` — chain description
- `README.md` — chain section
- `docs/WIKI-EXECUTIVE.md` — narrative (no crypto formulas)
- `docs/EU-AI-ACT-COMPLIANCE.md` — if chain is cited

**Step 1 — One commit per doc**

---

### Task 0.14: Remove legacy SHA3 paths from core (if unused)

**Step 1 — Grep for remaining `compute_sha3_512_string` callers**

```bash
grep -rn "compute_sha3_512_string\|compute_sha3_512" src/gateway/
```

If only attachment-tracker + signing-legacy remain, keep the function. If truly unused, remove.

**Step 2 — Commit**

---

### Task 0.15: Phase 0 acceptance — full suite + torture test + dashboard smoke

```bash
pytest tests/ -x -q 2>&1 | tail -20
cd src/gateway/lineage/dashboard && npm run build
# Start gateway, run through all 7 dashboard tabs
# Run tests/production/tier6_mcp.py against a live gateway
```

**Commit the branch-level summary.**

---

## Phase B — Silent-failure sweep

Runs after Phase 0 lands. Most items are unchanged; Task B.4 subsumed by Phase 0 is removed here.

### Task B.1: Log fallback retry failure — orchestrator.py:188-192
### Task B.2: Replace bare `pass` on intelligence enqueue — orchestrator.py:1670-1672
### Task B.3: walacor_reader + compliance/api silent `{}` → log + return `{}`
### Task B.4: Log Anthropic `_parse_data_url` failures
### Task B.5: Narrow Responses-API stream override exception
### Task B.6: Log identity resolution fallback
### Task B.7: Prometheus metric `pass` sweep (6 sites)

Each follows the Phase-B pattern from the original plan: test, fix, commit. Full TDD detail preserved in git history of this file's prior revision.

---

## Phase C — Resource leak

### Task C.1: Prune `_session_locks` on eviction (session_chain.py:124-135)

---

## Phase D — Test coverage backfill

### Task D.1: Anthropic request-translation coverage (12 cases)
### Task D.2: Anthropic SSE translator state machine + TCP-split regression fixture
### Task D.3: `walacor_reader` coverage for 5 query paths + type coercion
### Task D.4: `audit_intelligence` + `compliance/api` + `audit_classifier` contract tests

---

## Phase E — Type hardening

Sequence: one type per commit, full suite after each, revert commit on ripple failure.

### Task E.1: Freeze `ModelVerdict` identity, validate confidence range
### Task E.2: `LifecycleEvent` — per-variant payload types
### Task E.3: `CanonicalResponse` — frozen + auto-total; split `MappingReport`
### Task E.4: `AnomalyReport` — immutable + `Literal` codes
### Task E.5: `_ModelStats` — split `observe` / `z_score`
### Task E.6: `HarvesterSignal` — `Literal` model_name + `TypedDict` context
### Task E.7: `_AnthropicToOpenAISSE` — explicit FSM (uses D.2 fixtures as regression)
### Task E.8: Control-plane mutation input models (`AttestationUpsert`, etc.)

---

## Phase F — Hygiene

### Task F.1: Fix 3 factually-wrong comments
### Task F.2: Remove booby-trap `None` aliases in orchestrator.py:505-517
### Task F.3: Bare TODOs → tracking links or deletion
### Task F.4: "Phase 25 Task N" comment sweep (~35 sites, one commit per file)
### Task F.5: Deferral-comment cleanup (4 sites)
### Task F.6: Fluff / tutorial-narrative deletion in classifier/ + intelligence/

---

## Phase G — Final verification

### Task G.1: Full suite + branch-wide lint + dashboard smoke + Tier 6b live

```bash
pytest tests/ -x -q 2>&1 | tail -30
grep -rn "except.*:\s*$\|^\s*pass\s*$" src/gateway/ | grep -v "# intentional"
grep -rn "asyncio.create_task(" src/gateway/  # every one must retain a ref
grep -rn "compute_sha3_512" src/gateway/  # must match research conclusion (attachment + signing-deprecated only)
cd src/gateway/lineage/dashboard && npm run build
# Walk every dashboard tab; confirm no React hook errors, chain verify UI works
```

Final commit summarising the full remediation.

---

## Execution discipline

- **One task, one commit.** Every commit rebases cleanly on its predecessor.
- **Every Phase 0 commit leaves the suite green.** Additive → switch → delete. Never "break everything, fix in next commit."
- **Never skip a failing test.** If one fails for an unrelated reason, capture it as a new task.
- **Full suite after every Phase.** Ripple failures surface early.
- **If a Phase E type redesign ripples unexpectedly → revert that single commit.** Don't debug on top.
- **Dashboard smoke after Task 0.4 and Task 0.5.** Rules of Hooks + no orphan `js-sha3` import.
- **Don't silently widen scope.** If a task reveals intent we got wrong, stop and ask — don't paper over with a workaround.
