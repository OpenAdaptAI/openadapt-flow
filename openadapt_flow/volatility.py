"""Volatility classification for mined text evidence.

Postconditions and identity evidence must SELECT FOR STABILITY: text that
names a *moment* (clock times, freshly-dated rows), a *count* (counters,
badges), or noise (short/low-entropy fragments) cannot survive replay
against live data — a fresh OpenEMR recording once mined ``text_present
':01'`` (a clock-minute fragment) and every later replay halted on it the
moment the clock moved on (docs/validation/VALIDATION.md, Track D).

The classifier here is deliberately asymmetric about dates: a date NEAR the
recording date is content chronology ("last updated", log rows, message
lists) and volatile by construction, while a date FAR from the recording
date is identity data (a date of birth, a contract date) — exactly the text
a robust identity assertion wants. The old blanket timestamp filter ate
patient banners because a DOB matches a date pattern; the near/far split
fixes that without readmitting clocks.

Everything here is import-light (stdlib only) so both the compiler and the
runtime identity module can share one vocabulary of volatility.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

# Clock times are volatile at replay latency (the minute hand moves between
# record and replay). The second alternative catches bare OCR fragments like
# ':01' — a colon-led minute with no hour digit, which slipped past the old
# combined pattern and false-halted three live OpenEMR replays.
CLOCK_RE = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"  # 18:38, 6:05, 12:45:59
    r"|(?<![\w:]):\d{2}(?!\d)"  # bare ':01' OCR fragments
)

# Date-like fragments; whether one is volatile depends on its distance from
# the recording date (see `date_fragments_near`).
DATE_RE = re.compile(
    r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"  # 2026-07-08, 2026/7/8
    r"|(?<!\d)\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\d)"  # 07/08/2026, 8.7.26
)

# Backwards-compatible combined pattern (previously defined in
# runtime.identity). Matches any clock or date fragment, with no near/far
# discrimination — callers that can supply a reference date should prefer
# `classify_text` / `is_volatile_line`.
TIMESTAMP_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}:\d{2}"
)

# A date within this many days of the recording date is treated as content
# chronology (volatile); farther away it is identity data (stable). One year
# covers "last week's message" and calendar chrome; DOBs and document dates
# are typically years out. (An entity whose identity date falls inside the
# window — e.g. an infant's DOB — is filtered as volatile: the cost is a
# weaker assertion, never a wrong one.)
DATE_VOLATILITY_WINDOW_DAYS = 366

# Minimum squashed (lowercased, whitespace-free) length for a stable text
# candidate; shorter fragments match everywhere by accident.
MIN_SQUASHED_LEN = 4

# Minimum count of alphabetic characters: candidates dominated by digits and
# punctuation ('#1234', '(3)', '42', ':01') name counts and moments, not
# screen semantics.
MIN_ALPHA_CHARS = 3

# Minimum distinct characters in the squashed form (rejects OCR noise like
# '----' or 'aaaa').
MIN_DISTINCT_CHARS = 3


def _squash(text: str) -> str:
    return "".join(text.lower().split())


def _candidate_dates(fragment: str) -> list[date]:
    """All plausible calendar dates a date-like fragment could denote.

    Both field orders are tried for the ambiguous forms (D/M/Y vs M/D/Y,
    Y-M-D vs Y-D-M); two-digit years are read as 20xx. Invalid combinations
    are silently skipped.
    """
    numbers = [int(n) for n in re.findall(r"\d+", fragment)]
    if len(numbers) != 3:
        return []
    candidates: list[tuple[int, int, int]] = []
    if numbers[0] >= 1000:  # Y-M-D family
        y, a, b = numbers
        candidates = [(y, a, b), (y, b, a)]
    else:  # D/M/Y or M/D/Y family
        a, b, y = numbers
        if y < 100:
            y += 2000
        candidates = [(y, b, a), (y, a, b)]
    dates: list[date] = []
    for y, m, d in candidates:
        try:
            dates.append(date(y, m, d))
        except ValueError:
            continue
    return dates


def date_fragments_near(text: str, reference_date: date) -> Optional[bool]:
    """Whether ``text``'s date fragments sit near the reference date.

    Returns:
        None when the text contains no date fragment; True when ANY fragment
        is within ``DATE_VOLATILITY_WINDOW_DAYS`` of ``reference_date`` (or
        cannot be parsed at all — conservative: unparseable means volatile);
        False when every fragment parses and all are far.
    """
    found = False
    for match in DATE_RE.finditer(text):
        found = True
        candidates = _candidate_dates(match.group(0))
        if not candidates:
            return True  # date-like but unparseable: assume volatile
        nearest = min(abs((d - reference_date).days) for d in candidates)
        if nearest <= DATE_VOLATILITY_WINDOW_DAYS:
            return True
    return None if not found else False


def classify_text(
    text: str, *, reference_date: Optional[date] = None
) -> Optional[str]:
    """Classify a text fragment's volatility for use as mined evidence.

    Args:
        text: Raw OCR line text.
        reference_date: The recording date, enabling the near/far date
            split. When None, ALL date-bearing text is treated as volatile
            (the conservative pre-split behavior).

    Returns:
        None when the text is a stable candidate, else a short reason:
        ``too_short``, ``clock_time``, ``date`` (no reference available),
        ``near_date``, ``digit_dominated``, or ``low_entropy``.
    """
    squashed = _squash(text)
    if len(squashed) < MIN_SQUASHED_LEN:
        return "too_short"
    if CLOCK_RE.search(text):
        return "clock_time"
    if DATE_RE.search(text):
        if reference_date is None:
            return "date"
        if date_fragments_near(text, reference_date):
            return "near_date"
        # Far dates (DOB-class identity data) are stable; fall through so
        # the surrounding text still has to carry real content.
    if sum(1 for c in squashed if c.isalpha()) < MIN_ALPHA_CHARS:
        return "digit_dominated"
    if len(set(squashed)) < MIN_DISTINCT_CHARS:
        return "low_entropy"
    return None


def is_volatile_line(
    text: str, *, reference_date: Optional[date] = None
) -> bool:
    """Whether a whole OCR line is too volatile to serve as evidence.

    Used by the identity-context extractor: a line carrying a clock time or
    a near/unparseable date is dropped wholesale (its non-date remainder may
    itself be day-fresh content); a line whose only date is FAR from the
    recording date (a DOB in an identity banner) is kept intact — including
    the date, which is discriminative identity data.

    Args:
        text: Raw OCR line text.
        reference_date: The recording date; when None all dates are volatile
            (conservative — used by heal-time re-extraction, which has no
            recording date).
    """
    if CLOCK_RE.search(text):
        return True
    if reference_date is None:
        return bool(DATE_RE.search(text))
    return date_fragments_near(text, reference_date) is True
