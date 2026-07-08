"""Unit tests for the Replayer (openadapt_flow.runtime.replayer).

Backend and vision are both faked — no Playwright, no openadapt_flow.vision.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Landmark,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer

VIEWPORT = (300, 200)


def make_png(size=VIEWPORT, color=(240, 240, 240)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Match:
    def __init__(self, point, region, confidence=0.9):
        self.point = point
        self.region = region
        self.confidence = confidence


class FakeVision:
    """Scripted vision namespace covering everything the Replayer touches."""

    def __init__(self):
        self.template_results: list = []
        self.template_calls: list = []
        self.text_results: dict = {}
        self.text_calls: list = []
        self.ocr_lines: list = []
        self.phash_value = "aa"
        self.phash_dist = 0
        self.settle_count = 0

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

    def ocr(self, screen_png, *, region=None):
        return self.ocr_lines

    def phash_png(self, png, region=None):
        return self.phash_value

    def phash_distance(self, a, b):
        return self.phash_dist

    def wait_settled(self, backend, *, interval_s=0.1, stable_frames=2,
                     timeout_s=3.0):
        self.settle_count += 1
        return backend.screenshot()


class FakeBackend:
    def __init__(self, frame=None, viewport=VIEWPORT):
        self._frame = frame if frame is not None else make_png(viewport)
        self._viewport = viewport
        self.actions: list = []

    @property
    def viewport(self):
        return self._viewport

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press(self, key):
        self.actions.append(("press", key))

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy))


def click_step(step_id="s1", *, risk="reversible", expect=(),
               template="templates/btn.png", ocr_text="Save",
               landmarks=()) -> Step:
    return Step(
        id=step_id,
        intent=f"click '{ocr_text or step_id}'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=template,
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text=ocr_text,
            landmarks=list(landmarks),
        ),
        expect=list(expect),
        risk=risk,
    )


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


def test_happy_path_click_then_param_type(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(expect=[Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
            )]),
            Step(id="s2", intent="type note", action=ActionKind.TYPE,
                 param="note"),
        ],
    )
    replayer = Replayer(backend, vision=vision, poll_interval_s=0.01)
    report = replayer.run(
        workflow, params={"note": "hello world"},
        bundle_dir=bundle, run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [
        ("click", 110, 105, False),
        ("type", "hello world"),
    ]
    assert report.rung_counts == {"template": 1}
    assert report.heal_count == 0
    assert report.model_calls == 0
    assert report.total_ms > 0
    assert report.params == {"note": "hello world"}
    # Run directory artifacts.
    assert (run_dir / "report.json").is_file()
    loaded = RunReport.model_validate(
        json.loads((run_dir / "report.json").read_text())
    )
    assert loaded.success is True
    for step_id in ("s1", "s2"):
        assert (run_dir / f"steps/{step_id}_before.png").is_file()
        assert (run_dir / f"steps/{step_id}_after.png").is_file()
    assert report.results[0].before_png == "steps/s1_before.png"
    assert report.results[0].after_png == "steps/s1_after.png"
    assert report.results[0].postconditions_ok is True
    assert report.results[0].elapsed_ms > 0


def test_missing_param_fails_step_and_aborts_run(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            Step(id="t1", intent="type note", action=ActionKind.TYPE,
                 param="note"),
            Step(id="k1", intent="press enter", action=ActionKind.KEY,
                 key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, params={}, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1  # run aborted; k1 never executed
    assert "note" in report.results[0].error
    assert backend.actions == []  # nothing typed, nothing pressed


def test_param_overrides_recorded_literal_text(bundle, run_dir):
    """Compiled TYPE steps carry BOTH the recorded literal (step.text) and
    the param name; the runtime param value must win over the literal."""
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        params={"note": "recorded value"},
        steps=[Step(id="t1", intent="type <note>", action=ActionKind.TYPE,
                    text="recorded value", param="note")],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, params={"note": "runtime value"},
        bundle_dir=bundle, run_dir=run_dir,
    )
    assert report.success is True
    assert backend.actions == [("type", "runtime value")]
    assert report.params == {"note": "runtime value"}


def test_workflow_params_are_replay_defaults(bundle, run_dir):
    """workflow.params holds recorded example/default values; a replay with
    no explicit params must fall back to them instead of failing."""
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        params={"note": "recorded default"},
        steps=[Step(id="t1", intent="type <note>", action=ActionKind.TYPE,
                    param="note")],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("type", "recorded default")]
    assert report.params == {"note": "recorded default"}


def test_literal_text_type_step(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[Step(id="t1", intent="type literal", action=ActionKind.TYPE,
                    text="fixed text")],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("type", "fixed text")]


def test_risk_gate_blocks_irreversible_step_below_ocr(bundle, run_dir):
    vision = FakeVision()
    # Template file exists but never matches; no ocr_text; landmark resolves
    # -> geometry rung, which is below ocr.
    vision.text_results = {
        "Note": Match(point=(100, 50), region=(80, 45, 40, 10), confidence=0.7)
    }
    backend = FakeBackend()
    step = click_step(
        step_id="danger",
        risk="irreversible",
        ocr_text=None,
        landmarks=[Landmark(relation="left_of", ocr_text="Note",
                            distance_px=40)],
    )
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    result = report.results[0]
    assert result.ok is False
    assert "human confirmation" in result.error
    assert "danger" in result.error
    assert backend.actions == []  # DID NOT act
    assert result.resolution is not None  # resolution recorded for the report
    assert result.resolution.rung == "geometry"
    assert report.rung_counts == {}  # failed steps don't count
    assert result.heal is None


def test_postcondition_passes_after_resettle_retry(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    # First check fails (timeout_s=0 expires immediately); the single
    # re-settle retry then sees the text.
    vision.text_results = {
        "Done": [None,
                 Match(point=(10, 10), region=(5, 5, 20, 8), confidence=0.9)]
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step(expect=[Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text="Done", timeout_s=0.0
        )])],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert report.results[0].postconditions_ok is True
    # settle before + settle after action + one re-settle retry
    assert vision.settle_count == 3


def test_postcondition_polling_passes_within_timeout(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {
        "Done": [None, None,
                 Match(point=(10, 10), region=(5, 5, 20, 8), confidence=0.9)]
    }
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step(expect=[Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text="Done", timeout_s=1.0
        )])],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # Passed inside the polling loop -> no re-settle retry needed.
    assert vision.settle_count == 2


def test_semantic_drift_aborts_run_with_named_step(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.text_results = {"Banner": None}  # never appears
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            click_step(expect=[Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text="Banner",
                timeout_s=0.05,
            )]),
            Step(id="s2", intent="never runs", action=ActionKind.KEY,
                 key="Enter"),
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert len(report.results) == 1  # s2 never ran
    result = report.results[0]
    assert result.postconditions_ok is False
    assert "s1" in result.error
    assert "drift" in result.error
    # The report must embed the step's before/after screenshots.
    assert result.before_png == "steps/s1_before.png"
    assert result.after_png == "steps/s1_after.png"
    assert (run_dir / result.before_png).is_file()
    assert (run_dir / result.after_png).is_file()
    assert ("press", "Enter") not in backend.actions
    saved = json.loads((run_dir / "report.json").read_text())
    assert saved["success"] is False


def test_region_stable_postcondition_uses_phash(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision.phash_dist = 4  # within tolerance of 8
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[click_step(expect=[Postcondition(
            kind=PostconditionKind.REGION_STABLE, region=(0, 0, 40, 30),
            phash="deadbeef", phash_tolerance=8, timeout_s=0.2,
        )])],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True

    # Now exceed the tolerance: the same postcondition must fail.
    vision2 = FakeVision()
    vision2.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
    ]
    vision2.phash_dist = 20
    report2 = Replayer(FakeBackend(), vision=vision2,
                       poll_interval_s=0.01).run(
        Workflow(name="wf", steps=[click_step(expect=[Postcondition(
            kind=PostconditionKind.REGION_STABLE, region=(0, 0, 40, 30),
            phash="deadbeef", phash_tolerance=8, timeout_s=0.05,
        )])]),
        bundle_dir=bundle, run_dir=run_dir,
    )
    assert report2.success is False


def test_wait_step_only_settles(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[Step(id="w1", intent="wait for app", action=ActionKind.WAIT)],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == []  # no input injected
    assert vision.settle_count == 2  # settle before + settle after


def test_scroll_step_scrolls_backend(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[
            Step(id="sc1", intent="scroll by (0, 400)",
                 action=ActionKind.SCROLL, scroll_dx=0, scroll_dy=400),
            Step(id="sc2", intent="scroll by (-30, -120)",
                 action=ActionKind.SCROLL, scroll_dx=-30, scroll_dy=-120),
        ],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("scroll", 0, 400), ("scroll", -30, -120)]
    # No anchor -> no resolution, no heal.
    assert report.results[0].resolution is None
    assert report.heal_count == 0


def test_key_step_presses_key(bundle, run_dir):
    vision = FakeVision()
    backend = FakeBackend()
    workflow = Workflow(
        name="wf",
        steps=[Step(id="k1", intent="press enter", action=ActionKind.KEY,
                    key="Enter")],
    )
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("press", "Enter")]


def test_unresolvable_click_step_fails_without_acting(bundle, run_dir):
    vision = FakeVision()  # everything misses
    backend = FakeBackend()
    step = click_step()
    step.timeout_s = 0.2  # keep the resolution retry budget short in tests
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert backend.actions == []
    assert "resolve" in report.results[0].error.lower()
    assert "s1" in report.results[0].error


def test_resolution_retries_until_target_appears(bundle, run_dir):
    """A ladder miss on a stale frame retries with fresh settled frames
    until step.timeout_s (Step.timeout_s is the resolution retry budget)."""
    vision = FakeVision()
    # First OCR lookups miss (still-loading screen); then the label appears.
    vision.text_results["Save"] = [
        None,
        None,
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95),
    ]
    backend = FakeBackend()
    step = click_step(template="templates/missing.png")  # no template rungs
    step.timeout_s = 5.0
    workflow = Workflow(name="wf", steps=[step])
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions
    assert report.results[0].resolution.rung == "ocr"
    assert vision.settle_count >= 3  # initial + at least two retries
