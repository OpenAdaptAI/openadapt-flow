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
from openadapt_flow.runtime.identity import (
    verify_structured_identity,
    verify_target_identity,
)
from openadapt_flow.runtime.identity_template import (
    build_identity_template,
    verify_structured_template,
    verify_template_identity,
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


def test_canonical_identity_requires_exact_runtime_structured_comparison() -> None:
    valid = verify_structured_identity("Record: A123", " record:   a123 ")
    assert valid is not None
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=_step(),
            actuation_path="gui",
        )
        is None
    )

    for mutation in (
        {"expected": "Other record"},
        {"observed": "Other record"},
        {"coverage": 0.01},
    ):
        assert qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid.model_copy(update=mutation),
            step=_step(),
            actuation_path="gui",
        )


def test_canonical_identity_accepts_exact_runtime_context_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = "Patient Alice Smith date March Seven"
    valid = verify_target_identity(
        step.anchor.context_text,
        "Patient Alice Smith date March Seven",
    )

    assert valid.mode == "context"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_accepts_exact_template_structured_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    template = build_identity_template(
        None,
        structured_identity=step.anchor.structured_identity,
    )
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    valid = verify_structured_template(template, " record:   a123 ")
    assert valid is not None

    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_accepts_exact_template_context_comparison() -> None:
    step = _step()
    assert step.anchor is not None
    context_text = "Patient Alice Smith date March Seven"
    template = build_identity_template(context_text)
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    valid = verify_template_identity(template, context_text)

    assert valid.mode == "context"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
    )


def test_canonical_identity_binds_exact_runtime_parameter() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = "Patient Alice Smith account ABCXYZ"
    recorded_params = {"record_id": "ABCXYZ"}
    runtime_params = {"record_id": "QWERTY"}
    valid = verify_target_identity(
        step.anchor.context_text,
        "Patient Alice Smith account QWERTY",
        params=runtime_params,
        param_examples=recorded_params,
    )

    assert valid.mode == "param"
    assert valid.param == "record_id"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
            runtime_params=runtime_params,
            recorded_params=recorded_params,
        )
        is None
    )
    assert qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=valid.model_copy(update={"param": "other_record"}),
        step=step,
        actuation_path="gui",
        runtime_params=runtime_params,
        recorded_params=recorded_params,
    )


def test_canonical_template_identity_binds_exact_runtime_parameter() -> None:
    step = _step()
    assert step.anchor is not None
    context_text = "Patient Alice Smith account ABCXYZ"
    recorded_params = {"record_id": "ABCXYZ"}
    runtime_params = {"record_id": "QWERTY"}
    template = build_identity_template(
        context_text,
        param_examples=recorded_params,
    )
    assert template is not None
    step.anchor.identity_template = template
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    valid = verify_template_identity(
        template,
        "Patient Alice Smith account QWERTY",
        params=runtime_params,
        param_examples=recorded_params,
    )

    assert valid.mode == "param"
    assert valid.param == "record_id"
    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
            runtime_params=runtime_params,
            recorded_params=recorded_params,
        )
        is None
    )
    assert qualification_identity_evidence_error(
        policy=_canonical_policy(),
        check=valid,
        step=step,
        actuation_path="gui",
        runtime_params={"other_record": "QWERTY"},
        recorded_params=recorded_params,
    )


def test_canonical_identity_accepts_exact_runtime_pixel_shape() -> None:
    step = _step()
    assert step.anchor is not None
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    step.anchor.identifier_crop = "templates/identifiers/save.png"
    step.anchor.identifier_region = (0, 0, 100, 20)
    valid = IdentityCheck(
        status="verified",
        mode="pixel",
        coverage=1.0,
        expected="recorded identifier crop",
        observed=("live identifier crop matches after alignment (worst window 0.040)"),
    )

    assert (
        qualification_identity_evidence_error(
            policy=_canonical_policy(),
            check=valid,
            step=step,
            actuation_path="gui",
        )
        is None
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
