"""Regression test: every data file the gateway loads at runtime must
ship inside the installed wheel.

Without this guard, dropping a new ONNX model / label JSON / sanity
fixture into `src/gateway/...` produces a code-side change that boots
fine in editable install (`pip install -e .`) but fails the moment the
gateway runs from a wheel-based image — exactly the silent footgun
that left the safety classifier broken on prod for weeks (TF-IDF
vocab, IDF, SVD, labels all missing from the deployed image).

If you're adding a new data file, also add a matching glob to
`pyproject.toml` `[tool.setuptools.package-data]` and extend the list
below.
"""
from __future__ import annotations

from importlib import resources

import pytest


# (package_name, filename) — flat list keeps failures easy to read.
EXPECTED_DATA_FILES: list[tuple[str, str]] = [
    # Safety classifier — 200-d TF-IDF + SVD pipeline feeding the ONNX model.
    ("gateway.content", "safety_classifier.onnx"),
    ("gateway.content", "safety_classifier_labels.json"),
    ("gateway.content", "safety_tfidf_vocab.json"),
    ("gateway.content", "safety_tfidf_idf.npy"),
    ("gateway.content", "safety_tfidf_config.json"),
    ("gateway.content", "safety_svd_components.npy"),
    # Schema mapper — 139-d ONNX + canonical label list.
    ("gateway.schema", "schema_mapper.onnx"),
    ("gateway.schema", "schema_mapper_labels.json"),
    # Intent classifier — ONNX + labels + threshold params.
    ("gateway.classifier", "model.onnx"),
    ("gateway.classifier", "model_labels.json"),
    ("gateway.classifier", "model_params.json"),
    # Sanity test fixtures used by the pre-promotion gate.
    ("gateway.intelligence.sanity_tests", "schema_mapper_sanity.json"),
    ("gateway.intelligence.sanity_tests", "intent_sanity.json"),
    ("gateway.intelligence.sanity_tests", "safety_sanity.json"),
    # Compliance / policy templates the control plane loads on init.
    ("gateway.control.templates", "hipaa_baseline.json"),
    ("gateway.control.templates", "owasp_llm_top10.json"),
    ("gateway.control.templates", "eu_ai_act_baseline.json"),
    ("gateway.control.templates", "soc2_baseline.json"),
]


@pytest.mark.parametrize("package,filename", EXPECTED_DATA_FILES)
def test_data_file_packaged(package: str, filename: str) -> None:
    """The file must be reachable via importlib.resources.

    `importlib.resources.files()` works against both editable installs
    and wheel installs — when this passes locally and in CI (which
    installs from `pip install -e ".[dev]"`), the wheel itself will
    also contain the file because the same `package-data` glob feeds
    both paths.
    """
    pkg_files = resources.files(package)
    target = pkg_files / filename
    assert target.is_file(), (
        f"Data file missing from package: {package}/{filename}. "
        f"Add a matching glob to pyproject.toml [tool.setuptools.package-data]."
    )
