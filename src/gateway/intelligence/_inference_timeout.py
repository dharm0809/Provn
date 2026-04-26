"""Hot-path ONNX inference timeout helper.

A slow or malformed candidate `InferenceSession.run` can stall a request
indefinitely. We bound it by submitting to a small dedicated thread pool
and waiting with a timeout; on timeout the caller takes its deterministic
fallback path. The thread keeps running in the pool until the call returns
or the process exits — there is no portable way to cancel a sync C call —
but the request thread is freed.

Default timeout is read from `Settings.onnx_inference_timeout_ms` (env
`WALACOR_ONNX_INFERENCE_TIMEOUT_MS`). The helper is sync so the existing
sync classifier methods (`IntentClassifier._tier2_onnx`,
`SafetyClassifier.analyze`, `SchemaMapper._classify_onnx`) can adopt it
without rewriting the pipeline to async.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Small dedicated pool. Sized for the three concurrent hot-path classifiers
# (intent + safety + schema-mapper) plus a little headroom for in-flight
# candidates during shadow eval.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="onnx-infer")


class InferenceTimeout(Exception):
    """Raised when a hot-path ONNX inference exceeds its budget."""


def get_default_timeout_s() -> float:
    """Read configured timeout from Settings; fall back to 100ms if Settings can't load."""
    try:
        from gateway.config import get_settings
        ms = int(getattr(get_settings(), "onnx_inference_timeout_ms", 100))
        return max(0.001, ms / 1000.0)
    except Exception:
        return 0.1


def run_with_timeout(
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float | None = None,
    model: str = "unknown",
    **kwargs: Any,
) -> T:
    """Run `fn(*args, **kwargs)` in a thread pool, raise InferenceTimeout on overrun.

    Increments `onnx_inference_timeout_total{model=...}` Prometheus counter
    on timeout. Caller is responsible for catching and falling back to a
    deterministic path.
    """
    budget = timeout_s if timeout_s is not None else get_default_timeout_s()
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=budget)
    except FutureTimeoutError as exc:
        # bump the counter; don't cancel the future — onnxruntime ignores it.
        try:
            from gateway.metrics.prometheus import onnx_inference_timeout_total
            onnx_inference_timeout_total.labels(model=model).inc()
        except Exception:
            logger.debug("failed to increment onnx_inference_timeout_total", exc_info=True)
        raise InferenceTimeout(
            f"ONNX inference for model={model!r} exceeded {budget:.3f}s"
        ) from exc
