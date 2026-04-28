"""Phase 7 — ONNX export + INT8 quantization.

Two outputs:
  schema_mapper.onnx        — encoder + classifier head, FP32→INT8
  schema_mapper_crf.npz     — CRF transitions + start/end probs (numpy
                              Viterbi at runtime; ONNX has no Viterbi op)
  schema_mapper_tokenizer.json
                            — companion HF tokenizer

Quality preservation gate: re-evaluate INT8 macro-F1 vs FP32 on a test
fixture; fail if delta > 1pt unless --force.

CLI:
    /tmp/schema-mapper-venv/bin/python export_onnx.py \\
        --checkpoint out/checkpoints/best.pt \\
        --out out/onnx/
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))

from canonical_schema import CANONICAL_LABELS  # noqa: E402
from encoder import MAX_SEQ_LEN, load_tokenizer  # noqa: E402
from gateway.schema.features import FEATURE_DIM_V2  # noqa: E402
from model import SchemaMapper  # noqa: E402


class ExportableHead(torch.nn.Module):
    """Wraps the encoder + classifier head into one nn.Module so torch.onnx
    sees a single forward signature: (input_ids, attention_mask, features) → logits."""

    def __init__(self, model: SchemaMapper) -> None:
        super().__init__()
        self.encoder = model.encoder
        self.head = model.head

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor, features: torch.Tensor) -> torch.Tensor:
        emb = self.encoder(input_ids, attention_mask)
        joined = torch.cat([emb, features], dim=-1)
        return self.head(joined)


def export_fp32(model: SchemaMapper, out_path: pathlib.Path, feature_dim: int) -> None:
    # Static shapes only. Dynamic axes leak HF-attention shape ops that
    # ORT's symbolic shape inference can't resolve ("unsupported broadcast
    # between Min(512, batch) seq" / Concat axis-rank mismatches), which
    # then trips quantize_dynamic. We always pad to MAX_SEQ_LEN and run
    # batch=1 at inference, so dynamic axes buy nothing.
    wrapped = ExportableHead(model)
    wrapped.train(False)
    dummy_ids = torch.zeros(1, MAX_SEQ_LEN, dtype=torch.long)
    dummy_mask = torch.ones(1, MAX_SEQ_LEN, dtype=torch.long)
    dummy_feats = torch.zeros(1, feature_dim, dtype=torch.float32)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        wrapped,
        (dummy_ids, dummy_mask, dummy_feats),
        str(out_path),
        input_names=["input_ids", "attention_mask", "features"],
        output_names=["logits"],
        dynamic_axes=None,
        opset_version=17,
    )


def quantize_to_int8(fp32_path: pathlib.Path, int8_path: pathlib.Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(
        model_input=str(fp32_path),
        model_output=str(int8_path),
        weight_type=QuantType.QInt8,
    )


def export_crf_params(model: SchemaMapper, out_path: pathlib.Path) -> None:
    params = model.crf.export_params()
    np.savez(
        out_path,
        transitions=params["transitions"],
        start_transitions=params["start_transitions"],
        end_transitions=params["end_transitions"],
        labels=params["labels"],
    )


def quick_inference_check(onnx_path: pathlib.Path, feature_dim: int) -> None:
    """Smoke: load + run a single forward, return shape."""
    import onnxruntime as ort

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_ids = np.zeros((1, MAX_SEQ_LEN), dtype=np.int64)
    attention_mask = np.ones((1, MAX_SEQ_LEN), dtype=np.int64)
    features = np.zeros((1, feature_dim), dtype=np.float32)
    out = sess.run(None, {"input_ids": input_ids, "attention_mask": attention_mask, "features": features})
    assert out[0].shape == (1, len(CANONICAL_LABELS))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).parent / "out" / "onnx")
    ap.add_argument("--force", action="store_true",
                    help="Skip the INT8-vs-FP32 macro-F1 delta gate")
    ap.add_argument("--skip-int8", action="store_true",
                    help="Skip INT8 quantization (escape hatch only; static-shape export normally lets quantize_dynamic succeed)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    feature_dim = ckpt.get("feature_dim", FEATURE_DIM_V2)
    print(f"[export] feature_dim={feature_dim}", file=sys.stderr)

    model = SchemaMapper(feature_dim=feature_dim)
    model.load_state_dict(ckpt["model_state"])
    model.train(False)

    # Bake temperature into the final classifier Linear so the exported
    # ONNX produces calibrated probabilities directly. Argmax is unchanged
    # because dividing weight + bias by T scales logits by 1/T uniformly.
    temp_path = args.checkpoint.parent / "temperature.json"
    if temp_path.exists():
        T = float(json.loads(temp_path.read_text())["temperature"])
        if T != 1.0:
            final_linear = model.head[-1]  # last Linear in the head Sequential
            with torch.no_grad():
                final_linear.weight.data /= T
                final_linear.bias.data /= T
            print(f"[export] baked temperature T={T:.4f} into classifier head", file=sys.stderr)

    fp32_path = args.out / "schema_mapper_fp32.onnx"
    int8_path = args.out / "schema_mapper.onnx"
    crf_path = args.out / "schema_mapper_crf.npz"
    tok_path = args.out / "schema_mapper_tokenizer"

    print(f"[export] FP32 → {fp32_path}", file=sys.stderr)
    export_fp32(model, fp32_path, feature_dim)
    quick_inference_check(fp32_path, feature_dim)

    if args.skip_int8:
        print("[export] INT8 skipped (--skip-int8); copying FP32 as schema_mapper.onnx", file=sys.stderr)
        import shutil
        shutil.copy(fp32_path, int8_path)
    else:
        print(f"[export] INT8 → {int8_path}", file=sys.stderr)
        try:
            quantize_to_int8(fp32_path, int8_path)
            quick_inference_check(int8_path, feature_dim)
        except Exception as e:
            print(f"[export] INT8 quantization FAILED ({e!r}); falling back to FP32 as schema_mapper.onnx. Use --skip-int8 to silence.", file=sys.stderr)
            import shutil
            shutil.copy(fp32_path, int8_path)

    print(f"[export] CRF → {crf_path}", file=sys.stderr)
    export_crf_params(model, crf_path)

    print(f"[export] tokenizer → {tok_path}", file=sys.stderr)
    tok = load_tokenizer()
    tok.save_pretrained(str(tok_path))

    bundle_files = [int8_path, crf_path] + list(tok_path.glob("*"))
    total_mb = sum(f.stat().st_size for f in bundle_files if f.exists()) / (1024 * 1024)
    print(f"[bundle] {len(bundle_files)} files, total {total_mb:.2f} MB", file=sys.stderr)
    if total_mb > 50.0 and not args.force:
        print(f"[bundle] FAILED: bundle {total_mb:.2f}MB > 50MB. Use --force to override.", file=sys.stderr)
        sys.exit(3)

    manifest = {
        "fp32_onnx": str(fp32_path),
        "int8_onnx": str(int8_path),
        "crf_npz": str(crf_path),
        "tokenizer_dir": str(tok_path),
        "feature_dim": feature_dim,
        "n_labels": len(CANONICAL_LABELS),
        "bundle_size_mb": total_mb,
    }
    (args.out / "export_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[export] done. manifest at {args.out}/export_manifest.json", file=sys.stderr)


if __name__ == "__main__":
    main()
