import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import (  # noqa: E402
    CANONICAL_LABELS,
    COOCCUR_BIAS,
    CRF_FORBIDDEN_TRANSITIONS,
    EXCLUSIVE_GROUPS,
    ID_TO_LABEL,
    LABEL_DESCRIPTIONS,
    LABEL_TO_ID,
)


def test_label_count():
    assert len(CANONICAL_LABELS) == 19
    assert "UNKNOWN" in CANONICAL_LABELS
    assert "content" in CANONICAL_LABELS
    assert "tool_call_arguments" in CANONICAL_LABELS


def test_label_round_trip():
    for label in CANONICAL_LABELS:
        assert ID_TO_LABEL[LABEL_TO_ID[label]] == label
    assert len(LABEL_TO_ID) == len(CANONICAL_LABELS)
    assert len(ID_TO_LABEL) == len(CANONICAL_LABELS)


def test_exclusive_groups_are_subsets():
    for group_name, members in EXCLUSIVE_GROUPS.items():
        assert all(m in CANONICAL_LABELS for m in members), f"unknown label in {group_name}"


def test_descriptions_cover_all_labels():
    assert set(LABEL_DESCRIPTIONS.keys()) == set(CANONICAL_LABELS)


def test_forbidden_transitions_use_known_labels():
    for a, b in CRF_FORBIDDEN_TRANSITIONS:
        assert a in CANONICAL_LABELS
        assert b in CANONICAL_LABELS


def test_cooccur_bias_use_known_labels_and_floats():
    for a, b, w in COOCCUR_BIAS:
        assert a in CANONICAL_LABELS
        assert b in CANONICAL_LABELS
        assert isinstance(w, float)
