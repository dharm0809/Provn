"""Phase 25 Phase E: closed-loop distillation.

Components
----------
* `dataset.DatasetBuilder` — pulls divergent verdict rows, dedupes by
  input hash, class-balances, caps per-session contribution. Shared by
  every trainer.
* `trainers.*_trainer` — model-specific training code (lazy sklearn +
  skl2onnx import so the runtime path never pays the cost when
  distillation is idle).
* `worker.DistillationWorker` — the scheduler / orchestrator tying the
  above into a background asyncio task.

All of these run off the hot path. The serving ONNX models are untouched
until a Phase F shadow validator accepts a candidate.
"""
