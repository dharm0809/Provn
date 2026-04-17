"""Phase 25 Task 22: ShadowRunner + fire_shadow_text tests.

The ORT `InferenceSession` is not a test-env dependency — the session
cache is exercised via a monkeypatched `onnxruntime.InferenceSession`
stub so we can assert caching and driver dispatch without real ONNX
bytes.
"""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.registry import Candidate, ModelRegistry
from gateway.intelligence.shadow import ShadowRunner, fire_shadow_text, hash_input


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_onnxruntime(monkeypatch):
    fake = types.ModuleType("onnxruntime")
    load_log: list[str] = []

    class _FakeSession:
        def __init__(self, path, providers=None):
            load_log.append(str(path))
            self.path = str(path)

    fake.InferenceSession = _FakeSession
    fake._load_log = load_log  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    return fake


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _make_candidate(tmp_path: Path, model: str, version: str) -> Candidate:
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    path = tmp_path / "candidates" / f"{model}-{version}.onnx"
    path.write_bytes(b"candidate")
    return Candidate(model=model, version=version, path=path)


def _read_rows(db: IntelligenceDB) -> list[dict]:
    conn = sqlite3.connect(db.path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM shadow_comparisons ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


# ── session caching ────────────────────────────────────────────────────────

def test_get_session_loads_once_and_caches(tmp_path, fake_onnxruntime):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    cand = _make_candidate(tmp_path, "intent", "v1")

    s1 = runner.get_session("intent", cand)
    s2 = runner.get_session("intent", cand)

    assert s1 is s2
    # Only one load call even on repeated get.
    assert len(fake_onnxruntime._load_log) == 1


def test_get_session_separate_per_version(tmp_path, fake_onnxruntime):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    c1 = _make_candidate(tmp_path, "intent", "v1")
    c2 = _make_candidate(tmp_path, "intent", "v2")

    s1 = runner.get_session("intent", c1)
    s2 = runner.get_session("intent", c2)

    assert s1 is not s2
    assert len(fake_onnxruntime._load_log) == 2


def test_get_session_rejects_unknown_model(tmp_path, fake_onnxruntime):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    cand = _make_candidate(tmp_path, "intent", "v1")
    with pytest.raises(ValueError, match="unknown model"):
        runner.get_session("not_real", cand)


def test_get_session_concurrent_load_constructs_once(tmp_path, monkeypatch):
    """Regression for C1: two threads missing the cache must not both
    construct an InferenceSession — the loser would leak in ORT's arena.

    The session constructor sleeps briefly to widen the race window; with
    the guarding lock in place only one thread ever reaches the sleep.
    Without the lock, N threads race past the first `get` check and all
    construct, bumping `load_count` past 1.
    """
    import sys
    import threading
    import time
    import types

    load_count = 0
    load_count_lock = threading.Lock()

    fake = types.ModuleType("onnxruntime")

    class _SlowSession:
        def __init__(self, path, providers=None):
            nonlocal load_count
            with load_count_lock:
                load_count += 1
            time.sleep(0.05)  # 50ms — plenty of time for concurrent misses
            self.path = str(path)

    fake.InferenceSession = _SlowSession
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)

    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    cand = _make_candidate(tmp_path, "intent", "v1")

    start = threading.Event()
    sessions: list = []

    def _load() -> None:
        start.wait(timeout=5.0)
        sessions.append(runner.get_session("intent", cand))

    threads = [threading.Thread(target=_load) for _ in range(8)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=10.0)

    assert load_count == 1, f"expected 1 construction, got {load_count}"
    assert len({id(s) for s in sessions}) == 1, "all threads must get the same session"


# ── record row ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_record_writes_shadow_comparisons_row(tmp_path):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    await runner.record(
        model="intent", candidate_version="v1",
        input_hash="h" * 64,
        production_prediction="web_search",
        production_confidence=0.92,
        candidate_prediction="normal",
        candidate_confidence=0.81,
    )

    rows = _read_rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["model_name"] == "intent"
    assert r["candidate_version"] == "v1"
    assert r["production_prediction"] == "web_search"
    assert r["candidate_prediction"] == "normal"
    assert r["candidate_error"] is None


@pytest.mark.anyio
async def test_record_error_row(tmp_path):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    await runner.record(
        model="intent", candidate_version="v1",
        input_hash="h" * 64,
        production_prediction="normal",
        production_confidence=0.55,
        candidate_prediction=None,
        candidate_confidence=None,
        candidate_error="boom",
    )

    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_error"] == "boom"
    assert rows[0]["candidate_prediction"] is None
    assert rows[0]["candidate_confidence"] is None


# ── fire_shadow_text ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_fire_shadow_text_happy_path(tmp_path, fake_onnxruntime):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    cand = _make_candidate(tmp_path, "intent", "v1")

    seen_inputs: list[str] = []

    def _infer(session, text):
        seen_inputs.append(text)
        return "web_search", 0.88

    await fire_shadow_text(
        runner,
        model="intent",
        candidate=cand,
        input_text="search for cats",
        production_prediction="normal",
        production_confidence=0.6,
        infer_on_session=_infer,
    )

    assert seen_inputs == ["search for cats"]
    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_prediction"] == "web_search"
    assert abs(rows[0]["candidate_confidence"] - 0.88) < 1e-6
    assert rows[0]["input_hash"] == hash_input("search for cats")


@pytest.mark.anyio
async def test_fire_shadow_text_records_candidate_error_on_raise(tmp_path, fake_onnxruntime):
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    cand = _make_candidate(tmp_path, "intent", "v1")

    def _infer(session, text):
        raise RuntimeError("broken candidate")

    # Must not raise — the error must be recorded instead.
    await fire_shadow_text(
        runner,
        model="intent",
        candidate=cand,
        input_text="anything",
        production_prediction="normal",
        production_confidence=0.6,
        infer_on_session=_infer,
    )

    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_prediction"] is None
    assert "broken candidate" in (rows[0]["candidate_error"] or "")


# ── maybe_fire_shadow orchestration ─────────────────────────────────────────

@pytest.mark.anyio
async def test_maybe_fire_shadow_noop_when_no_candidate(tmp_path, fake_onnxruntime):
    from gateway.intelligence.shadow import maybe_fire_shadow

    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    registry = ModelRegistry(base_path=str(tmp_path))
    registry.ensure_structure()
    # No active candidate registered.

    maybe_fire_shadow(
        runner, registry,
        model_name="intent",
        input_text="hi",
        production_prediction="normal",
        production_confidence=0.9,
        infer_on_session=lambda s, t: ("x", 0.0),
    )
    # Give the loop a tick — still no rows written.
    import asyncio
    await asyncio.sleep(0)
    assert _read_rows(db) == []


@pytest.mark.anyio
async def test_maybe_fire_shadow_runs_candidate_when_active(tmp_path, fake_onnxruntime):
    from gateway.intelligence.shadow import maybe_fire_shadow

    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    registry = ModelRegistry(base_path=str(tmp_path))
    registry.ensure_structure()
    (tmp_path / "candidates" / "intent-v5.onnx").write_bytes(b"")
    registry.enable_shadow("intent", "v5")

    import asyncio

    done = asyncio.Event()

    def _infer(session, text):
        done.set()
        return "web_search", 0.77

    maybe_fire_shadow(
        runner, registry,
        model_name="intent",
        input_text="search for cats",
        production_prediction="normal",
        production_confidence=0.5,
        infer_on_session=_infer,
    )
    # Wait for the background task to finish (bounded).
    await asyncio.wait_for(done.wait(), timeout=1.0)
    # Give the record() task a tick to write.
    for _ in range(10):
        await asyncio.sleep(0)
        if _read_rows(db):
            break

    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_version"] == "v5"
    assert rows[0]["candidate_prediction"] == "web_search"


def test_maybe_fire_shadow_noop_with_none_runner(tmp_path):
    from gateway.intelligence.shadow import maybe_fire_shadow
    # Must not raise — simply no-ops.
    maybe_fire_shadow(
        None, None,
        model_name="intent",
        input_text="x",
        production_prediction="n",
        production_confidence=0.0,
        infer_on_session=lambda s, t: ("x", 0.0),
    )


@pytest.mark.anyio
async def test_fire_shadow_text_records_session_load_error(tmp_path, monkeypatch):
    # No fake_onnxruntime injected → `from onnxruntime import ...` fails.
    # The shadow path must still record the failure, not raise.
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    db = _make_db(tmp_path)
    runner = ShadowRunner(db)
    r2 = ModelRegistry(base_path=str(tmp_path))
    r2.ensure_structure()
    path = tmp_path / "candidates" / "intent-v1.onnx"
    path.write_bytes(b"")
    cand = Candidate(model="intent", version="v1", path=path)

    await fire_shadow_text(
        runner,
        model="intent",
        candidate=cand,
        input_text="x",
        production_prediction="normal",
        production_confidence=0.5,
        infer_on_session=lambda s, t: ("x", 0.0),
    )

    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_error"]
