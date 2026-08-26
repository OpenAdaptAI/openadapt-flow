"""Fail-closed resolution of hosted values from the sealed input schema."""

from __future__ import annotations

import math
from datetime import date

from openadapt_flow.ir import ParamKind, ParamSpec, Workflow
from openadapt_flow.runtime.authorization import effective_runtime_params


class AdmittedInputError(ValueError):
    """Hosted inputs do not fit the exact schema sealed into the workflow."""


def _validate_value(spec: ParamSpec, value: str) -> None:
    if spec.type is ParamKind.ENUM:
        if not spec.choices or value not in spec.choices:
            raise AdmittedInputError(
                f"parameter {spec.name!r} is outside its admitted enum"
            )
    elif spec.type is ParamKind.DATE:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise AdmittedInputError(
                f"parameter {spec.name!r} is not an ISO date"
            ) from exc
        if parsed.isoformat() != value:
            raise AdmittedInputError(
                f"parameter {spec.name!r} is not a canonical ISO date"
            )
    elif spec.type is ParamKind.NUMBER:
        try:
            number = float(value)
        except ValueError as exc:
            raise AdmittedInputError(
                f"parameter {spec.name!r} is not a number"
            ) from exc
        if not math.isfinite(number):
            raise AdmittedInputError(f"parameter {spec.name!r} must be a finite number")


def resolve_admitted_params(
    workflow: Workflow,
    supplied: dict[str, str],
    *,
    inline: bool,
) -> dict[str, str]:
    """Resolve exact hosted params without inventing or widening the schema.

    Hosted execution requires the sealed typed schema. Inline input can never
    carry a declared secret. Values obtained through a customer-local reference
    resolver may carry a secret, but they still pass the same exact name and
    type checks before the authorization digest is recomputed.
    """

    if not workflow.param_specs:
        raise AdmittedInputError(
            "hosted execution requires a sealed typed parameter schema"
        )
    if any(name != spec.name for name, spec in workflow.param_specs.items()):
        raise AdmittedInputError("the sealed parameter schema has a name mismatch")

    admitted_names = set(workflow.param_specs)
    unknown = sorted(set(supplied).difference(admitted_names))
    if unknown:
        raise AdmittedInputError(
            "hosted input contains parameter(s) outside the admitted schema: "
            + ", ".join(unknown)
        )
    if inline:
        inline_secrets = sorted(set(supplied).intersection(workflow.secret_params))
        if inline_secrets:
            raise AdmittedInputError(
                "inline hosted input contains declared secret parameter(s): "
                + ", ".join(inline_secrets)
            )

    resolved = effective_runtime_params(workflow, supplied)
    unknown_defaults = sorted(set(resolved).difference(admitted_names))
    if unknown_defaults:
        raise AdmittedInputError(
            "workflow defaults fall outside the admitted parameter schema"
        )
    for name, spec in sorted(workflow.param_specs.items()):
        value = resolved.get(name)
        if value is None:
            if spec.required:
                raise AdmittedInputError(f"required parameter {name!r} is missing")
            continue
        if not isinstance(value, str):
            raise AdmittedInputError(f"parameter {name!r} is not a string value")
        _validate_value(spec, value)
    return resolved
