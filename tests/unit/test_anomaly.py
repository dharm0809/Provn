"""Unit tests for EWMA latency anomaly detector."""

from gateway.metrics.anomaly import LatencyAnomalyDetector, _EWMAState


def test_ewma_first_value():
    """First value sets the mean directly."""
    state = _EWMAState(alpha=0.3)
    state.update(1.0)
    assert state.mean == 1.0
    assert state.var == 0.0
    assert state.count == 1


def test_ewma_converges():
    """EWMA converges toward repeated values."""
    state = _EWMAState(alpha=0.3)
    for _ in range(50):
        state.update(1.0)
    assert abs(state.mean - 1.0) < 0.01
    assert state.stddev < 0.01


def test_ewma_tracks_shift():
    """EWMA tracks upward shift in values."""
    state = _EWMAState(alpha=0.3)
    for _ in range(20):
        state.update(1.0)
    for _ in range(20):
        state.update(5.0)
    assert state.mean > 3.0  # Should have shifted up significantly


def test_detector_warmup():
    """No anomalies flagged during warm-up period."""
    d = LatencyAnomalyDetector(min_samples=10)
    for _ in range(9):
        assert d.record("p1", 100.0) is False  # Even extreme values during warmup


def test_detector_normal_values():
    """Normal values after warmup are not flagged."""
    d = LatencyAnomalyDetector(alpha=0.3, sigma_threshold=3.0, min_samples=5)
    for _ in range(10):
        d.record("p1", 1.0)
    assert d.record("p1", 1.1) is False


def test_detector_anomaly_flagged():
    """Extreme spike after warmup is flagged as anomaly."""
    d = LatencyAnomalyDetector(alpha=0.3, sigma_threshold=3.0, min_samples=5)
    # Establish baseline of ~1.0s
    for _ in range(20):
        d.record("p1", 1.0)
    # Spike should be anomalous
    assert d.record("p1", 100.0) is True


def test_detector_separate_providers():
    """Each provider has independent tracking."""
    d = LatencyAnomalyDetector(min_samples=5)
    for _ in range(20):
        d.record("ollama", 1.0)
        d.record("openai", 0.5)
    # Anomaly for one doesn't affect other
    assert d.record("ollama", 100.0) is True
    assert d.record("openai", 0.6) is False


def test_detector_get_stats():
    """get_stats returns current EWMA state."""
    d = LatencyAnomalyDetector(min_samples=5)
    assert d.get_stats("unknown") is None
    for _ in range(10):
        d.record("p1", 2.0)
    stats = d.get_stats("p1")
    assert stats is not None
    assert stats["count"] == 10
    assert abs(stats["mean"] - 2.0) < 0.1


def test_detector_get_stats_keys():
    """get_stats dict has expected keys."""
    d = LatencyAnomalyDetector()
    d.record("p1", 1.0)
    stats = d.get_stats("p1")
    assert stats is not None
    assert set(stats.keys()) == {"mean", "stddev", "count", "threshold"}
