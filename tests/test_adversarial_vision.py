"""Adversarial battery for the VISION-ONLY (pixel) resolution path.

Every case runs the REAL vision matcher + real resolution ladder against a
synthetic frame that reproduces a remote-display (RDP/Citrix/no-DOM) failure
mode. Each case is classified as one of:

  * PASS         -- resolves to (near) the true target.
  * HALT         -- resolver returns None (SAFE: the ladder falls through/halts).
  * MIS-RESOLVE  -- resolves to a DECOY / far from the true target (DANGEROUS:
                    a silent wrong click; nothing downstream catches it on an
                    unarmed step).

The SAFE INVARIANT asserted here is: on these frames the resolver must PASS or
HALT, never MIS-RESOLVE. Cases still failing that invariant on ``main`` are
marked ``xfail`` with a pointer to the vision-hardening design and the fix PRs
(#165 locality/uniqueness gate; #166 labeled-anchor landmark guard). This file
is a documenting battery -- it is not merged into main.

Key honest finding baked into these tests: the strict TEMPLATE_THRESHOLD (0.985)
plus the narrow scale ladder make DPI / theme / compression drift fail SAFE
(HALT / over-halt) rather than mis-resolve -- EXCEPT where a look-alike is
present, where ambiguity turns a degraded true target into a silent wrong click.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from openadapt_flow.ir import Anchor, Landmark
from openadapt_flow.runtime import resolver
from openadapt_flow.vision.match import find_template
from openadapt_flow.vision.ocr import find_text

FONT = cv2.FONT_HERSHEY_SIMPLEX


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _jpeg(arr: np.ndarray, quality: int) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _blank(w: int = 800, h: int = 500, shade: int = 230) -> np.ndarray:
    return np.full((h, w, 3), shade, np.uint8)


def _button(img, x, y, label, bg=(70, 70, 180), w=90, h=32):
    cv2.rectangle(img, (x, y), (x + w, y + h), bg, -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (20, 20, 20), 1)
    cv2.putText(
        img, label, (x + 8, y + h - 9), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA
    )
    return (x, y, w, h)


def _gear(img, cx, cy, col=(70, 70, 70), r=14):
    cv2.circle(img, (cx, cy), r, col, -1)
    cv2.circle(img, (cx, cy), r // 2, (230, 230, 230), -1)
    for k in range(8):
        a = k * math.pi / 4
        cv2.circle(
            img,
            (int(cx + r * 1.3 * math.cos(a)), int(cy + r * 1.3 * math.sin(a))),
            3,
            col,
            -1,
        )


class _Vision:
    find_template = staticmethod(find_template)
    find_text = staticmethod(find_text)


VISION = _Vision()


def _anchor(region, click, ocr_text=None, landmarks=None, pad=400) -> Anchor:
    return Anchor(
        template="t.png",
        region=region,
        click_point=click,
        ocr_text=ocr_text,
        landmarks=landmarks or [],
        search_pad=pad,
    )


def _classify(anchor, scr, tmpl, true_pt, decoy_pt, tol=30):
    res = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(tmpl))
    if res is None:
        return "HALT", None
    pt = res[0].point
    if math.hypot(pt[0] - true_pt[0], pt[1] - true_pt[1]) <= tol:
        return "PASS", res[0].rung
    return "MIS-RESOLVE", res[0].rung


def _assert_safe(anchor, scr, tmpl, true_pt, decoy_pt, tol=30):
    verdict, rung = _classify(anchor, scr, tmpl, true_pt, decoy_pt, tol)
    assert verdict in ("PASS", "HALT"), f"silent mis-resolution (rung={rung})"


# ---------------------------------------------------------------------------
# CLASS A -- AMBIGUITY (repeated near-identical widgets) : DANGEROUS
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="silent mis-resolution: a tooltip/cursor occludes the target icon and "
    "a clean identical decoy out-scores it. Fixed by the locality/uniqueness "
    "gate (PR #165). See .private/vision_hardening_2026_07_20.md class A.",
    strict=False,
)
def test_tooltip_occlusion_among_identical_icons_must_not_misresolve():
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    tmpl = scr[180:220, 280:320].copy()
    cv2.rectangle(scr, (292, 175), (320, 205), (255, 255, 0), -1)  # tooltip on target
    _assert_safe(
        _anchor((280, 180, 40, 40), (300, 200)), scr, tmpl, (300, 200), (150, 200)
    )


def test_two_identical_labeled_buttons_clean_resolves_recorded_one():
    """Both instances pristine: the recorded position must disambiguate."""
    scr = _blank()
    _button(scr, 300, 100, "Delete")  # true (top)
    _button(scr, 300, 300, "Delete")  # decoy (bottom)
    tmpl = scr[100:132, 300:390].copy()
    _assert_safe(
        _anchor((300, 100, 90, 32), (345, 116), ocr_text="Delete", pad=260),
        scr,
        tmpl,
        (345, 116),
        (345, 316),
    )


# ---------------------------------------------------------------------------
# CLASS B -- template_global blind-accept for LABELED anchors : DANGEROUS
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="silent mis-resolution: a labeled anchor's global template match is "
    "accepted with no landmark corroboration, so a far identical labeled decoy "
    "is clicked when the true target moved/vanished. Fixed by PR #166. See "
    ".private/vision_hardening_2026_07_20.md class B.",
    strict=False,
)
def test_template_global_labeled_far_decoy_contradicted_by_landmark():
    scr = _blank()
    # True target region (top-left) is EMPTY; a far identical labeled decoy
    # exists bottom-right; a landmark 'Name:' corroborates the top-left position.
    _button(scr, 610, 410, "OK", w=70)
    cv2.putText(scr, "Name:", (60, 120), FONT, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
    tmpl = scr[410:442, 610:680].copy()
    landmark = Landmark(
        relation="left_of", ocr_text="Name:", distance_px=120, dx_px=120, dy_px=0
    )
    anchor = _anchor(
        (150, 105, 70, 32), (185, 121), ocr_text="OK", landmarks=[landmark], pad=30
    )
    _assert_safe(anchor, scr, tmpl, (185, 121), (645, 426))


# ---------------------------------------------------------------------------
# CLASS C -- DPI / SCALING : fails SAFE today (icon-only halts; labeled -> OCR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pct", [125, 150, 200])
def test_dpi_icon_only_fails_safe(pct):
    """Record 100%, replay at higher DPI, icon-only (no OCR rescue).

    The scale ladder tops out at 1.18, so the template rung cannot match a
    1.25x-2.0x re-render; with no label it HALTS (safe over-halt), it does not
    mis-resolve. Documents the availability gap (design class C: DPI-normalized
    matching).
    """
    base = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(base, gx, 200)
    tmpl = base[180:220, 280:320].copy()
    f = pct / 100.0
    big = cv2.resize(base, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    true_pt = (int(300 * f), int(200 * f))
    verdict, _rung = _classify(
        _anchor((280, 180, 40, 40), (300, 200), pad=1200), big, tmpl, true_pt, (0, 0)
    )
    assert verdict in ("PASS", "HALT")


# ---------------------------------------------------------------------------
# CLASS D -- THEME INVERSION : template dies (grayscale), OCR rescues if labeled
# ---------------------------------------------------------------------------


def test_dark_theme_icon_only_fails_safe():
    light = _blank(shade=235)
    _gear(light, 400, 200, col=(70, 70, 70))
    tmpl = light[180:220, 380:420].copy()
    dark = _blank(shade=30)
    _gear(dark, 400, 200, col=(200, 200, 200))  # inverted palette
    verdict, _rung = _classify(
        _anchor((380, 180, 40, 40), (400, 200), pad=200), dark, tmpl, (400, 200), (0, 0)
    )
    assert verdict in ("PASS", "HALT")  # grayscale corr inverts -> HALT (safe)


# ---------------------------------------------------------------------------
# CLASS E -- COMPRESSION (ICA/HDX/JPEG) : degrades score, fails SAFE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("quality", [40, 20, 10])
def test_jpeg_compression_icon_only_fails_safe(quality):
    base = _blank(shade=235)
    _gear(base, 400, 200)
    tmpl = base[180:220, 380:420].copy()
    comp = _jpeg(base, quality)
    verdict, _rung = _classify(
        _anchor((380, 180, 40, 40), (400, 200), pad=200), comp, tmpl, (400, 200), (0, 0)
    )
    assert verdict in ("PASS", "HALT")


# ---------------------------------------------------------------------------
# CLASS F -- OCR rung has NO locality: nearest-label-anywhere wins
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="the OCR rung matches the FIRST/best fuzzy label anywhere on the "
    "frame with no locality constraint; a duplicate label far from the recorded "
    "position can be selected. Fix: locality-aware OCR rung (design class F).",
    strict=False,
)
def test_ocr_rung_prefers_label_near_recorded_position():
    # Template deliberately unmatchable (theme-inverted) so resolution reaches
    # the OCR rung; two identical 'Continue' labels, true=top.
    scr = _blank(shade=235)
    cv2.putText(scr, "Continue", (120, 110), FONT, 0.8, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.putText(scr, "Continue", (120, 420), FONT, 0.8, (0, 0, 0), 2, cv2.LINE_AA)
    tmpl = np.full((30, 90, 3), 10, np.uint8)  # will not match
    anchor = _anchor((110, 90, 120, 30), (170, 105), ocr_text="Continue", pad=60)
    _assert_safe(anchor, scr, tmpl, (170, 105), (170, 415), tol=40)


# ---------------------------------------------------------------------------
# CLASS G -- GEOMETRY rung applies the recorded offset BLINDLY
# ---------------------------------------------------------------------------


def test_geometry_rung_applies_offset_without_verifying_target_present():
    """Documents that the geometry rung emits a point from a landmark + offset
    with NO check that the target is actually rendered there (design class G:
    anchor-relative geometry robustness + post-resolution presence check)."""
    scr = _blank(700, 400)
    cv2.putText(scr, "Name:", (100, 150), FONT, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
    landmark = Landmark(
        relation="left_of", ocr_text="Name:", distance_px=150, dx_px=150, dy_px=0
    )
    anchor = _anchor((250, 120, 120, 30), (310, 140), landmarks=[landmark], pad=40)
    res = resolver.resolve(anchor, _png(scr), VISION, template_png=None)
    # It DOES resolve (geometry), landing at landmark_center + (150, 0), even
    # though nothing was verified at that point. This is a known limitation.
    assert res is not None
    assert res[0].rung == "geometry"
