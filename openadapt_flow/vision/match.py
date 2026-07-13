"""Multi-scale grayscale template matching.

Implements the `find_template` rung of the resolution ladder using
``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED`` over a small scale ladder.
All returned coordinates are in *screen* (full-frame) pixel space, even when
a ``search_region`` restricts the search.
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np
from pydantic import BaseModel

from openadapt_flow.ir import Point, Region


class Match(BaseModel):
    """A located target on screen.

    Attributes:
        point: Click/center point in screen coordinates.
        region: Matched region ``(x, y, w, h)`` in screen coordinates.
        confidence: Match confidence in ``[0, 1]``.
    """

    point: Point
    region: Region
    confidence: float


def _decode_gray(png: bytes) -> np.ndarray:
    """Decode PNG bytes to a grayscale ``uint8`` image.

    Args:
        png: PNG-encoded image bytes.

    Returns:
        2-D grayscale image array.

    Raises:
        ValueError: If the bytes cannot be decoded as an image.
    """
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    return img


def _clamp_region(region: Region, width: int, height: int) -> Optional[Region]:
    """Clamp ``region`` to image bounds; return None if empty after clamping."""
    x, y, w, h = region
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


# Score margin within which multiple match locations are considered a tie
# (repeated UI structure such as identical empty form fields produces exact
# score ties at several positions).
TIE_BREAK_EPS = 1e-3


def find_template(
    screen_png: bytes,
    template_png: bytes,
    *,
    search_region: Region | None = None,
    scales: tuple[float, ...] = (0.85, 1.0, 1.18),
    threshold: float = 0.82,
    prefer_near: Point | None = None,
) -> Match | None:
    """Locate a template crop on screen via multi-scale template matching.

    Grayscale ``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED`` is run at each
    scale (the *template* is resized); the best-scoring location across all
    scales wins if it clears ``threshold``. Scales at which the resized
    template would not fit inside the search image are skipped.

    Args:
        screen_png: Full-frame screenshot as PNG bytes.
        template_png: Template crop as PNG bytes.
        search_region: Optional ``(x, y, w, h)`` sub-region of the screen to
            search within (clamped to screen bounds). Returned coordinates
            are still full-screen coordinates.
        scales: Multiplicative scale factors applied to the template.
        threshold: Minimum ``TM_CCOEFF_NORMED`` score to accept a match.
        prefer_near: Optional expected match origin ``(x, y)`` in screen
            coordinates. When several locations score within
            ``TIE_BREAK_EPS`` of the best (repeated UI structure — e.g. two
            identical empty inputs), the tie is broken in favor of the
            location nearest this point instead of raster-scan order.

    Returns:
        The best :class:`Match` in screen coordinates, or ``None`` if no
        scale produced a score at or above ``threshold``.
    """
    screen = _decode_gray(screen_png)
    template = _decode_gray(template_png)
    sh, sw = screen.shape[:2]

    off_x, off_y = 0, 0
    haystack = screen
    if search_region is not None:
        clamped = _clamp_region(search_region, sw, sh)
        if clamped is None:
            return None
        off_x, off_y, rw, rh = clamped
        haystack = screen[off_y : off_y + rh, off_x : off_x + rw]

    hh, hw = haystack.shape[:2]
    th0, tw0 = template.shape[:2]

    # score, x, y, w, h, result map of the winning scale
    best: tuple[float, int, int, int, int, np.ndarray] | None = None
    for scale in scales:
        tw = max(1, int(round(tw0 * scale)))
        th = max(1, int(round(th0 * scale)))
        if tw > hw or th > hh:
            continue  # template larger than search area at this scale
        if scale == 1.0:
            scaled = template
        else:
            scaled = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(haystack, scaled, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if best is None or max_val > best[0]:
            best = (float(max_val), max_loc[0], max_loc[1], tw, th, result)

    if best is None or best[0] < threshold:
        return None

    score, mx, my, mw, mh, result = best
    if prefer_near is not None:
        ys, xs = np.where(result >= score - TIE_BREAK_EPS)
        if len(xs) > 1:
            px, py = prefer_near[0] - off_x, prefer_near[1] - off_y
            d2 = (xs.astype(np.int64) - px) ** 2 + (ys.astype(np.int64) - py) ** 2
            i = int(np.argmin(d2))
            mx, my = int(xs[i]), int(ys[i])
            score = float(result[my, mx])

    rx, ry = off_x + mx, off_y + my
    region: Region = (rx, ry, mw, mh)
    point: Point = (rx + mw // 2, ry + mh // 2)
    return Match(point=point, region=region, confidence=score)


# pixels_changed: threshold below which a per-pixel grayscale difference is
# attributed to encoder noise, and the minimum count of above-threshold
# pixels for the region to count as visibly changed. Headless screenshots of
# a static screen are byte-identical, so both bars are deliberately low —
# the check answers "did ANYTHING render?", not "what changed?".
CHANGE_THRESHOLD = 20
CHANGE_MIN_PIXELS = 4


def pixels_changed(
    before_png: bytes,
    after_png: bytes,
    *,
    region: Region | None = None,
    threshold: int = CHANGE_THRESHOLD,
    min_pixels: int = CHANGE_MIN_PIXELS,
) -> bool:
    """True when two frames differ visibly (optionally within ``region``).

    Used by the replayer's typed-input verification: any visible change in
    the field region distinguishes "keystrokes landed somewhere visible"
    from "keystrokes fell on a non-rendering target" (e.g. ``<body>`` after
    focus theft).

    Args:
        before_png: Frame before the action, PNG bytes.
        after_png: Frame after the action, PNG bytes.
        region: Optional (x, y, w, h) to restrict the comparison to.
        threshold: Per-pixel grayscale delta above which a pixel counts.
        min_pixels: How many counting pixels make the frames "changed".

    Returns:
        True when the frames differ visibly (mismatched dimensions count as
        changed).
    """
    before = _decode_gray(before_png)
    after = _decode_gray(after_png)
    if before.shape != after.shape:
        return True
    if region is not None:
        clamped = _clamp_region(region, before.shape[1], before.shape[0])
        if clamped is None:
            return False
        x, y, w, h = clamped
        before = before[y : y + h, x : x + w]
        after = after[y : y + h, x : x + w]
    diff = cv2.absdiff(before, after)
    return int(np.count_nonzero(diff > threshold)) >= min_pixels
