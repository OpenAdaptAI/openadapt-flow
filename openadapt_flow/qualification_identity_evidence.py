"""Exact qualification checks for retained identity evidence.

The qualification project is already bound to the signed case report through
its contract digest.  This module checks the other half of that contract: the
retained :class:`IdentityCheck` must describe the exact policy that governed
the action.  A bare ``status="verified"`` is never sufficient.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Optional

from openadapt_flow.ir import IdentityCheck, Step

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.qualification import IdentityPolicy


_EVIDENCE_CLASS_BY_SOURCE = {
    "structured": "application_structured_text",
    "identifier_region": "recorded_and_live_region",
    "captured_context": "captured_context_ocr",
    "application": "application_identity",
    "session": "session_identity",
    "workflow_state": "workflow_state_identity",
    "api_parameter": "api_request_effect_binding",
}

_PIXEL_VERIFIED_OBSERVATION = re.compile(
    r"live identifier crop matches after alignment "
    r"\(worst window (?P<distance>0\.\d{3})\)"
)


def _quorum_shape_error(
    check: IdentityCheck,
    *,
    required: int,
    expected_signals: Sequence[tuple[str, str, str]],
) -> Optional[str]:
    """Validate one exact GUI signal-quorum result."""

    if check.mode != "signal_quorum":
        return "retained identity mode does not match signal_quorum policy"
    actual_signals = [
        (item.signal, item.source, item.match) for item in check.signal_evidence
    ]
    if actual_signals != expected_signals:
        return "retained identity signals do not match the exact qualified policy"
    if any(
        item.evidence_class != _EVIDENCE_CLASS_BY_SOURCE.get(item.source)
        for item in check.signal_evidence
    ):
        return "retained identity evidence class does not match its source"
    if any(item.verdict == "conflict" for item in check.signal_evidence):
        return "retained identity evidence contains a conflicting signal"
    verified = sum(item.verdict == "verified" for item in check.signal_evidence)
    if (
        check.quorum_required != required
        or check.quorum_verified != verified
        or verified < required
    ):
        return "retained identity quorum does not match the exact qualified policy"
    expected_coverage = verified / len(check.signal_evidence)
    if abs(check.coverage - expected_coverage) > 1e-9:
        return "retained identity coverage does not match its signal evidence"
    if check.expected or check.observed or check.param is not None:
        return "signal-quorum evidence contains an incompatible ladder payload"
    return None


def _canonical_ladder_error(
    check: IdentityCheck,
    *,
    step: Step,
    runtime_params: Mapping[str, str],
    recorded_params: Mapping[str, str],
) -> Optional[str]:
    """Validate a canonical-ladder result against one runtime-emittable shape."""

    if (
        check.signal_evidence
        or check.quorum_required is not None
        or check.quorum_verified is not None
    ):
        return "canonical ladder evidence contains an incompatible quorum payload"
    anchor = step.anchor
    if anchor is None:
        return "canonical ladder evidence belongs to an action without an anchor"

    if check.mode == "pixel":
        if not anchor.identifier_crop or anchor.identifier_region is None:
            return "pixel identity evidence has no retained identifier crop"
        match = _PIXEL_VERIFIED_OBSERVATION.fullmatch(check.observed)
        if (
            check.coverage != 1.0
            or check.expected != "recorded identifier crop"
            or check.param is not None
            or match is None
        ):
            return "pixel identity evidence is not an exact runtime verdict"
        from openadapt_flow.runtime.identity import PIXEL_VERIFY_MAX_WINDOW

        if float(match.group("distance")) > PIXEL_VERIFY_MAX_WINDOW:
            return "pixel identity evidence exceeds the runtime verification bound"
        return None

    try:
        expected: Optional[IdentityCheck]
        if check.mode == "structured":
            template = anchor.identity_template
            if template is not None and template.structured:
                from openadapt_flow.runtime.identity_template import (
                    verify_structured_template,
                )

                expected = verify_structured_template(
                    template,
                    check.observed,
                    params=dict(runtime_params),
                    param_examples=dict(recorded_params),
                )
            else:
                from openadapt_flow.runtime.identity import verify_structured_identity

                expected = verify_structured_identity(
                    anchor.structured_identity,
                    check.observed,
                )
        elif check.mode in {"context", "param"}:
            template = anchor.identity_template
            if template is not None and template.tokens:
                from openadapt_flow.runtime.identity_template import (
                    verify_template_identity,
                )

                expected = verify_template_identity(
                    template,
                    check.observed,
                    params=dict(runtime_params),
                    param_examples=dict(recorded_params),
                )
            elif anchor.context_text is not None:
                from openadapt_flow.runtime.identity import verify_target_identity

                expected = verify_target_identity(
                    anchor.context_text,
                    check.observed,
                    params=dict(runtime_params),
                    param_examples=dict(recorded_params),
                )
            else:
                expected = None
        else:
            return "retained identity mode is not a definitive canonical ladder result"
    except (TypeError, ValueError):
        return "canonical ladder evidence could not be reproduced"

    if expected is None or expected.status != "verified" or expected != check:
        return "canonical ladder evidence does not reproduce the runtime verdict"
    return None


def qualification_identity_evidence_error(
    *,
    policy: "IdentityPolicy",
    check: Optional[IdentityCheck],
    step: Step,
    actuation_path: str,
    runtime_params: Optional[Mapping[str, str]] = None,
    recorded_params: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Return why a representative action did not prove its exact identity policy.

    GUI quorum evidence must retain every configured signal, source, match mode,
    verdict, and the exact configured quorum.  Canonical-ladder evidence must
    retain the definitive ladder result rather than a status-only placeholder.
    API actuation uses the workflow's exact API identity bindings, but still
    requires the same qualified semantic signal set and quorum.
    """

    if check is None or check.status != "verified":
        return "representative action lacks verified identity evidence"

    enforcement = policy.enforcement.value
    if actuation_path == "api":
        if enforcement != "signal_quorum":
            return "qualified API identity requires an explicit signal_quorum policy"
        binding = step.api_binding
        if binding is None or not binding.identity:
            return "qualified API action lacks exact request/effect identity bindings"
        policy_keys = [signal.key.value for signal in policy.signals]
        binding_keys = [item.key for item in binding.identity]
        if binding_keys != policy_keys:
            return "API identity bindings do not match the exact qualified signal set"
        expected = [(key, "api_parameter", "exact") for key in binding_keys]
        if any(
            item.evidence_class != "api_request_effect_binding"
            for item in check.signal_evidence
        ):
            return "retained API identity evidence class is invalid"
        if any(item.verdict != "verified" for item in check.signal_evidence):
            return "retained API identity evidence is not fully verified"
        return _quorum_shape_error(
            check,
            required=policy.quorum,
            expected_signals=expected,
        )

    if actuation_path != "gui":
        return "representative action uses an unknown identity actuation path"

    if enforcement == "signal_quorum":
        from openadapt_flow.qualification import (
            identity_policy_independence_errors,
            identity_signal_runtime_available,
        )

        if identity_policy_independence_errors(policy):
            return "qualified identity policy reuses a correlated signal"
        if any(
            not identity_signal_runtime_available(step, signal)
            for signal in policy.signals
        ):
            return "qualified identity signal is not executable for this action"
        expected = [
            (signal.key.value, signal.source.value, signal.match.value)
            for signal in policy.signals
        ]
        return _quorum_shape_error(
            check,
            required=policy.quorum,
            expected_signals=expected,
        )

    if enforcement != "canonical_ladder":
        return "qualified identity policy uses an unknown enforcement mode"
    return _canonical_ladder_error(
        check,
        step=step,
        runtime_params=runtime_params or {},
        recorded_params=recorded_params or {},
    )
