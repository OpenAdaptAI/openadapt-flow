"""Target-identity evidence: capture at compile time, verify before acting.

Template/geometry evidence answers "does the recorded *position* still look
right?", not "is this the recorded *target*?" — for a button inside a table
row the discriminative text (the row's name column) sits outside the
template crop, so a pixel-identical sibling row scores ~1.0 while being the
wrong entity (see docs/validation/VALIDATION.md, Track A).

This module closes that gap with a horizontal *context band*: the full-width
strip of OCR text at the target's row, minus the target's own label (labels
are mutable evidence the ladder heals through — rename drift must not fail
identity). The compiler stores the band text on the anchor
(``Anchor.context_text``); the replayer re-reads the band around the
*resolved* point before clicking and requires either:

- **context mode** — a lenient coverage match against the recorded band
  text (contiguous common runs of >= ``MIN_BLOCK`` squashed characters must
  cover >= ``COVERAGE_THRESHOLD`` of it), or
- **param mode** — when a workflow parameter's demonstrated value is
  embedded in the recorded band (a parameterized *target*, e.g. the patient
  row), the RUN's value for that parameter must appear in the live band
  instead. This is how a parameterized-target bundle re-anchors: the
  recorded row text describes the demo's entity, not the run's.

Both checks compare squashed (lowercased, whitespace-free) text, the same
OCR-tolerant form used by ``benchmark.verify``. Timestamp-bearing lines are
excluded from the recorded context (volatile by construction, same
rationale as the compiler's postcondition filter).

NOTE (commit 8421d51): parameterized values are never asserted as compiled
POSTCONDITIONS — that rule stands. Identity verification is a *pre-action*
check against runtime values, which is exactly what a parameterized target
needs.

Everything here is import-light (no cv2/OCR at module import); image work is
lazy so unit tests can fake the vision namespace.
"""

from __future__ import annotations

import difflib
from datetime import date
from typing import Any, Iterable, Optional

from openadapt_flow.ir import IdentityCheck, Point, Region
from openadapt_flow.volatility import (  # noqa: F401 - TIMESTAMP_RE re-exported
    TIMESTAMP_RE,
    is_volatile_line,
)

# Recorded band text shorter than this (squashed) is too weak to
# discriminate anything and is not stored — the check must never fire on
# e.g. a lone stray glyph.
MIN_CONTEXT_CHARS = 8

# A workflow parameter's demonstrated value must be at least this long
# (squashed) to switch the check into param mode; 1-2 char examples match
# everywhere by accident.
MIN_PARAM_CHARS = 3

# Context mode: fraction of the recorded band's squashed characters that
# must be covered by contiguous common runs of >= MIN_BLOCK chars. Measured
# on MockMed: the true row re-reads at ~1.0; a look-alike row sharing every
# column except the name covers ~0.67 (the shared columns) — 0.8 splits the
# populations with margin on both sides.
COVERAGE_THRESHOLD = 0.8
MIN_BLOCK = 3

# Param mode: required contiguous run for the run's parameter value, scaled
# for short values (a full 16-char run cannot exist inside a 5-char name).
MAX_RUN_REQUIRED = 16

# Volatile-line policy lives in openadapt_flow.volatility (shared with the
# compiler's postcondition mining): clock-bearing lines and lines dated near
# the recording are dropped from identity context, but a line whose only
# date is FAR from the recording date — a DOB in an identity banner — is
# kept intact, dates included: that date is discriminative identity data,
# not chronology. TIMESTAMP_RE is re-exported from there for backwards
# compatibility.


def squash(text: str) -> str:
    """Lowercase and remove ALL whitespace (OCR-tolerant comparison form)."""
    return "".join(text.lower().split())


def band_region(
    point: Point, band_height: int, viewport: tuple[int, int]
) -> Region:
    """Full-width horizontal band centered on ``point``'s row.

    Args:
        point: The (recorded or resolved) click point.
        band_height: Band height in pixels — the anchor's template crop
            height, so record- and replay-time bands cover the same strip.
        viewport: (width, height) of the frame.

    Returns:
        (x, y, w, h) clamped to the viewport.
    """
    vw, vh = viewport
    h = max(1, min(band_height, vh))
    y = min(max(0, point[1] - h // 2), max(0, vh - h))
    return (0, y, vw, min(h, vh - y))


def _intersects(a: Region, b: Region) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def context_from_lines(
    lines: Iterable[Any],
    *,
    exclude_region: Region,
    band: Region,
    min_confidence: float = 0.5,
    reference_date: Optional[date] = None,
) -> Optional[str]:
    """Extract the context-band text from full-frame OCR lines.

    Keeps confident lines whose vertical center lies inside ``band`` and
    which do NOT intersect ``exclude_region`` (the target's own crop: its
    label is mutable evidence, healed through on rename drift, so it must
    not participate in identity). Volatile lines — clock times, dates near
    the recording date — are dropped; a line whose only date is FAR from
    ``reference_date`` (a DOB in an identity banner) is kept whole, date
    included. Kept lines are joined left-to-right.

    Args:
        lines: OCR line objects (``text``/``region``/``confidence``).
        exclude_region: The anchor's template crop region.
        band: The context band (see :func:`band_region`).
        min_confidence: Minimum OCR confidence for a line to count.
        reference_date: The recording date, enabling the near/far date
            split; when None every date-bearing line is dropped (heal-time
            re-extraction has no recording date).

    Returns:
        The joined band text, or None when the surviving text is too short
        (< ``MIN_CONTEXT_CHARS`` squashed chars) to discriminate anything.
    """
    _, band_y, _, band_h = band
    kept = []
    for line in lines:
        text = (getattr(line, "text", "") or "").strip()
        if not text or getattr(line, "confidence", 0.0) < min_confidence:
            continue
        lx, ly, lw, lh = line.region
        center_y = ly + lh // 2
        if not (band_y <= center_y < band_y + band_h):
            continue
        if _intersects(line.region, exclude_region):
            continue
        if is_volatile_line(text, reference_date=reference_date):
            continue
        kept.append((lx, text))
    if not kept:
        return None
    kept.sort(key=lambda item: item[0])
    joined = " ".join(text for _, text in kept)
    if len(squash(joined)) < MIN_CONTEXT_CHARS:
        return None
    return joined


def required_run(length: int) -> int:
    """Contiguous-run requirement for a needle of ``length`` squashed chars.

    ``verify_note_saved``-style 16-char runs for long values, scaled down to
    ~80% of the needle for short ones (a 16-char run cannot exist inside a
    5-char name), never below 3.
    """
    return min(MAX_RUN_REQUIRED, max(3, -(-length * 4 // 5)))


def _matching_blocks(needle: str, hay: str) -> list:
    # autojunk=False: the default heuristic marks frequent characters of a
    # long OCR haystack as junk, silently collapsing real matches (same
    # pitfall benchmark.verify documents).
    return difflib.SequenceMatcher(
        None, needle, hay, autojunk=False
    ).get_matching_blocks()


def longest_run(needle: str, hay: str) -> int:
    """Longest contiguous common run between two squashed strings."""
    if not needle or not hay:
        return 0
    if needle in hay:
        return len(needle)
    return max((b.size for b in _matching_blocks(needle, hay)), default=0)


def coverage(needle: str, hay: str) -> float:
    """Fraction of ``needle`` covered by contiguous runs >= ``MIN_BLOCK``.

    Scattered 1-2 char coincidences accumulate on any text-dense screen, so
    only blocks of at least ``MIN_BLOCK`` characters count.
    """
    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    matched = sum(
        b.size for b in _matching_blocks(needle, hay) if b.size >= MIN_BLOCK
    )
    return matched / len(needle)


def embedded_params(
    context_text: str, param_examples: dict[str, str]
) -> list[str]:
    """Names of parameters whose demonstrated value is embedded in the band.

    A parameter's example value counts as embedded when its squashed form is
    covered >= ``COVERAGE_THRESHOLD`` inside the squashed context (OCR may
    mangle the odd character of the recorded band).
    """
    ctx = squash(context_text)
    names = []
    for name, example in param_examples.items():
        ex = squash(example or "")
        if len(ex) < MIN_PARAM_CHARS:
            continue
        if coverage(ex, ctx) >= COVERAGE_THRESHOLD:
            names.append(name)
    return names


def verify_target_identity(
    context_text: str,
    observed_text: str,
    *,
    params: Optional[dict[str, str]] = None,
    param_examples: Optional[dict[str, str]] = None,
) -> IdentityCheck:
    """Judge whether the live band matches the recorded target's identity.

    Args:
        context_text: The anchor's recorded context band text.
        observed_text: Live band text (joined OCR lines) around the
            RESOLVED click point.
        params: Effective run parameter values (run values merged over the
            recorded defaults).
        param_examples: The workflow's recorded example values per param.

    Returns:
        An :class:`~openadapt_flow.ir.IdentityCheck`:

        - ``verified`` — the target's identity evidence matched.
        - ``mismatch`` — the band is readable but does NOT match: the
          resolver found something at a plausible position that is not the
          recorded target (or, in param mode, not the run's entity).
        - ``unreadable`` — OCR produced no usable text in the live band;
          identity cannot be judged either way.
    """
    params = params or {}
    param_examples = param_examples or {}
    hay = squash(observed_text)
    expected = context_text

    in_band = embedded_params(context_text, param_examples)
    if in_band:
        # Param mode: the demonstrated band embeds a parameter's demo value,
        # so the band text describes the DEMO's entity. Re-anchor on the
        # run's value instead of the recorded text.
        if not hay:
            return IdentityCheck(
                status="unreadable", mode="param", expected=expected
            )
        for name in in_band:
            value = squash(params.get(name, param_examples[name]))
            run = longest_run(value, hay)
            need = required_run(len(value))
            if run < need:
                return IdentityCheck(
                    status="mismatch",
                    mode="param",
                    coverage=(run / need) if need else 0.0,
                    expected=params.get(name, param_examples[name]),
                    observed=observed_text,
                    param=name,
                )
        return IdentityCheck(
            status="verified",
            mode="param",
            coverage=1.0,
            expected=expected,
            observed=observed_text,
            param=in_band[0],
        )

    if not hay:
        return IdentityCheck(status="unreadable", expected=expected)
    cov = coverage(squash(context_text), hay)
    return IdentityCheck(
        status="verified" if cov >= COVERAGE_THRESHOLD else "mismatch",
        coverage=round(cov, 4),
        expected=expected,
        observed=observed_text,
    )


def upscale_crop(frame_png: bytes, region: Region, factor: int = 2) -> Optional[bytes]:
    """Crop ``region`` from a frame and upscale it (cubic) for a re-OCR.

    Dense small text (real EMR tables) is undercounted by OCR at native
    resolution; a 2x retry recovers most dropped lines (same technique as
    ``benchmark.verify``). Returns None when the frame cannot be decoded or
    the region is empty.
    """
    import cv2  # lazy: keep this module import-light for unit tests
    import numpy as np

    img = cv2.imdecode(np.frombuffer(frame_png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    x0, y0 = max(0, region[0]), max(0, region[1])
    x1 = min(w, region[0] + region[2])
    y1 = min(h, region[1] + region[3])
    if x1 <= x0 or y1 <= y0:
        return None
    crop = img[y0:y1, x0:x1]
    up = cv2.resize(
        crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC
    )
    ok, buf = cv2.imencode(".png", up)
    return buf.tobytes() if ok else None
