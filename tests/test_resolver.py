"""Unit tests for the resolution ladder (openadapt_flow.runtime.resolver).

All vision behavior is scripted via FakeVision — openadapt_flow.vision is
never imported here.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.ir import Anchor, Landmark
from openadapt_flow.runtime.resolver import (
    RUNG_ORDER,
    is_below_ocr,
    pad_region,
    png_size,
    resolve,
)

VIEWPORT = (300, 200)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Match:
    """Match-like object (point/region/confidence) without importing vision."""

    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class FakeVision:
    """Scripted find_template / find_text namespace."""

    def __init__(self):
        self.template_results: list = []
        self.template_calls: list = []
        self.text_results: dict = {}
        self.text_calls: list = []

    def find_template(self, screen_png, template_png, *, search_region=None,
                      prefer_near=None,
                      scales=(0.85, 1.0, 1.18), threshold=0.82):
        self.template_calls.append(search_region)
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8):
        self.text_calls.append(text)
        result = self.text_results.get(text)
        if isinstance(result, list):
            return result.pop(0) if result else None
        return result


class FakeGrounder:
    def __init__(self, result=None):
        self.result = result
        self.calls: list = []

    def locate(self, screen_png, intent, ocr_text=None):
        self.calls.append((intent, ocr_text))
        return self.result


@pytest.fixture()
def screen() -> bytes:
    return make_png()


@pytest.fixture()
def anchor() -> Anchor:
    return Anchor(
        template="templates/a.png",
        region=(100, 100, 50, 20),
        click_point=(110, 105),
        ocr_text="Save",
        search_pad=30,
    )


def test_template_rung_hit_uses_padded_search_region(screen, anchor):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(125, 110), region=(100, 100, 50, 20), confidence=0.95)
    ]
    resolution, matched = resolve(
        anchor, screen, vision, None, "click 'Save'",
        template_png=b"tpl", viewport=VIEWPORT,
    )
    assert resolution.rung == "template"
    # Same region as recorded -> click point identical to the recorded one.
    assert resolution.point == (110, 105)
    assert matched == (100, 100, 50, 20)
    assert resolution.confidence == pytest.approx(0.95)
    assert resolution.elapsed_ms >= 0.0
    # Local search region = anchor.region padded by search_pad, clamped.
    assert vision.template_calls == [(70, 70, 110, 80)]


def test_search_region_clamped_to_viewport(screen):
    anchor = Anchor(
        template="templates/a.png",
        region=(10, 5, 40, 20),
        click_point=(30, 15),
        search_pad=30,
    )
    vision = FakeVision()
    vision.template_results = [
        Match(point=(30, 15), region=(10, 5, 40, 20), confidence=0.9)
    ]
    resolve(anchor, screen, vision, template_png=b"tpl", viewport=VIEWPORT)
    assert vision.template_calls[0] == (0, 0, 80, 55)
    # Standalone check of the clamp helper at the far edge.
    assert pad_region((280, 190, 15, 8), 30, VIEWPORT) == (250, 160, 50, 40)


def test_template_global_fallback_with_scaled_click_point(screen, anchor):
    vision = FakeVision()
    # Local search misses, global finds a 2x-scaled match.
    vision.template_results = [
        None,
        Match(point=(250, 220), region=(200, 200, 100, 40), confidence=0.88),
    ]
    resolution, matched = resolve(
        anchor, screen, vision, template_png=b"tpl", viewport=(400, 400)
    )
    assert resolution.rung == "template_global"
    # Offset (10, 5) inside the anchor region scales by (2.0, 2.0).
    assert resolution.point == (220, 210)
    assert matched == (200, 200, 100, 40)
    assert len(vision.template_calls) == 2
    assert vision.template_calls[1] is None  # full-frame search


def test_ocr_rung_uses_match_point_directly(screen, anchor):
    vision = FakeVision()
    vision.text_results = {
        "Save": Match(point=(150, 60), region=(140, 55, 40, 12), confidence=0.8)
    }
    resolution, matched = resolve(
        anchor, screen, vision, template_png=b"tpl", viewport=VIEWPORT
    )
    assert resolution.rung == "ocr"
    assert resolution.point == (150, 60)
    assert matched == (140, 55, 40, 12)
    assert vision.text_calls == ["Save"]


def test_template_rungs_skipped_without_template_bytes(screen, anchor):
    vision = FakeVision()
    vision.text_results = {
        "Save": Match(point=(150, 60), region=(140, 55, 40, 12), confidence=0.8)
    }
    resolution, _ = resolve(anchor, screen, vision, viewport=VIEWPORT)
    assert resolution.rung == "ocr"
    assert vision.template_calls == []  # never attempted


def test_ocr_rung_skipped_when_anchor_has_no_text(screen):
    anchor = Anchor(
        template="templates/a.png",
        region=(100, 100, 50, 20),
        click_point=(110, 105),
        ocr_text=None,
    )
    vision = FakeVision()
    result = resolve(anchor, screen, vision, viewport=VIEWPORT)
    assert result is None
    assert vision.text_calls == []  # no ocr, no landmarks


def test_geometry_rung_single_landmark(screen):
    anchor = Anchor(
        template="templates/a.png",
        region=(100, 100, 50, 20),
        click_point=(110, 105),
        ocr_text="Save",
        landmarks=[Landmark(relation="left_of", ocr_text="Note", distance_px=40)],
    )
    vision = FakeVision()
    vision.text_results = {
        "Save": None,  # ocr rung misses
        "Note": Match(point=(100, 50), region=(80, 45, 40, 10), confidence=0.7),
    }
    resolution, matched = resolve(anchor, screen, vision, viewport=VIEWPORT)
    assert resolution.rung == "geometry"
    # Landmark left_of the target -> target is distance_px to its right.
    assert resolution.point == (140, 50)
    # Matched region is anchor-region-sized, centered on the estimate.
    assert matched == (115, 40, 50, 20)
    assert resolution.confidence == pytest.approx(0.7 * 0.9)


def test_geometry_rung_averages_multiple_landmarks(screen):
    anchor = Anchor(
        template="templates/a.png",
        region=(100, 100, 50, 20),
        click_point=(110, 105),
        landmarks=[
            Landmark(relation="left_of", ocr_text="Note", distance_px=40),
            Landmark(relation="above", ocr_text="Type", distance_px=20),
        ],
    )
    vision = FakeVision()
    vision.text_results = {
        "Note": Match(point=(100, 50), region=(80, 45, 40, 10), confidence=0.6),
        "Type": Match(point=(140, 30), region=(120, 25, 40, 10), confidence=0.8),
    }
    resolution, _ = resolve(anchor, screen, vision, viewport=VIEWPORT)
    assert resolution.rung == "geometry"
    # left_of estimate (140, 50); above estimate (140, 50) -> average identical.
    assert resolution.point == (140, 50)
    assert resolution.confidence == pytest.approx(0.7 * 0.9)


def test_grounder_rung_last_and_receives_intent(screen, anchor):
    vision = FakeVision()  # everything misses
    grounder = FakeGrounder(
        Match(point=(33, 44), region=(20, 40, 26, 8), confidence=0.5)
    )
    resolution, matched = resolve(
        anchor, screen, vision, grounder, "click 'Save'",
        template_png=b"tpl", viewport=VIEWPORT,
    )
    assert resolution.rung == "grounder"
    assert resolution.point == (33, 44)
    assert matched == (20, 40, 26, 8)
    assert grounder.calls == [("click 'Save'", "Save")]


def test_all_rungs_fail_returns_none(screen, anchor):
    vision = FakeVision()
    grounder = FakeGrounder(None)
    assert (
        resolve(anchor, screen, vision, grounder,
                template_png=b"tpl", viewport=VIEWPORT)
        is None
    )
    assert grounder.calls  # the grounder was consulted last


def test_ladder_order_is_frozen():
    assert RUNG_ORDER == (
        "template", "template_global", "ocr", "geometry", "grounder"
    )
    assert not is_below_ocr("template")
    assert not is_below_ocr("template_global")
    assert not is_below_ocr("ocr")
    assert is_below_ocr("geometry")
    assert is_below_ocr("grounder")


def test_viewport_parsed_from_png_header_when_omitted(anchor):
    screen = make_png(size=(123, 77))
    assert png_size(screen) == (123, 77)
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.9)
    ]
    resolution, _ = resolve(anchor, screen, vision, template_png=b"tpl")
    assert resolution.rung == "template"
    # Search region clamped to the parsed 123x77 viewport.
    assert vision.template_calls[0] == (70, 70, 53, 7)


class RatioRecordingVision(FakeVision):
    """FakeVision that records the min_ratio passed per find_text query."""

    def __init__(self):
        super().__init__()
        self.min_ratios: dict = {}

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8):
        self.min_ratios[text] = min_ratio
        return super().find_text(
            screen_png, text, region=region, min_ratio=min_ratio
        )


def test_ocr_rung_requires_strict_label_ratio(screen, anchor):
    """Regression: the ocr rung must query with min_ratio >= 0.9 so a
    near-miss label (e.g. 'New Encounter' for 'Save Encounter', difflib
    ratio ~0.81) cannot hijack the click; resolution falls through to
    geometry instead."""
    strict_anchor = anchor.model_copy(
        update={
            "landmarks": [
                Landmark(
                    relation="left_of",
                    ocr_text="Note",
                    distance_px=40,
                    dx_px=40,
                    dy_px=0,
                )
            ]
        }
    )
    vision = RatioRecordingVision()
    # A strict matcher returns nothing for the renamed label...
    vision.text_results = {
        "Save": None,
        "Note": Match(point=(70, 105), region=(50, 95, 40, 20), confidence=0.97),
    }
    resolution, _ = resolve(
        strict_anchor, screen, vision, None, "click 'Save'",
        template_png=None, viewport=VIEWPORT,
    )
    assert resolution is not None
    assert resolution.rung == "geometry"
    assert resolution.point == (110, 105)
    assert vision.min_ratios["Save"] >= 0.9
    assert vision.min_ratios["Note"] >= 0.9
