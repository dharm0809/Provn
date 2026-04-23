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
