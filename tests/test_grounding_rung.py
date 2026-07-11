"""Grounding rung x identity gate — composition and safety pins.

Covers the three claims of docs/grounding_rung.md:

1. :class:`GuiOwlGrounder` parses/scales an open grounder's coordinate reply
   correctly (the Qwen3-VL 0-1000 gotcha) and honours the Grounder protocol.
2. The rung composes with the identity gate at the REPLAYER level: a grounder
   proposal is verified before any click — a proposal on a wrong entity
   safe-halts (no click), a proposal on the true row clicks.
3. Over the frozen adversary corpora the composition converts false-aborts to
   successes while keeping false-accept at exactly 0.000%.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.runtime import GuiOwlGrounder, Replayer, parse_grounder_point
from openadapt_flow.runtime.grounder import (
    COORD_NORM_1000,
    COORD_PIXEL,
    GrounderMatch,
)
from openadapt_flow.validation.grounding_composition import (
    FaithfulMockGrounder,
    run_composition,
)

VIEWPORT = (1280, 800)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# -- coordinate parsing / scaling --------------------------------------------


@pytest.mark.parametrize(
    "reply, space, expected",
    [
        ('{"x": 500, "y": 250}', COORD_NORM_1000, (640, 200)),
        ('{"x": 500, "y": 250}', COORD_PIXEL, (500, 250)),
        ('{"point": [750, 500]}', COORD_NORM_1000, (960, 400)),
        ("the click is at (250, 600)", COORD_NORM_1000, (320, 480)),
        (
            "<|box_start|>(100,100),(300,300)<|box_end|>",
            COORD_NORM_1000,
            (256, 160),
        ),
    ],
)
def test_parse_grounder_point_shapes(reply, space, expected):
    assert parse_grounder_point(reply, VIEWPORT, space) == expected


@pytest.mark.parametrize(
    "reply",
    ['{"x": null, "y": null}', "the target is not visible", "", "1234"],
)
def test_parse_grounder_point_none(reply):
    assert parse_grounder_point(reply, VIEWPORT, COORD_NORM_1000) is None


def test_norm_1000_scaling_is_viewport_relative():
    # Same normalized coord maps to different pixels per viewport size.
    assert parse_grounder_point('{"x": 500, "y": 500}', (1000, 600),
                                COORD_NORM_1000) == (500, 300)
    assert parse_grounder_point('{"x": 500, "y": 500}', (2000, 1000),
                                COORD_NORM_1000) == (1000, 500)


def test_parsed_point_is_clamped_to_frame():
    assert parse_grounder_point('{"x": 2000, "y": 2000}', (1000, 600),
                                COORD_PIXEL) == (999, 599)


# -- GuiOwlGrounder wiring (via injected transport, no model/network) --------


def test_gui_owl_grounder_returns_match_from_reply():
    g = GuiOwlGrounder(
        transport=lambda png, intent, ocr: '{"x": 500, "y": 250}',
        coord_space=COORD_NORM_1000,
    )
    match = g.locate(make_png(), "click Save", "Save")
    assert isinstance(match, GrounderMatch)
    assert match.point == (640, 200)  # 500/1000*1280, 250/1000*800


def test_gui_owl_grounder_returns_none_on_not_visible():
    g = GuiOwlGrounder(
        transport=lambda png, intent, ocr: '{"x": null, "y": null}',
    )
    assert g.locate(make_png(), "click Save") is None


def test_gui_owl_grounder_forwards_intent_and_ocr():
    seen: list = []

    def transport(png, intent, ocr):
        seen.append((intent, ocr))
        return '{"x": 100, "y": 100}'

    GuiOwlGrounder(transport=transport).locate(make_png(), "click 'Open'", "Open")
    assert seen == [("click 'Open'", "Open")]


def test_gui_owl_grounder_unknown_backend_raises():
    with pytest.raises(ValueError, match="backend"):
        GuiOwlGrounder(backend="bogus")


def test_gui_owl_grounder_http_without_endpoint_raises():
    with pytest.raises((ValueError, ImportError)):
        GuiOwlGrounder(backend="http")


def test_gui_owl_grounder_satisfies_protocol():
    from openadapt_flow.runtime.grounder import Grounder

    g = GuiOwlGrounder(transport=lambda p, i, o: '{"x":1,"y":1}')
    assert isinstance(g, Grounder)


# -- replayer-level composition: propose (grounder) + dispose (identity) -----

# The identity band the gate reads; long enough to clear MIN_CONTEXT_CHARS.
_TRUE_BAND = "Sample, Jane 1990-01-01 F MRN A123456"
_WRONG_BAND = "Testcase, Alex 1985-05-05 M MRN B987654"


class _Line:
    def __init__(self, text, region):
        self.text = text
        self.region = region


class _GroundOnlyVision:
    """All deterministic rungs miss; OCR returns a scripted identity band."""

    def __init__(self, band_text):
        self._band = band_text
        self.template_calls: list = []

    def find_template(self, *a, **k):
        return None

    def find_text(self, *a, **k):
        return None

    def ocr(self, screen_png, *, region=None):
        # One band line on the resolved point's row (center y=300), placed
        # left of the excluded target crop (x 420-580) so it participates in
        # identity rather than being filtered as the target's own label.
        return [_Line(self._band, (20, 292, 380, 16))]

    def wait_settled(self, backend, **k):
        return backend.screenshot()


class _Backend:
    def __init__(self, viewport=(1000, 600)):
        self._vp = viewport
        self._frame = make_png(viewport)
        self.actions: list = []

    @property
    def viewport(self):
        return self._vp

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))


def _grounder_step():
    # Anchor with a template path that doesn't exist in the (empty) bundle, so
    # template rungs are skipped; ocr_text present but vision.find_text misses;
    # no landmarks -> geometry empty; context_text arms the identity gate.
    return Step(
        id="s1",
        intent="click the referral's Open button",
        action=ActionKind.CLICK,
        risk="reversible",
        anchor=Anchor(
            template="templates/missing.png",
            region=(420, 268, 160, 64),
            click_point=(500, 300),
            ocr_text="Open",
            context_text=_TRUE_BAND,
            landmarks=[],
        ),
    )


def _run_with_grounder(vision, tmp_path):
    backend = _Backend()
    grounder = FaithfulMockGrounder(point=(500, 300))
    workflow = Workflow(name="wf", steps=[_grounder_step()])
    report = Replayer(
        backend, vision=vision, grounder=grounder, poll_interval_s=0.01
    ).run(
        workflow,
        params={},
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )
    return report, backend, grounder


def test_grounder_proposal_on_true_row_verifies_and_clicks(tmp_path):
    (tmp_path / "bundle").mkdir()
    report, backend, grounder = _run_with_grounder(
        _GroundOnlyVision(_TRUE_BAND), tmp_path
    )
    assert grounder.calls, "grounder rung must have fired"
    result = report.results[0]
    assert result.resolution is not None
    assert result.resolution.rung == "grounder"
    assert result.identity is not None and result.identity.status == "verified"
    # Identity passed -> the grounder's proposed point is clicked.
    assert backend.actions == [("click", 500, 300, False)]
    assert report.model_calls == 1


def test_grounder_proposal_on_wrong_entity_safe_halts(tmp_path):
    """The safety pin: the grounder confidently points at a target, but the
    live band is a DIFFERENT entity — the identity gate must halt the run and
    NO click may happen. The grounder cannot buy a wrong target a pass."""
    (tmp_path / "bundle").mkdir()
    report, backend, grounder = _run_with_grounder(
        _GroundOnlyVision(_WRONG_BAND), tmp_path
    )
    assert grounder.calls, "grounder rung must have fired"
    result = report.results[0]
    assert result.resolution is not None
    assert result.resolution.rung == "grounder"
    assert result.identity is not None
    assert result.identity.status == "mismatch"
    assert backend.actions == []  # never clicked
    assert report.success is False
    assert "Identity check failed" in (result.error or "")


def test_healthy_ladder_never_consults_the_grounder(tmp_path):
    """When a deterministic rung resolves, the grounder is never called
    (the hot path stays model-free — the memo's invariant)."""
    (tmp_path / "bundle").mkdir()

    class _TemplateHitVision(_GroundOnlyVision):
        def find_text(self, screen_png, text, **k):
            # OCR rung resolves the target's own label on the true row.
            class M:
                point = (500, 300)
                region = (420, 268, 160, 64)
                confidence = 0.99

            return M() if text == "Open" else None

    report, backend, grounder = _run_with_grounder(
        _TemplateHitVision(_TRUE_BAND), tmp_path
    )
    assert grounder.calls == []  # grounder never consulted
    assert report.model_calls == 0
    assert report.results[0].resolution.rung == "ocr"


# -- corpus-wide composition: false-abort down, false-accept 0.000% ----------


def test_composition_over_frozen_corpora():
    res = run_composition()
    # Every case is ladder-exhausted here, so the grounder fires on all.
    assert res.grounder_fired == res.total_cases
    # Safety invariant: not one wrong-entity case may verify.
    assert res.wrong_total > 3000
    assert res.wrong_false_accepts == 0
    assert res.false_accept_rate == 0.0
    # Availability: a large fraction of present-target halts are recovered.
    assert res.present_total > 2000
    assert res.present_recovered > 0
    assert res.false_abort_reduction > 0.5
    # Ambiguous (v2 'indistinguishable') pairs are not counted as accepts.
    assert res.ambiguous_verified == 0
