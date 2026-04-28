"""Temperature scaling for the schema_mapper classifier.

Fits a single scalar T to minimize NLL on a calibration set, then writes
`out/checkpoints/temperature.json`. Apply at inference by dividing logits
by T before softmax — argmax (and all argmax-based gates) is preserved;
only confidence is rescaled.

Calibration set = test_synth_cal (rename variants of held-out providers
via teacher_rename_vocab, partitioned to a half disjoint from the gate
measurement split). This is the only set with realistic test-distribution
confusion mass: val + rename together give the model only ~0.1% errors
(too easy → T converges to ~2 but doesn't generalize to held-out OOD),
while the raw 33-sample test gold has too few errors to fit on without
overfitting and leaks into gate 5.

CLI:
    python calibrate.py --checkpoint out/checkpoints/best.pt
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))

from encoder import MAX_SEQ_LEN, load_tokenizer  # noqa: E402
from gateway.schema.features import FEATURE_DIM_V2  # noqa: E402
from model import SchemaMapper  # noqa: E402
from run_eval import make_test_synth_samples  # noqa: E402


def _collect_calibration_samples(specs_dir: pathlib.Path, vocab_path: pathlib.Path,
                                  n_per_provider: int = 250) -> list:
    """Test_synth_cal half — rename variants of held-out providers, even
    variant_idx only (matches run_eval.py's deterministic split)."""
    all_synth = make_test_synth_samples(specs_dir, vocab_path, n_per_provider=n_per_provider)
    def _vidx(s) -> int:
        try:
            return int(s.variant_id.rsplit("#", 1)[-1])
        except (ValueError, AttributeError):
            return 0
    return [s for s in all_synth if _vidx(s) % 2 == 0]


def _gather_logits(model: SchemaMapper, tokenizer, samples, batch_size: int = 32) -> tuple[torch.Tensor, torch.Tensor]:
    all_logits, all_labels = [], []
    for i in range(0, len(samples), batch_size):
        batch = samples[i : i + batch_size]
        enc = tokenizer([s.text for s in batch], truncation=True, max_length=MAX_SEQ_LEN,
                        padding="max_length", return_tensors="pt")
        feats = torch.tensor([s.features for s in batch], dtype=torch.float32)
        with torch.no_grad():
            out = model(enc["input_ids"], enc["attention_mask"], feats)
        all_logits.append(out["logits"].cpu())
        all_labels.extend([s.label for s in batch])
    return torch.cat(all_logits, dim=0), torch.tensor(all_labels, dtype=torch.long)


def _fit_temperature(logits: torch.Tensor, labels: torch.Tensor) -> float:
    """LBFGS over scalar T (parameterized as exp(log_T) to keep T > 0)."""
    log_T = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_T], lr=0.1, max_iter=200, line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        T = log_T.exp()
        loss = F.cross_entropy(logits / T, labels)
        loss.backward()
        return loss

    opt.step(closure)
    return float(log_T.exp().item())


def _ece(probs: np.ndarray, labels: np.ndarray, n_bins: int = 15) -> float:
    confidences = probs.max(axis=-1)
    predictions = probs.argmax(axis=-1)
    accuracies = (predictions == labels).astype(np.float64)
    bin_boundaries = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bin_boundaries[:-1], bin_boundaries[1:]):
        in_bin = (confidences > lo) & (confidences <= hi)
        prop = in_bin.mean()
        if prop > 0:
            acc_in_bin = accuracies[in_bin].mean()
            conf_in_bin = confidences[in_bin].mean()
            ece += abs(acc_in_bin - conf_in_bin) * prop
    return float(ece)


def main() -> None:
    here = pathlib.Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=pathlib.Path, default=here / "out" / "checkpoints" / "best.pt")
    ap.add_argument("--specs-dir", type=pathlib.Path, default=here / "data" / "provider_specs")
    ap.add_argument("--vocab", type=pathlib.Path, default=here / "data" / "teacher_rename_vocab.json")
    ap.add_argument("--n-per-provider", type=int, default=250)
    ap.add_argument("--out", type=pathlib.Path, default=here / "out" / "checkpoints" / "temperature.json")
    args = ap.parse_args()

    print(f"[calibrate] loading {args.checkpoint}", file=sys.stderr)
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    feature_dim = ckpt.get("feature_dim", FEATURE_DIM_V2)
    model = SchemaMapper(feature_dim=feature_dim)
    model.load_state_dict(ckpt["model_state"])
    model.train(False)
    tokenizer = load_tokenizer()

    samples = _collect_calibration_samples(args.specs_dir, args.vocab, args.n_per_provider)
    print(f"[calibrate] test_synth_cal samples: {len(samples)}", file=sys.stderr)

    logits, labels = _gather_logits(model, tokenizer, samples)

    pre_probs = F.softmax(logits, dim=-1).numpy()
    pre_ece = _ece(pre_probs, labels.numpy())
    pre_nll = float(F.cross_entropy(logits, labels).item())

    T = _fit_temperature(logits, labels)
    post_probs = F.softmax(logits / T, dim=-1).numpy()
    post_ece = _ece(post_probs, labels.numpy())
    post_nll = float(F.cross_entropy(logits / T, labels).item())

    print(f"[calibrate] T = {T:.4f}", file=sys.stderr)
    print(f"[calibrate] calibration-set NLL: {pre_nll:.4f} → {post_nll:.4f}", file=sys.stderr)
    print(f"[calibrate] calibration-set ECE: {pre_ece:.4f} → {post_ece:.4f}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "temperature": T,
        "calibration_set": {"split": "test_synth_cal", "n_samples": len(samples)},
        "calibration_ece_before": pre_ece,
        "calibration_ece_after": post_ece,
        "calibration_nll_before": pre_nll,
        "calibration_nll_after": post_nll,
    }, indent=2))
    print(f"[calibrate] wrote {args.out}", file=sys.stderr)
    print(f"temperature={T:.6f}")


if __name__ == "__main__":
    main()
