"""OCR utilities backed by rapidocr-onnxruntime.

The RapidOCR engine is expensive to construct, so a single module-level
instance is created lazily on first use and reused for the process lifetime.
All returned coordinates are in *screen* (full-frame) pixel space, even when
a ``region`` restricts the OCR input.
"""

from __future__ import annotations

import difflib
import threading
from typing import Any, Optional

import cv2
import numpy as np
from pydantic import BaseModel

from openadapt_flow.ir import Region
from openadapt_flow.vision.match import Match, _clamp_region

_engine: Any = None
_engine_lock = threading.Lock()


def _get_engine() -> Any:
    """Return the process-wide RapidOCR engine, creating it on first use."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


class OcrLine(BaseModel):
    """One recognized text line.

    Attributes:
        text: Recognized text.
        region: Bounding box ``(x, y, w, h)`` in screen coordinates.
        confidence: Recognition confidence in ``[0, 1]``.
    """

    text: str
    region: Region
    confidence: float


def _decode_bgr(png: bytes) -> np.ndarray:
    """Decode PNG bytes to a BGR ``uint8`` image."""
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    return img


def ocr(screen_png: bytes, *, region: Region | None = None) -> list[OcrLine]:
    """Run OCR on a screenshot (optionally restricted to a region).

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        region: Optional ``(x, y, w, h)`` sub-region to OCR (clamped to the
            frame). Returned line regions are offset back into full-screen
            coordinates.

    Returns:
        Recognized lines in engine order; empty list if nothing was found.
    """
    img = _decode_bgr(screen_png)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region is not None:
        clamped = _clamp_region(region, w, h)
        if clamped is None:
            return []
        off_x, off_y, rw, rh = clamped
        img = img[off_y : off_y + rh, off_x : off_x + rw]

    result, _elapse = _get_engine()(img)
    lines: list[OcrLine] = []
    if not result:
        return lines
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = int(min(xs)), int(min(ys))
        x1, y1 = int(max(xs)), int(max(ys))
        lines.append(
            OcrLine(
                text=str(text),
                region=(off_x + x0, off_y + y0, x1 - x0, y1 - y0),
                confidence=float(score),
            )
        )
    return lines


def normalize_text(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


def find_text(
    screen_png: bytes,
    text: str,
    *,
    region: Region | None = None,
    min_ratio: float = 0.8,
) -> Match | None:
    """Locate a text label on screen via OCR plus fuzzy matching.

    Each OCR line is compared to ``text`` with
    ``difflib.SequenceMatcher.ratio()`` over normalized (lowercased,
    whitespace-collapsed) strings; the best line at or above ``min_ratio``
    wins.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        text: Target text to find.
        region: Optional ``(x, y, w, h)`` sub-region to search within.
        min_ratio: Minimum similarity ratio in ``[0, 1]`` to accept.

    Returns:
        A :class:`Match` centered on the best-matching line's bounding box,
        or ``None`` if no line is similar enough.
    """
    target = normalize_text(text)
    if not target:
        return None
    best: Optional[tuple[float, OcrLine]] = None
    for line in ocr(screen_png, region=region):
        ratio = difflib.SequenceMatcher(
            None, normalize_text(line.text), target
        ).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, line)
    if best is None or best[0] < min_ratio:
        return None
    ratio, line = best
    x, y, w, h = line.region
    return Match(
        point=(x + w // 2, y + h // 2), region=line.region, confidence=ratio
    )
