"""Freeze tests for adversarial corpus v2.

Same discipline as v1 (tests/test_adversary_corpus.py): the v2
generator, seed, and committed hash manifest are frozen BEFORE the
redesigned matcher is evaluated on the corpus, so post-hoc tuning of
the corpus toward the matcher is detectable in git history. v1 stays
byte-identical (its own freeze test still pins it); v2 is a versioned
extension with its own seed and manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.runtime.identity import squash
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    build_manifest,
    corpus_canonical,
)
from openadapt_flow.validation.adversary_corpus_v2 import (
    FROZEN_SEED_V2,
    LABEL_INDISTINGUISHABLE,
    generate_corpus_v2,
    letter_confusion_variants,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = (
    REPO_ROOT / "docs/validation/adversary_corpus_v2_manifest.json"
)


def test_corpus_v2_matches_frozen_manifest():
    committed = json.loads(MANIFEST_PATH.read_text())
    manifest = build_manifest(generate_corpus_v2(), seed=FROZEN_SEED_V2)
    assert manifest["seed"] == FROZEN_SEED_V2 == committed["seed"]
    assert manifest["sha256"] == committed["sha256"]
    assert manifest["counts"] == committed["counts"]
    assert manifest["n_total"] == committed["n_total"]


def test_corpus_v2_is_deterministic():
    assert generate_corpus_v2() == generate_corpus_v2()


def test_corpus_v2_scale_and_labels():
    pairs = generate_corpus_v2()
    by_label: dict[str, int] = {}
    for p in pairs:
        by_label[p.label] = by_label.get(p.label, 0) + 1
        assert len(squash(p.recorded)) >= 12, p
        assert p.observed.strip(), p
    assert set(by_label) == {
        LABEL_DIFFERENT,
        LABEL_SAME,
        LABEL_INDISTINGUISHABLE,
    }
    assert by_label[LABEL_DIFFERENT] >= 1500
    assert by_label[LABEL_INDISTINGUISHABLE] >= 200
    assert by_label[LABEL_SAME] >= 400


def test_collision_pairs_are_confusion_equivalent_by_construction():
    """The collision classes exist BECAUSE v1 excluded confusion-
    equivalent pairs: here the recorded and observed bands (or at
    least the collided name inside them) MUST be canonical-equal for
    the ids-same and misread categories — that is the class."""
    for p in generate_corpus_v2():
        if p.category in (
            "confusion_collision_name_only",
            "confusion_collision_ids_same",
            "confusion_misread_true_row",
        ):
            assert corpus_canonical(squash(p.recorded)) == corpus_canonical(
                squash(p.observed)
            ), p


def test_letter_confusion_variants_are_systematic_and_sound():
    assert "Nell" in letter_confusion_variants("Neil")
    assert "Gall" in letter_confusion_variants("Gail")
    assert "Mamie" in letter_confusion_variants("Marnie")
    for name in ("Neil", "William", "Daniel"):
        for variant in letter_confusion_variants(name):
            assert variant.lower() != name.lower()
            assert variant.isalpha()
            assert corpus_canonical(variant) == corpus_canonical(name)


def test_v1_manifest_untouched():
    """v2 must not move v1: the v1 manifest on disk still carries the
    original frozen seed and hash (v1's own freeze test verifies the
    regeneration; this pins the file identity)."""
    v1 = json.loads(
        (REPO_ROOT / "docs/validation/adversary_corpus_manifest.json")
        .read_text()
    )
    assert v1["seed"] == 20260710
    assert v1["n_total"] == 4360
