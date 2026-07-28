"""Shared deterministic evaluator for workflow-program transition guards.

The runtime and the outcome classifier must apply the same predicate
semantics.  Keeping the implementation here prevents a report verifier from
accepting a claimed visual verdict that the runtime contract would not
produce.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

from openadapt_flow.ir import Predicate, PredicateKind
from openadapt_flow.runtime.resolver import resolve

PROGRAM_PREDICATE_EVALUATOR_ID = "openadapt.program-predicate-evaluator/v1"
PROGRAM_PREDICATE_EVALUATOR_SHA256 = hashlib.sha256(
    PROGRAM_PREDICATE_EVALUATOR_ID.encode("utf-8")
).hexdigest()


def predicate_template_refs(predicate: Predicate | None) -> tuple[str, ...]:
    """Return the sorted unique template references read by ``predicate``."""

    if predicate is None:
        return ()
    refs: set[str] = set()
    if (
        predicate.kind is PredicateKind.ANCHOR_RESOLVES
        and predicate.anchor is not None
        and predicate.anchor.template
    ):
        refs.add(predicate.anchor.template)
    for operand in predicate.operands:
        refs.update(predicate_template_refs(operand))
    return tuple(sorted(refs))


def predicate_uses_frame(predicate: Predicate | None) -> bool:
    """Return whether ``predicate`` reads the retained application frame."""

    if predicate is None:
        return False
    if predicate.kind in {
        PredicateKind.ANCHOR_RESOLVES,
        PredicateKind.TEXT_PRESENT,
        PredicateKind.TEXT_ABSENT,
    }:
        return True
    return any(predicate_uses_frame(operand) for operand in predicate.operands)


def evaluate_program_predicate(
    predicate: Predicate,
    frame_png: bytes,
    params: Mapping[str, str],
    *,
    vision: Any,
    viewport: tuple[int, int] | None,
    asset_loader: Callable[[str], bytes | None],
) -> bool:
    """Evaluate one predicate with the canonical model-free contract.

    ``asset_loader`` is the only bundle dependency.  During execution it reads
    the admitted bundle.  During later verification it reads hash-bound copies
    retained with the exact transition frame.
    """

    kind = predicate.kind
    if kind is PredicateKind.ANCHOR_RESOLVES:
        if predicate.anchor is None:
            return False
        template_png = (
            asset_loader(predicate.anchor.template)
            if predicate.anchor.template
            else None
        )
        return (
            resolve(
                predicate.anchor,
                frame_png,
                vision,
                None,
                predicate.intent or predicate.anchor.ocr_text or "",
                template_png=template_png,
                viewport=viewport,
            )
            is not None
        )
    if kind is PredicateKind.TEXT_PRESENT:
        return bool(predicate.text) and vision.text_present(frame_png, predicate.text)
    if kind is PredicateKind.TEXT_ABSENT:
        return not (predicate.text and vision.text_present(frame_png, predicate.text))
    if kind is PredicateKind.PARAM_EQUALS:
        return predicate.param is not None and str(params.get(predicate.param)) == str(
            predicate.value
        )
    if kind is PredicateKind.AND:
        return all(
            evaluate_program_predicate(
                operand,
                frame_png,
                params,
                vision=vision,
                viewport=viewport,
                asset_loader=asset_loader,
            )
            for operand in predicate.operands
        )
    if kind is PredicateKind.OR:
        return any(
            evaluate_program_predicate(
                operand,
                frame_png,
                params,
                vision=vision,
                viewport=viewport,
                asset_loader=asset_loader,
            )
            for operand in predicate.operands
        )
    if kind is PredicateKind.NOT:
        return bool(predicate.operands) and not evaluate_program_predicate(
            predicate.operands[0],
            frame_png,
            params,
            vision=vision,
            viewport=viewport,
            asset_loader=asset_loader,
        )
    return False
