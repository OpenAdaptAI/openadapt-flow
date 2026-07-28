"""Vision utilities: template matching, OCR, perceptual hashing, settling.

Public API (see DESIGN.md "Vision API"):

- :class:`Match`, :func:`find_template`, :func:`find_structural_template`
- :class:`OcrLine`, :func:`ocr`, :func:`find_text`,
  :func:`find_text_candidates`, :func:`text_present`, :func:`upscale_png`
- :func:`phash_png`, :func:`phash_distance`
- :func:`pixels_changed`
- :func:`wait_settled`, :func:`wait_settled_result`, :class:`SettleResult`
"""

from typing import Any

from openadapt_flow.vision.hashing import phash_distance, phash_png
from openadapt_flow.vision.match import (
    Match,
    find_structural_template,
    find_template,
    pixels_changed,
)
from openadapt_flow.vision.ocr import (
    AmbiguousOcrMatchError,
    ContradictoryOcrEvidenceError,
    OcrLine,
    OcrResolutionRefused,
    find_text,
    find_text_candidates,
    ocr,
    text_present,
    upscale_png,
)
from openadapt_flow.vision.settle import (
    SettleResult,
    wait_settled,
    wait_settled_result,
)

_PROGRAM_PREDICATE_RUNTIME_STATE = {
    "preprocess_op",
    "session",
}


def _contract_state(value: Any, *, depth: int = 0) -> Any:
    """Return stable behavior-affecting state for the built-in OCR engine."""

    if value is None or isinstance(value, (bool, float, int, str)):
        return value
    if depth >= 3:
        return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}
    if isinstance(value, (list, tuple)):
        return [_contract_state(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _contract_state(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    try:
        attributes = vars(value)
    except TypeError:
        attributes = {}
    state = {
        str(key): _contract_state(item, depth=depth + 1)
        for key, item in sorted(attributes.items())
        if not str(key).startswith("_")
        and str(key) not in _PROGRAM_PREDICATE_RUNTIME_STATE
    }
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "state": state,
    }


def program_predicate_contract() -> dict[str, Any]:
    """Describe the exact active built-in visual-predicate configuration."""

    from rapidocr_onnxruntime import RapidOCR

    from openadapt_flow.vision import ocr as _ocr_callable

    ocr_module = __import__(_ocr_callable.__module__, fromlist=["_engine"])
    engine = getattr(ocr_module, "_engine", None)
    if engine is None:
        # Contract the default lazy engine before the first OCR call. This
        # produces the same semantic configuration before and after lazy
        # initialization, while the dependency artifact digest binds its exact
        # models and native runtime.
        engine = RapidOCR()
    return {
        "ocr_backend": "rapidocr-onnxruntime",
        "ocr_engine": _contract_state(engine),
    }


__all__ = [
    "AmbiguousOcrMatchError",
    "ContradictoryOcrEvidenceError",
    "Match",
    "OcrLine",
    "OcrResolutionRefused",
    "SettleResult",
    "find_structural_template",
    "find_template",
    "find_text",
    "find_text_candidates",
    "ocr",
    "phash_distance",
    "phash_png",
    "pixels_changed",
    "program_predicate_contract",
    "text_present",
    "upscale_png",
    "wait_settled",
    "wait_settled_result",
]
