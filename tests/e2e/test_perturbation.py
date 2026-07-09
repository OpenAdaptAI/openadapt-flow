"""Track A — perturbation/drift matrix (adversarial validation).

Replays the SAME compiled bundle (recorded at 1280x800, default MockMed)
under one perturbation at a time and pins down what actually happens. These
are CHARACTERIZATION tests: several of them assert on known failure modes —
including silent wrong-patient writes — so that any change in behavior
(fix or regression) is caught loudly. The experiment matrix, severity
ranking, and evidence pointers live in ``docs/validation/VALIDATION.md``.

Outcome vocabulary used in comments below (see VALIDATION.md):

- pass:         run succeeded and did what the demonstration did.
- safe-halt:    run stopped with an accurate report; no further actions.
- false-abort:  safe-halt whose cause was cosmetic, not semantic.
- wrong-action: an action executed on the wrong target, or wrong state
  written. THE critical class. Some wrong-actions below end in a reported
  SUCCESS — those are the headline findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PARAMS, drift_url
from .validation_utils import describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


class TestViewportChange:
    """MockMed's layout is left-anchored with max-width 960, so growing the
    window does not move anything: this pins the (unearned) easy case."""

    @pytest.mark.parametrize("viewport", [(1440, 900), (1024, 768)])
    def test_larger_and_smaller_viewports_pass(
        self, bundle, mockmed_url, _browser, run_dir, viewport
    ) -> None:
        report, state = replay_on_page(
            _browser, bundle.dir, mockmed_url, run_dir,
            params=dict(PARAMS), viewport=viewport,
        )
        assert report.success, describe(report, state)
        assert report.heal_count == 0
        assert state["hash"] == "#patient/p1"

    def test_short_viewport_halts_without_saving(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """900x360 leaves the Save button below the fold. The workflow
        recorded no scrolls, so the replayer has no recorded gesture to
        adapt (closed-loop scrolling only extends RECORDED scroll steps) —
        the run must halt without saving anything. Observed: a FALSE-ABORT
        arrives even earlier, at the note-field click, because that step's
        REGION_STABLE region extends below the smaller viewport."""
        report, state = replay_on_page(
            _browser, bundle.dir, mockmed_url, run_dir,
            params=dict(PARAMS), viewport=(900, 360),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None  # nothing was saved
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id in ("step_008", "step_010"), describe(report, state)


class TestScaleChanges:
    """Device-scale-factor and CSS zoom perturbations.

    Both defeat every resolution rung's assumptions (the template scale
    ladder tops out at 1.18x; OCR/landmark coordinates come back in frame
    pixels that no longer equal input pixels under dsf=2). The safety net
    holds — postconditions abort the run at the FIRST step — but a purely
    cosmetic 125% zoom rendering the workflow 0% replayable is an
    availability failure worth stating plainly."""

    def test_device_scale_factor_2_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser, bundle.dir, mockmed_url, run_dir,
            params=dict(PARAMS), device_scale_factor=2,
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)

    def test_css_zoom_125_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "zoom"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)


class TestFontDrift:
    def test_font_size_bump_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """A user-side font-size preference (16px -> 19px) reflows text and
        shifts layout. Unlike theme drift (which the README showcases as
        fully healed), font drift FALSE-ABORTS at the very first step: the
        REGION_STABLE postcondition's phash cannot tolerate reflowed glyph
        metrics, and its stored template crop no longer matches either.
        Cosmetic drift, zero replayability."""
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "font"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)
        assert failed.postconditions_ok is False


class TestDataDrift:
    """Rows added/removed between recording and replay — the dangerous
    quadrant. The discriminative evidence for a table row's button (the
    patient's NAME) sits outside the 160x64 template crop, and the
    compiler's timestamp filter drops the patient banner from the click
    step's postconditions (the DOB reads as a date), leaving only the
    patient-AGNOSTIC 'No encounters yet.' — so a wrong row click sails
    through every check and the encounter is SAVED TO THE WRONG PATIENT
    with a green report."""

    def test_lookalike_row_above_target_saves_to_wrong_patient(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """WRONG-ACTION, SILENT. A new referral with the same reason and
        priority as the target lands directly above it: its Open-button
        crop is pixel-identical to the recorded template, at exactly the
        recorded position. The template rung matches it with confidence
        ~1.0, the encounter is saved to the imposter, and the run reports
        success. If this test ever fails, the failure mode was FIXED —
        update docs/validation/VALIDATION.md and invert the assertions."""
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "lookalike"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p0", describe(report, state)  # imposter
        assert state["banner"] is not None  # encounter really was saved

    def test_missing_target_row_saves_to_wrong_patient(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """WRONG-ACTION, SILENT. The target referral is GONE. The desired
        behavior is a safe halt ('never click a lookalike'); instead the
        neighbouring patient's row now occupies the recorded position,
        every rung that fires resolves to it (template: near-identical
        button crop; ocr: first 'Open' on screen; geometry: header landmark
        offset), and the encounter is saved to that patient with a green
        report."""
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "missing"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p2", describe(report, state)  # Alex, not Jane
        assert state["banner"] is not None

    def test_data_growth_reports_success_without_verifying_patient(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """Four unrelated referrals arrive above the target, shifting it
        ~228px down (outside the local search pad). The run reports success
        either way, but NOTHING in the bundle verifies which patient
        received the encounter: on the recording platform the local
        template rung matched the FIRST imposter row (>=0.985 despite
        different reason/priority text in the crop) and saved to the wrong
        patient. Whether the click lands on the imposter (local rung) or
        the true target (global rung finds the identical crop lower down)
        is platform/rendering-dependent — which is exactly the problem."""
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "grow"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is True, describe(report, state)
        assert state["banner"] is not None
        assert state["hash"] in ("#patient/g1", "#patient/p1"), describe(
            report, state
        )

    def test_empty_state_halts_before_reaching_the_table(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """SAFE-HALT (by luck, one step early): with no referrals at all,
        the SIGN-IN step's own postcondition — which asserts another data
        row's text ('Cardiology follow-up') as if it were an invariant —
        fails first. The halt is safe, but note the mechanism: postcondition
        text mined from mutable table DATA, not from chrome."""
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "empty"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id in ("step_004", "step_005"), describe(report, state)


class TestSlowApp:
    """Timeout envelope: renders delayed 4s recover (postcondition polling
    plus ladder retry absorb them); renders delayed 12s exceed the ~5.5s
    postcondition window and safe-halt with an accurate report."""

    def test_4s_render_delay_recovers(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser, bundle.dir, drift_url(mockmed_url, "slow"), run_dir,
            params=dict(PARAMS),
        )
        assert report.success, describe(report, state)
        assert state["hash"] == "#patient/p1"

    def test_12s_render_delay_halts_safely(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser, bundle.dir,
            drift_url(mockmed_url, "slow") + "&slowms=12000", run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_004", describe(report, state)
        assert "Postconditions failed" in (failed.error or "")
