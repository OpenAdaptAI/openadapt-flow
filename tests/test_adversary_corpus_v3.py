"""Freeze tests for adversarial corpus v3 (identifier letter/digit).

Same discipline as v1/v2: the v3 generator, seed, and committed hash
manifest are frozen BEFORE the identifier-suspect matcher change is
evaluated on the corpus. v1 and v2 stay byte-identical (their own freeze
tests pin them).
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.runtime.identity import ocr_canonical, squash, tokenize
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    build_manifest,
)
from openadapt_flow.validation.adversary_corpus_v3 import (
    FROZEN_SEED_V3,
    generate_corpus_v3,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = (
    REPO_ROOT / "docs/validation/adversary_corpus_v3_manifest.json"
)


def test_corpus_v3_matches_frozen_manifest():
    committed = json.loads(MANIFEST_PATH.read_text())
    manifest = build_manifest(generate_corpus_v3(), seed=FROZEN_SEED_V3)
    assert manifest["seed"] == FROZEN_SEED_V3 == committed["seed"]
    assert manifest["sha256"] == committed["sha256"]
    assert manifest["counts"] == committed["counts"]
    assert manifest["n_total"] == committed["n_total"]


def test_corpus_v3_is_deterministic():
    assert generate_corpus_v3() == generate_corpus_v3()


def test_corpus_v3_scale_and_labels():
    pairs = generate_corpus_v3()
    assert len(pairs) >= 300
    for p in pairs:
        assert p.label == LABEL_DIFFERENT
        assert p.category == "id_letter_digit_collision"
        assert len(squash(p.recorded)) >= 12, p
        assert p.observed.strip(), p


def test_v3_pairs_are_confusion_equivalent_and_id_only_differ():
    """Recorded and observed bands must canonicalize equal (the whole
    point — the ONLY difference is a letter/digit-confusable char inside
    the identifier), yet be raw-unequal, and the differing token must
    contain a digit (an identifier, not a name)."""
    for p in generate_corpus_v3():
        assert squash(p.recorded) != squash(p.observed), p
        assert ocr_canonical(squash(p.recorded)) == ocr_canonical(
            squash(p.observed)
        ), p
        rec = tokenize(p.recorded)
        obs = tokenize(p.observed)
        assert len(rec) == len(obs), p
        differing = [
            (a, b) for a, b in zip(rec, obs) if a != b
        ]
        # exactly the identifier token differs, and it contains a digit
        assert len(differing) == 1, p
        a, b = differing[0]
        assert any(c.isdigit() for c in a), p
        assert ocr_canonical(a) == ocr_canonical(b), p
