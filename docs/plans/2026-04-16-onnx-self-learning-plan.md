# ONNX Self-Learning Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the self-learning feedback loop so the three ONNX models (Intent, SchemaMapper, Safety) continuously improve from production traffic, validated via shadow mode, promoted via dashboard with audit-chain anchored lifecycle events — without compromising the Gateway's observer identity.

**Architecture:** Five components (Verdict Log, Feedback Harvesters, Distillation Worker, Model Registry, Shadow Validator) inside the gateway process. Dual-write pattern: high-volume telemetry in new `intelligence.db` SQLite, compliance-critical lifecycle events anchored to Walacor via new ETId `onnx_lifecycle_event`. Promotion is human-in-loop by default; `auto_promote=true` is opt-in per model. **Core principle: ONNX produces verdicts; policies decide actions.**

**Tech Stack:** Python 3.11+, asyncio, sqlite3 (stdlib), onnxruntime, sklearn + skl2onnx (both already in project for training), pydantic-settings for config, existing WAL + Walacor dual-write pattern, existing dashboard SPA (vanilla JS under `src/gateway/lineage/dashboard/`).

**Design reference:** `docs/plans/2026-04-16-onnx-self-learning-design.md` — full architecture, storage split, error handling, compliance posture.

---

## Phase Overview

| Phase | Focus | Tasks |
|---|---|---|
| A | Foundation — config, schemas, ETId, data types | 1–4 |
| B | Verdict Log — buffer, flusher, integration | 5–8 |
| C | Model Registry — directory layout, atomic swap, reload | 9–12 |
| D | Feedback Harvesters — Intent, SchemaMapper, Safety | 13–16 |
| E | Distillation Worker — scheduler, trainer, dataset fingerprint | 17–21 |
| F | Shadow Validator — parallel inference, gate evaluation | 22–25 |
| G | Promotion API — control plane endpoints | 26–29 |
| H | Dashboard — Intelligence sub-tab | 30–34 |
| I | Observability + chaos validation | 35–37 |

Each task is one commit. Strict TDD for logic-heavy tasks (buffer, gates, lock, harvesters); lighter touch for boilerplate (dir init, config).

---

## Phase A — Foundation

### Task 1: Config entries for the intelligence layer

**Files:**
- Modify: `src/gateway/config.py`
- Modify: `.env.example`
- Test: `tests/unit/test_config_intelligence.py`

**Conventions (apply to every task, not just Task 1):**
- Follow the existing style of the file being edited — do NOT invent alphabetical ordering or new header styles. The `Settings` class is organized chronologically by phase; `.env.example` uses box-drawing section headers like `# ── Phase N: Name ──────`.
- Use pydantic `Field(default=..., ge=..., le=..., description=...)` form, matching how every other field in `config.py` is declared.
- Add bounds (`ge`/`le`) on ratios (0.0–1.0) and positive-integer counts (`ge=1`) so misconfigured env vars fail at startup, not later.
- Namespace field names so they're unambiguous without context — e.g. `onnx_models_base_path`, not `models_base_path` (the gateway already has many "model" concepts).

**Step 1: Write the failing test**

```python
# tests/unit/test_config_intelligence.py
from __future__ import annotations

import pytest

from gateway.config import Settings, get_settings


def test_bounds_rejected():
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        Settings(shadow_max_disagreement=1.5)
    with pytest.raises(ValueError):
        Settings(teacher_llm_sample_rate=-0.1)
    with pytest.raises(ValueError):
        Settings(verdict_retention_days=0)


def test_intelligence_defaults():
    get_settings.cache_clear()
    s = Settings()
    assert s.intelligence_enabled is True
    assert s.verdict_retention_days == 30
    assert s.distillation_min_divergences == 500
    assert s.shadow_sample_target == 1000
    assert s.shadow_min_accuracy_delta == 0.02
    assert s.shadow_max_disagreement == 0.40
    assert s.shadow_max_error_rate == 0.05
    assert s.auto_promote_models == ""
    assert s.teacher_llm_sample_rate == 0.01


def test_auto_promote_list():
    get_settings.cache_clear()
    s = Settings(auto_promote_models="intent,safety")
    assert s.auto_promote_models_list == ["intent", "safety"]
```

**Step 2: Run to verify fail**

```
pytest tests/unit/test_config_intelligence.py -v
# expect: AttributeError on intelligence_enabled
```

**Step 3: Add fields to `Settings` in `src/gateway/config.py`**

Append a new `# ── Phase 25: Intelligence / ONNX Self-Learning ──────────────────────────` block at the end of the class (chronological by phase — matches the existing organization of the file). Use pydantic `Field(...)` form with bounds where applicable:

```python
intelligence_enabled: bool = Field(default=True, description="Enable ONNX intelligence layer")
intelligence_db_path: str = Field(default="", description="SQLite path. Empty → {wal_path}/intelligence.db")
onnx_models_base_path: str = Field(default="", description="Base dir for ONNX artifacts. Empty → src/gateway/models/")
verdict_retention_days: int = Field(default=30, ge=1, description="Retention for captured verdicts (days)")
distillation_schedule_cron: str = Field(default="0 2 * * *", description="Cron for nightly distillation")
distillation_min_divergences: int = Field(default=500, ge=1, description="Min divergences to trigger distillation")
shadow_sample_target: int = Field(default=1000, ge=1, description="Target sample size for shadow eval")
shadow_min_accuracy_delta: float = Field(default=0.02, ge=0.0, le=1.0, description="Min accuracy improvement to promote")
shadow_max_disagreement: float = Field(default=0.40, ge=0.0, le=1.0, description="Max candidate↔production disagreement")
shadow_max_error_rate: float = Field(default=0.05, ge=0.0, le=1.0, description="Max candidate inference error rate")
auto_promote_models: str = Field(default="", description="Comma-separated models eligible for auto-promotion")
teacher_llm_url: str = Field(default="", description="Teacher LLM URL (empty = disabled)")
teacher_llm_sample_rate: float = Field(default=0.01, ge=0.0, le=1.0, description="Teacher labeling sample rate")

@property
def auto_promote_models_list(self) -> list[str]:
    return [m.strip() for m in self.auto_promote_models.split(",") if m.strip()]
```

**Step 4: Document in `.env.example`**

Append a new `# ── Phase 25: Intelligence / ONNX Self-Learning ──────────` section (box-drawing header, matching the other section headers in the file) with each var and a one-line comment. The variable name for `onnx_models_base_path` is `WALACOR_ONNX_MODELS_BASE_PATH`.

**Step 5: Run to verify pass**

```
pytest tests/unit/test_config_intelligence.py -v
# expect: 2 passed
```

**Step 6: Commit**

```
git add src/gateway/config.py .env.example tests/unit/test_config_intelligence.py
git commit -m "feat(config): add intelligence layer settings for self-learning loop"
```

---

### Task 2: SQLite schema for `intelligence.db`

**Files:**
- Create: `src/gateway/intelligence/db.py`
- Test: `tests/unit/test_intelligence_db.py`

**Notes on conventions** (these apply to all later SQLite work in the intelligence package too):
- Connections use `isolation_level=None` (autocommit). The `with self._connect() as conn:` block does NOT rollback on exception — any caller that needs atomic batch writes must issue explicit `BEGIN IMMEDIATE` / `COMMIT`.
- Introspection queries (anything reading `sqlite_master`) must filter `name NOT LIKE 'sqlite_%'`. SQLite eagerly creates internal bookkeeping tables (e.g., `sqlite_sequence` for AUTOINCREMENT) at DDL time, and they'll leak into consumer code that does equality checks or DDL iteration.

**Step 1: Write the failing test**

```python
# tests/unit/test_intelligence_db.py
from __future__ import annotations

import sqlite3

import pytest

from gateway.intelligence.db import IntelligenceDB


def test_db_creates_tables(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    tables = db.list_tables()
    assert "onnx_verdicts" in tables
    assert "shadow_comparisons" in tables
    assert "training_snapshots" in tables


def test_db_is_idempotent(tmp_path):
    p = str(tmp_path / "int.db")
    IntelligenceDB(p).init_schema()
    IntelligenceDB(p).init_schema()  # second call must not raise


def test_list_tables_hides_sqlite_internals(tmp_path):
    # AUTOINCREMENT columns cause SQLite to eagerly create `sqlite_sequence` at
    # DDL time; callers doing equality comparisons shouldn't see it.
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    assert set(db.list_tables()) == {
        "onnx_verdicts",
        "shadow_comparisons",
        "training_snapshots",
    }


def test_unique_constraint_on_training_dataset_hash(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO training_snapshots (model_name, dataset_hash, row_ids_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("intent", "abc123", "[1,2,3]", "2026-04-16T00:00:00+00:00"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO training_snapshots (model_name, dataset_hash, row_ids_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("intent", "abc123", "[4,5]", "2026-04-16T00:00:01+00:00"),
            )
            conn.commit()
    finally:
        conn.close()
```

**Step 2: Run to verify fail**

```
pytest tests/unit/test_intelligence_db.py -v
# expect: ModuleNotFoundError
```

**Step 3: Implement `IntelligenceDB`**

```python
"""SQLite store for the Phase 25 self-learning intelligence layer.

Connections open in autocommit mode (`isolation_level=None`). The `with _connect()`
block does NOT rollback on exception; callers needing atomic batch writes must issue
explicit `BEGIN IMMEDIATE` / `COMMIT`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

SCHEMA = """
CREATE TABLE IF NOT EXISTS onnx_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_features_json TEXT NOT NULL,
    prediction TEXT NOT NULL,
    confidence REAL NOT NULL,
    request_id TEXT,
    timestamp TEXT NOT NULL,
    divergence_signal TEXT,
    divergence_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_model_time ON onnx_verdicts(model_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_verdicts_divergence ON onnx_verdicts(divergence_signal, model_name) WHERE divergence_signal IS NOT NULL;

CREATE TABLE IF NOT EXISTS shadow_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    production_prediction TEXT NOT NULL,
    production_confidence REAL NOT NULL,
    candidate_prediction TEXT,
    candidate_confidence REAL,
    candidate_error TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_model_version ON shadow_comparisons(model_name, candidate_version);

CREATE TABLE IF NOT EXISTS training_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    dataset_hash TEXT NOT NULL UNIQUE,
    row_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class IntelligenceDB:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def list_tables(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
```

**Step 4: Run tests**

```
pytest tests/unit/test_intelligence_db.py -v
# expect: 2 passed
```

**Step 5: Commit**

```
git add src/gateway/intelligence/db.py tests/unit/test_intelligence_db.py
git commit -m "feat(intelligence): SQLite schema for verdict log + shadow + training snapshots"
```

---

### Task 3: Walacor ETId + lifecycle event data types

**Files:**
- Create: `src/gateway/intelligence/events.py`
- Modify: `src/gateway/config.py` (add `walacor_lifecycle_events_etid` field)
- Modify: `src/gateway/walacor/client.py` (add `lifecycle_events_etid` constructor param)
- Modify: `.env.example` (add `WALACOR_LIFECYCLE_EVENTS_ETID=9000024` line)
- Test: `tests/unit/test_lifecycle_events.py`

**Background** (read before coding):
- Walacor ETIds are **numeric ints**, not strings. Existing config fields: `walacor_executions_etid=9000021`, `walacor_attempts_etid=9000022`, `walacor_tool_events_etid=9000023`. Pick `9000024` for lifecycle events.
- The ETId is passed in the HTTP header (`"ETId": str(etid)` in `WalacorClient._headers`), **not** in the record payload. `LifecycleEvent.to_record()` must return the payload only — no `etid` key.
- Task 3 ONLY scaffolds: config field, constructor param, the event dataclass/enum/factories, tests. The full retry-with-backoff `write_lifecycle_event(...)` method on `WalacorClient` is **Task 21**, not here.

**Step 1: Write the failing test**

```python
# tests/unit/test_lifecycle_events.py
from __future__ import annotations

from gateway.intelligence.events import (
    LifecycleEvent,
    EventType,
    build_training_fingerprint,
    build_promotion_event,
    build_candidate_created,
    build_shadow_validation_complete,
    build_model_rejected,
)


def test_event_type_enum():
    assert EventType.TRAINING_DATASET_FINGERPRINT.value == "training_dataset_fingerprint"
    assert EventType.CANDIDATE_CREATED.value == "candidate_created"
    assert EventType.SHADOW_VALIDATION_COMPLETE.value == "shadow_validation_complete"
    assert EventType.MODEL_PROMOTED.value == "model_promoted"
    assert EventType.MODEL_REJECTED.value == "model_rejected"


def test_to_record_does_not_include_etid():
    # ETId lives in the HTTP header, not the payload — to_record must NOT include it.
    ev = build_promotion_event(
        model_name="intent", candidate_version="v3", dataset_hash="deadbeef",
        shadow_metrics={"accuracy": 0.94}, approver="alice@example.com",
    )
    rec = ev.to_record()
    assert "etid" not in rec
    assert rec["event_type"] == "model_promoted"
    assert "timestamp" in rec


def test_build_training_fingerprint_deterministic():
    row_ids = [3, 1, 2]
    ev = build_training_fingerprint(model_name="intent", row_ids=row_ids, content_hash="abc")
    assert ev.event_type == EventType.TRAINING_DATASET_FINGERPRINT
    assert ev.payload["row_ids"] == [1, 2, 3]  # sorted
    assert ev.payload["content_hash"] == "abc"
    assert ev.payload["model_name"] == "intent"
    assert len(ev.payload["dataset_hash"]) == 64  # sha256 hex


def test_training_fingerprint_is_order_independent():
    a = build_training_fingerprint(model_name="intent", row_ids=[3, 1, 2], content_hash="x")
    b = build_training_fingerprint(model_name="intent", row_ids=[2, 3, 1], content_hash="x")
    assert a.payload["dataset_hash"] == b.payload["dataset_hash"]


def test_build_promotion_event():
    ev = build_promotion_event(
        model_name="intent", candidate_version="v3", dataset_hash="deadbeef",
        shadow_metrics={"accuracy": 0.94}, approver="alice@example.com",
    )
    assert ev.event_type == EventType.MODEL_PROMOTED
    assert ev.payload["approver"] == "alice@example.com"
    assert ev.payload["shadow_metrics"]["accuracy"] == 0.94


def test_build_candidate_created():
    ev = build_candidate_created(
        model_name="safety", candidate_version="v7", dataset_hash="abc",
        training_sample_count=842,
    )
    assert ev.event_type == EventType.CANDIDATE_CREATED
    assert ev.payload["training_sample_count"] == 842


def test_build_shadow_validation_complete():
    ev = build_shadow_validation_complete(
        model_name="schema_mapper", candidate_version="v4",
        metrics={"accuracy_delta": 0.03, "disagreement": 0.12, "samples": 1000},
        passed=True,
    )
    assert ev.event_type == EventType.SHADOW_VALIDATION_COMPLETE
    assert ev.payload["passed"] is True
    assert ev.payload["metrics"]["samples"] == 1000


def test_build_model_rejected():
    ev = build_model_rejected(
        model_name="intent", candidate_version="v5",
        reason="accuracy delta below threshold", stage="shadow",
    )
    assert ev.event_type == EventType.MODEL_REJECTED
    assert ev.payload["reason"] == "accuracy delta below threshold"
    assert ev.payload["stage"] == "shadow"


def test_to_record_timestamp_is_iso8601():
    # All other Walacor/WAL records in this codebase use ISO-8601 UTC strings.
    # Lifecycle events must match so dashboard range-queries and cross-ETId
    # joins work consistently.
    ev = build_candidate_created(
        model_name="intent", candidate_version="v1", dataset_hash="h",
        training_sample_count=1,
    )
    assert isinstance(ev.timestamp, str)
    from datetime import datetime
    parsed = datetime.fromisoformat(ev.timestamp)
    assert parsed.tzinfo is not None  # must carry explicit UTC offset


def test_to_record_top_level_fields_override_payload():
    # If a caller accidentally includes "event_type" or "timestamp" keys in
    # payload, `to_record()` must emit the canonical top-level values — the
    # audit stream cannot be corrupted by payload collisions.
    ev = LifecycleEvent(
        event_type=EventType.MODEL_PROMOTED,
        payload={
            "event_type": "ATTACKER_FORGED",
            "timestamp": "1970-01-01T00:00:00+00:00",
            "real_data": "kept",
        },
        timestamp="2026-04-16T12:34:56+00:00",
    )
    rec = ev.to_record()
    assert rec["event_type"] == "model_promoted"  # canonical value wins
    assert rec["timestamp"] == "2026-04-16T12:34:56+00:00"  # canonical timestamp wins
    assert rec["real_data"] == "kept"  # unrelated payload fields pass through
```

**Step 2: Run to verify fail**

```
.venv/bin/python -m pytest tests/unit/test_lifecycle_events.py -v
# expect: ModuleNotFoundError for gateway.intelligence.events
```

**Step 3: Implement `src/gateway/intelligence/events.py`**

Key design choices locked in during Task 3 review:
- **`timestamp` is an ISO-8601 UTC string**, not unix-float seconds. Every other Walacor/WAL record in this codebase uses ISO-8601 (see `core/models/execution.py:17`). Staying consistent now avoids a schema migration in Task 21.
- **`to_record()` spreads `payload` FIRST, then top-level `event_type` / `timestamp`**. Canonical top-level values always win, so a caller that accidentally includes those keys in `payload` cannot corrupt the audit stream.
- **`stage` on `build_model_rejected` is `Literal["load", "sanity", "shadow", "manual"]`** (aliased as `RejectionStage`). Catches typos at type-check time.
- **`_dataset_hash` preserves duplicate `row_ids`** — a multiset, not a set. Two copies of row 17 is a different dataset than one copy; the hash reflects that.

```python
"""Lifecycle events for the Phase 25 ONNX self-learning loop.

Records model-registry actions to the Walacor audit chain under a dedicated ETId
(configurable via `walacor_lifecycle_events_etid`, default 9000024). `LifecycleEvent`
is the in-memory representation; `to_record()` produces the payload the Walacor
client submits — the ETId itself travels in the HTTP header, not the payload.

Full write-with-retry plumbing lives in Task 21's walacor_writer.py. This module
only defines types + factory builders.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

# Finite set of rejection stages. Typed so callers catch typos at type-check time
# rather than corrupting the audit stream with misspelled stage labels.
RejectionStage = Literal["load", "sanity", "shadow", "manual"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    TRAINING_DATASET_FINGERPRINT = "training_dataset_fingerprint"
    CANDIDATE_CREATED = "candidate_created"
    SHADOW_VALIDATION_COMPLETE = "shadow_validation_complete"
    MODEL_PROMOTED = "model_promoted"
    MODEL_REJECTED = "model_rejected"


@dataclass
class LifecycleEvent:
    event_type: EventType
    payload: dict[str, Any]
    timestamp: str = field(default_factory=_utcnow_iso)

    def to_record(self) -> dict[str, Any]:
        # Payload is spread FIRST so top-level `event_type` and `timestamp`
        # always win — if a caller accidentally includes those keys in payload,
        # the canonical values are preserved rather than silently overridden.
        # ETId travels in the HTTP header, NOT in this payload.
        return {
            **self.payload,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
        }


def _dataset_hash(row_ids: list[int], content_hash: str) -> str:
    # Duplicates in `row_ids` are preserved (via `sorted`, not `set()`).
    # Fingerprints reflect the exact training multiset — two identical rows
    # produce a different hash than one row, because they're different datasets.
    canonical = json.dumps(
        {"row_ids": sorted(row_ids), "content_hash": content_hash}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_training_fingerprint(
    *, model_name: str, row_ids: list[int], content_hash: str
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.TRAINING_DATASET_FINGERPRINT,
        payload={
            "model_name": model_name,
            "row_ids": sorted(row_ids),
            "content_hash": content_hash,
            "dataset_hash": _dataset_hash(row_ids, content_hash),
        },
    )


def build_candidate_created(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    training_sample_count: int,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.CANDIDATE_CREATED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "dataset_hash": dataset_hash,
            "training_sample_count": training_sample_count,
        },
    )


def build_shadow_validation_complete(
    *, model_name: str, candidate_version: str,
    metrics: dict[str, Any], passed: bool,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.SHADOW_VALIDATION_COMPLETE,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "metrics": metrics,
            "passed": passed,
        },
    )


def build_promotion_event(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    shadow_metrics: dict[str, Any], approver: str,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.MODEL_PROMOTED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "dataset_hash": dataset_hash,
            "shadow_metrics": shadow_metrics,
            "approver": approver,
        },
    )


def build_model_rejected(
    *, model_name: str, candidate_version: str, reason: str, stage: RejectionStage,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.MODEL_REJECTED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "reason": reason,
            "stage": stage,
        },
    )
```

**Step 4: Add config field for lifecycle events ETId**

In `src/gateway/config.py`, add a new field alongside the existing `walacor_*_etid` block (the block currently contains `walacor_executions_etid`, `walacor_attempts_etid`, `walacor_tool_events_etid`). Follow the exact same `Field(default=..., description=..., validation_alias=AliasChoices(...))` pattern:

```python
walacor_lifecycle_events_etid: int = Field(
    default=9000024,
    description="Walacor ETId for ONNX lifecycle event records (training fingerprint, candidate, shadow, promote, reject)",
    validation_alias=AliasChoices("WALACOR_LIFECYCLE_EVENTS_ETID", "walacor_lifecycle_events_etid"),
)
```

**Step 5: Extend `WalacorClient` constructor**

In `src/gateway/walacor/client.py`:
- Add `lifecycle_events_etid: int = 9000024` to `__init__` signature (right after `tool_events_etid`).
- Store as `self._lifecycle_events_etid = lifecycle_events_etid` alongside the other `self._*_etid` fields.
- Include in the `logger.info("WalacorClient ready ...")` line.
- Do NOT add a `write_lifecycle_event(...)` method here — that's Task 21.

Also update the call sites that instantiate `WalacorClient` (check `main.py`) to pass `lifecycle_events_etid=settings.walacor_lifecycle_events_etid`.

**Step 6: Document in `.env.example`**

Add a line in the existing Walacor ETId block (where `WALACOR_TOOL_EVENTS_ETID` lives):

```
WALACOR_LIFECYCLE_EVENTS_ETID=9000024              # Phase 25 ONNX lifecycle events (training, candidate, shadow, promotion, rejection)
```

**Step 7: Run tests**

```
.venv/bin/python -m pytest tests/unit/test_lifecycle_events.py -v
# expect: 10 passed
```

Also smoke-check that you didn't break existing Walacor tests:

```
.venv/bin/python -m pytest tests/unit/ -k "walacor or config" -v
```

**Step 8: Commit**

```
git add src/gateway/intelligence/events.py src/gateway/config.py src/gateway/walacor/client.py .env.example tests/unit/test_lifecycle_events.py
# also stage main.py only if you had to update a WalacorClient instantiation
git commit -m "feat(intelligence): lifecycle event types + Walacor ETId 9000024"
```

---

### Task 4: `ModelVerdict` dataclass + `intelligence/__init__.py`

**Files:**
- Modify: `src/gateway/intelligence/__init__.py` (file already exists — keep/update the module docstring, add `ModelVerdict` export)
- Create: `src/gateway/intelligence/types.py`
- Test: `tests/unit/test_intelligence_types.py`

**Note on `__init__.py`:** The file was added in commit `eb188d9` (Apr 6, Schema Intelligence v2) and currently contains only a one-line docstring. Modify it to reflect the broader Phase 25 scope and re-export `ModelVerdict`. Do NOT replace the file — preserve the package structure and update the docstring.

**Design decisions locked in (reuse conventions from Tasks 2 + 3):**
- **`timestamp` is an ISO-8601 UTC string**, matching every other Walacor/WAL record in the codebase (`core/models/execution.py`, `wal/writer.py`, and Task 3's `LifecycleEvent`). Task 2's `onnx_verdicts.timestamp` column is `TEXT NOT NULL` — this type matches.
- **`from_inference` is a keyword-only classmethod** (`*,`) — prevents ambiguous positional arg ordering.
- **`input_hash` is SHA-256 of `input_text.encode()`** — hex-encoded, 64 chars. Two identical prompts hash the same; enables dedup downstream.
- **`input_features_json` defaults to `"{}"`** (empty dict JSON-serialized) when `features` is not supplied — never null, so the SQLite NOT NULL column is always satisfied.
- **`__init__.py` is MODIFIED, not created.** The package already exists (added Apr 6, Schema Intelligence v2). Preserve the existing docstring's intent but broaden it to cover Phase 25's verdict/distillation scope.

**Step 1: Test**

```python
# tests/unit/test_intelligence_types.py
from __future__ import annotations

from datetime import datetime

from gateway.intelligence.types import ModelVerdict


def test_verdict_from_inference():
    v = ModelVerdict.from_inference(
        model_name="intent", input_text="search for python",
        prediction="web_search", confidence=0.87, request_id="req-1",
    )
    assert v.model_name == "intent"
    assert v.prediction == "web_search"
    assert v.confidence == 0.87
    assert v.request_id == "req-1"
    assert len(v.input_hash) == 64  # sha256
    assert v.input_features_json == "{}"  # default when no features supplied
    assert v.divergence_signal is None
    assert v.divergence_source is None


def test_verdict_timestamp_is_iso8601_utc():
    v = ModelVerdict.from_inference(
        model_name="intent", input_text="x", prediction="normal", confidence=0.5,
    )
    assert isinstance(v.timestamp, str)
    parsed = datetime.fromisoformat(v.timestamp)
    assert parsed.tzinfo is not None


def test_input_hash_deterministic():
    v1 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    v2 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    assert v1.input_hash == v2.input_hash


def test_input_hash_differs_on_different_text():
    v1 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    v2 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello worlds",  # one char different
        prediction="safe", confidence=0.99,
    )
    assert v1.input_hash != v2.input_hash


def test_features_json_is_sorted():
    # Sort for determinism — two logically-equal feature dicts should serialize
    # to the same JSON string regardless of insertion order.
    v1 = ModelVerdict.from_inference(
        model_name="schema", input_text="x", prediction="content",
        confidence=0.8, features={"b": 2, "a": 1},
    )
    v2 = ModelVerdict.from_inference(
        model_name="schema", input_text="x", prediction="content",
        confidence=0.8, features={"a": 1, "b": 2},
    )
    assert v1.input_features_json == v2.input_features_json
```

**Step 2: Implement**

```python
# src/gateway/intelligence/types.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ModelVerdict:
    model_name: str
    input_hash: str
    input_features_json: str
    prediction: str
    confidence: float
    request_id: str | None
    timestamp: str = field(default_factory=_utcnow_iso)
    divergence_signal: str | None = None
    divergence_source: str | None = None

    @classmethod
    def from_inference(
        cls,
        *,
        model_name: str,
        input_text: str,
        prediction: str,
        confidence: float,
        request_id: str | None = None,
        features: dict[str, Any] | None = None,
    ) -> "ModelVerdict":
        input_hash = hashlib.sha256(input_text.encode()).hexdigest()
        # sort_keys so logically-equal feature dicts produce identical JSON —
        # lets downstream dedup/fingerprinting treat key order as insignificant.
        features_json = json.dumps(features or {}, sort_keys=True)
        return cls(
            model_name=model_name,
            input_hash=input_hash,
            input_features_json=features_json,
            prediction=prediction,
            confidence=confidence,
            request_id=request_id,
        )
```

**Step 3: Modify `src/gateway/intelligence/__init__.py`**

The current file contains:
```python
"""Background LLM intelligence — async enrichment via local Ollama models."""
```

Update it to reflect Phase 25's broader scope AND re-export `ModelVerdict`, **without discarding the existing intent**:

```python
"""Intelligence layer for the gateway.

Original scope (Apr 2026): background LLM enrichment via local Ollama models.
Phase 25 adds a self-learning feedback loop on top: ONNX verdict capture,
shadow-mode candidate validation, and audit-chain anchored model promotion.
"""
from __future__ import annotations

from gateway.intelligence.types import ModelVerdict

__all__ = ["ModelVerdict"]
```

**Step 4: Test + commit**

```
.venv/bin/python -m pytest tests/unit/test_intelligence_types.py -v
# expect: 5 passed
git add src/gateway/intelligence/__init__.py src/gateway/intelligence/types.py tests/unit/test_intelligence_types.py
git commit -m "feat(intelligence): ModelVerdict dataclass with deterministic input hashing"
```

---

## Phase B — Verdict Log

### Task 5: `VerdictBuffer` (in-memory bounded deque)

**Files:**
- Create: `src/gateway/intelligence/verdict_buffer.py`
- Test: `tests/unit/test_verdict_buffer.py`

**Step 1: Test the critical behaviors**

```python
# tests/unit/test_verdict_buffer.py
from __future__ import annotations
import pytest
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.intelligence.types import ModelVerdict

def _mk(i: int) -> ModelVerdict:
    return ModelVerdict.from_inference(
        model_name="intent", input_text=f"t{i}",
        prediction="normal", confidence=0.9,
    )

def test_buffer_enqueue_and_drain():
    b = VerdictBuffer(max_size=10)
    b.record(_mk(1))
    b.record(_mk(2))
    drained = b.drain()
    assert len(drained) == 2

def test_buffer_overflow_drops_oldest():
    b = VerdictBuffer(max_size=3)
    for i in range(5):
        b.record(_mk(i))
    drained = b.drain()
    # newest 3 survive
    assert [v.input_hash for v in drained] == [_mk(i).input_hash for i in [2, 3, 4]]
    assert b.dropped_total == 2

def test_drain_is_batched():
    b = VerdictBuffer(max_size=100)
    for i in range(50):
        b.record(_mk(i))
    batch1 = b.drain(max_batch=20)
    assert len(batch1) == 20
    batch2 = b.drain(max_batch=20)
    assert len(batch2) == 20
    batch3 = b.drain(max_batch=20)
    assert len(batch3) == 10
    assert b.drain(max_batch=20) == []
```

**Step 2: Run fail**

**Step 3: Implement**

```python
# src/gateway/intelligence/verdict_buffer.py
from __future__ import annotations
from collections import deque
from gateway.intelligence.types import ModelVerdict

class VerdictBuffer:
    def __init__(self, max_size: int = 10_000) -> None:
        self._buf: deque[ModelVerdict] = deque(maxlen=max_size)
        self._dropped = 0
        self._max = max_size

    def record(self, verdict: ModelVerdict) -> None:
        if len(self._buf) >= self._max:
            self._dropped += 1
        self._buf.append(verdict)

    def drain(self, max_batch: int = 500) -> list[ModelVerdict]:
        out: list[ModelVerdict] = []
        while self._buf and len(out) < max_batch:
            out.append(self._buf.popleft())
        return out

    @property
    def dropped_total(self) -> int:
        return self._dropped

    @property
    def size(self) -> int:
        return len(self._buf)
```

**Step 4: Test + commit**

```
pytest tests/unit/test_verdict_buffer.py -v
git commit -m "feat(intelligence): VerdictBuffer with bounded deque and drop-oldest overflow"
```

---

### Task 6: `VerdictFlushWorker` (batched async SQLite writer)

**Files:**
- Create: `src/gateway/intelligence/verdict_flush.py`
- Test: `tests/unit/test_verdict_flush.py`

**Step 1: Test**

```python
# tests/unit/test_verdict_flush.py
from __future__ import annotations
import pytest, asyncio, sqlite3
from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.intelligence.verdict_flush import VerdictFlushWorker
from gateway.intelligence.types import ModelVerdict

pytestmark = pytest.mark.anyio

@pytest.fixture
def anyio_backend():
    return "asyncio"

async def test_flush_writes_to_db(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    buf = VerdictBuffer(max_size=100)
    for i in range(5):
        buf.record(ModelVerdict.from_inference(
            model_name="intent", input_text=f"t{i}",
            prediction="normal", confidence=0.9,
        ))
    worker = VerdictFlushWorker(buf, db, flush_interval_s=0.01, batch_size=10)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    worker.stop()
    await task
    with sqlite3.connect(db.path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM onnx_verdicts").fetchone()[0]
    assert count == 5
```

**Step 2–4: Implement, test, commit**

```python
# src/gateway/intelligence/verdict_flush.py
from __future__ import annotations
import asyncio, logging
from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)

class VerdictFlushWorker:
    def __init__(
        self, buffer: VerdictBuffer, db: IntelligenceDB,
        flush_interval_s: float = 1.0, batch_size: int = 500,
    ) -> None:
        self._buf = buffer
        self._db = db
        self._interval = flush_interval_s
        self._batch = batch_size
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                batch = self._buf.drain(max_batch=self._batch)
                if batch:
                    await asyncio.to_thread(self._write_batch, batch)
            except Exception:
                logger.exception("verdict flush iteration failed")

    def stop(self) -> None:
        self._running = False

    def _write_batch(self, verdicts) -> None:
        import sqlite3
        conn = sqlite3.connect(self._db.path)
        try:
            conn.executemany(
                "INSERT INTO onnx_verdicts "
                "(model_name, input_hash, input_features_json, prediction, confidence, "
                "request_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(v.model_name, v.input_hash, v.input_features_json, v.prediction,
                  v.confidence, v.request_id, v.timestamp) for v in verdicts],
            )
            conn.commit()
        finally:
            conn.close()
```

Commit: `feat(intelligence): batched async flush worker for verdict log`

---

### Task 7: Hot-path integration — record verdicts from each ONNX inference

**Files:**
- Modify: `src/gateway/classifier/intent.py` (add `verdict_buffer.record(...)` after inference)
- Modify: `src/gateway/schema/mapper.py` (same)
- Modify: `src/gateway/content/safety_classifier.py` (same)
- Modify: `src/gateway/pipeline/context.py` (add `verdict_buffer` field)
- Modify: `src/gateway/main.py` (init VerdictBuffer + VerdictFlushWorker in lifespan)
- Test: `tests/unit/test_verdict_integration.py`

**Step 1: Add to `PipelineContext`**

```python
# src/gateway/pipeline/context.py — add field
verdict_buffer: "VerdictBuffer | None" = None
```

**Step 2: Init in `main.py` lifespan**

In `_init_intelligence(...)` (new helper in `main.py`):
1. If `settings.intelligence_enabled`: resolve `intelligence_db_path` (default `{wal_path}/intelligence.db`)
2. Init `IntelligenceDB(path)` + `init_schema()`
3. Init `VerdictBuffer(max_size=10_000)`
4. Init `VerdictFlushWorker(buffer, db)`
5. `ctx.verdict_buffer = buffer`; `ctx.intelligence_db = db`
6. `ctx.intelligence_flush_task = asyncio.create_task(worker.run())`

In shutdown: `worker.stop()` + `await ctx.intelligence_flush_task`.

**Step 3: Record in each ONNX client's `predict()` / `classify()` method**

Grep for the three inference sites and add a non-blocking record call. Example for `classifier/intent.py`:

```python
# after the existing inference + return value is computed
if self._verdict_buffer is not None:
    self._verdict_buffer.record(ModelVerdict.from_inference(
        model_name="intent", input_text=prompt,
        prediction=label, confidence=float(confidence),
        request_id=request_id,
    ))
```

The `_verdict_buffer` is passed at construction time via the existing factory/init path in `main.py` / `orchestrator.py`. Keep it an Optional — inference still works without it (fail-open).

**Step 4: Integration test**

```python
async def test_hot_path_records_verdict(tmp_path, monkeypatch):
    # spin up gateway with test settings, send a request, assert verdict appears in DB
    ...
```

**Step 5: Test + commit**

```
pytest tests/unit/test_verdict_integration.py -v
git commit -m "feat(intelligence): wire verdict recording into Intent/Schema/Safety hot paths"
```

---

### Task 8: TTL sweeper for verdict log

**Files:**
- Create: `src/gateway/intelligence/retention.py`
- Test: `tests/unit/test_verdict_retention.py`

**Step 1: Test**

```python
async def test_sweeper_deletes_old_verdicts(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    # insert rows with timestamps 0 (old) and now (new)
    # run sweeper with retention_days=30
    # assert only new remains
    ...
```

**Step 2–4: Implement**

`RetentionSweeper` class with `async def run()` that wakes every hour and runs `DELETE FROM onnx_verdicts WHERE timestamp < ?` with cutoff = `time.time() - retention_days * 86400`. Also prunes `shadow_comparisons` for rejected/promoted candidates.

Register as another background task in `main.py` lifespan.

**Step 5: Commit**

```
git commit -m "feat(intelligence): hourly TTL sweeper for verdict log and shadow comparisons"
```

---

## Phase C — Model Registry

### Task 9: Directory layout + `ModelRegistry` skeleton

**Files:**
- Create: `src/gateway/intelligence/registry.py`
- Test: `tests/unit/test_model_registry.py`

**Step 1: Test**

```python
def test_registry_ensures_directories(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    assert (tmp_path / "production").is_dir()
    assert (tmp_path / "candidates").is_dir()
    assert (tmp_path / "archive").is_dir()
    assert (tmp_path / "archive" / "failed").is_dir()

def test_registry_lists_production_models(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"fake")
    (tmp_path / "production" / "schema_mapper.onnx").write_bytes(b"fake")
    assert set(r.list_production_models()) == {"intent", "schema_mapper"}

def test_registry_lists_candidates(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "safety-v5.onnx").write_bytes(b"fake")
    cands = r.list_candidates()
    assert any(c.model == "intent" and c.version == "v2" for c in cands)
```

**Step 2–4: Implement**

```python
# src/gateway/intelligence/registry.py
from __future__ import annotations
import asyncio, os, re
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Candidate:
    model: str
    version: str
    path: Path

_CAND_RE = re.compile(r"^(?P<model>[a-z_]+)-(?P<version>[a-zA-Z0-9_.\-]+)\.onnx$")

class ModelRegistry:
    def __init__(self, base_path: str) -> None:
        self.base = Path(base_path)
        self._locks: dict[str, asyncio.Lock] = {}

    def ensure_structure(self) -> None:
        for sub in ("production", "candidates", "archive", "archive/failed"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    def production_path(self, model: str) -> Path:
        return self.base / "production" / f"{model}.onnx"

    def list_production_models(self) -> list[str]:
        prod = self.base / "production"
        return [p.stem for p in prod.glob("*.onnx")]

    def list_candidates(self) -> list[Candidate]:
        cands = []
        for p in (self.base / "candidates").glob("*.onnx"):
            m = _CAND_RE.match(p.name)
            if m:
                cands.append(Candidate(model=m["model"], version=m["version"], path=p))
        return cands

    def lock_for(self, model: str) -> asyncio.Lock:
        if model not in self._locks:
            self._locks[model] = asyncio.Lock()
        return self._locks[model]
```

**Step 5: Commit**

```
git commit -m "feat(intelligence): ModelRegistry with directory layout and per-model locks"
```

---

### Task 10: Atomic swap (promote) + archive

**Files:**
- Modify: `src/gateway/intelligence/registry.py`
- Test: add to `tests/unit/test_model_registry.py`

**Step 1: Test**

```python
async def test_promote_swaps_atomically(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"v1")
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    await r.promote("intent", "v2")
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"v2"
    archived = list((tmp_path / "archive").glob("intent-*.onnx"))
    assert len(archived) == 1

async def test_promote_missing_candidate_raises(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    with pytest.raises(FileNotFoundError):
        await r.promote("intent", "v99")
```

**Step 2–4: Implement `async def promote(...)` and `async def rollback(...)` on ModelRegistry**

```python
async def promote(self, model: str, version: str) -> None:
    async with self.lock_for(model):
        cand_path = self.base / "candidates" / f"{model}-{version}.onnx"
        if not cand_path.exists():
            raise FileNotFoundError(cand_path)
        prod_path = self.production_path(model)
        if prod_path.exists():
            archive_path = self.base / "archive" / f"{model}-{int(prod_path.stat().st_mtime)}.onnx"
            os.rename(prod_path, archive_path)
        os.rename(cand_path, prod_path)

async def rollback(self, model: str, archived_filename: str) -> None:
    async with self.lock_for(model):
        archive_path = self.base / "archive" / archived_filename
        if not archive_path.exists():
            raise FileNotFoundError(archive_path)
        prod_path = self.production_path(model)
        if prod_path.exists():
            new_archive = self.base / "archive" / f"{model}-{int(prod_path.stat().st_mtime)}-preswap.onnx"
            os.rename(prod_path, new_archive)
        os.rename(archive_path, prod_path)
```

**Step 5: Commit**

---

### Task 11: `InferenceSession` reload signaling

**Files:**
- Modify: `src/gateway/classifier/intent.py`
- Modify: `src/gateway/schema/mapper.py`
- Modify: `src/gateway/content/safety_classifier.py`
- Modify: `src/gateway/intelligence/registry.py`

Add a `reload()` method on each ONNX client that rebuilds the `InferenceSession` from the current production path. The registry broadcasts a reload signal (simple: version counter) and clients check it on each inference call. If the counter moved since last inference, rebuild session before inferring.

Test: `test_session_reloads_after_promote` — promote, next inference uses new `.onnx` content.

**Commit:** `feat(intelligence): model swap triggers InferenceSession reload`

---

### Task 12: Existing ONNX clients use ModelRegistry paths

**Files:** all three ONNX client files

Replace hardcoded relative paths (e.g., `Path(__file__).parent / "model.onnx"`) with a lookup via `ctx.model_registry.production_path("intent")`. Ship a migration step in `main.py` startup that copies existing in-repo `.onnx` files into `{models_base}/production/` on first run (idempotent).

**Commit:** `refactor(intelligence): ONNX clients resolve model paths via ModelRegistry`

---

## Phase D — Feedback Harvesters

### Task 13: Harvester base + async queue infra

**Files:**
- Create: `src/gateway/intelligence/harvesters/__init__.py`
- Create: `src/gateway/intelligence/harvesters/base.py`
- Test: `tests/unit/test_harvester_base.py`

Define `Harvester` ABC with `async def process(signal: HarvesterSignal) -> None`. Define `HarvesterSignal` dataclass (request_id, model_name, prediction, response_payload, context). Build `HarvesterRunner` that consumes from an `asyncio.Queue` and dispatches to registered harvesters concurrently.

Hook point: a new `finally` block in `orchestrator._build_and_write_record` (or wherever the response record is finalized) enqueues the signal. Non-blocking — user response doesn't wait.

**Commit:** `feat(intelligence): harvester framework with async queue + ABC`

---

### Task 14: SchemaMapper harvester (simplest — overflow keys)

**Files:**
- Create: `src/gateway/intelligence/harvesters/schema_mapper.py`
- Test: `tests/unit/test_schema_mapper_harvester.py`

Reads `schema_mapper_overflow_keys` from the response metadata. For each overflow key where the canonical schema's fallback rule (in `mapper.py` lines 143–160) produced a label, write back a divergence signal to the original verdict row:

```
UPDATE onnx_verdicts SET divergence_signal=?, divergence_source='schema_overflow_fallback'
WHERE id=(SELECT id FROM onnx_verdicts WHERE request_id=? AND model_name='schema_mapper' ORDER BY timestamp DESC LIMIT 1)
```

**Commit:** `feat(intelligence): SchemaMapper harvester captures overflow keys as training signal`

---

### Task 15: Safety harvester (SafetyClassifier ↔ LlamaGuard disagreement)

**Files:**
- Create: `src/gateway/intelligence/harvesters/safety.py`
- Test: `tests/unit/test_safety_harvester.py`

When both SafetyClassifier and LlamaGuard ran on the same input (check `analyzer_decisions` metadata), compare their verdict labels. If they disagree, write Llama Guard's label as the teacher signal:

```
UPDATE onnx_verdicts SET divergence_signal=<llama_guard_label>, divergence_source='llama_guard_disagreement'
WHERE model_name='safety' AND request_id=?
```

Only record disagreements; agreement cases are already correct and add no training signal.

**Commit:** `feat(intelligence): Safety harvester captures SafetyClassifier↔LlamaGuard disagreements`

---

### Task 16: Intent harvester (next-turn contradiction + sampled teacher LLM)

**Files:**
- Create: `src/gateway/intelligence/harvesters/intent.py`
- Test: `tests/unit/test_intent_harvester.py`

Two signals:
1. **Immediate:** did the classified intent actually act? (`web_search` classification + tool_events_detail shows no tool called → false positive `web_search`)
2. **Deferred (next-turn):** store pending intent verdicts by session_id; when next turn in same session arrives, check if the user's follow-up contradicts the prior classification (e.g., prior was `normal`, follow-up is `"search for..."` → prior was a missed `web_search`)
3. **Sampled teacher (1%):** if `teacher_llm_url` is configured, call the teacher with a relabel prompt; use its verdict as the signal. Log cost via a metric.

**Commit:** `feat(intelligence): Intent harvester with immediate + next-turn + teacher LLM signals`

---

## Phase E — Distillation Worker

### Task 17: Training dataset builder

**Files:**
- Create: `src/gateway/intelligence/distillation/dataset.py`
- Test: `tests/unit/test_distillation_dataset.py`

Query verdicts with non-null `divergence_signal` since last successful training. Dedupe by `input_hash`. Class-balance (cap per-class to min_class_count × 2 if imbalanced). Cap per-session contribution to 10% (adversarial robustness). Return `(X, y, row_ids)`.

**Commit:** `feat(intelligence): training dataset builder with class balancing and anti-adversarial cap`

---

### Task 18: Intent model trainer (sklearn + skl2onnx)

**Files:**
- Create: `src/gateway/intelligence/distillation/trainers/intent_trainer.py`
- Test: `tests/unit/test_intent_trainer.py`

Input: `(X, y)` where X is list of prompt strings, y is list of labels. Output: path to new `.onnx` file.

Pipeline: TfidfVectorizer(analyzer='char_wb', ngram_range=(3,5)) → LogisticRegression. Fit, then convert via skl2onnx. Save per-class confidence calibration table alongside (platt scaling or isotonic — published as `{model}-{version}-calibration.json`).

**Commit:** `feat(intelligence): Intent trainer with TF-IDF + LR + per-class calibration export`

---

### Task 19: SchemaMapper + Safety trainers

**Files:**
- Create: `src/gateway/intelligence/distillation/trainers/schema_trainer.py`
- Create: `src/gateway/intelligence/distillation/trainers/safety_trainer.py`

SchemaMapper: GradientBoostingClassifier on value-aware features (see existing `mapper.py:174 extract_features`). Safety: same TF-IDF char_wb approach as current `safety_classifier.py` with same 8 labels.

**Commit:** `feat(intelligence): SchemaMapper and Safety trainers`

---

### Task 20: `DistillationWorker` — scheduler + orchestration

**Files:**
- Create: `src/gateway/intelligence/distillation/worker.py`
- Test: `tests/unit/test_distillation_worker.py`

```python
class DistillationWorker:
    async def run(self) -> None:
        while self._running:
            await asyncio.sleep(self._poll_interval)
            if self._should_trigger():
                await self._run_cycle()

    async def _run_cycle(self) -> None:
        for model in ("intent", "schema_mapper", "safety"):
            try:
                await self._train_one(model)
            except Exception:
                logger.exception("training failed for %s", model)

    async def _train_one(self, model: str) -> None:
        X, y, row_ids = await asyncio.to_thread(self._builder.build, model)
        if len(X) < self._min_samples:
            return
        candidate_path = await asyncio.to_thread(self._trainer_for(model).train, X, y)
        content_hash = hash_file(candidate_path)
        fp_event = build_training_fingerprint(model_name=model, row_ids=row_ids, content_hash=content_hash)
        await self._walacor.write_lifecycle_event(fp_event)
        create_event = build_candidate_created(model_name=model, version=..., dataset_hash=fp_event.payload["dataset_hash"])
        await self._walacor.write_lifecycle_event(create_event)
        self._registry.enable_shadow(model, version=...)
```

Trigger policy: nightly at `distillation_schedule_cron` OR divergence count threshold OR dashboard force.

**Commit:** `feat(intelligence): DistillationWorker orchestrating dataset→train→fingerprint→candidate`

---

### Task 21: Walacor lifecycle event writer

**Files:**
- Create: `src/gateway/intelligence/walacor_writer.py`
- Test: `tests/unit/test_intelligence_walacor_writer.py`

Thin wrapper around existing `walacor_client.write_record(...)` that handles retries with exponential backoff, writes to ETId `onnx_lifecycle_event`, and mirrors to SQLite table `lifecycle_events_mirror` for dashboard reads.

**Commit:** `feat(intelligence): Walacor writer for lifecycle events with retry + SQLite mirror`

---

## Phase F — Shadow Validator

### Task 22: Shadow inference wiring

**Files:**
- Create: `src/gateway/intelligence/shadow.py`
- Modify: `src/gateway/intelligence/registry.py` (add `active_candidate(model)` method)
- Modify: each ONNX client to check for active candidate and fire shadow inference

**Step 1: Test**

```python
async def test_shadow_inference_runs_in_parallel():
    # set up registry with a candidate
    # run an inference
    # assert production result returned to caller
    # assert shadow_comparisons row written
```

**Step 2–4: Implement**

In each ONNX client's inference method, after production inference completes:

```python
candidate = self._registry.active_candidate("intent")
if candidate is not None:
    asyncio.create_task(self._run_shadow(candidate, input_text, production_prediction, production_confidence))
```

`_run_shadow` loads the candidate's InferenceSession (cached per candidate version), runs inference in a thread pool, writes to `shadow_comparisons`.

**Commit:** `feat(intelligence): shadow inference runs in parallel, non-blocking`

---

### Task 23: Metrics computation + McNemar test

**Files:**
- Create: `src/gateway/intelligence/shadow_metrics.py`
- Test: `tests/unit/test_shadow_metrics.py`

Compute from `shadow_comparisons` table for a given (model, candidate_version):
- `candidate_accuracy`, `production_accuracy` (against divergence_signal as ground truth where available)
- `disagreement_rate`
- `candidate_error_rate` (candidate_error IS NOT NULL)
- McNemar test p-value for paired predictions (use `scipy.stats.mcnemar` or hand-roll with binom test)

Test with known distributions: statsistically significant difference → low p; no difference → high p.

**Commit:** `feat(intelligence): shadow metrics with McNemar paired test`

---

### Task 24: Gate evaluation + auto-promote branch

**Files:**
- Create: `src/gateway/intelligence/shadow_gate.py`
- Test: `tests/unit/test_shadow_gate.py`

```python
@dataclass
class GateResult:
    passed: bool
    reasons: list[str]  # human-readable reasons for pass/fail
    metrics: dict[str, float]

def evaluate_gate(metrics: ShadowMetrics, settings: Settings) -> GateResult:
    reasons = []
    if metrics.sample_count < settings.shadow_sample_target:
        reasons.append(f"insufficient samples: {metrics.sample_count} < {settings.shadow_sample_target}")
    if metrics.candidate_accuracy - metrics.production_accuracy < settings.shadow_min_accuracy_delta:
        reasons.append(f"accuracy delta {...} below threshold")
    if metrics.disagreement_rate > settings.shadow_max_disagreement:
        reasons.append(f"disagreement {...} above threshold")
    if metrics.candidate_error_rate > settings.shadow_max_error_rate:
        reasons.append(f"error rate {...} above threshold")
    if metrics.mcnemar_p_value >= 0.05:
        reasons.append("not statistically significant (McNemar p >= 0.05)")
    return GateResult(passed=not reasons, reasons=reasons or ["all gates passed"], metrics={...})
```

Auto-promote branch: when gate passes AND `model in settings.auto_promote_models_list` → call `registry.promote(...)` and write `model_promoted` Walacor event. Else write `shadow_validation_complete` + surface in dashboard.

**Commit:** `feat(intelligence): promotion gate evaluation + auto-promote branch`

---

### Task 25: Offline sanity test set runner

**Files:**
- Create: `src/gateway/intelligence/sanity_tests/`
  - `intent_sanity.json` (50 examples × 6 classes = 300 labeled rows)
  - `schema_sanity.json` (50 × 20 classes)
  - `safety_sanity.json` (50 × 8 classes)
- Create: `src/gateway/intelligence/sanity_runner.py`

Before a candidate enters shadow, run the offline sanity set. Reject if accuracy < 70% on any class.

**Commit:** `feat(intelligence): offline sanity test runner with per-class accuracy gate`

---

## Phase G — Promotion API

### Task 26: Intelligence endpoints — list production/candidates

**Files:**
- Create: `src/gateway/intelligence/api.py`
- Modify: `src/gateway/main.py` (register routes)
- Test: `tests/unit/test_intelligence_api.py`

Endpoints under `/v1/control/intelligence`:
- `GET /models` — list production models + version + loaded timestamp
- `GET /candidates` — list candidates + shadow progress + metrics
- `GET /history/{model}` — promotion history

All require API key (same middleware as `/v1/control`).

**Commit:** `feat(intelligence): control plane read APIs for models and candidates`

---

### Task 27: Promote / Reject / Rollback endpoints

**Files:**
- Modify: `src/gateway/intelligence/api.py`

- `POST /v1/control/intelligence/promote/{model}/{version}` — runs gate, if passes: promote + Walacor event with `CallerIdentity` as approver
- `POST /v1/control/intelligence/reject/{model}/{version}` — writes `model_rejected` event, moves candidate to `archive/failed/`
- `POST /v1/control/intelligence/rollback/{model}` — picks most recent archived version, swaps back

Include idempotency: second promote of same (model, version) returns 409.

**Commit:** `feat(intelligence): promote/reject/rollback endpoints with caller-identity approver`

---

### Task 28: Force retrain endpoint

**Files:**
- Modify: `src/gateway/intelligence/api.py`

`POST /v1/control/intelligence/retrain/{model}` — kicks `DistillationWorker._run_cycle_for(model)` immediately, returns 202 with job handle.

**Commit:** `feat(intelligence): force-retrain endpoint`

---

### Task 29: Verdict log inspector endpoint

**Files:**
- Modify: `src/gateway/intelligence/api.py`

`GET /v1/control/intelligence/verdicts?model=intent&divergence_only=true&limit=100`

Returns top divergence types with counts + sample rows.

**Commit:** `feat(intelligence): verdict log inspector endpoint`

---

## Phase H — Dashboard

### Task 30: Intelligence sub-tab wiring

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/main.jsx`
- Modify: `src/gateway/lineage/dashboard/src/views/Control.jsx` (or wherever tabs live)
- Create: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`

Add the sub-tab to the Control navigation. Auth: reuse existing API key gate.

**Commit:** `feat(dashboard): add Intelligence sub-tab shell`

---

### Task 31: Production models + candidates views

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`
- Modify: `src/gateway/lineage/dashboard/src/api.js` (add fetchers)

Production models table: name / version / loaded / prediction count / trailing accuracy. Candidates table: model / version / source summary / samples collected / target / accuracy delta / disagreement / Promote / Reject buttons.

**Commit:** `feat(dashboard): production models + candidates views`

---

### Task 32: Promote / Reject confirmation modals

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`

Modal shows: metrics table, gate evaluation result, approver identity (from JWT/header), "Confirm" button. On confirm → POST to endpoint; on success refresh candidates list.

**Commit:** `feat(dashboard): promote/reject confirmation modals with metrics preview`

---

### Task 33: Promotion history + rollback

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`

Timeline view of past promotions: approver, timestamp, candidate version, metrics snapshot, dataset hash (clickable to view fingerprint on chain), one-click rollback button.

**Commit:** `feat(dashboard): promotion history timeline + one-click rollback`

---

### Task 34: Verdict log inspector + Force Retrain button

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`

Inspector: select model → shows top divergence types as bar chart + sample rows with input_hash, prediction, confidence, divergence signal. Force Retrain: per-model button that POSTs to retrain endpoint.

**Commit:** `feat(dashboard): verdict inspector + force retrain controls`

---

## Phase I — Observability + Chaos

### Task 35: Prometheus metrics

**Files:**
- Modify: `src/gateway/metrics.py` (or wherever Prometheus metrics are defined)

Add:
- `verdict_buffer_dropped_total{model}`
- `verdict_buffer_size{model}`
- `intelligence_db_write_failures_total`
- `candidate_rejected_total{model,reason}`
- `model_promoted_total{model}`
- `shadow_inference_errors_total{model}`
- `distillation_run_duration_seconds{model}`

**Commit:** `feat(intelligence): Prometheus metrics for verdict buffer, shadow, promotion`

---

### Task 36: Health endpoint exposure

**Files:**
- Modify: `src/gateway/health.py`

Add `intelligence` section: db path, verdict log row count, active candidates per model, last training timestamp, last promotion timestamp.

**Commit:** `feat(health): expose intelligence layer status`

---

### Task 37: Chaos validation on EC2

**Files:**
- Create: `tests/production/tier6c_intelligence_chaos.py`

Chaos scenarios (to run against EC2 gateway, not CI):
1. Fill verdict buffer past max → assert drops, inference latency unchanged
2. Kill SQLite mid-flush → assert in-memory buffer retained, flush resumes
3. Corrupt a candidate `.onnx` → assert auto-rejected, production unaffected
4. Pull Walacor offline mid-promotion → assert promotion waits, retries, eventually succeeds or surfaces error in dashboard
5. Kill gateway mid-training → assert restart resumes, no partial candidate in `production/`
6. Flood one class of Intent divergences from one session → assert per-session cap limits influence on trained model
7. Race two concurrent promote clicks → assert one succeeds, one gets 409
8. Rollback with missing archive file → assert blocked with clear error

**Commit:** `test(chaos): intelligence layer robustness scenarios`

---

## Completion criteria

- All 37 tasks committed
- Unit suite green (target: ≥30 new tests)
- Gateway starts + runs with `WALACOR_INTELLIGENCE_ENABLED=true` without errors
- Force-retrain on a real model produces a candidate; shadow collects ≥100 samples in live traffic; promotion succeeds end-to-end with audit chain event on Walacor
- Chaos scenarios run on EC2; findings tracked in follow-up issues

---

## After Phase 25

Layer 2 (verdicts as policy conditions) gets its own design doc + plan. It will extend the existing `policy_engine` with new condition types reading from `onnx_verdicts` metadata on the request — enabling operator-authored rules like `if safety.category in ("violence","hate") and confidence > 0.9 then deny` without any change to how ONNX itself behaves.
