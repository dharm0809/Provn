from gateway.pipeline.forwarder import (
    record_stream_interruption,
    stream_interruptions_snapshot,
    _stream_interruption_log,
)


def setup_function():
    _stream_interruption_log.clear()


def test_empty():
    assert stream_interruptions_snapshot() == {
        "interruptions_60s": 0,
        "last_interruption": None,
    }


def test_records():
    record_stream_interruption(provider="ollama", detail="client disconnect")
    snap = stream_interruptions_snapshot()
    assert snap["interruptions_60s"] == 1
    assert snap["last_interruption"]["provider"] == "ollama"
    assert snap["last_interruption"]["detail"] == "client disconnect"
    assert "ts" in snap["last_interruption"]


def test_last_interruption_scoped_to_window(monkeypatch):
    import gateway.pipeline.forwarder as fwd

    monkeypatch.setattr(fwd.time, "time", lambda: 1000.0)
    record_stream_interruption(provider="ollama", detail="ancient")
    monkeypatch.setattr(fwd.time, "time", lambda: 1100.0)
    snap = stream_interruptions_snapshot()
    assert snap["interruptions_60s"] == 0
    assert snap["last_interruption"] is None
