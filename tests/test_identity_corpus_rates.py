"""Regression net: identity matcher error rates on the FROZEN corpora.

This is the false-negative-RATE guard the sibling reopening demanded
(third wrong-patient reopening; docs/validation/VALIDATION.md): instead
of pinning only the adversaries that found the last bug, the matcher is
held to measured rates on the full held-out corpora — v1 (4360 pairs,
frozen 2026-07-10 before the rebuild) AND v2 (2240 pairs, frozen
2026-07-10 before the out-of-corpus redesign; the classes v1 excluded
by construction — see tests/test_adversary_corpus_v2.py).

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

# Measured at the v1+v2 ROC-chosen operating point (IDENTITY_ROC.md):
# v1 false aborts 21.2% overall after the out-of-corpus redesign —
# concentrated in occlusion (93%: bands with dropped tokens; in the
# 2026-07-10 recount ~half of those still had both name tokens readable
# and aborted on trailing DOB/MRN loss — an availability cost, stated
# plainly, NOT an epistemic-virtue framing), letter-letter confusion
# noise (~33%, the indistinguishable class where abort is correct for
# both readings), compound noise (38%), and capitalized adjacent-row
# bleed (26%, the price of the unexplained-name budget that closes the
# observed-superset blocker). Budget set with headroom for genuinely
# neutral refactors.
V1_FALSE_ABORT_BUDGET = 0.23

# v2 same_entity classes (digit-class noise, lowercase bleed, hyphenated
# splits) measure 0.0%; small headroom only.
V2_FALSE_ABORT_BUDGET = 0.02


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
