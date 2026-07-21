"""Locality + uniqueness gate for template resolution (find_template gated on
``prefer_near``, consumed by the resolution ladder's template rungs).

These exercise the REAL vision matcher (not a scripted fake): on a pixel-only
substrate a frame routinely contains several near-identical widgets, and the
raw arg-max silently clicks an ARBITRARY one. The gate must instead prefer the
instance where the target was recorded and HALT (return None) when the target
is not uniquely present where expected -- converting the most dangerous silent
mis-resolution class into a safe halt. See docs and the vision-hardening design.
"""

from __future__ import annotations

import cv2
import numpy as np

from openadapt_flow.ir import Anchor
from openadapt_flow.runtime import resolver
from openadapt_flow.vision import match as match_mod
from openadapt_flow.vision.match import find_template
from openadapt_flow.vision.ocr import find_text

FONT = cv2.FONT_HERSHEY_SIMPLEX


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _blank(w: int = 800, h: int = 500, shade: int = 230) -> np.ndarray:
    return np.full((h, w, 3), shade, np.uint8)


def _button(img: np.ndarray, x: int, y: int, label: str, w: int = 90, h: int = 32):
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 180), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (20, 20, 20), 1)
    cv2.putText(
        img, label, (x + 8, y + h - 9), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA
    )
    return (x, y, w, h)


def _gear(img: np.ndarray, cx: int, cy: int, r: int = 14) -> None:
    cv2.circle(img, (cx, cy), r, (70, 70, 70), -1)
    cv2.circle(img, (cx, cy), r // 2, (230, 230, 230), -1)
    for k in range(8):
        a = k * np.pi / 4
        cv2.circle(
            img,
            (int(cx + r * 1.3 * np.cos(a)), int(cy + r * 1.3 * np.sin(a))),
            3,
            (70, 70, 70),
            -1,
        )


class _Vision:
    find_template = staticmethod(find_template)
    find_text = staticmethod(find_text)


VISION = _Vision()


def _anchor(region, click, ocr_text=None, pad=400) -> Anchor:
    return Anchor(
        template="t.png",
        region=region,
        click_point=click,
        ocr_text=ocr_text,
        landmarks=[],
        search_pad=pad,
    )


# --- find_template gate (unit) ---------------------------------------------


def test_prefer_near_prefers_expected_over_higher_scoring_lookalike() -> None:
    """The expected instance wins over a strictly-HIGHER-scoring look-alike.

    The old tie-break only re-ordered matches within ~1e-3 of the best, so a
    look-alike scoring even slightly higher than the recorded target was clicked
    regardless of position. Here the expected (top) instance carries a faint 1px
    scratch (score < 1.0 but still >= threshold) while the decoy (bottom) is
    pristine (score 1.0); the gate must still choose the top.
    """
    scr = _blank()
    _button(scr, 300, 100, "Delete")  # expected (top)
    _button(scr, 300, 300, "Delete")  # pristine look-alike (bottom), scores higher
    template = scr[100:132, 300:390].copy()
    # Faintly perturb ONLY the expected instance so the decoy strictly outscores
    # it, yet it stays comfortably above threshold (a few changed pixels).
    cv2.line(scr, (305, 116), (330, 116), (60, 60, 170), 1)
    top = find_template(
        _png(scr), _png(template), search_region=(295, 95, 100, 42), threshold=0.0
    )
    bottom = find_template(
        _png(scr), _png(template), search_region=(295, 295, 100, 42), threshold=0.0
    )
    # The decoy strictly outscores the perturbed-but-detectable expected target
    # (the regime where the OLD ~1e-3 tie-break clicked the decoy).
    assert top is not None and bottom is not None
    assert bottom.confidence > top.confidence >= 0.97
    m = find_template(_png(scr), _png(template), threshold=0.97, prefer_near=(300, 100))
    assert m is not None
    assert abs(m.point[1] - 116) <= 20  # chose the TOP (expected), not the bottom


def test_ambiguous_lookalikes_none_near_expected_returns_none() -> None:
    """>= 2 peaks clear threshold and none is where expected -> HALT (None)."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    # Expect the target where the target ISN'T uniquely present (all gears equal).
    m = find_template(
        _png(scr), _png(template), threshold=0.985, prefer_near=(700, 400)
    )
    assert m is None


def test_single_unique_far_match_is_kept() -> None:
    """A lone peak far from expected is a legitimately moved unique target."""
    scr = _blank()
    _button(scr, 300, 350, "Save")
    template = scr[350:382, 300:390].copy()
    m = find_template(
        _png(scr), _png(template), threshold=0.985, prefer_near=(300, 100)
    )
    assert m is not None
    assert abs(m.point[1] - 366) <= 20


def test_gate_is_inactive_without_prefer_near() -> None:
    """Postcondition matching (no prefer_near) keeps the plain arg-max match."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    m = find_template(_png(scr), _png(template), threshold=0.985)
    assert m is not None  # unchanged: returns some instance, never None here


# --- resolution ladder (integration through resolve) -----------------------


def test_resolve_halts_on_occluded_target_among_identical_icons() -> None:
    """Tooltip/cursor occludes the target icon; identical decoys remain.

    Old behavior clicked a clean decoy silently; the gate now halts (all rungs
    fail) rather than resolve the wrong icon.
    """
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    cv2.rectangle(scr, (292, 175), (320, 205), (255, 255, 0), -1)  # tooltip on target
    anchor = _anchor((280, 180, 40, 40), (300, 200), ocr_text=None, pad=400)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is None


def test_resolve_picks_true_target_in_near_tie() -> None:
    """Two identical labeled buttons, both clean: resolve the recorded one."""
    scr = _blank()
    _button(scr, 300, 100, "Delete")
    _button(scr, 300, 300, "Delete")
    template = scr[100:132, 300:390].copy()
    anchor = _anchor((300, 100, 90, 32), (345, 116), ocr_text="Delete", pad=260)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is not None
    resolution, _region = result
    assert abs(resolution.point[1] - 116) <= 20  # top (recorded), not bottom


def test_resolve_accepts_unique_target_at_expected() -> None:
    scr = _blank()
    _button(scr, 300, 100, "Save")
    template = scr[100:132, 300:390].copy()
    anchor = _anchor((300, 100, 90, 32), (345, 116), ocr_text="Save", pad=200)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "template"
    assert abs(resolution.point[0] - 345) <= 20 and abs(resolution.point[1] - 116) <= 20


def test_peaks_above_suppresses_neighbors() -> None:
    """The NMS peak enumerator yields one peak per identical instance."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    gray_scr = cv2.cvtColor(scr, cv2.COLOR_BGR2GRAY)
    tmpl = gray_scr[180:220, 280:320]
    result = cv2.matchTemplate(gray_scr, tmpl, cv2.TM_CCOEFF_NORMED)
    peaks = match_mod._peaks_above(result, 0.985, tmpl.shape[1], tmpl.shape[0])
    # Four identical gears -> at least four distinct strong peaks, not hundreds.
    assert 4 <= len(peaks) <= match_mod.MAX_PEAKS
