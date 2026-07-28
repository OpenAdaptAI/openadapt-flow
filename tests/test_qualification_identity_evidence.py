"""Exact retained identity-policy proof for qualification evidence."""

from __future__ import annotations

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    IdentityCheck,
    IdentitySignalEvidence,
    Step,
)
from openadapt_flow.qualification import (
    IdentityEnforcement,
    IdentityEvidenceSource,
    IdentityMatchMode,
    IdentityNormalizer,
    IdentityPolicy,
    IdentitySignalKey,
    IdentitySignalPolicy,
)
from openadapt_flow.qualification_identity_evidence import (
    qualification_identity_evidence_error,
)


def _step() -> Step:
    return Step(
        id="save",
        intent="Save",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/save.png",
            region=(0, 0, 100, 20),
            click_point=(50, 10),
            structured_identity="Record: A123",
            context_text="DOB: 2000-01-01",
        ),
    )


def _canonical_policy() -> IdentityPolicy:
    return IdentityPolicy(
        step_id="save",
        enforcement=IdentityEnforcement.CANONICAL_LADDER,
    )


def _quorum_policy() -> IdentityPolicy:
    return IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            ),
            IdentitySignalPolicy(
                key=IdentitySignalKey.SECONDARY_IDENTIFIER,
                source=IdentityEvidenceSource.CAPTURED_CONTEXT,
                match=IdentityMatchMode.NORMALIZED,
                normalizers=[IdentityNormalizer.CASEFOLD],
                region=(0, 0, 100, 20),
                extract_pattern=r"DOB: (?P<value>[0-9/-]+)",
            ),
        ],
        quorum=1,
    )


def _quorum_check() -> IdentityCheck:
    return IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=0.5,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="record_id",
                source="structured",
                verdict="verified",
                evidence_class="application_structured_text",
                match="exact",
            ),
            IdentitySignalEvidence(
                signal="secondary_identifier",
                source="captured_context",
                verdict="unverifiable",
                evidence_class="captured_context_ocr",
                match="normalized",
            ),
        ],
        quorum_required=1,
        quorum_verified=1,
    )


def test_canonical_identity_requires_retained_ladder_comparison() -> None:
    valid = IdentityCheck(
        status="verified",
        mode="structured",
        coverage=1.0,
        expected="retained identity",
        observed="live identity",
    )
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=_step(),
            actuation_path="gui",
        )
        is None
    )
    assert qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=valid.model_copy(update={"observed": ""}),
        step=_step(),
        actuation_path="gui",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"quorum_required": 2},
        {"quorum_verified": 2},
        {"coverage": 1.0},
        {"mode": "structured"},
        {
            "signal_evidence": [
                IdentitySignalEvidence(
                    signal="record_id",
                    source="structured",
                    verdict="verified",
                    evidence_class="application_structured_text",
                    match="exact",
                )
            ]
        },
        {
            "signal_evidence": [
                IdentitySignalEvidence(
                    signal="record_id",
                    source="structured",
                    verdict="verified",
                    evidence_class="application_structured_text",
                    match="exact",
                ),
                IdentitySignalEvidence(
                    signal="secondary_identifier",
                    source="captured_context",
                    verdict="unverifiable",
                    evidence_class="captured_context_ocr",
                    match="exact",
                ),
            ]
        },
    ],
)
def test_signal_quorum_refuses_forged_policy_shape(
    mutation: dict[str, object],
) -> None:
    assert qualification_identity_evidence_error(
        policy=_quorum_policy(),
        check=_quorum_check().model_copy(update=mutation),
        step=_step(),
        actuation_path="gui",
    )


def test_signal_quorum_accepts_exact_policy_with_one_unreadable_signal() -> None:
    assert (
        qualification_identity_evidence_error(
            policy=_quorum_policy(),
            check=_quorum_check(),
            step=_step(),
            actuation_path="gui",
        )
        is None
    )


def test_api_identity_requires_exact_policy_signal_set() -> None:
    step = _step()
    step.api_binding = ApiBinding(
        url_template="/records/{record_id}",
        identity=[
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/url/record_id"],
            )
        ],
    )
    policy = IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            )
        ],
        quorum=1,
    )
    check = IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=1.0,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="record_id",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            )
        ],
        quorum_required=1,
        quorum_verified=1,
    )
    assert (
        qualification_identity_evidence_error(
            policy=policy,
            check=check,
            step=step,
            actuation_path="api",
        )
        is None
    )
    wrong_key = check.model_copy(deep=True)
    wrong_key.signal_evidence[0].signal = "subject_name"
    assert qualification_identity_evidence_error(
        policy=policy,
        check=wrong_key,
        step=step,
        actuation_path="api",
    )


def test_api_identity_refuses_reordered_policy_bindings() -> None:
    step = _step()
    step.api_binding = ApiBinding(
        url_template="/records/{record_id}/{secondary_id}",
        identity=[
            ApiIdentityBinding(
                key="secondary_identifier",
                param="secondary_id",
                effect_field="secondary_id",
                request_pointers=["/url/secondary_id"],
            ),
            ApiIdentityBinding(
                key="record_id",
                param="record_id",
                effect_field="record_id",
                request_pointers=["/url/record_id"],
            ),
        ],
    )
    policy = IdentityPolicy(
        step_id="save",
        signals=[
            IdentitySignalPolicy(
                key=IdentitySignalKey.RECORD_ID,
                source=IdentityEvidenceSource.STRUCTURED,
                extract_pattern=r"Record: (?P<value>[A-Z0-9]+)",
            ),
            IdentitySignalPolicy(
                key=IdentitySignalKey.SECONDARY_IDENTIFIER,
                source=IdentityEvidenceSource.CAPTURED_CONTEXT,
                region=(0, 0, 100, 20),
                extract_pattern=r"DOB: (?P<value>[0-9/-]+)",
            ),
        ],
        quorum=2,
    )
    check = IdentityCheck(
        status="verified",
        mode="signal_quorum",
        coverage=1.0,
        signal_evidence=[
            IdentitySignalEvidence(
                signal="secondary_identifier",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            ),
            IdentitySignalEvidence(
                signal="record_id",
                source="api_parameter",
                verdict="verified",
                evidence_class="api_request_effect_binding",
                match="exact",
            ),
        ],
        quorum_required=2,
        quorum_verified=2,
    )

    assert qualification_identity_evidence_error(
        policy=policy,
        check=check,
        step=step,
        actuation_path="api",
    )
