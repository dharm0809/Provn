from gateway.pipeline.tool_executor import (
    record_tool_exception, tool_exceptions_snapshot, _tool_exception_log,
)


def setup_function():
    _tool_exception_log.clear()


def test_snapshot_empty():
    assert tool_exceptions_snapshot() == {"exceptions_60s": 0, "last_exception": None}


def test_snapshot_records():
    record_tool_exception(tool="web_search", error="timeout after 10000ms")
    snap = tool_exceptions_snapshot()
    assert snap["exceptions_60s"] == 1
    assert snap["last_exception"]["tool"] == "web_search"


def test_last_exception_scoped_to_window(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1000.0)
    record_tool_exception(tool="web_search", error="ancient")
    monkeypatch.setattr(_time, "time", lambda: 1100.0)  # 100s later
    snap = tool_exceptions_snapshot()
    assert snap["exceptions_60s"] == 0
    assert snap["last_exception"] is None
