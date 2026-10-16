"""The identifier crop is re-cut where the target moved, whatever the rung."""

from types import SimpleNamespace

import openadapt_flow.runtime.replayer as replayer_mod
from openadapt_flow.runtime.replayer import Replayer, Resolution


def _replayer(vision, monkeypatch):
    monkeypatch.setattr(replayer_mod.identity_mod, "crop_region", lambda png, region: b"live")
    r = Replayer.__new__(Replayer)
    r.vision = vision
    r._asset_bytes = lambda bundle_dir, rel, workflow=None: b"template"
    return r


ANCHOR = SimpleNamespace(
    click_point=(462, 317), region=(382, 285, 160, 64), ocr_text="FirstName",
    template="templates/step_003.png",
)


def test_ocr_rung_measures_from_the_label_not_the_click(tmp_path, monkeypatch):
    # The label sits at (44, 33) in the recorded template and in the live crop:
    # the screen did not move, even though the OCR rung resolved 35 px left of
    # the recorded click.
    vision = SimpleNamespace(find_text=lambda png, text, **k: SimpleNamespace(point=(44, 33)))
    res = Resolution(rung="ocr", point=(427, 319), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (1, 1)


def test_ocr_rung_cancels_centroid_jitter(tmp_path, monkeypatch):
    points = {b"template": (44, 33), b"live": (45, 34)}
    vision = SimpleNamespace(find_text=lambda png, text, **k: SimpleNamespace(point=points[png]))
    res = Resolution(rung="ocr", point=(425, 317), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (0, 0)


def test_template_rung_keeps_click_displacement(tmp_path, monkeypatch):
    vision = SimpleNamespace(find_text=lambda *a, **k: 1 / 0)
    res = Resolution(rung="template", point=(470, 320), confidence=0.99, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (8, 3)


def test_ocr_label_not_found_falls_back(tmp_path, monkeypatch):
    vision = SimpleNamespace(find_text=lambda *a, **k: None)
    res = Resolution(rung="ocr", point=(427, 319), confidence=0.95, elapsed_ms=0)
    r = _replayer(vision, monkeypatch)
    assert r._identifier_displacement(ANCHOR, res, b"frame", tmp_path) == (-35, 2)
