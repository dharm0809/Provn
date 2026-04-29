"""promotion-gate evaluation + auto-promote branch.

Decides whether a candidate can leave the shadow pen. Consumes
`ShadowMetrics` from Task 23, checks every threshold the operator has
configured, and — when all gates pass AND the model is on the
`auto_promote_models_list` — flips the registry over via
`registry.promote` and emits a `model_promoted` lifecycle event. When
the gate fails, or when auto-promote is disabled for this model, it
writes `shadow_validation_complete` with the metrics and the pass flag
so the dashboard can surface the decision for human approval.

Threshold semantics
-------------------
Every threshold is additive: a candidate fails the gate as soon as ANY
threshold is violated. All failure reasons are collected (not just the
first) so the dashboard can present a complete picture.

Auto-promote guard rails
------------------------
* The auto-promote branch runs ONLY when the gate passes AND the model
  name is in the configured allowlist. Otherwise promotion is a manual
  dashboard action (Tasks 27-32).
* After auto-promote the shadow marker is cleared — the candidate has
  moved into production and is no longer a shadow target.
* The promotion event carries `approver="auto"` by default so audit
  readers can distinguish automated from human-approved promotions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from gateway.intelligence.events import (
    LifecycleEvent,
    build_promotion_blocked,
    build_promotion_event,
    build_shadow_validation_complete,
)
from gateway.intelligence.registry import ModelRegistry
from gateway.intelligence.sanity_adapters import build_infer_fn, is_wired
from gateway.intelligence.sanity_runner import SanityResult, SanityRunner
from gateway.intelligence.shadow_metrics import ShadowMetrics

logger = logging.getLogger(__name__)

# McNemar significance threshold — paired with `shadow_min_accuracy_delta`
# to avoid promoting a candidate on noise. 0.05 is the conventional
# academic cutoff; tune per deployment if needed.
_MCNEMAR_ALPHA: float = 0.05


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str]
    metrics: dict[str, Any]
    promoted: bool = False
    promotion_event: LifecycleEvent | None = None
    shadow_complete_event: LifecycleEvent | None = None
    # Set when the sanity-runner blocked an otherwise-passing
    # candidate. Carries the per-class accuracy / failing classes for
    # the dashboard. None when sanity wasn't run (model unwired) or
    # when sanity passed.
    sanity_result: SanityResult | None = None
    promotion_blocked_event: LifecycleEvent | None = None


def evaluate_gate(metrics: ShadowMetrics, settings: Any) -> GateResult:
    """Return a pure-evaluation result — no side effects.

    `reasons` always has at least one entry: either the list of gate
    violations (when `passed=False`) or `["all gates passed"]`.
    """
    reasons: list[str] = []

    if metrics.sample_count < int(settings.shadow_sample_target):
        reasons.append(
            f"insufficient samples: {metrics.sample_count} "
            f"< {settings.shadow_sample_target}"
        )

    # Accuracy delta only checked when we actually have ground truth.
    # A zero-delta on zero-labeled rows is "no evidence either way" —
    # bundle that into the sample-count gate above by leaving accuracy
    # alone here.
    if metrics.labeled_count > 0:
        delta = metrics.candidate_accuracy - metrics.production_accuracy
        if delta < float(settings.shadow_min_accuracy_delta):
            reasons.append(
                f"accuracy delta {delta:.4f} "
                f"< threshold {settings.shadow_min_accuracy_delta}"
            )

    if metrics.disagreement_rate > float(settings.shadow_max_disagreement):
        reasons.append(
            f"disagreement rate {metrics.disagreement_rate:.4f} "
            f"> threshold {settings.shadow_max_disagreement}"
        )

    if metrics.candidate_error_rate > float(settings.shadow_max_error_rate):
        reasons.append(
            f"candidate error rate {metrics.candidate_error_rate:.4f} "
            f"> threshold {settings.shadow_max_error_rate}"
        )

    # McNemar is only meaningful with labeled evidence; a high p-value
    # with zero labeled rows is the default (we treat it as "can't
    # distinguish") and rightly blocks promotion.
    if metrics.mcnemar_p_value >= _MCNEMAR_ALPHA:
        reasons.append(
            f"McNemar p={metrics.mcnemar_p_value:.4f} "
            f"not statistically significant (alpha={_MCNEMAR_ALPHA})"
        )

    passed = not reasons
    return GateResult(
        passed=passed,
        reasons=reasons if reasons else ["all gates passed"],
        metrics=_metrics_to_dict(metrics),
    )


async def process_candidate(
    *,
    metrics: ShadowMetrics,
    settings: Any,
    registry: ModelRegistry,
    walacor_writer: Any | None = None,
    dataset_hash: str = "",
    approver: str = "auto",
    sanity_runner: SanityRunner | None = None,
    sanity_check: Any | None = None,
) -> GateResult:
    """Evaluate the gate and take the appropriate side-effect branch.

    On pass + model in `auto_promote_models_list`:
      1. Run the offline sanity gate against the labeled fixture
         (`SanityRunner`). A pass-or-fail-loud contract:
           * sanity passes → continue to promotion.
           * sanity fails (per-class accuracy floor, missing fixture,
             or adapter raised) → BLOCK. Append the sanity reason to
             `gate.reasons`, route to the shadow_complete branch, and
             emit a `model_promotion_blocked` lifecycle event.
         Models without a wired sanity adapter (see
         `sanity_adapters.WIRED_MODELS`) skip the check with a loud
         warning so we don't permanently strand `safety` /
         `schema_mapper` promotions.
      2. `registry.promote(...)` — atomic swap.
      3. `registry.disable_shadow(...)` — clear the shadow marker.
      4. Emit `model_promoted` via the Walacor writer.

    On pass-but-not-auto OR fail (gate or sanity):
      Emit `shadow_validation_complete` with `passed` flag so the
      dashboard (Tasks 30-34) can render approve/reject buttons.

    All I/O is fail-open at the writer layer (Task 21 owns retries);
    a writer outage logs but doesn't undo the local side effects.
    """
    gate = evaluate_gate(metrics, settings)

    auto_list: list[str] = list(getattr(settings, "auto_promote_models_list", []) or [])
    should_auto = gate.passed and metrics.model_name in auto_list

    promoted = False
    promotion_event: LifecycleEvent | None = None
    shadow_complete_event: LifecycleEvent | None = None
    promotion_blocked_event: LifecycleEvent | None = None
    sanity_result: SanityResult | None = None
    # Mutable copy so we can append the sanity-block reason without
    # rebuilding the list inside the GateResult.
    reasons: list[str] = list(gate.reasons)

    if should_auto:
        # Run the offline sanity gate. Models without a wired adapter
        # are short-circuited with a warning — see sanity_adapters.
        # `sanity_check` is overridable for tests / advanced wiring;
        # production calls fall through to `_run_sanity_check` which
        # walks the registry → adapter → SanityRunner pipeline.
        check = sanity_check or _run_sanity_check
        sanity_result, sanity_failure_reason = await asyncio.to_thread(
            check,
            metrics.model_name,
            metrics.candidate_version,
            registry,
            sanity_runner or SanityRunner(),
        )
        if sanity_failure_reason is not None:
            # Block promotion. Treat ALL sanity failures (fixture-fail,
            # missing fixture, adapter-raise) the same way: never
            # silently approve.
            should_auto = False
            # Replace the "all gates passed" placeholder with the
            # concrete sanity reason — caller should see why we blocked.
            if reasons == ["all gates passed"]:
                reasons = [sanity_failure_reason]
            else:
                reasons.append(sanity_failure_reason)
            promotion_blocked_event = build_promotion_blocked(
                model_name=metrics.model_name,
                candidate_version=metrics.candidate_version,
                failing_classes=(
                    list(sanity_result.failing_classes)
                    if sanity_result is not None else []
                ),
                per_class_accuracy=(
                    dict(sanity_result.per_class_accuracy)
                    if sanity_result is not None else None
                ),
                overall_accuracy=(
                    sanity_result.overall_accuracy
                    if sanity_result is not None else None
                ),
                total_examples=(
                    sanity_result.total_examples
                    if sanity_result is not None else None
                ),
                error_count=(
                    sanity_result.error_count
                    if sanity_result is not None else None
                ),
                detail=sanity_failure_reason,
            )
            await _write_lifecycle(walacor_writer, promotion_blocked_event)
            try:
                from gateway.metrics.prometheus import candidate_rejected_total
                candidate_rejected_total.labels(
                    model=metrics.model_name,
                    reason="sanity",
                ).inc()
            except Exception:
                logger.debug("candidate_rejected_total metric failed", exc_info=True)
            logger.warning(
                "sanity gate BLOCKED auto-promote of %s %s: %s",
                metrics.model_name, metrics.candidate_version,
                sanity_failure_reason,
            )

    if should_auto:
        try:
            await registry.promote(metrics.model_name, metrics.candidate_version)
            registry.disable_shadow(metrics.model_name)
            promoted = True
            promotion_event = build_promotion_event(
                model_name=metrics.model_name,
                candidate_version=metrics.candidate_version,
                dataset_hash=dataset_hash,
                shadow_metrics=gate.metrics,
                approver=approver,
            )
            await _write_lifecycle(walacor_writer, promotion_event)
            try:
                from gateway.metrics.prometheus import model_promoted_total
                model_promoted_total.labels(model=metrics.model_name).inc()
            except Exception:
                logger.debug("model_promoted_total metric failed", exc_info=True)
            logger.info(
                "auto-promoted %s candidate %s (approver=%s)",
                metrics.model_name, metrics.candidate_version, approver,
            )
        except Exception:
            # Promotion itself failed — surface through the
            # shadow_complete path so the dashboard can see why.
            logger.exception(
                "auto-promote of %s %s failed — falling back to manual review",
                metrics.model_name, metrics.candidate_version,
            )
            promoted = False

    # Always emit shadow_complete unless we actually promoted — that
    # event is the "completed, awaiting decision" signal.
    if not promoted:
        # `passed` here reflects the COMBINED gate+sanity verdict so
        # the dashboard renders a consistent state — a candidate that
        # cleared ShadowMetrics but failed sanity is NOT "passed".
        combined_passed = gate.passed and promotion_blocked_event is None
        shadow_complete_event = build_shadow_validation_complete(
            model_name=metrics.model_name,
            candidate_version=metrics.candidate_version,
            metrics=gate.metrics,
            passed=combined_passed,
        )
        await _write_lifecycle(walacor_writer, shadow_complete_event)
        # Gate-failure counts as an auto-rejection signal for ops
        # dashboards even though the candidate file stays in
        # candidates/ awaiting human review. We use the first failing
        # gate reason as the label so e.g. "accuracy_delta" vs
        # "mcnemar" buckets distinct. Sanity rejections already
        # incremented the metric above; don't double-count.
        if not gate.passed:
            try:
                from gateway.metrics.prometheus import candidate_rejected_total
                first = gate.reasons[0] if gate.reasons else "gate_failed"
                # Bucket by short reason key, not the full sentence,
                # to keep cardinality bounded.
                reason_key = first.split()[0][:40]
                candidate_rejected_total.labels(
                    model=metrics.model_name,
                    reason=f"gate:{reason_key}",
                ).inc()
            except Exception:
                logger.debug("candidate_rejected_total metric failed", exc_info=True)

    return GateResult(
        passed=gate.passed,
        reasons=reasons,
        metrics=gate.metrics,
        promoted=promoted,
        promotion_event=promotion_event,
        shadow_complete_event=shadow_complete_event,
        sanity_result=sanity_result,
        promotion_blocked_event=promotion_blocked_event,
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _metrics_to_dict(m: ShadowMetrics) -> dict[str, Any]:
    return {
        "model_name": m.model_name,
        "candidate_version": m.candidate_version,
        "sample_count": m.sample_count,
        "labeled_count": m.labeled_count,
        "candidate_accuracy": m.candidate_accuracy,
        "production_accuracy": m.production_accuracy,
        "disagreement_rate": m.disagreement_rate,
        "candidate_error_rate": m.candidate_error_rate,
        "mcnemar_p_value": m.mcnemar_p_value,
    }


def _run_sanity_check(
    model_name: str,
    candidate_version: str,
    registry: ModelRegistry,
    runner: SanityRunner,
) -> tuple[SanityResult | None, str | None]:
    """Run the offline sanity gate for a candidate.

    Returns `(result, failure_reason)`:
      * `(SanityResult, None)`  — sanity ran AND passed.
      * `(SanityResult, reason)` — sanity ran AND failed (per-class
        accuracy floor, missing fixture).
      * `(None, None)`          — model is unwired by design; sanity
        skipped (caller continues to promote).
      * `(None, reason)`        — adapter raised or the registry path
        was missing — treated as a failure (caller blocks).

    Synchronous on purpose: the caller dispatches to a thread because
    onnxruntime + JSON I/O don't belong on the event loop.
    """
    if not is_wired(model_name):
        # Known-deferred model — log loudly so operators don't forget
        # this carve-out exists, but allow promotion. Once the trainer
        # side-cars land for safety / schema_mapper this branch
        # disappears (the strategy table swaps `_NotWired` for the
        # real adapter and `is_wired` returns True).
        logger.warning(
            "sanity adapter not wired for %s — skipping sanity gate, "
            "ShadowMetrics is the sole arbiter for this candidate",
            model_name,
        )
        return None, None

    try:
        candidate_path = registry.candidate_path(model_name, candidate_version)
    except ValueError as e:
        # Unknown / malformed model name — should never happen since
        # ALLOWED_MODEL_NAMES gates this upstream, but if it does
        # treat as a sanity FAILURE, never silently approve.
        return None, f"sanity adapter: invalid model name: {e}"

    try:
        infer_fn = build_infer_fn(model_name, candidate_path)
    except FileNotFoundError:
        return None, f"sanity adapter: candidate file missing at {candidate_path}"
    except NotImplementedError as e:
        # Adapter is `_NotWired` — defensive: `is_wired` should have
        # short-circuited above. Treat as failure.
        return None, f"sanity adapter: {e}"
    except Exception as e:
        logger.exception(
            "sanity adapter raised while building infer_fn for %s %s",
            model_name, candidate_version,
        )
        return None, f"sanity adapter raised: {e!r}"

    try:
        result = runner.run(model_name, infer_fn)
    except Exception as e:
        # Runner itself crashed (NOT a per-example error — those are
        # caught inside `runner.run`). Block promotion.
        logger.exception(
            "SanityRunner.run raised for %s %s",
            model_name, candidate_version,
        )
        return None, f"sanity runner crashed: {e!r}"

    if result.passed:
        return result, None

    # Surface a stable, short reason string so the dashboard can group
    # blocks by category. The full per-class breakdown lives on the
    # `model_promotion_blocked` event payload.
    if result.total_examples == 0:
        reason = "sanity: no fixture authored"
    else:
        reason = (
            f"sanity: per-class accuracy below floor for "
            f"{','.join(result.failing_classes)}"
        )
    return result, reason


async def _write_lifecycle(writer: Any | None, event: LifecycleEvent) -> None:
    if writer is None:
        logger.debug(
            "no lifecycle writer wired — skipping %s",
            event.event_type.value,
        )
        return
    try:
        if hasattr(writer, "write_event"):
            await writer.write_event(event)
        elif hasattr(writer, "write_lifecycle_event"):
            await writer.write_lifecycle_event(event)
        else:
            await writer.write_record(event.to_record())
    except Exception:
        logger.warning(
            "lifecycle write for %s failed",
            event.event_type.value, exc_info=True,
        )
