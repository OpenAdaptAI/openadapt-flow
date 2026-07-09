"""Track B — mid-run fault injection (adversarial validation).

Sabotages MockMed state BETWEEN steps of a live replay via ``ChaosBackend``
and pins down halt-vs-improvise behavior. Several tests characterize known
failure modes (silent wrong-patient save, silent empty-note save); see
``docs/validation/VALIDATION.md`` for the ranked matrix.

Click order in the canonical demo: 1=username 2=password 3=sign-in 4=Open
5=New Encounter 6=Triage 7=note field 8=Save. Type order: 1=username
2=password 3=note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend

from .conftest import PARAMS
from .validation_utils import ChaosBackend, describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)


def chaos(inject, *, after_click=None, after_type=None):
    """Backend factory wiring an injection to a click/type trigger."""

    def factory(page) -> PlaywrightBackend:
        return ChaosBackend(
            page, inject=inject, after_click=after_click, after_type=after_type
        )

    return factory


def run(bundle, mockmed_url, _browser, tmp_path: Path, factory):
    return replay_on_page(
        _browser, bundle.dir, mockmed_url, tmp_path / "run",
        params=dict(PARAMS), backend_factory=factory,
    )


class TestEntityDeletedMidRun:
    def test_target_row_deleted_after_login_saves_to_wrong_patient(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """WRONG-ACTION, SILENT. Between signing in and clicking 'Open',
        the target patient's referral row is deleted (as if another user
        claimed it). Desired: safe halt. Observed: the next patient's row
        slides into the recorded position, resolves, and the encounter is
        saved to THAT patient with a green report — the mid-run twin of the
        drift=missing perturbation. If this test fails, the failure mode
        was fixed: update docs/validation/VALIDATION.md and invert it."""

        def inject(page):
            page.wait_for_selector("#open-p1", timeout=5000)
            page.evaluate(
                "document.getElementById('open-p1').closest('tr').remove()"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_click=3),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p2", describe(report, state)
        assert state["banner"] is not None


class TestBlockingModalMidRun:
    def test_opaque_overlay_before_save_halts_without_clicking(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT. An opaque 'maintenance' overlay appears after the
        note is typed. Every resolution rung fails (nothing recorded is
        visible), the ladder retries until the step timeout, and the run
        aborts naming the save step — no click was fired into the overlay."""

        def inject(page):
            page.evaluate(
                "var d = document.createElement('div');"
                "d.id = 'chaos-overlay';"
                "d.style.cssText = 'position:fixed;inset:0;background:#222a35;"
                "color:#fff;z-index:9999;display:flex;align-items:center;"
                "justify-content:center;font-size:28px';"
                "d.textContent = 'System maintenance in progress';"
                "document.body.appendChild(d);"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_type=3),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_010", describe(report, state)
        assert failed.resolution is None  # halted BEFORE acting
        assert "Could not resolve" in (failed.error or "")

    def test_invisible_click_shield_before_save_halts_after_wasted_click(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT (with one neutralized click). A fully transparent
        overlay intercepts pointer events. Vision sees an unchanged screen,
        resolves the save button, and clicks — into the shield. Nothing
        happens, postconditions fail, the run aborts. No state was written,
        but note the runtime cannot tell 'button clicked and app ignored
        it' from 'click never reached the app'."""

        def inject(page):
            page.evaluate(
                "var d = document.createElement('div');"
                "d.id = 'chaos-shield';"
                "d.style.cssText = "
                "'position:fixed;inset:0;background:transparent;z-index:9999';"
                "document.body.appendChild(d);"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_type=3),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_010", describe(report, state)
        assert failed.resolution is not None  # it DID click
        assert failed.postconditions_ok is False


class TestLayoutSwapMidRun:
    def test_swapped_type_buttons_still_select_the_right_type(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """PASS (healed). 'Triage' and 'Consult' swap positions after the
        encounter form renders. Their labels differ, so lower rungs
        re-locate the true 'Triage' at its new position and the saved
        encounter carries the correct type. Swaps of visually IDENTICAL
        controls are the lookalike case in test_perturbation.py — those go
        wrong."""

        def inject(page):
            page.wait_for_selector("#type-consult", timeout=5000)
            page.evaluate(
                "var seg = document.getElementById('type-seg');"
                "seg.insertBefore(document.getElementById('type-consult'),"
                "document.getElementById('type-triage'));"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_click=5),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        assert state["enc_item"] is not None
        assert state["enc_item"].startswith("Triage"), describe(report, state)


class TestFocusStolenBeforeTyping:
    def test_blur_between_click_and_type_saves_empty_note_silently(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """WRONG-ACTION (wrong state written), SILENT. Focus is stolen
        between clicking the note field and typing — a one-line stand-in
        for any app that re-renders or pops a late dialog at the wrong
        moment. The keystrokes fall on <body>, the note is lost, the
        encounter is saved EMPTY, and the run reports success: TYPE steps
        never verify the field content, and parameterized values are (by
        design) excluded from every postcondition, so nothing checks the
        note arrived. If this fails, the failure mode was fixed — invert."""

        def inject(page):
            page.evaluate(
                "document.activeElement && document.activeElement.blur()"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_click=7),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        # The banner proves the save; its bare text proves the note is gone.
        assert state["banner"] is not None
        assert state["banner"].strip() == "Encounter saved —", describe(
            report, state
        )


class TestNavigationHijackMidRun:
    def test_navigate_away_before_save_halts_without_saving(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """SAFE-HALT. The app navigates back to the task list right after
        the note-field click (session bounce, deep-link, etc.). The
        note-field step's own postconditions fail on the wrong screen and
        the run aborts before anything is typed or saved."""

        def inject(page):
            page.evaluate("location.hash = '#tasks'")

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_click=7),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        assert state["hash"] == "#tasks"
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_008", describe(report, state)


class TestTargetRenamedMidRun:
    def test_save_button_renamed_mid_run_heals_and_saves(
        self, bundle, mockmed_url, _browser, tmp_path
    ) -> None:
        """PASS (healed). The save button's label changes to 'Commit
        Record' after the form renders. Template and OCR evidence die, the
        geometry rung (landmarks are unchanged) resolves it, the anchor is
        healed, and the encounter saves normally."""

        def inject(page):
            page.wait_for_selector("#save-encounter", timeout=5000)
            page.evaluate(
                "document.getElementById('save-encounter')"
                ".textContent = 'Commit Record'"
            )

        report, state = run(
            bundle, mockmed_url, _browser, tmp_path,
            chaos(inject, after_click=5),
        )
        assert report.success is True, describe(report, state)
        assert state["hash"] == "#patient/p1"
        assert state["banner"] is not None
        assert report.heal_count >= 1, describe(report, state)
