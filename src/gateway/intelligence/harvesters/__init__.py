"""ONNX verdict harvesters.

Harvesters run AFTER a request has been audited to the WAL, inspect the
captured context (response, analyzer decisions, overflow keys, next-turn
state), and back-write a `divergence_signal` onto the matching row in
`onnx_verdicts`. That divergence signal is what the distillation worker
(Tasks 17-20) later consumes as the label for retraining.

This package ships the framework (ABC + signal dataclass + async runner)
in Task 13. Per-model harvesters arrive in Tasks 14-16.
"""
from gateway.intelligence.harvesters.base import (
    Harvester,
    HarvesterRunner,
    HarvesterSignal,
)

__all__ = ["Harvester", "HarvesterRunner", "HarvesterSignal"]
