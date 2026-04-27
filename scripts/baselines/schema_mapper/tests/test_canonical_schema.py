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
    assert len(CANONICAL_LABELS) == 20
    assert "UNKNOWN" in CANONICAL_LABELS
    assert "content" in CANONICAL_LABELS
    assert "tool_call_arguments" in CANONICAL_LABELS
    assert "response_timestamp" in CANONICAL_LABELS


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


def test_primary_content_group_removed():
    """Regression: ('content', 'thinking_content') was REMOVED from
    EXCLUSIVE_GROUPS because DeepSeek-Reasoner / xAI Grok 4 return both
    under the same parent `choices[0].message`. They are co-occurring,
    not exclusive."""
    for group_name, members in EXCLUSIVE_GROUPS.items():
        members_set = set(members)
        assert not (
            "content" in members_set and "thinking_content" in members_set
        ), (
            f"EXCLUSIVE_GROUPS[{group_name!r}] re-introduced (content, thinking_content) — "
            "they co-occur on reasoning models, see deepseek.json example[1]."
        )


def test_content_thinking_cooccur_bias_present():
    """The pair lives in COOCCUR_BIAS instead of EXCLUSIVE_GROUPS."""
    pairs = {(a, b) for a, b, _ in COOCCUR_BIAS}
    assert ("content", "thinking_content") in pairs or (
        "thinking_content",
        "content",
    ) in pairs
