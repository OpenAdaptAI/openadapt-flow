"""Unit tests for openadapt_flow.volatility — the stability classifier that
postcondition mining and identity-context extraction share.

The concrete failures these pin down (docs/validation/VALIDATION.md):

- a live OpenEMR recording mined ``text_present ':01'`` (a clock-minute OCR
  fragment) and every later replay false-halted on it;
- the old blanket timestamp filter ate identity banners because a DOB looks
  like a date.
"""

from __future__ import annotations

from datetime import date

import pytest

from openadapt_flow.volatility import (
    CLOCK_RE,
    DATE_VOLATILITY_WINDOW_DAYS,
    classify_text,
    date_fragments_near,
    is_volatile_line,
)

RECORDED = date(2026, 7, 8)  # the reference (recording) date used throughout


class TestClockFragments:
    """Clock times are volatile at replay latency — always rejected."""

    @pytest.mark.parametrize(
        "text",
        [
            ":01",  # the OpenEMR false-halt fragment, verbatim
            "18:38",
            "6:05",
            "12:45:59",
            "Last updated 14:32",
            "Message received at 9:07 today",
        ],
    )
    def test_clock_bearing_text_is_volatile(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is not None

    def test_the_openemr_colon_fragment_never_survives(self) -> None:
        """The ':01'-class fragment must be volatile via EVERY route: the
        clock regex, the length floor, and the digit-dominance rule."""
        assert CLOCK_RE.search(":01")
        assert classify_text(":01", reference_date=RECORDED) == "too_short"
        assert classify_text("x :01 y", reference_date=RECORDED) == "clock_time"

    def test_clock_line_is_volatile_for_identity_context(self) -> None:
        assert is_volatile_line("12:45", reference_date=RECORDED)
        assert is_volatile_line("Updated 14:32", reference_date=RECORDED)


class TestCountersAndNoise:
    @pytest.mark.parametrize(
        "text,reason",
        [
            ("42", "too_short"),
            ("#1234", "digit_dominated"),
            ("(3)", "too_short"),
            ("12345", "digit_dominated"),
            ("3 / 12", "digit_dominated"),
            ("----", "digit_dominated"),
            ("aaaa", "low_entropy"),
            ("OK", "too_short"),
        ],
    )
    def test_fragments_are_volatile(self, text: str, reason: str) -> None:
        assert classify_text(text, reference_date=RECORDED) == reason

    @pytest.mark.parametrize(
        "text",
        [
            "No encounters yet.",
            "Encounter saved",
            "Patient Messages",
            "Chart synchronization complete",
            "Page 2 of 9",  # digits present but alpha-dominated: stable
        ],
    )
    def test_real_ui_text_is_stable(self, text: str) -> None:
        assert classify_text(text, reference_date=RECORDED) is None


class TestDates:
    """Near dates are chronology (volatile); far dates are identity data."""

    def test_dob_far_from_recording_is_stable(self) -> None:
        banner = "Jane Sample DOB 1980-01-01"
        assert classify_text(banner, reference_date=RECORDED) is None
        assert not is_volatile_line(banner, reference_date=RECORDED)

    def test_date_near_recording_is_volatile(self) -> None:
        row = "Lab result posted 2026-07-07"
        assert classify_text(row, reference_date=RECORDED) == "near_date"
        assert is_volatile_line(row, reference_date=RECORDED)

    def test_two_digit_year_near_date_is_volatile(self) -> None:
        assert classify_text("Visit 8.7.26", reference_date=RECORDED) == "near_date"

    def test_mdy_and_dmy_orders_both_considered(self) -> None:
        # 07/08/2026 is near whether read M/D/Y or D/M/Y.
        assert date_fragments_near("07/08/2026", RECORDED) is True
        # 01/02/1980 is far under both readings.
        assert date_fragments_near("01/02/1980", RECORDED) is False

    def test_unparseable_date_like_text_is_conservatively_volatile(self) -> None:
        assert date_fragments_near("99/99/2026", RECORDED) is True

    def test_no_reference_date_treats_all_dates_as_volatile(self) -> None:
        """Heal-time re-extraction has no recording date: conservative."""
        assert classify_text("Jane Sample DOB 1980-01-01") == "date"
        assert is_volatile_line("Jane Sample DOB 1980-01-01")

    def test_bare_far_date_without_text_is_still_volatile(self) -> None:
        """A DOB is stable evidence only inside an identity region with real
        text; a bare date fragment has no semantics to anchor."""
        assert (
            classify_text("1980-01-01", reference_date=RECORDED)
            == "digit_dominated"
        )

    def test_window_boundary(self) -> None:
        near = RECORDED.toordinal() - DATE_VOLATILITY_WINDOW_DAYS
        far = near - 2
        near_s = date.fromordinal(near).isoformat()
        far_s = date.fromordinal(far).isoformat()
        assert date_fragments_near(near_s, RECORDED) is True
        assert date_fragments_near(far_s, RECORDED) is False

    def test_text_without_dates_returns_none(self) -> None:
        assert date_fragments_near("No encounters yet.", RECORDED) is None
