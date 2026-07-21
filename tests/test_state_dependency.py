"""State-dependency robustness (docs/LIMITS.md "state dependency").

Replay must not assume an identical starting state. These tests pin three
model-free defenses, all added around the unchanged hardened action leaf:

* ``wait_settled_result`` exposes whether the screen actually settled, so a
  caller can refuse to act on a stale / mid-transition frame (the flagged
  "wait_settled proceeds on stale frames" bug);
* the replayer's opt-in ``require_settled`` readiness gate HALTs gracefully on a
  screen that never settles (a slow load), rather than acting on it;
* a KNOWN interstitial (survey modal, "What's New" notice, cookie banner) at a
  step's entry is auto-dismissed (then re-settled) or HALTed on gracefully with
  a clear report, never a blind wrong action.

Backend and vision are faked (reused from test_replayer) -- no Playwright, no
OCR stack, ZERO model calls.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.ir import (
    Anchor,
    Interstitial,
    Predicate,
    PredicateKind,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision import wait_settled, wait_settled_result
from tests.test_replayer import (
    FakeBackend,
    FakeVision,
    Match,
    click_step,
    make_png,
)

VIEWPORT = (300, 200)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture()
def bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    (bundle_dir / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    return bundle_dir


@pytest.fixture()
def run_dir(tmp_path):
    return tmp_path / "run"


class _AnimatedBackend(FakeBackend):
    """A backend whose frame changes every screenshot -- it never settles."""

    def __init__(self, viewport=VIEWPORT):
        super().__init__(viewport=viewport)
        self._tick = 0

    def screenshot(self):
        # Move a large black bar across the frame each poll so the PERCEPTUAL
        # hash actually differs frame to frame (a flat shade change would hash
        # identically and count as settled).
        self._tick += 1
        img = Image.new("RGB", self._viewport, (240, 240, 240))
        w, h = self._viewport
        x = (self._tick * 37) % max(1, w - 40)
        for px in range(x, min(x + 40, w)):
            for py in range(h):
                img.putpixel((px, py), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class SettleVision(FakeVision):
    """FakeVision plus the readiness-aware ``wait_settled_result`` the real
    vision module exposes. ``settled`` is scripted so a test can force the
    never-settling (slow-load) path deterministically."""

    def __init__(self, settled: bool = True):
        super().__init__()
        self.settled = settled
        self.result_calls = 0

    def wait_settled_result(
        self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0
    ):
        from openadapt_flow.vision import SettleResult

        self.result_calls += 1
        png = backend.screenshot()
        return SettleResult(
            png=png,
            settled=self.settled,
            stable_frames=stable_frames if self.settled else 1,
            required_frames=stable_frames,
            elapsed_s=0.0,
        )


# -- wait_settled_result: the settled signal is no longer swallowed ----------


def test_wait_settled_result_reports_settled_on_a_stable_screen():
    backend = FakeBackend()  # returns one fixed frame forever
    result = wait_settled_result(backend, interval_s=0.001, stable_frames=2)
    assert result.settled is True
    assert result.png == backend.screenshot()


def test_wait_settled_result_reports_unsettled_on_a_changing_screen():
    backend = _AnimatedBackend()
    result = wait_settled_result(
        backend, interval_s=0.001, stable_frames=3, timeout_s=0.05
    )
    assert result.settled is False
    assert result.stable_frames < result.required_frames


def test_wait_settled_wrapper_returns_frame_and_warns(caplog):
    """The bare wrapper still returns the most recent frame on timeout, but the
    not-settled fact is now recoverable (via wait_settled_result) and logged."""
    backend = _AnimatedBackend()
    import logging

    with caplog.at_level(logging.WARNING):
        png = wait_settled(backend, interval_s=0.001, stable_frames=3, timeout_s=0.05)
    assert isinstance(png, bytes)
    assert any("did not settle" in r.message for r in caplog.records)


# -- require_settled readiness gate: never act on a mid-transition frame ------


def test_require_settled_halts_on_a_screen_that_never_settles(bundle, run_dir):
    """A slow-loading / perpetually-animating screen HALTs gracefully instead
    of acting on a stale frame -- and never issues the click."""
    vision = SettleVision(settled=False)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = _AnimatedBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is False
    assert "starting state not ready" in report.results[0].error
    assert backend.actions == []  # no click on the un-ready screen


def test_require_settled_proceeds_once_the_screen_settles(bundle, run_dir):
    vision = SettleVision(settled=True)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        require_settled=True,
        settle_readiness_timeout_s=0.05,
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert ("click", 110, 105, False) in backend.actions


def test_require_settled_off_by_default_preserves_behavior(bundle, run_dir):
    """Default (off): even a never-settling screen acts exactly as before, so
    inherently-animated UIs are not over-halted and behavior is unchanged."""
    vision = SettleVision(settled=False)
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = _AnimatedBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert any(a[0] == "click" for a in backend.actions)


def test_require_settled_degrades_when_facade_cannot_report(bundle, run_dir):
    """A lightweight vision facade without wait_settled_result must not crash or
    block when require_settled is on -- it falls back to proceed-with-warning."""
    vision = FakeVision()  # no wait_settled_result
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(
        backend, vision=vision, poll_interval_s=0.005, require_settled=True
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True


# -- interstitials: dismiss-if-known, else halt gracefully -------------------


def _present_then_absent(match: Match) -> list:
    """Scripted find_text/text_present: present on the first probe, absent
    after (i.e. once the interstitial is dismissed)."""
    return [match, None]


def test_known_interstitial_dismissed_by_key_then_step_runs(bundle, run_dir):
    vision = FakeVision()
    vision.text_results = {
        "rate us": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="satisfaction survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    # The overlay was dismissed BEFORE the target click.
    assert backend.actions[0] == ("press", "Escape")
    assert ("click", 110, 105, False) in backend.actions


def test_known_interstitial_dismissed_by_anchor_click(bundle, run_dir):
    vision = FakeVision()
    vision.text_results = {
        "What's New": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    # First find_template resolves the dismiss button; second resolves the step.
    vision.template_results = [
        Match((200, 20), (190, 10, 30, 20)),  # "Continue" button
        Match((110, 105), (100, 100, 50, 20)),  # the step's target
    ]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="release notice",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="What's New"),
                dismiss_anchor=Anchor(
                    template="templates/btn.png",
                    region=(190, 10, 30, 20),
                    click_point=(200, 20),
                    ocr_text="Continue",
                ),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions[0] == ("click", 200, 20, False)  # dismiss first
    assert ("click", 110, 105, False) in backend.actions  # then the target


def test_blocking_interstitial_with_no_dismissal_halts_gracefully(bundle, run_dir):
    """A known interstitial with no safe auto-dismissal HALTs naming it -- a
    clear report, and it never issues the underlying click."""
    vision = FakeVision()
    vision.text_results = {"maintenance": Match((10, 10), (0, 0, 5, 5))}
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="maintenance banner",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="maintenance"),
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "maintenance banner" in report.results[0].error
    assert "blocking interstitial" in report.results[0].error
    assert backend.actions == []  # never clicked beneath the overlay


def test_interstitial_that_persists_halts_after_bounded_attempts(bundle, run_dir):
    """An interstitial that keeps re-appearing despite dismissal HALTs (safe
    direction) rather than looping forever or acting beneath it."""
    vision = FakeVision()
    # Always present (never absent): dismissal never clears it.
    vision.text_results = {"stuck": Match((10, 10), (0, 0, 5, 5))}
    backend = FakeBackend()
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="stuck modal",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="stuck"),
                dismiss_key="Escape",
            )
        ],
    )
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert "persisted" in report.results[0].error
    assert backend.actions.count(("press", "Escape")) == 3  # bounded attempts
    assert not any(a[0] == "click" for a in backend.actions)


def test_operator_supplied_interstitial_applies_without_recompiling(bundle, run_dir):
    """An operator can pass interstitials to the Replayer; they merge with the
    bundle's own (here the bundle declares none)."""
    vision = FakeVision()
    vision.text_results = {
        "cookies": _present_then_absent(Match((10, 10), (0, 0, 5, 5)))
    }
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])  # no bundle interstitials
    report = Replayer(
        backend,
        vision=vision,
        poll_interval_s=0.005,
        interstitials=[
            Interstitial(
                name="cookie banner",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="cookies"),
                dismiss_key="Enter",
            )
        ],
    ).run(wf, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    assert backend.actions[0] == ("press", "Enter")


def test_no_interstitials_declared_is_byte_for_byte_unchanged(bundle, run_dir):
    vision = FakeVision()
    vision.template_results = [Match((110, 105), (100, 100, 50, 20))]
    backend = FakeBackend()
    wf = Workflow(name="wf", steps=[click_step()])
    report = Replayer(backend, vision=vision, poll_interval_s=0.005).run(
        wf, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert backend.actions == [("click", 110, 105, False)]


# -- IR round-trip ------------------------------------------------------------


def test_interstitials_round_trip_through_bundle(bundle):
    wf = Workflow(
        name="wf",
        steps=[click_step()],
        interstitials=[
            Interstitial(
                name="survey",
                detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="rate us"),
                dismiss_key="Escape",
            )
        ],
    )
    out = bundle / "wf"
    wf.save(out)
    loaded = Workflow.load(out)
    assert len(loaded.interstitials) == 1
    assert loaded.interstitials[0].name == "survey"
    assert loaded.interstitials[0].dismiss_key == "Escape"
    assert loaded.interstitials[0].detect.kind == PredicateKind.TEXT_PRESENT
