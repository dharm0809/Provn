"""ONNX Safety Classifier — lightweight Llama Guard replacement.

Classifies text into 8 safety categories using a GradientBoosting model
trained on TF-IDF character n-grams + safety pattern features. Ships as
a 1.5MB ONNX file — no external model downloads, no Ollama, no torch.

Implements ContentAnalyzer for seamless integration with the existing
content analysis pipeline (response_evaluator.py).

Categories (mapped to Llama Guard S1-S14):
  - safe: No safety concerns
  - violence: S1 — Violence, weapons, physical harm
  - sexual: S3 — Sexual content
  - criminal: S2 — Criminal planning, illegal activities
  - self_harm: S5 — Self-harm, suicide
  - hate_speech: S9 — Hate speech, discrimination
  - dangerous: S10 — Dangerous activities, drug synthesis, weapons
  - child_safety: S4 — Child exploitation (CSAM) → always BLOCK

Actions:
  - child_safety → BLOCK (zero tolerance)
  - violence, dangerous, criminal → WARN (flag for review)
  - sexual, hate_speech, self_harm → WARN (configurable to BLOCK)
  - safe → PASS
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from gateway.content.base import ContentAnalyzer, Decision, Verdict

if TYPE_CHECKING:
    from gateway.intelligence.registry import ModelRegistry
    from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).parent
_ONNX_PATH = _MODEL_DIR / "safety_classifier.onnx"
_LABELS_PATH = _MODEL_DIR / "safety_classifier_labels.json"
_VOCAB_PATH = _MODEL_DIR / "safety_tfidf_vocab.json"
_IDF_PATH = _MODEL_DIR / "safety_tfidf_idf.npy"
_SVD_PATH = _MODEL_DIR / "safety_svd_components.npy"
_CONFIG_PATH = _MODEL_DIR / "safety_tfidf_config.json"

# Categories that trigger BLOCK (zero tolerance)
_BLOCK_CATEGORIES = frozenset({"child_safety"})

# Categories that trigger WARN (flagged for review, configurable)
_WARN_CATEGORIES = frozenset({
    "violence", "sexual", "criminal", "self_harm",
    "hate_speech", "dangerous",
})

# ── Pattern features (same as training script) ──────────────────────────────

_VIOLENCE_PATTERNS = re.compile(
    r"\b(kill|murder|attack|weapon|bomb|explode|stab|shoot|assault|torture|"
    r"hurt|injure|maim|strangle|suffocate|poison|arson|beat|punch|slash)\b", re.I
)
_SEXUAL_PATTERNS = re.compile(
    r"\b(explicit|erotic|pornograph|sexual|nude|naked|intercourse|orgasm|"
    r"genitals|foreplay|masturbat|fetish|bondage|seduct|arousal)\b", re.I
)
_CRIMINAL_PATTERNS = re.compile(
    r"\b(hack|steal|fraud|forge|counterfeit|launder|smuggle|blackmail|"
    r"extort|phishing|ransomware|burglar|theft|embezzle|bribe|scam)\b", re.I
)
_SELF_HARM_PATTERNS = re.compile(
    r"\b(suicide|self.?harm|cut myself|end my life|kill myself|overdose|"
    r"painless death|hang myself|slit wrist|want to die|no reason to live)\b", re.I
)
_HATE_PATTERNS = re.compile(
    r"\b(inferior race|racial supremacy|genocide|ethnic cleansing|"
    r"hate.{0,10}(group|race|religion)|discriminat|dehumaniz|"
    r"propaganda.{0,10}(against|targeting)|supremacist)\b", re.I
)
_DANGEROUS_PATTERNS = re.compile(
    r"\b(synthesiz|manufactur|meth|fentanyl|nerve agent|biological weapon|"
    r"chemical weapon|explosive device|chlorine gas|ricin|dirty bomb|"
    r"improvised explosive|IED)\b", re.I
)
_CHILD_PATTERNS = re.compile(
    r"\b(child.{0,5}(sex|abuse|exploit)|minor.{0,5}sex|groom.{0,5}(child|minor|teen)|"
    r"csam|pedo|underage.{0,5}sex)\b", re.I
)


def _extract_safety_features(text: str) -> list[float]:
    """Extract safety pattern features (must match training script exactly)."""
    return [
        len(_VIOLENCE_PATTERNS.findall(text)),
        len(_SEXUAL_PATTERNS.findall(text)),
        len(_CRIMINAL_PATTERNS.findall(text)),
        len(_SELF_HARM_PATTERNS.findall(text)),
        len(_HATE_PATTERNS.findall(text)),
        len(_DANGEROUS_PATTERNS.findall(text)),
        len(_CHILD_PATTERNS.findall(text)),
        len(text),
        sum(1 for c in text if c.isupper()) / max(len(text), 1),
        text.count("!") / max(len(text), 1),
        text.count("?") / max(len(text), 1),
        len(text.split()),
        1.0 if any(w in text.lower() for w in ("how to", "instructions", "guide to", "teach me", "show me how")) else 0.0,
    ]


class SafetyClassifier(ContentAnalyzer):
    """ONNX-powered content safety classifier.

    Implements ContentAnalyzer interface for plug-in with the existing
    response_evaluator pipeline. Replaces Llama Guard for environments
    without Ollama or GPU.
    """

    analyzer_id = "truzenai.safety.v1"

    def __init__(
        self,
        verdict_buffer: "VerdictBuffer | None" = None,
        registry: "ModelRegistry | None" = None,
        model_name: str | None = None,
        intelligence_db: "Any | None" = None,
    ) -> None:
        self._session = None
        self._input_name = ""
        self._labels: list[str] = []
        self._vocab: dict[str, int] = {}
        self._idf: np.ndarray | None = None
        self._svd_components: np.ndarray | None = None
        self._ngram_range = (3, 5)
        self._loaded = False
        self._verdict_buffer = verdict_buffer

        # optional `ModelRegistry` wiring — see `intelligence/reload.py`.
        # Only the `.onnx` session is swap-reloaded; TF-IDF vocab, IDF, and SVD
        # components are stable training artifacts that do not change with
        # retraining in the current distillation setup.
        from gateway.intelligence.reload import ReloadState
        self._reload_state = ReloadState(
            registry=registry, model_name=model_name, db=intelligence_db,
        )

        self._load()

    def _load(self) -> None:
        """Load ONNX model + TF-IDF vocabulary + SVD components."""
        try:
            # Labels
            if _LABELS_PATH.exists():
                with open(_LABELS_PATH) as f:
                    self._labels = json.load(f)

            # Vocabulary
            if _VOCAB_PATH.exists():
                with open(_VOCAB_PATH) as f:
                    self._vocab = json.load(f)

            # IDF weights
            if _IDF_PATH.exists():
                self._idf = np.load(str(_IDF_PATH))

            # SVD components
            if _SVD_PATH.exists():
                self._svd_components = np.load(str(_SVD_PATH))

            # Config
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH) as f:
                    config = json.load(f)
                    self._ngram_range = tuple(config.get("ngram_range", [3, 5]))

            # ONNX model — when a registry is wired it owns the session
            # lifecycle; skip the packaged-default session construction
            # (the first `analyze` call triggers `_maybe_reload` to build
            # from the current production file). Labels/vocab/IDF/SVD
            # artifacts above are still loaded — those are stable training
            # outputs that don't participate in the model swap.
            if self._reload_state.registry is not None:
                logger.info(
                    "SafetyClassifier: deferring ONNX load to registry "
                    "(%d categories, %d vocab terms)",
                    len(self._labels), len(self._vocab),
                )
            elif _ONNX_PATH.exists():
                from onnxruntime import InferenceSession
                self._session = InferenceSession(str(_ONNX_PATH), providers=["CPUExecutionProvider"])
                self._input_name = self._session.get_inputs()[0].name
                self._loaded = True
                logger.info("SafetyClassifier: ONNX loaded (%d categories, %d vocab terms)",
                            len(self._labels), len(self._vocab))
            else:
                logger.warning("SafetyClassifier: ONNX model not found at %s", _ONNX_PATH)

        except Exception as e:
            logger.warning("SafetyClassifier: load failed (fail-open): %s", e)

    def _tfidf_transform(self, text: str) -> np.ndarray:
        """Manual TF-IDF transform using saved vocabulary + IDF weights.

        Reproduces sklearn's TfidfVectorizer(analyzer='char_wb') output
        without requiring sklearn at inference time.
        """
        # Generate character n-grams (char_wb = word boundary aware)
        text_lower = f" {text.lower()} "  # char_wb adds space boundaries
        ngrams: dict[str, int] = {}
        for n in range(self._ngram_range[0], self._ngram_range[1] + 1):
            for i in range(len(text_lower) - n + 1):
                gram = text_lower[i:i + n]
                if gram in self._vocab:
                    ngrams[gram] = ngrams.get(gram, 0) + 1

        # Build TF vector (sublinear_tf = True → log(1 + tf))
        n_features = len(self._idf) if self._idf is not None else len(self._vocab)
        tf_vector = np.zeros(n_features, dtype=np.float32)
        for gram, count in ngrams.items():
            idx = self._vocab.get(gram)
            if idx is not None and idx < n_features:
                tf_vector[idx] = np.log1p(count)  # sublinear_tf

        # Apply IDF
        if self._idf is not None:
            tf_vector *= self._idf

        # L2 normalize
        norm = np.linalg.norm(tf_vector)
        if norm > 0:
            tf_vector /= norm

        return tf_vector

    def _featurize(self, text: str) -> np.ndarray:
        """Extract full feature vector (TF-IDF + safety patterns + SVD)."""
        # TF-IDF features
        tfidf_vec = self._tfidf_transform(text)

        # Safety pattern features
        safety_feats = np.array(_extract_safety_features(text), dtype=np.float32)

        # Concatenate
        combined = np.concatenate([tfidf_vec, safety_feats])

        # SVD reduction
        if self._svd_components is not None:
            combined = combined @ self._svd_components.T

        return combined.reshape(1, -1).astype(np.float32)

    def analyze(self, text: str) -> Decision:
        """Classify text for safety. Returns Decision with verdict and category."""
        # refresh session from registry if a new version was promoted.
        self._maybe_reload()

        # `label` is the raw ONNX class label (e.g. "safe", "violence") — used
        # both as the verdict prediction and for the downstream recording hook.
        label = "safe"
        confidence = 0.0

        if not self._loaded or not text:
            decision = Decision(
                analyzer_id=self.analyzer_id,
                verdict=Verdict.PASS,
                confidence=0.0,
                category="safety",
                reason="classifier_unavailable" if not self._loaded else "empty_input",
            )
        else:
            try:
                from gateway.intelligence._inference_timeout import (
                    InferenceTimeout,
                    run_with_timeout,
                )

                features = self._featurize(text)
                features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)

                outputs = run_with_timeout(
                    self._session.run, None, {self._input_name: features}, model="safety",
                )
                pred_idx = int(outputs[0][0])
                label = self._labels[pred_idx] if pred_idx < len(self._labels) else "safe"

                # Get confidence from probabilities if available
                confidence = 1.0
                if len(outputs) > 1:
                    probs = outputs[1][0]
                    if isinstance(probs, dict):
                        confidence = float(max(probs.values())) if probs else 0.5
                    elif hasattr(probs, '__getitem__'):
                        confidence = float(probs[pred_idx])

                # Determine verdict
                if label == "safe":
                    verdict = Verdict.PASS
                    reason = "content_safe"
                elif label in _BLOCK_CATEGORIES:
                    verdict = Verdict.BLOCK
                    reason = f"unsafe_{label}"
                elif label in _WARN_CATEGORIES:
                    verdict = Verdict.WARN
                    reason = f"flagged_{label}"
                else:
                    verdict = Verdict.PASS
                    reason = f"unknown_category_{label}"

                decision = Decision(
                    analyzer_id=self.analyzer_id,
                    verdict=verdict,
                    confidence=round(confidence, 3),
                    category=label if label != "safe" else "safety",
                    reason=reason,
                )
            except InferenceTimeout as e:
                self._record_fail_open("inference_timeout")
                logger.warning("SafetyClassifier inference timed out (fail-open): %s", e)
                label = "safe"
                confidence = 0.0
                decision = Decision(
                    analyzer_id=self.analyzer_id,
                    verdict=Verdict.PASS,
                    confidence=0.0,
                    category="safety",
                    reason="onnx_timeout",
                )
            except Exception as e:
                self._record_fail_open("inference_failed")
                logger.warning("SafetyClassifier inference failed (fail-open): %s", e)
                # Reset for recording: inference failed, treat as low-confidence safe.
                label = "safe"
                confidence = 0.0
                decision = Decision(
                    analyzer_id=self.analyzer_id,
                    verdict=Verdict.PASS,
                    confidence=0.0,
                    category="safety",
                    reason=f"inference_error: {e}",
                )

        # record verdict for self-learning (observational only).
        # Never allowed to break inference — wrap the whole stanza defensively.
        if self._verdict_buffer is not None:
            try:
                from gateway.util.request_context import request_id_var
                from gateway.intelligence.types import ModelVerdict
                rid = request_id_var.get() or None
                self._verdict_buffer.record(
                    ModelVerdict.from_inference(
                        model_name="safety",
                        input_text=text or "",
                        prediction=label,
                        confidence=float(confidence),
                        request_id=rid,
                        version=self._reload_state.current_version,
                    )
                )
            except Exception:
                logger.debug("verdict recording failed", exc_info=True)

        return decision

    def configure(self, policies: list[dict]) -> None:
        """Intentional no-op: ONNX safety verdicts are observer-only.

        SafetyClassifier is an ML/ONNX analyzer. Per the project's observer-identity
        invariant, ML verdicts always observe + log and never act unilaterally; any
        enforcement action must flow through declarative policies elsewhere in the
        pipeline. Accepting (and ignoring) `policies` keeps this class conformant
        with the ContentAnalyzer protocol without allowing the control plane to
        promote ONNX verdicts to BLOCK. Do not replace with real overrides without
        revisiting EU AI Act compliance implications (static thresholds are preferred).
        """
        return None

    def reload(self) -> None:
        """Rebuild the `InferenceSession` from the registry's production path.

        Refreshes `_input_name` and flips `_loaded` so the fail-open branch
        in `analyze` doesn't short-circuit after a successful swap. Fail-safe.
        """
        from gateway.intelligence.reload import maybe_reload

        def _build(path: str):
            from onnxruntime import InferenceSession
            return InferenceSession(path, providers=["CPUExecutionProvider"])

        def _adopt(session) -> None:
            self._session = session
            try:
                self._input_name = session.get_inputs()[0].name
            except Exception:
                logger.debug("SafetyClassifier.reload: could not refresh input_name", exc_info=True)
            self._loaded = True

        maybe_reload(self._reload_state, _build, _adopt, label="safety")

    def _maybe_reload(self) -> None:
        """Hot-path hook — poll generation, rebuild session if it moved."""
        if self._reload_state.registry is None:
            return
        self.reload()
