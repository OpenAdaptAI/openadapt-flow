"""Regression net: identity matcher error rates on the FROZEN corpus.

This is the false-negative-RATE guard the sibling reopening demanded
(third wrong-patient reopening; docs/validation/VALIDATION.md): instead
of pinning only the adversaries that found the last bug, the matcher is
held to measured rates on the full held-out corpus (4360 pairs, frozen
before the fix — tests/test_adversary_corpus.py pins the freeze).

If the false-accept assertion fails, a wrong-entity band verifies again:
that is a P0, not a threshold to renegotiate. If the false-abort budget
fails, the matcher got stricter than the documented availability cost —
update docs/validation/IDENTITY_ROC.md (regenerate it) and LIMITS.md in
the same change.
"""

from __future__ import annotations

from openadapt_flow.runtime.identity import verify_target_identity
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    generate_corpus,
)

# Measured at the ROC-chosen operating point (see IDENTITY_ROC.md):
# false abort 10.69% overall, dominated by the occlusion category (90% —
# bands whose identity tokens were not read at all; refusing those is
# correct). Budget set with headroom for genuinely neutral refactors.
FALSE_ABORT_BUDGET = 0.12


def test_zero_false_accepts_on_frozen_corpus():
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


def test_false_abort_rate_within_documented_budget():
    pairs = [p for p in generate_corpus() if p.label == LABEL_SAME]
    aborted = sum(
        verify_target_identity(p.recorded, p.observed).status != "verified"
        for p in pairs
    )
    rate = aborted / len(pairs)
    assert rate <= FALSE_ABORT_BUDGET, (
        f"false-abort rate {rate:.2%} exceeds the documented "
        f"{FALSE_ABORT_BUDGET:.0%} budget"
    )
