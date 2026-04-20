"""`InferenceSession` reload signaling helper.

All three ONNX clients (IntentClassifier, SchemaIntelligence, SchemaMapper,
SafetyClassifier) poll a per-model generation counter on the registry and
rebuild their `onnxruntime.InferenceSession` when it moves. The logic is
identical across clients — this module provides the shared primitive so
the four sites stay in lockstep.

Reload semantics:
  * Only triggers when `registry` AND `model_name` are both set. Clients
    that don't wire the registry retain their pre-Phase-25 behavior.
  * Fail-safe: a missing production file or a failed `InferenceSession`
    construction logs a warning and keeps the previous session in place.
    Inference must never break because of a reload failure.
  * Called at the top of every hot-path inference entry point. The check
    itself is cheap (dict lookup + int compare); the actual ORT rebuild
    only runs when the generation has moved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from gateway.intelligence.registry import ModelRegistry

logger = logging.getLogger(__name__)


@dataclass
class ReloadState:
    """Holds the registry reference, model name, and last-observed generation.

    `-1` on `last_generation` guarantees the first `maybe_reload` call
    rebuilds (since registry generations start at 0). A client created
    without a registry keeps `registry=None` and `maybe_reload` short-circuits.
    """
    registry: "ModelRegistry | None" = None
    model_name: str | None = None
    last_generation: int = -1


def maybe_reload(
    state: ReloadState,
    build_session: Callable[[str], object],
    on_success: Callable[[object], None],
    label: str,
) -> None:
    """Call `build_session(path)` if the registry generation has moved.

    Parameters
    ----------
    state
        Mutable reload state owned by the client (updated in place).
    build_session
        Callable that takes a string path and returns the new session.
        Client passes its own ORT factory — usually a lambda wrapping
        `onnxruntime.InferenceSession(path, providers=[...])`.
    on_success
        Called with the new session when construction succeeds. Lets the
        client rebind its internal `_onnx_session`/`_session` attribute.
    label
        Human-readable tag for logs ("intent", "schema_mapper", ...).

    Fail-safe — never raises. Missing file or construction error leaves
    the previous session untouched.
    """
    if state.registry is None or state.model_name is None:
        return
    current = state.registry.get_generation(state.model_name)
    if current == state.last_generation:
        return
    # Advance the marker BEFORE attempting the rebuild so a persistent
    # failure doesn't retry every inference — one attempt per generation
    # bump is enough. A subsequent promote will bump again and retry.
    state.last_generation = current
    path = state.registry.production_path(state.model_name)
    if not path.exists():
        logger.debug(
            "%s reload skipped: production file %s missing", label, path
        )
        return
    try:
        session = build_session(str(path))
    except Exception:
        logger.warning(
            "%s reload failed, keeping previous session", label, exc_info=True
        )
        return
    on_success(session)
    logger.info("%s session reloaded from %s (generation=%d)", label, path, current)
