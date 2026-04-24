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


def test_content_safety_abort_recorded():
    from gateway.pipeline.forwarder import (
        _stream_interruption_log, record_stream_interruption, stream_interruptions_snapshot,
    )
    _stream_interruption_log.clear()
    record_stream_interruption(provider="ollama", detail="content_safety_abort")
    snap = stream_interruptions_snapshot()
    assert snap["last_interruption"]["detail"] == "content_safety_abort"


# Issue-1 fix: record_stream_interruption is gated on _exc is not None inside
# the finally block. Post-success audit-write failures do NOT inflate
# interruptions_60s. Verification is by code inspection (L468-area) — the
# gate is not practically testable without driving the full SSE pipeline.
