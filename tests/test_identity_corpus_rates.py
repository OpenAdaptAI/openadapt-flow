"""Regression net: identity matcher error rates on the FROZEN corpora.

This is the false-negative-RATE guard the sibling reopening demanded
(third wrong-patient reopening; docs/validation/VALIDATION.md): instead
of pinning only the adversaries that found the last bug, the matcher is
held to measured rates on the full held-out corpora — v1 (4360 pairs),
v2 (2240 pairs, the classes v1 excluded by construction) and v3 (300
pairs, identifier letter/digit collisions — the 5th-reopening class v1's
digit-only mrn_digit_swap could not surface).

If a false-accept assertion fails, a wrong-entity band verifies again:
that is a P0, not a threshold to renegotiate. If the false-abort budget
fails, the matcher got stricter than the documented availability cost —
update docs/validation/IDENTITY_ROC.md (regenerate it) and LIMITS.md in
the same change.

v2's INDISTINGUISHABLE label (the true row misread by a letter-letter
confusion, textually identical to a real sibling): abort is the correct
outcome for both readings, so a VERIFY there counts as a false accept
and an abort is a justified abort, never a false abort.
"""

from __future__ import annotations

from openadapt_flow.runtime.identity import verify_target_identity
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    generate_corpus,
)
from openadapt_flow.validation.adversary_corpus_v2 import (
    LABEL_INDISTINGUISHABLE,
    generate_corpus_v2,
)
from openadapt_flow.validation.adversary_corpus_v3 import generate_corpus_v3

# Measured at the ROC-chosen operating point (IDENTITY_ROC.md), AFTER the
# 5th-reopening identifier-suspect fix:
# v1 false aborts 28.2% — occlusion (93%), and the digit-class noise
# classes ocr_confusion (66%) and compound_noise (68%) ROSE from 33/38%
# because digit-class OCR noise that lands on an identifier token (DOB,
# MRN, phone) now aborts under the identifier-suspect rule (the true-row
# identifier-noise availability cost, disclosed in LIMITS.md), plus
# capitalized adjacent-row bleed (26%). Budget set with headroom for
# genuinely neutral refactors.
V1_FALSE_ABORT_BUDGET = 0.30

# v2 same_entity: digit_confusion_true_row ROSE from 0% to ~49% — the
# same identifier-noise cost (half those rows have an MRN the digit noise
# hit); lowercase bleed and hyphenated splits stay 0%.
V2_FALSE_ABORT_BUDGET = 0.18


def test_zero_false_accepts_on_frozen_corpus_v1():
    """No different_entity pair may EVER verify. Zero, not a rate."""
    offenders = [
        (p.category, p.recorded, p.observed)
        for p in generate_corpus()
        if p.label == LABEL_DIFFERENT
        and verify_target_identity(p.recorded, p.observed).status
        == "verified"
    ]
    assert not offenders, (
        f"{len(offenders)} wrong-entity bands VERIFIED — wrong-patient "
        f"P0 reopened. First offenders: {offenders[:5]}"
    )


def test_zero_false_accepts_on_frozen_corpus_v2():
    """Zero verifies across v2's different_entity AND indistinguishable
    labels (a verify on an indistinguishable pair is a false accept for
    its textually identical different-entity twin)."""
    offenders = [
        (p.label, p.category, p.recorded, p.observed)
        for p in generate_corpus_v2()
        if p.label in (LABEL_DIFFERENT, LABEL_INDISTINGUISHABLE)
        and verify_target_identity(p.recorded, p.observed).status
        == "verified"
    ]
    assert not offenders, (
        f"{len(offenders)} wrong-entity/indistinguishable bands VERIFIED "
        f"— out-of-corpus P0 reopened. First offenders: {offenders[:5]}"
    )


def test_zero_false_accepts_on_frozen_corpus_v3():
    """No identifier letter/digit collision (a DIFFERENT patient's
    MRN/account number one confusable char apart) may EVER verify — the
    5th wrong-patient reopening. Zero, not a rate."""
    offenders = [
        (p.recorded, p.observed)
        for p in generate_corpus_v3()
        if p.label == LABEL_DIFFERENT
        and verify_target_identity(p.recorded, p.observed).status
        == "verified"
    ]
    assert not offenders, (
        f"{len(offenders)} identifier-collision bands VERIFIED — "
        f"5th wrong-patient P0 reopened. First offenders: {offenders[:5]}"
    )


def test_v1_false_abort_rate_within_documented_budget():
    pairs = [p for p in generate_corpus() if p.label == LABEL_SAME]
    aborted = sum(
        verify_target_identity(p.recorded, p.observed).status != "verified"
        for p in pairs
    )
    rate = aborted / len(pairs)
    assert rate <= V1_FALSE_ABORT_BUDGET, (
        f"v1 false-abort rate {rate:.2%} exceeds the documented "
        f"{V1_FALSE_ABORT_BUDGET:.0%} budget"
    )


def test_v2_false_abort_rate_within_documented_budget():
    pairs = [p for p in generate_corpus_v2() if p.label == LABEL_SAME]
    aborted = sum(
        verify_target_identity(p.recorded, p.observed).status != "verified"
        for p in pairs
    )
    rate = aborted / len(pairs)
    assert rate <= V2_FALSE_ABORT_BUDGET, (
        f"v2 false-abort rate {rate:.2%} exceeds the documented "
        f"{V2_FALSE_ABORT_BUDGET:.0%} budget"
    )
