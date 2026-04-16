from __future__ import annotations

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


def test_size_reflects_buffer_state():
    b = VerdictBuffer(max_size=10)
    assert b.size == 0
    for i in range(5):
        b.record(_mk(i))
    assert b.size == 5
    b.drain(max_batch=2)
    assert b.size == 3
