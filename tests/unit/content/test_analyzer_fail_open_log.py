"""Fail-open deque mixin on ContentAnalyzer base — Task 1.2."""

from gateway.content.llama_guard import LlamaGuardAnalyzer


def _make_analyzer() -> LlamaGuardAnalyzer:
    # LlamaGuardAnalyzer requires ollama_url; construct without touching network.
    return LlamaGuardAnalyzer(ollama_url="http://localhost:11434")


def test_analyzer_fail_open_snapshot_empty():
    a = _make_analyzer()
    assert a.fail_open_snapshot() == {"fail_opens_60s": 0, "last_fail_open": None}


def test_analyzer_fail_open_snapshot_records():
    a = _make_analyzer()
    a._record_fail_open("timeout")
    a._record_fail_open("connection refused")
    snap = a.fail_open_snapshot()
    assert snap["fail_opens_60s"] == 2
    assert snap["last_fail_open"]["reason"] == "connection refused"
    assert "ts" in snap["last_fail_open"]


def test_presidio_unavailable_records_fail_open():
    import asyncio

    from gateway.content.presidio_pii import PresidioPIIAnalyzer

    a = PresidioPIIAnalyzer.__new__(PresidioPIIAnalyzer)  # bypass __init__
    a._engine = None
    a._available = False
    asyncio.run(a.analyze("hello"))
    snap = a.fail_open_snapshot()
    assert snap["fail_opens_60s"] == 1
    assert snap["last_fail_open"]["reason"] == "unavailable"


def test_analyzer_last_fail_open_scoped_to_window(monkeypatch):
    import time as _time

    a = LlamaGuardAnalyzer(ollama_url="http://localhost:11434")

    # Ancient fail-open (>60s ago)
    monkeypatch.setattr(_time, "time", lambda: 1000.0)
    a._record_fail_open("ancient_timeout")

    # Now, window is clean
    monkeypatch.setattr(_time, "time", lambda: 1100.0)  # 100s later
    snap = a.fail_open_snapshot()

    assert snap["fail_opens_60s"] == 0
    assert snap["last_fail_open"] is None


def test_prompt_guard_unavailable_records_fail_open():
    import asyncio

    from gateway.content.prompt_guard import PromptGuardAnalyzer

    a = PromptGuardAnalyzer.__new__(PromptGuardAnalyzer)  # bypass __init__
    a._available = False
    asyncio.run(a.analyze("hello"))
    snap = a.fail_open_snapshot()
    assert snap["fail_opens_60s"] == 1
    assert snap["last_fail_open"]["reason"] == "unavailable"
