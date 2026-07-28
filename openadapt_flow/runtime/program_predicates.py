"""Shared deterministic evaluator for workflow-program transition guards.

The runtime and the outcome classifier must apply the same predicate
semantics.  Keeping the implementation here prevents a report verifier from
accepting a claimed visual verdict that the runtime contract would not
produce.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import io
import json
import platform
from collections.abc import Hashable
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from PIL import Image

from openadapt_flow.ir import Predicate, PredicateKind
from openadapt_flow.runtime.resolver import resolve

PROGRAM_PREDICATE_EVALUATOR_ID = "openadapt.program-predicate-evaluator/v2"


def _source_sha256(value: Any) -> str:
    """Hash the installed implementation behind one evaluator dependency."""

    try:
        source = inspect.getsource(value).encode("utf-8")
    except (OSError, TypeError):
        module = inspect.getmodule(value)
        module_path = getattr(module, "__file__", None)
        if module_path is None:
            source = repr(value).encode("utf-8")
        else:
            source = Path(module_path).read_bytes()
    return hashlib.sha256(source).hexdigest()


@lru_cache(maxsize=32)
def _evaluator_contract_for_implementation(vision_type: Hashable) -> str:
    """Bind the verifier to exact code, runtime, and dependency versions.

    A version label alone does not identify executable predicate semantics.
    The retained contract therefore names the installed evaluator and resolver
    source, the concrete vision implementation, Python, and the image/OCR
    dependency versions used by that implementation.
    """

    versions: dict[str, str] = {}
    for distribution in ("numpy", "opencv-python", "pillow", "pytesseract"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "absent"
    payload = {
        "contract": PROGRAM_PREDICATE_EVALUATOR_ID,
        "python": platform.python_version(),
        "evaluator_module_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "evaluator_source_sha256": _source_sha256(evaluate_program_predicate),
        "resolver_source_sha256": _source_sha256(resolve),
        "vision_implementation": (
            f"{getattr(vision_type, '__module__', '')}."
            f"{getattr(vision_type, '__qualname__', getattr(vision_type, '__name__', ''))}"
        ),
        "vision_source_sha256": _source_sha256(vision_type),
        "dependencies": versions,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def program_predicate_evaluator_contract_sha256(vision: Any) -> str:
    """Return the cached exact contract for one vision implementation."""

    vision_type = vision if inspect.ismodule(vision) else type(vision)
    return _evaluator_contract_for_implementation(cast(Hashable, vision_type))


def exact_png_size(frame_png: bytes) -> tuple[int, int]:
    """Read the exact pixel dimensions bound by retained PNG bytes."""

    with Image.open(io.BytesIO(frame_png)) as image:
        image.verify()
        return int(image.width), int(image.height)


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
