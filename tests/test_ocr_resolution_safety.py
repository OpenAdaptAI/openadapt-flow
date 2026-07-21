"""Fail-closed OCR targeting for repeated labels and contradictory evidence.

These are small synthetic mechanism tests. They carry no learned thresholds,
deployment data, or application-specific resolution recipes.
"""

from __future__ import annotations

import importlib
import io
from typing import Any

import pytest
from PIL import Image

from openadapt_flow.ir import Anchor, Landmark, Region
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.vision.match import Match
from openadapt_flow.vision.ocr import AmbiguousOcrMatchError, OcrLine, find_text

VIEWPORT = (640, 480)
ocr_module = importlib.import_module("openadapt_flow.vision.ocr")
_AMBIGUOUS = object()


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", VIEWPORT, (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _match(point: tuple[int, int], region: Region) -> Match:
    return Match(point=point, region=region, confidence=1.0)


def _anchor(*, landmarks: list[Landmark] | None = None) -> Anchor:
    return Anchor(
        template="templates/delete.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        ocr_text="Delete",
        landmarks=landmarks or [],
        search_pad=40,
    )


class _Vision:
    """Region-aware scripted OCR namespace; template matching always misses."""

    def __init__(self, text_results: dict[tuple[str, Region | None], Any]):
        self.text_results = text_results
        self.text_calls: list[tuple[str, Region | None]] = []

    @staticmethod
    def find_template(*_args: Any, **_kwargs: Any) -> None:
        return None

    def find_text(
        self,
        _screen_png: bytes,
        text: str,
        *,
        region: Region | None = None,
        min_ratio: float = 0.8,
        raise_on_ambiguity: bool = False,
    ) -> Match | None:
        del min_ratio
        self.text_calls.append((text, region))
        result = self.text_results.get((text, region))
        if result is _AMBIGUOUS:
            if raise_on_ambiguity:
                raise AmbiguousOcrMatchError("synthetic duplicate OCR lines")
            return None
        return result


class _Grounder:
    def __init__(self) -> None:
        self.calls = 0

    def locate(self, *_args: Any, **_kwargs: Any) -> Match:
        self.calls += 1
        return _match((600, 440), (580, 430, 40, 20))


def test_find_text_preserves_best_match_for_non_targeting_callers(monkeypatch) -> None:
    """Readiness/presence callers keep the historical best-match behavior."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Delete", region=(100, 300, 60, 20), confidence=0.98),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    result = find_text(b"synthetic", "Delete", min_ratio=0.9)

    assert result is not None
    assert result.point == (130, 110)


def test_find_text_can_signal_duplicate_qualifying_lines(monkeypatch) -> None:
    """Resolution callers can distinguish ambiguity from an ordinary miss."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Delete", region=(100, 300, 60, 20), confidence=0.98),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    with pytest.raises(AmbiguousOcrMatchError):
        find_text(
            b"synthetic",
            "Delete",
            min_ratio=0.9,
            raise_on_ambiguity=True,
        )


def test_find_text_preserves_unique_success(monkeypatch) -> None:
    """A unique qualifying line still resolves exactly as before."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Unrelated", region=(100, 300, 80, 20), confidence=0.99),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    result = find_text(b"synthetic", "Delete", min_ratio=0.9)

    assert result is not None
    assert result.point == (130, 110)
    assert result.region == (100, 100, 60, 20)


def test_resolver_uses_local_ocr_before_global_and_short_circuits() -> None:
    """A unique local label wins without consulting the full frame."""
    local_region = (40, 50, 170, 112)
    vision = _Vision({("Delete", local_region): _match((125, 106), (95, 96, 60, 20))})

    result = resolve(_anchor(), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)
    assert vision.text_calls == [("Delete", local_region)]


def test_resolver_uses_global_ocr_only_after_local_miss() -> None:
    """A uniquely moved label remains resolvable after local evidence misses."""
    local_region = (40, 50, 170, 112)
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _match((400, 300), (370, 290, 60, 20)),
        }
    )

    result = resolve(_anchor(), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (400, 300)
    assert vision.text_calls == [("Delete", local_region), ("Delete", None)]


def test_local_ocr_ambiguity_halts_without_weaker_fallback() -> None:
    """Duplicate local labels are a refusal, not a miss or grounding prompt."""
    local_region = (40, 50, 170, 112)
    vision = _Vision({("Delete", local_region): _AMBIGUOUS})
    grounder = _Grounder()

    result = resolve(_anchor(), _png(), vision, grounder, viewport=VIEWPORT)

    assert result is None
    assert vision.text_calls == [("Delete", local_region)]
    assert grounder.calls == 0


def test_global_ocr_ambiguity_halts_without_weaker_fallback() -> None:
    """A local miss may search globally, but global duplicates must halt."""
    local_region = (40, 50, 170, 112)
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _AMBIGUOUS,
        }
    )
    grounder = _Grounder()

    result = resolve(_anchor(), _png(), vision, grounder, viewport=VIEWPORT)

    assert result is None
    assert vision.text_calls == [("Delete", local_region), ("Delete", None)]
    assert grounder.calls == 0


def test_ambiguous_landmark_halts_instead_of_blind_geometry() -> None:
    """A repeated row landmark cannot be averaged into a coordinate action."""
    anchor = Anchor(
        template="templates/icon.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Name:",
                distance_px=80,
                dx_px=80,
                dy_px=0,
            )
        ],
    )
    vision = _Vision({("Name:", None): _AMBIGUOUS})
    grounder = _Grounder()

    result = resolve(anchor, _png(), vision, grounder, viewport=VIEWPORT)

    assert result is None
    assert vision.text_calls == [("Name:", None)]
    assert grounder.calls == 0


def test_labeled_far_decoy_contradicted_by_landmark_halts() -> None:
    """A far global label cannot outrank independent recorded-row geometry."""
    local_region = (40, 50, 170, 112)
    landmark = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=200,
        dx_px=200,
        dy_px=0,
    )
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _match((545, 416), (515, 406, 60, 20)),
            ("Name:", None): _match((100, 106), (70, 96, 60, 20)),
        }
    )

    result = resolve(_anchor(landmarks=[landmark]), _png(), vision, viewport=VIEWPORT)

    # The contradicted OCR result is a refusal. In particular, the ladder does
    # not turn the same landmark into an unverified geometry action.
    assert result is None
    assert vision.text_calls == [
        ("Delete", local_region),
        ("Delete", None),
        ("Name:", None),
    ]


def test_local_ocr_candidate_contradicted_by_landmark_halts() -> None:
    """Landmark contradiction is enforced on the local OCR path too."""
    local_region = (40, 50, 170, 112)
    landmark = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    vision = _Vision(
        {
            ("Delete", local_region): _match((190, 140), (160, 130, 60, 20)),
            ("Name:", None): _match((50, 60), (20, 50, 60, 20)),
        }
    )

    result = resolve(_anchor(landmarks=[landmark]), _png(), vision, viewport=VIEWPORT)

    assert result is None
    assert vision.text_calls == [("Delete", local_region), ("Name:", None)]
