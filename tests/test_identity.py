"""Unit tests for the target-identity layer (runtime.identity).

Import-light on purpose (no cv2/OCR): everything here exercises the pure
matching logic. The four adversarial-review probes are pinned verbatim —
each was a reproduced silent wrong-verify (or false abort) against the
first identity implementation, and must never come back:

1. B1 wrong entity: shared long row text dominated char coverage, so
   "Ann Wu <same procedure>" verified at 0.89 against a "Jane Li" band.
2. B1 generic band: "Active High 3" vs "Active High 7" verified at 0.91.
3. B2 param disarm: any short param demo value in the band ("High")
   switched to param mode, which ignored the band residue entirely — a
   wrong patient verified at 1.0.
4. P1a: param mode verified ANY row containing the run's value (a
   messages row mentioning "Susan" verified for patient "Susan").

Plus the modal-overlay false abort (order-sensitive matching scored a
token permutation of the same band at ~0.66 < 0.8 on live OpenEMR).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.runtime.identity import (
    COVERAGE_THRESHOLD,
    MIN_CONTEXT_CHARS,
    MIN_PARAM_CHARS,
    UNCOVERED_RUN_CAP,
    band_match,
    band_region,
    context_from_lines,
    coverage,
    embedded_params,
    lines_near_point,
    longest_run,
    required_run,
    squash,
    substitute_param,
    tokenize,
    verify_target_identity,
)

ROW = "Jane Li Comprehensive metabolic panel with lipid screening High"
WRONG_ROW = "Ann Wu Comprehensive metabolic panel with lipid screening High"


def line(text: str, region=(0, 100, 200, 20), confidence: float = 0.9):
    return SimpleNamespace(text=text, region=region, confidence=confidence)


# -- the reviewer probes, verbatim ------------------------------------------


class TestReviewerProbes:
    def test_b1_wrong_entity_with_shared_procedure_text_mismatches(self):
        """Shared row text must not buy a wrong name a pass: 'Jane Li' ->
        'Ann Wu' is a 6-char contiguous uncovered run, over the cap, even
        though raw coverage is ~0.89."""
        check = verify_target_identity(ROW, WRONG_ROW)
        assert check.status == "mismatch"
        cov, uncovered = band_match(ROW, WRONG_ROW)
        assert cov >= COVERAGE_THRESHOLD  # coverage alone WOULD pass
        assert uncovered > UNCOVERED_RUN_CAP  # the residue cap catches it

    def test_b1_generic_band_never_arms(self):
        """'Active High 3' (11 squashed chars) is generic — any sibling row
        shares it. Too short to discriminate: unreadable, never verified."""
        assert len(squash("Active High 3")) < MIN_CONTEXT_CHARS
        check = verify_target_identity("Active High 3", "Active High 7")
        assert check.status == "unreadable"

    def test_b2_param_value_in_band_cannot_disarm_the_check(self):
        """A param demo value ('High') embedded in the band must not switch
        the check into a mode that ignores the band: the substituted band's
        non-param residue still has to match."""
        check = verify_target_identity(
            ROW,
            "Ann Wu Totally different row content High",
            params={"priority": "High"},
            param_examples={"priority": "High"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"

    def test_p1a_any_row_containing_the_value_does_not_verify(self):
        """Param mode: a messages row mentioning 'Susan' is not Susan's
        target row — the recorded band's residue is missing."""
        check = verify_target_identity(
            "Open chart for Phil (active)",
            "Message from Susan re lab results",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"
        assert check.param == "patient"


# -- true-positive behavior around the probes --------------------------------


class TestTruePositives:
    def test_true_row_verifies(self):
        assert verify_target_identity(ROW, ROW).status == "verified"

    def test_ocr_jitter_verifies(self):
        """Per-character OCR noise ('paln' ~ 'pain', '5ample' ~ 'sample')
        must not abort a correct target."""
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Jane 5ample Knee paln referral Hlgh",
        )
        assert check.status == "verified"

    def test_token_permutation_verifies(self):
        """Live OpenEMR false abort: page chrome around a modal re-reads in
        a different segmentation order — same tokens, different order and
        merging. Order must not matter."""
        check = verify_target_identity(
            "PPV + Show All <Back to Patient ShowActive",
            "<Back to Patient PPV + Show All Show Active",
        )
        assert check.status == "verified"

    def test_param_mode_verifies_when_residue_is_stable(self):
        """The legitimate param re-anchor: the run's value replaced the
        demo's, everything else on the row still matches."""
        check = verify_target_identity(
            "Open chart for Phil (active)",
            "Open chart for Susan (active)",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "verified"
        assert check.mode == "param"
        assert check.param == "patient"

    def test_param_mode_mismatches_when_entity_row_text_varies_too(self):
        """DISCLOSED LIMIT (LIMITS.md): when the non-param row text also
        varies with the entity (a patient search result carries the
        surname), the substituted band cannot match and the run halts —
        safety over availability; clicking by position is what caused the
        wrong-patient writes."""
        check = verify_target_identity(
            "Belford, Phil MRN A12",
            "Underwood, Susan Ardmore",
            params={"patient": "Susan"},
            param_examples={"patient": "Phil"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"


class TestWrongEntities:
    def test_lookalike_row_mismatches(self):
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Taylor Duplicate Knee pain referral High",
        )
        assert check.status == "mismatch"

    def test_unrelated_row_mismatches(self):
        check = verify_target_identity(
            "Jane Sample Knee pain referral High",
            "Pat Placeholder Orthopedics intake Low",
        )
        assert check.status == "mismatch"
        assert check.coverage == 0.0

    def test_empty_observed_is_unreadable(self):
        check = verify_target_identity(ROW, "   ")
        assert check.status == "unreadable"


# -- band_match internals ------------------------------------------------------


class TestBandMatch:
    def test_exact_match(self):
        assert band_match(ROW, ROW) == (1.0, 0)

    def test_adjacent_unmatched_tokens_merge_into_one_run(self):
        cov, uncovered = band_match(ROW, WRONG_ROW)
        assert uncovered == len("janeli")

    def test_separated_unmatched_tokens_do_not_merge(self):
        cov, uncovered = band_match(
            "alpha beta gamma delta", "alpha WRONG gamma NOPE"
        )
        assert uncovered == max(len("beta"), len("delta"))

    def test_short_tokens_match_only_verbatim(self):
        # 'li' must not match inside 'lipid'.
        cov, _ = band_match("li", "lipid screening")
        assert cov == 0.0
        cov, _ = band_match("li", "jane li")
        assert cov == 1.0

    def test_containment_tolerates_merged_tokens(self):
        # Recorded 'ShowActive' vs observed 'Show Active' (and vice versa).
        cov, uncovered = band_match("ShowActive", "Show Active")
        assert cov == 1.0 and uncovered == 0

    def test_empty_inputs(self):
        assert band_match("", "anything") == (0.0, 0)
        cov, uncovered = band_match("abcdef", "")
        assert cov == 0.0 and uncovered == 6

    def test_coverage_boundary_at_threshold(self):
        """Exactly one 4-char token uncovered out of 20 chars: coverage
        0.8 == threshold and residue 4 == cap pass; a 5-char uncovered
        token fails the cap."""
        cov, uncovered = band_match(
            "abcd efgh ijkl mnop qrst", "abcd efgh ijkl mnop XXXX"
        )
        assert cov == pytest.approx(0.8)
        assert uncovered == 4
        # ok at the boundary:
        expected = "abcd efgh ijkl mnop qrst"
        check = verify_target_identity(expected, "abcd efgh ijkl mnop XXXX")
        assert check.status == "verified"
        # one char more of contiguous residue fails:
        check = verify_target_identity(
            "abcd efgh ijkl mnop qrstu", "abcd efgh ijkl mnop XXXXX"
        )
        assert check.status == "mismatch"

    def test_tokenize(self):
        assert tokenize("  Jane   Li \n panel ") == ["jane", "li", "panel"]
        assert tokenize("") == []


# -- helpers ---------------------------------------------------------------


class TestHelpers:
    def test_longest_run(self):
        assert longest_run("abcdef", "xxabcdefyy") == 6
        assert longest_run("abcdef", "abcXdef") == 3
        assert longest_run("", "abc") == 0
        assert longest_run("abc", "") == 0

    def test_required_run_scales(self):
        assert required_run(30) == 16  # capped
        assert required_run(16) == 13  # ~80%
        assert required_run(5) == 4
        assert required_run(3) == 3  # floor
        assert required_run(1) == 3

    def test_coverage_ignores_scattered_short_blocks(self):
        assert coverage("abcxyzdef", "abc123def") == pytest.approx(6 / 9)
        assert coverage("abcdef", "abcdef") == 1.0
        assert coverage("", "x") == 0.0

    def test_band_region_clamps_to_viewport(self):
        assert band_region((500, 10), 64, (1280, 800)) == (0, 0, 1280, 64)
        assert band_region((500, 795), 64, (1280, 800)) == (0, 736, 1280, 64)
        assert band_region((500, 400), 64, (1280, 800)) == (0, 368, 1280, 64)
        # Band taller than the viewport clamps to the viewport.
        assert band_region((10, 10), 2000, (100, 50)) == (0, 0, 100, 50)

    def test_embedded_params_edges(self):
        band = "Open chart for Phil (active)"
        assert embedded_params(band, {"patient": "Phil"}) == ["patient"]
        # Below MIN_PARAM_CHARS: never triggers param mode.
        short = "x" * (MIN_PARAM_CHARS - 1)
        assert embedded_params(f"row with {short}", {"p": short}) == []
        assert embedded_params(band, {"patient": ""}) == []
        assert embedded_params(band, {}) == []
        # Value not in the band: no param mode.
        assert embedded_params(band, {"patient": "Zebra"}) == []

    def test_substitute_param_verbatim(self):
        assert (
            substitute_param("Open chart for Phil now", "Phil", "Susan")
            == "Open chart for Susan now"
        )
        # Case-insensitive.
        assert (
            substitute_param("open PHIL chart", "Phil", "Susan")
            == "open Susan chart"
        )

    def test_substitute_param_fallback_drops_value_tokens(self):
        # The demo value never appears verbatim (OCR mangled it): tokens
        # belonging to it are dropped and the run's value appended —
        # matching is order-insensitive so position does not matter.
        out = substitute_param("Open chart for Phi1 now", "Phil", "Susan")
        assert "Phi1" not in out
        assert "Susan" in out
        assert "Open" in out and "chart" in out

    def test_substitute_param_empty_example(self):
        assert substitute_param("band text", "  ", "x") == "band text"


# -- row refinement (one-row-off) -------------------------------------------


class TestLinesNearPoint:
    def test_one_row_off_resolution_reads_the_wrong_row_only(self):
        """Dense table, ~25px rows, 64px coarse band: a point resolved one
        row off must read ITS row's text, not the adjacent true row's —
        text bleed from the true row must not verify a wrong-row click."""
        true_row = line("Jane Li Comprehensive panel", region=(0, 118, 300, 16))
        wrong_row = line("Ann Wu Basic metabolic panel", region=(0, 143, 300, 16))
        # Resolved point sits on the wrong row (y=151).
        kept = lines_near_point([true_row, wrong_row], 151)
        assert kept == [wrong_row]
        # Resolved on the true row keeps the true row.
        kept = lines_near_point([true_row, wrong_row], 126)
        assert kept == [true_row]

    def test_small_lines_get_minimum_slack(self):
        tiny = line("x", region=(0, 100, 10, 2))  # center y=101
        assert lines_near_point([tiny], 104) == [tiny]  # within min 4px
        assert lines_near_point([tiny], 110) == []


class TestContextFromLines:
    BAND = (0, 100, 1280, 64)

    def test_point_refines_to_one_row(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("Ann Wu Basic metabolic panel", region=(10, 140, 300, 16)),
        ]
        ctx = context_from_lines(
            lines, exclude_region=(2000, 0, 1, 1), band=self.BAND,
            point=(150, 118),
        )
        assert ctx == "Jane Li Comprehensive panel"

    def test_without_point_keeps_the_whole_band(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("Ann Wu Basic metabolic panel", region=(10, 140, 300, 16)),
        ]
        ctx = context_from_lines(
            lines, exclude_region=(2000, 0, 1, 1), band=self.BAND
        )
        assert "Jane Li" in ctx and "Ann Wu" in ctx

    def test_timestamp_lines_dropped(self):
        lines = [
            line("Jane Li Comprehensive panel", region=(10, 110, 300, 16)),
            line("DOB 1980-01-01", region=(320, 110, 100, 16)),
        ]
        ctx = context_from_lines(
            lines, exclude_region=(2000, 0, 1, 1), band=self.BAND,
            point=(150, 118),
        )
        assert ctx == "Jane Li Comprehensive panel"

    def test_too_short_band_returns_none(self):
        lines = [line("Active High 3", region=(10, 110, 100, 16))]
        assert (
            context_from_lines(
                lines, exclude_region=(2000, 0, 1, 1), band=self.BAND,
                point=(50, 118),
            )
            is None
        )
