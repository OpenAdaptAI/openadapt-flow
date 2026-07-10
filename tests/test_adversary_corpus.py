"""Freeze tests for the adversarial identity corpus.

The corpus is a held-out evaluation set for the identity band matcher:
the generator, seed, and the committed hash manifest were frozen BEFORE
the matcher was evaluated or changed (see the module docstring and
docs/validation/VALIDATION.md). If any of these tests fail, the corpus
moved after freezing — a change that must be explicitly disclosed and
justified as a generator BUG fix, never as tuning.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.runtime.identity import squash
from openadapt_flow.validation.adversary_corpus import (
    FROZEN_SEED,
    LABEL_DIFFERENT,
    LABEL_SAME,
    build_manifest,
    corpus_canonical,
    generate_corpus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "docs/validation/adversary_corpus_manifest.json"


def test_corpus_matches_frozen_manifest():
    """The regenerated corpus must hash to the committed manifest —
    post-hoc tuning of the generator against the matcher is detectable
    here."""
    committed = json.loads(MANIFEST_PATH.read_text())
    manifest = build_manifest(generate_corpus())
    assert manifest["seed"] == FROZEN_SEED == committed["seed"]
    assert manifest["sha256"] == committed["sha256"]
    assert manifest["counts"] == committed["counts"]
    assert manifest["n_total"] == committed["n_total"]


def test_corpus_is_deterministic():
    assert generate_corpus() == generate_corpus()


def test_corpus_scale_and_labels():
    """>= 2000 pairs per class, valid labels, non-degenerate bands."""
    pairs = generate_corpus()
    same = [p for p in pairs if p.label == LABEL_SAME]
    different = [p for p in pairs if p.label == LABEL_DIFFERENT]
    assert len(same) >= 2000
    assert len(different) >= 2000
    assert len(same) + len(different) == len(pairs)
    for p in pairs:
        # Recorded bands are always armable (>= MIN_CONTEXT_CHARS): the
        # corpus evaluates the matcher, not the arming floor.
        assert len(squash(p.recorded)) >= 12, p
        assert p.observed.strip(), p


def test_different_entity_pairs_are_not_ocr_equivalent():
    """No different_entity pair may be a plausible OCR misread of its own
    recorded band under the corpus's frozen confusion model — that would
    be a mislabeled pair, unwinnable for any matcher."""
    for p in generate_corpus():
        if p.label != LABEL_DIFFERENT:
            continue
        assert corpus_canonical(squash(p.recorded)) != corpus_canonical(
            squash(p.observed)
        ), p
