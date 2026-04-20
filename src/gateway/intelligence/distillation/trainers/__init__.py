"""ONNX distillation trainers.

One module per ONNX model. Each trainer takes a
`(X, y, version, candidates_dir)` input and produces a candidate `.onnx`
file at `candidates_dir / f"{model_name}-{version}.onnx"` plus an
adjacent `{model_name}-{version}-calibration.json` with per-class
statistics.

The actual heavy lifting (sklearn fit, skl2onnx convert) is imported
lazily so the runtime path never pays for it when distillation is
idle.
"""
