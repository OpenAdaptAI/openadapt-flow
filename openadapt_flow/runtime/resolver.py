"""Resolution ladder: locate a step's target on the live screen.

The ladder walks progressively weaker (but more drift-tolerant) evidence:

1. ``template``        — template match inside ``anchor.region`` padded by
   ``anchor.search_pad`` (clamped to the viewport).
2. ``template_global``  — template match over the full frame.
3. ``ocr``              — fuzzy text match on ``anchor.ocr_text``.
4. ``geometry``         — locate landmark text and offset by
   relation/distance to estimate the target point.
5. ``grounder``         — optional injected model-backed grounding.

The ``vision`` argument is a namespace-like object (the real
``openadapt_flow.vision`` module or a test fake) exposing ``find_template``
and ``find_text``.
"""

from __future__ import annotations

import struct
import time
from typing import Any, Optional

from openadapt_flow.ir import Anchor, Point, Region, Resolution, Rung

RUNG_ORDER: tuple[Rung, ...] = (
    "template",
    "template_global",
    "ocr",
    "geometry",
    "grounder",
)

_GEOMETRY_CONFIDENCE_SCALE = 0.9  # geometry is indirect evidence

# Minimum TM_CCOEFF_NORMED score for the template rungs. Deliberately
# stricter than vision.find_template's general-purpose default: an exact
# same-theme re-render scores >= 0.999 while a same-position button whose
# LABEL changed (rename drift) still scores ~0.95-0.97, and such a step must
# fall through to the ocr/geometry rungs (and be healed) rather than be
# treated as template-stable.
TEMPLATE_THRESHOLD = 0.985


def is_below_ocr(rung: Rung) -> bool:
    """Return True if ``rung`` is weaker evidence than the ``ocr`` rung.

    Used by the risk gate: irreversible steps must not act on a resolution
    from a rung below ``ocr`` (i.e. ``geometry`` or ``grounder``).

    Args:
        rung: The resolution rung to classify.

    Returns:
        True for ``geometry`` and ``grounder``, False otherwise.
    """
    return RUNG_ORDER.index(rung) > RUNG_ORDER.index("ocr")


def png_size(png: bytes) -> tuple[int, int]:
    """Read (width, height) from a PNG header without decoding the image.

    Args:
        png: PNG file bytes.

    Returns:
        (width, height) in pixels.

    Raises:
        ValueError: If the bytes are not a valid PNG.
    """
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def pad_region(region: Region, pad: int, viewport: tuple[int, int]) -> Region:
    """Pad ``region`` by ``pad`` pixels on all sides, clamped to the viewport.

    Args:
        region: (x, y, w, h) region to pad.
        pad: Padding in pixels.
        viewport: (width, height) of the frame.

    Returns:
        The padded, clamped (x, y, w, h) region.
    """
    x, y, w, h = region
    vw, vh = viewport
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(vw, x + w + pad)
    y1 = min(vh, y + h + pad)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def _clamp_region_of_size(
    center: Point, size: tuple[int, int], viewport: tuple[int, int]
) -> Region:
    """Build a region of ``size`` centered at ``center``, clamped in-bounds."""
    vw, vh = viewport
    w = min(size[0], vw)
    h = min(size[1], vh)
    x = min(max(0, center[0] - w // 2), max(0, vw - w))
    y = min(max(0, center[1] - h // 2), max(0, vh - h))
    return (x, y, w, h)


def _scaled_click_point(anchor: Anchor, matched: Region) -> Point:
    """Map the recorded click point into a matched region.

    Click point = matched region origin + (anchor.click_point - anchor.region
    origin) scaled by the matched/anchor region size ratio.
    """
    ax, ay, aw, ah = anchor.region
    mx, my, mw, mh = matched
    sx = mw / aw if aw else 1.0
    sy = mh / ah if ah else 1.0
    dx = anchor.click_point[0] - ax
    dy = anchor.click_point[1] - ay
    return (int(round(mx + dx * sx)), int(round(my + dy * sy)))


def _estimate_from_landmark(
    relation: str,
    landmark_point: Point,
    distance_px: int,
    dx_px: Optional[int] = None,
    dy_px: Optional[int] = None,
) -> Point:
    """Estimate the target point from one located landmark.

    When exact ``dx_px``/``dy_px`` offsets are available (compiler output),
    the target is ``landmark_point + (dx_px, dy_px)``. Otherwise ``relation``
    is interpreted as the landmark's position relative to the target: a
    landmark that is ``left_of`` the target implies the target sits
    ``distance_px`` to the landmark's right, and so on.
    """
    x, y = landmark_point
    if dx_px is not None and dy_px is not None:
        return (x + dx_px, y + dy_px)
    if relation == "left_of":
        return (x + distance_px, y)
    if relation == "right_of":
        return (x - distance_px, y)
    if relation == "above":
        return (x, y + distance_px)
    if relation == "below":
        return (x, y - distance_px)
    raise ValueError(f"unknown landmark relation: {relation!r}")


def resolve(
    anchor: Anchor,
    screen_png: bytes,
    vision: Any,
    grounder: Optional[Any] = None,
    intent: str = "",
    *,
    template_png: Optional[bytes] = None,
    viewport: Optional[tuple[int, int]] = None,
) -> Optional[tuple[Resolution, Region]]:
    """Walk the resolution ladder for ``anchor`` against a live frame.

    Args:
        anchor: The step's anchor (template path/region, ocr text, landmarks).
        screen_png: Current frame as PNG bytes.
        vision: Namespace-like object exposing ``find_template(screen, tmpl,
            *, search_region=None)`` and ``find_text(screen, text)``, each
            returning a Match-like object (``point``/``region``/
            ``confidence``) or None.
        grounder: Optional Grounder; its ``locate`` rung runs last.
        intent: Human-readable step intent, forwarded to the grounder.
        template_png: The anchor's template crop bytes, if available. When
            None the two template rungs are skipped.
        viewport: (width, height) of the frame; parsed from ``screen_png``'s
            PNG header when omitted.

    Returns:
        ``(resolution, matched_region)`` on success, where ``matched_region``
        is the screen region the evidence matched (used for healing), or None
        when every rung fails. ``resolution.elapsed_ms`` is the total time
        spent across all rungs attempted.
    """
    t0 = time.monotonic()
    if viewport is None:
        viewport = png_size(screen_png)

    def elapsed_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    # Rung 1: template within the padded local search region.
    if template_png is not None:
        search_region = pad_region(anchor.region, anchor.search_pad, viewport)
        match = vision.find_template(
            screen_png,
            template_png,
            search_region=search_region,
            threshold=TEMPLATE_THRESHOLD,
            prefer_near=(anchor.region[0], anchor.region[1]),
        )
        if match is not None:
            resolution = Resolution(
                rung="template",
                point=_scaled_click_point(anchor, tuple(match.region)),
                confidence=float(match.confidence),
                elapsed_ms=elapsed_ms(),
            )
            return resolution, tuple(match.region)

        # Rung 2: template over the full frame.
        match = vision.find_template(
            screen_png,
            template_png,
            threshold=TEMPLATE_THRESHOLD,
            prefer_near=(anchor.region[0], anchor.region[1]),
        )
        if match is not None:
            resolution = Resolution(
                rung="template_global",
                point=_scaled_click_point(anchor, tuple(match.region)),
                confidence=float(match.confidence),
                elapsed_ms=elapsed_ms(),
            )
            return resolution, tuple(match.region)

    # Rung 3: OCR text match.
    if anchor.ocr_text:
        match = vision.find_text(screen_png, anchor.ocr_text)
        if match is not None:
            resolution = Resolution(
                rung="ocr",
                point=(int(match.point[0]), int(match.point[1])),
                confidence=float(match.confidence),
                elapsed_ms=elapsed_ms(),
            )
            return resolution, tuple(match.region)

    # Rung 4: geometry from landmarks.
    estimates: list[Point] = []
    confidences: list[float] = []
    for landmark in anchor.landmarks:
        lm_match = vision.find_text(screen_png, landmark.ocr_text)
        if lm_match is None:
            continue
        estimates.append(
            _estimate_from_landmark(
                landmark.relation,
                (int(lm_match.point[0]), int(lm_match.point[1])),
                landmark.distance_px,
                getattr(landmark, "dx_px", None),
                getattr(landmark, "dy_px", None),
            )
        )
        confidences.append(float(lm_match.confidence))
    if estimates:
        px = int(round(sum(p[0] for p in estimates) / len(estimates)))
        py = int(round(sum(p[1] for p in estimates) / len(estimates)))
        region = _clamp_region_of_size(
            (px, py), (anchor.region[2], anchor.region[3]), viewport
        )
        resolution = Resolution(
            rung="geometry",
            point=(px, py),
            confidence=(sum(confidences) / len(confidences))
            * _GEOMETRY_CONFIDENCE_SCALE,
            elapsed_ms=elapsed_ms(),
        )
        return resolution, region

    # Rung 5: optional grounder.
    if grounder is not None:
        match = grounder.locate(screen_png, intent, anchor.ocr_text)
        if match is not None:
            resolution = Resolution(
                rung="grounder",
                point=(int(match.point[0]), int(match.point[1])),
                confidence=float(match.confidence),
                elapsed_ms=elapsed_ms(),
            )
            return resolution, tuple(match.region)

    return None
