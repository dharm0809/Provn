from gateway.intelligence.worker import IntelligenceWorker


def test_snapshot_defaults():
    w = IntelligenceWorker()
    snap = w.snapshot()
    assert snap["running"] is False
    assert snap["queue_depth"] == 0
    assert snap["last_error"] is None
    assert "oldest_job_age_s" in snap


def test_snapshot_records_last_error():
    w = IntelligenceWorker()
    w._record_error("KeyError on _CLASSIFY_PROMPT")
    snap = w.snapshot()
    assert snap["last_error"] is not None
    assert snap["last_error"]["detail"] == "KeyError on _CLASSIFY_PROMPT"


def test_last_error_scoped_to_window(monkeypatch):
    from gateway.intelligence import worker as worker_mod
    w = IntelligenceWorker()
    monkeypatch.setattr(worker_mod.time, "time", lambda: 1000.0)
    w._record_error("ancient")
    monkeypatch.setattr(worker_mod.time, "time", lambda: 1100.0)
    assert w.snapshot()["last_error"] is None
