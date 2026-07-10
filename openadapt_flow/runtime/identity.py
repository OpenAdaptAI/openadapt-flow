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

- **context mode** — an order-insensitive token match against the recorded
  band text (see :func:`band_match`): matched tokens must cover >=
  ``COVERAGE_THRESHOLD`` of the recorded band AND no contiguous run of
  uncovered recorded characters may exceed ``UNCOVERED_RUN_CAP`` — a wrong
  entity is a contiguous mismatch (a replaced name), even when long shared
  row text keeps raw coverage high. Order-insensitivity matters because
  OCR re-reads the same band in a different segmentation order between
  visits (e.g. page chrome around a modal), or
- **param mode** — when a workflow parameter's demonstrated value is
  embedded in the recorded band (a parameterized *target*, e.g. the patient
  row), the run's value is substituted into the recorded band and the WHOLE
  substituted band is verified: the run's value must appear in the live
  band AND the band's non-param residue must still match. A band that
  merely mentions the run's value somewhere is not identity.

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
import re
from typing import Any, Iterable, Optional

from openadapt_flow.ir import IdentityCheck, Point, Region

# Recorded band text shorter than this (squashed) is too weak to
# discriminate anything and is not stored — generic fragments like
# "Active High 3" (11 squashed chars) otherwise arm false confidence: any
# sibling row sharing the generic columns would verify. Bands this short
# also yield "unreadable" (proceed flagged, never verified) when they
# reach verification via an older bundle.
MIN_CONTEXT_CHARS = 12

# A workflow parameter's demonstrated value must be at least this long
# (squashed) to switch the check into param mode; 1-3 char examples match
# everywhere by accident. Kept at 4 (not higher) so real-world first-name
# parameters ("Phil") still get param-mode substitution — an over-trigger
# is harmless now that param mode verifies the WHOLE substituted band
# (the run's value alone can no longer disarm the residue check).
MIN_PARAM_CHARS = 4

# Fraction of the recorded band's squashed characters that must be covered
# by matched tokens. Measured with the token matcher on MockMed rows: the
# true row re-reads at 1.0 (0.97+ under injected OCR jitter); a look-alike
# row sharing every column except the name covers ~0.67 — 0.8 splits the
# populations with margin on both sides.
COVERAGE_THRESHOLD = 0.8

# Coverage alone is defeated when shared text dominates the band (a short
# wrong name next to a long shared procedure string covers 0.89): a wrong
# entity is a CONTIGUOUS mismatch, so no contiguous run of unmatched
# recorded characters (adjacent unmatched tokens merge) may exceed this
# cap. 4 tolerates a single genuinely mutable short cell ("with" garbled
# beyond token similarity, a 1-char counter) while a replaced name —
# "Jane Li" -> "Ann Wu" leaves 6 contiguous uncovered chars — fails.
UNCOVERED_RUN_CAP = 4

# Token matching tiers (see band_match): a token is matched when it appears
# verbatim among the observed tokens, OR >= TOKEN_RUN_FRACTION of it
# appears as one contiguous run anywhere in the squashed observed text
# (segmentation-independent containment), OR some observed token is
# whole-token similar at >= TOKEN_SIM_RATIO. 0.7 separates OCR jitter
# ("paln" ~ "pain" 0.75, "hlgh" ~ "high" 0.75) from different words that
# merely share letters ("jane" ~ "panel" 0.67, "jane" ~ "ann" 0.57).
MIN_BLOCK = 3
TOKEN_RUN_FRACTION = 0.8
TOKEN_SIM_RATIO = 0.7

# Param mode: required contiguous run for the run's parameter value, scaled
# for short values (a full 16-char run cannot exist inside a 5-char name).
MAX_RUN_REQUIRED = 16

# Band-line refinement: a line belongs to the resolved point's row when its
# vertical center is within this fraction of its own height from the point
# (minimum slack in px below). The anchor's 64px template height spans 2-3
# rows of a dense table (~25px/row); matching per-row prevents a
# one-row-off resolution from verifying on text bleed from the true row.
ROW_PROXIMITY_FACTOR = 0.75
ROW_PROXIMITY_MIN_PX = 4

# Lines containing a date or clock time are volatile by construction and
# never part of the recorded context (the compiler applies the same rule to
# postconditions; DOB columns on real EMRs hit this too, deliberately).
TIMESTAMP_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"  # 2026-07-08, 2026/7/8
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"  # 07/08/2026, 8.7.26
    r"|\d{1,2}:\d{2}"  # 18:38, 6:05
)


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


def lines_near_point(lines: Iterable[Any], point_y: int) -> list[Any]:
    """Filter OCR lines to the single text row containing ``point_y``.

    A line belongs to the point's row when its vertical center is within
    ``ROW_PROXIMITY_FACTOR`` of its own height from ``point_y`` (with a
    small minimum slack). The coarse band (the anchor's template height,
    64px) spans 2-3 rows of a dense table; identity must be judged against
    the resolved point's OWN row, or a one-row-off resolution verifies on
    text bleed from the adjacent true row.

    Args:
        lines: OCR line objects (``text``/``region``).
        point_y: The click point's y in the same coordinate space as the
            lines' regions.

    Returns:
        The lines belonging to the point's row.
    """
    kept = []
    for line in lines:
        _, ly, _, lh = line.region
        center_y = ly + lh // 2
        slack = max(ROW_PROXIMITY_MIN_PX, int(ROW_PROXIMITY_FACTOR * lh))
        if abs(center_y - point_y) <= slack:
            kept.append(line)
    return kept


def context_from_lines(
    lines: Iterable[Any],
    *,
    exclude_region: Region,
    band: Region,
    point: Optional[Point] = None,
    min_confidence: float = 0.5,
) -> Optional[str]:
    """Extract the context-band text from full-frame OCR lines.

    Keeps confident lines whose vertical center lies inside ``band`` and
    which do NOT intersect ``exclude_region`` (the target's own crop: its
    label is mutable evidence, healed through on rename drift, so it must
    not participate in identity). Timestamp-bearing lines are dropped as
    volatile. When ``point`` is given, lines are further restricted to the
    point's own text row (see :func:`lines_near_point`) so the recorded
    band matches what replay-time verification reads — one row, not the
    2-3 rows a 64px band spans in a dense table. Kept lines are joined
    left-to-right.

    Args:
        lines: OCR line objects (``text``/``region``/``confidence``).
        exclude_region: The anchor's template crop region.
        band: The context band (see :func:`band_region`).
        point: The click point (row refinement); None keeps the whole band.
        min_confidence: Minimum OCR confidence for a line to count.

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
        if TIMESTAMP_RE.search(text):
            continue
        kept.append((lx, line, text))
    if point is not None:
        near = lines_near_point([line for _, line, _ in kept], point[1])
        kept = [item for item in kept if item[1] in near]
    if not kept:
        return None
    kept.sort(key=lambda item: item[0])
    joined = " ".join(text for _, _, text in kept)
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
    only blocks of at least ``MIN_BLOCK`` characters count. Used for
    embedded-parameter detection; band verification uses the stricter
    :func:`band_match` (which also penalizes uncovered residue).
    """
    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    matched = sum(
        b.size for b in _matching_blocks(needle, hay) if b.size >= MIN_BLOCK
    )
    return matched / len(needle)


def tokenize(text: str) -> list[str]:
    """Split on whitespace and squash each token (lowercase, no spaces)."""
    return [squash(tok) for tok in text.split() if squash(tok)]


def _token_matched(token: str, hay_squashed: str, hay_tokens: list[str]) -> bool:
    """Whether one recorded token is present in the observed band.

    Three tiers, all order-insensitive (OCR re-reads the same band in a
    different segmentation order between visits — token order must not
    matter):

    1. verbatim: the token appears as an observed token;
    2. containment: >= ``TOKEN_RUN_FRACTION`` of the token appears as ONE
       contiguous run anywhere in the squashed observed text (tolerates
       the engine merging tokens: recorded "ShowActive" vs observed
       "Show Active"); requires the run to be >= ``MIN_BLOCK`` chars, so
       1-2 char tokens can only match verbatim (a lone "li" must not
       match inside "lipid");
    3. similarity: some observed token of >= ``MIN_BLOCK`` chars is
       whole-token similar at >= ``TOKEN_SIM_RATIO`` (OCR jitter:
       "paln" ~ "pain"; a genuinely different name — "ann" vs "jane",
       ratio 0.57 — stays below the bar).
    """
    if token in hay_tokens:
        return True
    if len(token) >= MIN_BLOCK:
        need = max(MIN_BLOCK, -(-len(token) * 4 // 5))  # ceil(0.8 * len)
        if longest_run(token, hay_squashed) >= need:
            return True
        for observed in hay_tokens:
            if len(observed) < MIN_BLOCK:
                continue
            ratio = difflib.SequenceMatcher(
                None, token, observed, autojunk=False
            ).ratio()
            if ratio >= TOKEN_SIM_RATIO:
                return True
    return False


def band_match(expected_text: str, observed_text: str) -> tuple[float, int]:
    """Match a recorded band against a live band, token-wise.

    Order-insensitive (see :func:`_token_matched`) with residue tracking:
    walking the recorded tokens in order, contiguous runs of UNMATCHED
    tokens accumulate their squashed lengths — a wrong entity is a
    contiguous mismatch ("Jane Li" replaced by "Ann Wu" leaves a 6-char
    uncovered run) even when long shared text keeps overall coverage high.

    Args:
        expected_text: The recorded (or parameter-substituted) band text.
        observed_text: The live band text.

    Returns:
        ``(coverage, max_uncovered_run)`` — the fraction of recorded
        squashed characters in matched tokens, and the longest contiguous
        run of uncovered squashed characters.
    """
    expected_tokens = tokenize(expected_text)
    if not expected_tokens:
        return 0.0, 0
    hay_squashed = squash(observed_text)
    hay_tokens = tokenize(observed_text)
    matched_chars = 0
    total_chars = 0
    uncovered_runs: list[int] = []
    current_run = 0
    for token in expected_tokens:
        total_chars += len(token)
        if hay_squashed and _token_matched(token, hay_squashed, hay_tokens):
            matched_chars += len(token)
            if current_run:
                uncovered_runs.append(current_run)
                current_run = 0
        else:
            current_run += len(token)
    if current_run:
        uncovered_runs.append(current_run)
    return matched_chars / total_chars, max(uncovered_runs, default=0)


def _token_belongs_to(token: str, value_squashed: str) -> bool:
    """Whether a band token is (part of) a parameter's demonstrated value."""
    if not token or not value_squashed:
        return False
    if token in value_squashed or value_squashed in token:
        return True
    ratio = difflib.SequenceMatcher(
        None, token, value_squashed, autojunk=False
    ).ratio()
    # Same-token-with-OCR-jitter question ("Phi1" ~ "Phil"): the token
    # similarity tier's threshold applies.
    return ratio >= TOKEN_SIM_RATIO


def substitute_param(band_text: str, example: str, value: str) -> str:
    """Replace a parameter's demonstrated value in the band with the run's.

    Verbatim occurrences are replaced in place (case-insensitive). When
    the example does not appear verbatim (OCR mangled it into the band),
    tokens belonging to the example are dropped and the run's value is
    appended — band matching is order-insensitive, so the position of the
    substituted value does not matter.
    """
    if not example.strip():
        return band_text
    pattern = re.compile(re.escape(example), re.IGNORECASE)
    if pattern.search(band_text):
        return pattern.sub(value, band_text)
    example_squashed = squash(example)
    kept = [
        tok
        for tok in band_text.split()
        if not _token_belongs_to(squash(tok), example_squashed)
    ]
    return " ".join(kept + [value])


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

    if len(squash(context_text)) < MIN_CONTEXT_CHARS:
        # A band this short ("Active High 3") is generic: any sibling row
        # sharing the generic columns would verify. The compiler no longer
        # stores such bands; when one arrives via an older bundle, identity
        # cannot be judged — proceed flagged, never verified.
        return IdentityCheck(
            status="unreadable", expected=expected, observed=observed_text
        )

    in_band = embedded_params(context_text, param_examples)
    if in_band:
        # Param mode: the demonstrated band embeds a parameter's demo value,
        # so that PART of the band describes the demo's entity — substitute
        # the run's value into the recorded band and verify the WHOLE
        # substituted band. The recorded non-param residue is never
        # discarded: a band that merely contains the run's value somewhere
        # (any row mentioning "Susan") must not verify.
        if not hay:
            return IdentityCheck(
                status="unreadable", mode="param", expected=expected
            )
        substituted = context_text
        for name in in_band:
            substituted = substitute_param(
                substituted,
                param_examples[name],
                params.get(name, param_examples[name]),
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
        cov, uncovered = band_match(substituted, observed_text)
        ok = cov >= COVERAGE_THRESHOLD and uncovered <= UNCOVERED_RUN_CAP
        return IdentityCheck(
            status="verified" if ok else "mismatch",
            mode="param",
            coverage=round(cov, 4),
            expected=substituted,
            observed=observed_text,
            param=in_band[0],
        )

    if not hay:
        return IdentityCheck(status="unreadable", expected=expected)
    cov, uncovered = band_match(context_text, observed_text)
    ok = cov >= COVERAGE_THRESHOLD and uncovered <= UNCOVERED_RUN_CAP
    return IdentityCheck(
        status="verified" if ok else "mismatch",
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
