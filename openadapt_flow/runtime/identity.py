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
  ``COVERAGE_THRESHOLD`` of the recorded band, no contiguous run of
  uncovered recorded characters may exceed ``UNCOVERED_RUN_CAP`` (a wrong
  entity is a contiguous mismatch — a replaced name — even when long
  shared row text keeps raw coverage high), AND no more than
  ``CONTRADICTED_CHARS_CAP`` characters may be NEAR-MISSES: tokens only
  match when OCR-equivalent under the engine's character-confusion
  classes, and an unmatched token whose observed counterpart is a
  near-name ('Phil'/'Philip', 'John'/'Joan', an off-by-one DOB) is
  affirmative wrong-sibling evidence with its own, stricter budget.
  Since the 2026-07-10 out-of-corpus review, four further budgets close
  the classes corpus v1 excluded by construction: a name-plausible token
  matched ONLY by letter-letter confusion equivalence is SUSPECT (a
  Neil/Nell collision is indistinguishable from a misread — abort, see
  ``SUSPECT_CHARS_CAP``); a raw-unequal, non-confusion-equivalent 1-2
  char token is contradiction (middle initials, the SEX column); an
  observed name-shaped token the recorded band cannot explain is
  contradiction (appended names, two-row merges, rows that merely
  MENTION the recorded patient); and an ABSENT name-like alphabetic
  token >= 4 chars refuses even inside the generic run cap (identity
  must not verify with its identity token never read).
  Order-insensitivity matters because OCR re-reads the same band in a
  different segmentation order between visits (e.g. page chrome around a
  modal), or
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
from collections import Counter
from datetime import date
from typing import Any, Iterable, NamedTuple, Optional

from openadapt_flow.ir import IdentityCheck, Point, Region
from openadapt_flow.volatility import (  # noqa: F401 - TIMESTAMP_RE re-exported
    TIMESTAMP_RE,
    is_volatile_line,
)

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
# beyond recognition, a 1-char counter) while a replaced name —
# "Jane Li" -> "Ann Wu" leaves 6 contiguous uncovered chars — fails.
# Operating point picked from the held-out adversarial ROC
# (docs/validation/IDENTITY_ROC.md).
UNCOVERED_RUN_CAP = 4

# Token matching (see band_match): a recorded token is MATCHED only when
# some observed token — or a concatenation of consecutive observed tokens
# (OCR split the token), or when a concatenation of consecutive recorded
# tokens (OCR joined them) — is OCR-EQUIVALENT to it: identical after
# canonicalizing the character classes real OCR engines confuse
# (l/1/i/|, O/0, 5/s, 2/z, 8/b, 9/g, rn/m, cl/d, vv/w). There is no raw
# similarity-ratio tier and no partial-containment tier anymore: both
# accepted semantic *extensions* of name tokens ('Phil' inside 'Philip'
# at containment 1.0; 'John' vs 'Joan' at ratio 0.75) — the third
# reopening of the wrong-patient P0 (near-name siblings; see
# docs/validation/VALIDATION.md). A substitution of a/o mid-token is a
# different word, not OCR noise; OCR noise has characteristic char-class
# patterns, and only those are accepted.
MIN_BLOCK = 3

# Whole-token similarity used ONLY by substitute_param's token-ownership
# test (which band tokens belong to a parameter's demo value), not by
# band matching.
TOKEN_SIM_RATIO = 0.7

# A recorded token that is NOT matched is CONTRADICTED — affirmative
# evidence of a different entity, not mere absence — when a >=3-char
# observed token is a near-miss for it: canonically similar at >=
# CONTRADICTION_SIM (DOB/MRN single-field edits ~0.9, John/Joan 0.75,
# 3-char names Ted/Tad 0.67), or one canonically contains the other with
# alphabetic residue ('Phil' inside 'Phillipa', sim 0.67 — a semantic
# extension). An unmatched recorded ALPHABETIC token paired with an
# unexplained observed alphabetic token is likewise contradiction (a
# replaced word: 'Amy' -> 'Kim' at sim 0.0), and a generational suffix
# (Jr/Sr/II/III/IV) present on exactly one side always contradicts.
# Contradicted characters are capped separately from mere-absence runs:
# absence is often OCR dropout (occlusion, dropped tokens — the $-cost
# fallback direction), while contradiction is the wrong-sibling-row
# signature. Both caps come from the held-out ROC.
CONTRADICTION_SIM = 0.62
CONTRADICTED_CHARS_CAP = 0

# SUSPECT tokens (2026-07-10 out-of-corpus review): a DISCRIMINATOR token
# whose ONLY match is confusion-equivalence — canonical-equal but
# raw-unequal — is charged to this zero budget, because a confusion-only
# match on a discriminator is indistinguishable at band level from a
# wrong-row read. Two discriminator classes qualify:
#
#   - NAMES (Blocker 1): a name-plausible token (>= MIN_BLOCK chars, no
#     digits on either side) — 'Neil'/'Nell' (i/l), 'Clay'/'Day' (cl/d),
#     'Marnie'/'Mamie' (rn/m) are DIFFERENT REAL NAMES that canonicalize
#     identically. Digit-class noise on a NAME ('Belford'/'Be1ford',
#     '5ample') stays a clean match — a human name contains no digit, so
#     the recorded token being all-alpha proves it is a name and the
#     digit came from OCR, not a rival name.
#
#   - IDENTIFIERS (5th reopening, second review): a RECORDED token that
#     CONTAINS A DIGIT (MRN, account number, chart ref, DOB) whose match
#     needed a letter/digit confusion (l/1, O/0, S/5, Z/2, B/8, g/9) is
#     a different identifier, 'A01234'/'AO1234' — and the identifier is
#     precisely what disambiguates same-name patients. The round-3 rule
#     missed this because it keyed on name-plausibility (false for any
#     token with a digit), so the suspect budget was OFF for exactly the
#     tokens whose confusion equivalence is most dangerous. Scoping on
#     the RECORDED token (not the observed one) is what keeps
#     name-with-digit-noise verifying while identifier-with-digit
#     aborting: the recording carries the ground truth of the token's
#     type. All-digit differences (748291 vs 748292) are NOT
#     confusion-equivalent (two digits are never in one class), so they
#     mismatch via coverage/contradiction, not here.
#
# The budget is zero for both. For identifiers this is option A of the
# review (no corroboration escape): a confusion-differing identifier
# aborts even when name and DOB raw-match, so two same-name patients
# distinguished only by an OCR-confusable identifier char never verify —
# the availability cost (true-row identifier OCR noise now halts) is the
# cheap direction and is disclosed in docs/LIMITS.md.
SUSPECT_CHARS_CAP = 0

# Unexplained observed NAME-SHAPED tokens (review Blocker 3): an
# observed-side superset used to verify unconditionally — context mode
# had no unexplained-observed-token budget (param mode always had one via
# its raw run requirement). An observed token that is name-shaped
# (leading uppercase in the raw band, alphabetic-dominated,
# name-plausible, >= MIN_BLOCK chars) and that the recorded band cannot
# explain is affirmative evidence of a different or merged row: an
# appended middle name, a second row OCR-merged into the band, or a
# message/cc row that MENTIONS the recorded patient. Budget zero, from
# the v1+v2 ROC. Lowercase adjacent-row bleed (mid-procedure words) is
# deliberately exempt — the legitimate spurious-token class.
UNEXPLAINED_NAME_TOKENS_CAP = 0

# Absent name-like tokens (review Major 4): the generic uncovered-run
# cap tolerates a 4-char absence, which let a band verify with its
# 4-char first name never read at all ('Belford, Phil' vs 'Belford,').
# Absence of a name-like ALPHABETIC token is worse than absence of
# trailing numerics (DOB/MRN dropout is the common $-cost OCR direction;
# a missing name is the identity token itself), so unmatched
# alphabetic-dominated, name-plausible tokens carry their own cap: 3
# chars — a dropped 'MRN' label or generic 3-char word is tolerated, a
# >= 4-char name-like token must be read (raw or confusion) to verify.
ABSENT_NAME_TOKEN_CAP = 3

# Characters that cannot appear in a real person's name but do appear in
# OCR confusion classes: a raw-unequal token pair involving one of these
# is genuine OCR noise, not a possible different name.
_NON_NAME_CHARS = frozenset("0123456789|!")

# Generational suffixes: presence on one side only is identity-bearing
# ('Belford, Phil' vs 'Belford, Phil Jr' is a different patient).
# Membership is tested on the OCR-canonical form (a live 'II' commonly
# reads as 'lI'/'Il'; 'Sr' as '5r') — see _is_generational_suffix.
GENERATIONAL_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})

# OCR character-confusion classes (squashed/lowercased space). A pair of
# tokens is OCR-equivalent iff equal after canonicalization: multi-char
# shapes first (rn->m, cl->d, vv->w), then per-char class representatives.
_CONFUSION_GROUPS = ("l1i|!", "o0", "s5", "z2", "b8", "g9")
_CONFUSION_MULTI = (("rn", "m"), ("cl", "d"), ("vv", "w"))
_CONFUSION_CANON = {
    ch: group[0] for group in _CONFUSION_GROUPS for ch in group
}

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


def regions_intersect(a: Region, b: Region) -> bool:
    """Whether two (x, y, w, h) regions overlap. Public because the
    replayer uses it to exclude the resolved target's own crop from the
    live band (mirroring the compiler's record-time exclusion — the
    label is mutable evidence and must not participate in identity, and
    an ASYMMETRIC observed band would trip the unexplained-name budget
    on the target's own label)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


_intersects = regions_intersect


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
    reference_date: Optional[date] = None,
) -> Optional[str]:
    """Extract the context-band text from full-frame OCR lines.

    Keeps confident lines whose vertical center lies inside ``band`` and
    which do NOT intersect ``exclude_region`` (the target's own crop: its
    label is mutable evidence, healed through on rename drift, so it must
    not participate in identity). Volatile lines — clock times, dates near
    the recording date — are dropped; a line whose only date is FAR from
    ``reference_date`` (a DOB in an identity banner) is kept whole, date
    included. When ``point`` is given, lines are further restricted to the
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


def ocr_canonical(token: str) -> str:
    """Canonical form under the OCR character-confusion classes.

    Two tokens are OCR-equivalent iff their canonical forms are equal —
    'paln' == 'pain' (l/i), '5ample' == 'sample' (5/s), 'cornpre' ==
    'compre' (rn/m) — while 'john' != 'joan' (a/o is not an OCR
    confusion) and 'phil' != 'philip' (extension is not noise).
    """
    t = token.lower()
    for a, b in _CONFUSION_MULTI:
        t = t.replace(a, b)
    return "".join(_CONFUSION_CANON.get(ch, ch) for ch in t)


def _alpha_dominated(token: str) -> bool:
    return sum(ch.isalpha() for ch in token) * 2 >= len(token)


def _name_plausible(token: str) -> bool:
    """Whether every character of a squashed token could appear in a real
    name (no digits, no confusion-class symbols): only such tokens can
    collide with a DIFFERENT real name under letter-letter confusions."""
    return not any(ch in _NON_NAME_CHARS for ch in token)


def _has_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _suspicious_pair(expected: str, observed: str) -> bool:
    """A canonical-equal, raw-unequal token pair whose match is a
    confusion-only match on a DISCRIMINATOR — indistinguishable at band
    level from a wrong-row read. Two qualifying shapes:

    - NAME collision (Neil/Nell): both sides name-plausible and long
      enough to be a name;
    - IDENTIFIER collision (A01234/AO1234): the RECORDED token contains a
      digit (an MRN/account/DOB), so a letter/digit confusion turned it
      into a DIFFERENT identifier. Scoped on the RECORDED token: a name
      OCR'd WITH a digit ('Belford' -> 'Be1ford') has an all-alpha
      recorded token and is NOT suspect (clean OCR noise), while an
      identifier is suspect regardless of the observed side.
    """
    if _has_digit(expected):
        # Recorded identifier matched only via confusion: a different ID.
        return True
    return (
        len(expected) >= MIN_BLOCK
        and _name_plausible(expected)
        and _name_plausible(observed)
    )


def _name_shaped(raw_token: str, squashed_token: str) -> bool:
    """Whether an observed band token looks like part of a person's name:
    leading uppercase in the RAW band text, alphabetic-dominated,
    name-plausible, at least MIN_BLOCK squashed chars. Used by the
    unexplained-observed-token budget — lowercase adjacent-row bleed
    (mid-procedure words) is deliberately not name-shaped."""
    return (
        len(squashed_token) >= MIN_BLOCK
        and raw_token[:1].isupper()
        and _alpha_dominated(squashed_token)
        and _name_plausible(squashed_token)
    )


_GEN_SUFFIX_CANON = frozenset(
    ocr_canonical(s) for s in GENERATIONAL_SUFFIXES
)


def _is_generational_suffix(token: str) -> bool:
    """Whether a squashed token is a Jr/Sr/II/III/IV generational suffix,
    tolerating OCR confusions ('lI' for 'II', '5r' for 'Sr')."""
    return ocr_canonical(token) in _GEN_SUFFIX_CANON


class BandMatch(NamedTuple):
    """Result of matching a recorded band against a live band.

    Attributes:
        coverage: Fraction of recorded squashed characters in matched
            tokens.
        max_uncovered_run: Longest contiguous run of unmatched recorded
            characters (adjacent unmatched tokens merge) — absence
            evidence (OCR dropout or a replaced stretch).
        contradicted_chars: Total squashed characters of recorded tokens
            with a NEAR-MISS in the observed band — affirmative evidence
            of a different entity (sibling names, edited DOB/MRN fields,
            generational suffixes, replaced words, changed short tokens
            like a middle initial or the sex column).
        suspect_chars: Total squashed characters of recorded
            name-plausible tokens matched ONLY by letter-letter
            confusion equivalence — a Neil/Nell-class collision,
            indistinguishable from a misread of the true row (abort is
            the correct outcome for both readings).
        unexplained_name_tokens: Observed name-shaped tokens the
            recorded band cannot explain — appended names, merged rows,
            rows that mention the recorded patient.
        max_absent_alpha_token: Longest unmatched name-like alphabetic
            recorded token — absence of the identity token itself, worse
            than trailing-numerics dropout.
    """

    coverage: float
    max_uncovered_run: int
    contradicted_chars: int
    suspect_chars: int = 0
    unexplained_name_tokens: int = 0
    max_absent_alpha_token: int = 0


def _match_tokens(
    exp: list[str], obs: list[str]
) -> tuple[list[bool], list[bool], list[bool], list[bool]]:
    """Mark matched recorded tokens and explained observed tokens.

    Order-insensitive at token granularity (OCR re-reads the same band in
    a different segmentation order between visits), with explicit
    merge/split handling — splits and joins preserve LOCAL adjacency even
    when segments permute:

    - single: an observed token is OCR-equivalent to the recorded token;
    - split: consecutive recorded tokens concatenate (OCR-equivalently)
      to one observed token (recorded 'Show' 'Active' vs observed
      'ShowActive');
    - join: consecutive observed tokens concatenate to one recorded
      token (recorded 'ShowActive' vs observed 'Show Active').

    OCR-equivalence is canonical-form equality (:func:`ocr_canonical`):
    there is deliberately NO similarity-ratio or partial-containment
    acceptance — those tiers verified near-name siblings (Phil/Philip,
    John/Joan), the third wrong-patient reopening.

    For each matched recorded token the match QUALITY is tracked too:
    ``raw_matched`` marks tokens with a raw-equal (squashed-identical)
    counterpart, and ``suspect_evidence`` marks tokens whose only
    evidence is a letter-letter confusion equivalence against a
    name-plausible observed counterpart (the Neil/Nell collision class —
    see :data:`SUSPECT_CHARS_CAP`).

    Returns:
        ``(matched, explained, raw_matched, suspect_evidence)``.
    """
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c = [ocr_canonical(t) for t in obs]
    matched = [False] * len(exp)
    explained = [False] * len(obs)
    raw_matched = [False] * len(exp)
    suspect_evidence = [False] * len(exp)

    def mark(i: int, expected_raw: str, observed_raw: str) -> None:
        matched[i] = True
        if expected_raw == observed_raw:
            raw_matched[i] = True
        elif _suspicious_pair(expected_raw, observed_raw):
            suspect_evidence[i] = True

    # single-token equivalence (mark every equivalent observed copy)
    for i, ec in enumerate(exp_c):
        for j, oc in enumerate(obs_c):
            if ec == oc:
                mark(i, exp[i], obs[j])
                explained[j] = True

    # split: consecutive recorded tokens -> one observed token
    for i in range(len(exp)):
        for size in (2, 3, 4):
            if i + size > len(exp):
                break
            if all(matched[i : i + size]):
                continue
            concat_raw = "".join(exp[i : i + size])
            concat_c = ocr_canonical(concat_raw)
            if len(concat_c) < MIN_BLOCK:
                continue
            for j, oc in enumerate(obs_c):
                if oc == concat_c:
                    rawok = concat_raw == obs[j]
                    for m in range(i, i + size):
                        matched[m] = True
                        if rawok:
                            raw_matched[m] = True
                        elif _suspicious_pair(concat_raw, obs[j]):
                            suspect_evidence[m] = True
                    explained[j] = True

    # join: one recorded token -> consecutive observed tokens
    for i, token in enumerate(exp):
        if matched[i] or len(exp_c[i]) < MIN_BLOCK:
            continue
        for j in range(len(obs)):
            for size in (2, 3, 4):
                if j + size > len(obs):
                    break
                concat_raw = "".join(obs[j : j + size])
                if ocr_canonical(concat_raw) == exp_c[i]:
                    mark(i, token, concat_raw)
                    for m in range(j, j + size):
                        explained[m] = True
                    break
            if matched[i]:
                break
    return matched, explained, raw_matched, suspect_evidence


def _contradicted(
    exp: list[str],
    obs: list[str],
    matched: list[bool],
    explained: list[bool],
    *,
    contradiction_sim: float,
) -> list[bool]:
    """Mark recorded tokens whose absence is a NEAR-MISS, not dropout.

    A wrong sibling row does not merely lack the recorded name — it shows
    a near-name in its place. Five contradiction shapes (rationale on the
    constants above):

    - near-miss similarity: an observed >=3-char token is canonically
      similar at >= ``contradiction_sim``;
    - semantic extension: one token canonically contains the other with
      alphabetic residue ('Phil' in 'Phillipa');
    - replacement: the recorded token is alphabetic and some UNexplained
      alphabetic observed token exists ('Amy' -> 'Kim', similarity 0);
    - short-token replacement (2026-07-10 review, Blocker 2): a 1-2 char
      alphabetic recorded token, unmatched, with an unexplained observed
      alphabetic token of the SAME length that is not
      confusion-equivalent — a changed middle initial ('J' -> 'K'), the
      SEX column ('M' -> 'F'), a 2-char name ('Al' -> 'Bo'). These sat
      below MIN_BLOCK and were invisible to every rule;
    - generational suffix on either side, unmatched.
    """
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c = [ocr_canonical(t) for t in obs]
    contradicted = [False] * len(exp)
    unexplained_alpha = any(
        not explained[j] and len(o) >= MIN_BLOCK and _alpha_dominated(o)
        for j, o in enumerate(obs)
    )
    obs_suffix_unexplained = any(
        not explained[j] and _is_generational_suffix(o)
        for j, o in enumerate(obs)
    )
    for i, token in enumerate(exp):
        if matched[i]:
            continue
        if _is_generational_suffix(token):
            contradicted[i] = True
            continue
        if len(token) < MIN_BLOCK:
            # Short-token replacement (Blocker 2) is handled with
            # COUNT-based accounting in band_match: a replaced initial
            # that happens to duplicate another band token (a middle
            # initial 'F' colliding with the sex column's 'F') would look
            # "explained" here per-pair while the multiset clearly shows
            # one copy missing and a foreign copy standing in its place.
            continue
        if unexplained_alpha and _alpha_dominated(token):
            contradicted[i] = True
            continue
        ec = exp_c[i]
        for j, oc in enumerate(obs_c):
            if len(obs[j]) < MIN_BLOCK or oc == ec:
                continue
            shorter, longer = sorted((ec, oc), key=len)
            if (
                len(shorter) >= MIN_BLOCK
                and shorter in longer
                and any(
                    ch.isalpha() for ch in longer.replace(shorter, "", 1)
                )
            ):
                contradicted[i] = True
                break
            ratio = difflib.SequenceMatcher(
                None, ec, oc, autojunk=False
            ).ratio()
            if ratio >= contradiction_sim:
                contradicted[i] = True
                break
    if obs_suffix_unexplained:
        # A generational suffix the recorded band does not have: the
        # observed row is a different generation of the same name. Charge
        # it to the nearest recorded name evidence: mark ALL unmatched
        # recorded tokens contradicted, and if everything matched, the
        # caller's suffix flag below still forces the contradiction.
        for i in range(len(exp)):
            if not matched[i]:
                contradicted[i] = True
    return contradicted


def band_match(
    expected_text: str,
    observed_text: str,
    *,
    contradiction_sim: float = CONTRADICTION_SIM,
) -> BandMatch:
    """Match a recorded band against a live band, token-wise.

    Order-insensitive (see :func:`_match_tokens`) with two kinds of
    residue, tracked separately because they mean different things:

    - **uncovered runs** — walking the recorded tokens in order,
      contiguous runs of UNMATCHED tokens accumulate their squashed
      lengths: a wrong entity is a contiguous mismatch ("Jane Li"
      replaced by "Ann Wu" leaves a 6-char uncovered run) even when long
      shared row text keeps overall coverage high;
    - **contradicted chars** — recorded tokens with a NEAR-MISS in the
      observed band (see :func:`_contradicted`): 'Phil' vs 'Philip' is
      only a 4-char absence (within any workable run cap) but it is
      affirmative evidence of a sibling, so it is charged to a separate,
      much stricter budget.

    An observed generational suffix absent from the recorded band
    contradicts even when every recorded token matched ('Belford, Phil'
    vs 'Belford, Phil Jr'): the returned ``contradicted_chars`` is
    forced positive.

    Args:
        expected_text: The recorded (or parameter-substituted) band text.
        observed_text: The live band text.
        contradiction_sim: Near-miss similarity threshold (exposed for
            the ROC harness; production uses ``CONTRADICTION_SIM``).

    Returns:
        A :class:`BandMatch` (coverage, max_uncovered_run,
        contradicted_chars, suspect_chars, unexplained_name_tokens,
        max_absent_alpha_token).
    """
    exp = tokenize(expected_text)
    if not exp:
        return BandMatch(0.0, 0, 0)
    obs_raw = [tok for tok in observed_text.split() if squash(tok)]
    obs = [squash(tok) for tok in obs_raw]
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c_all = [ocr_canonical(t) for t in obs]
    matched, explained, raw_matched, suspect_evidence = _match_tokens(
        exp, obs
    )
    contradicted = _contradicted(
        exp, obs, matched, explained, contradiction_sim=contradiction_sim
    )

    matched_chars = 0
    total_chars = 0
    contradicted_chars = 0
    suspect_chars = 0
    max_absent_alpha = 0
    uncovered_runs: list[int] = []
    current_run = 0
    for i, token in enumerate(exp):
        total_chars += len(token)
        if matched[i]:
            matched_chars += len(token)
            if not raw_matched[i] and suspect_evidence[i]:
                # Matched ONLY by letter-letter confusion equivalence:
                # the Neil/Nell collision class (Blocker 1).
                suspect_chars += len(token)
            if current_run:
                uncovered_runs.append(current_run)
                current_run = 0
        else:
            current_run += len(token)
            if contradicted[i]:
                contradicted_chars += len(token)
            if _alpha_dominated(token) and _name_plausible(token):
                # Absent name-like token (Major 4): identity must not
                # verify when its identity token was never read.
                max_absent_alpha = max(max_absent_alpha, len(token))
    if current_run:
        uncovered_runs.append(current_run)
    if contradicted_chars == 0 and any(
        not explained[j] and _is_generational_suffix(o)
        for j, o in enumerate(obs)
    ):
        # Fully-matched band plus an unexplained observed Jr/Sr/II: the
        # generation differs even though every recorded token matched.
        contradicted_chars = max(
            (len(o) for o in obs if _is_generational_suffix(o)), default=2
        )
    # Short-token replacement (Blocker 2), multiset accounting: 'J' -> 'K'
    # middle initials, the SEX column 'M' -> 'F', 2-char names 'Al' ->
    # 'Bo'. Count-based on canonical forms, because a replaced initial
    # can DUPLICATE another band token ('Frank R ... F' -> 'Frank F ...
    # F' leaves both observed 'F's "explained" per-pair while the
    # multiset shows one 'R' missing and a foreign 'F' in its place).
    # Absence alone (OCR dropped a short token) stays tolerated: a
    # replacement needs a missing copy AND a same-length foreign copy.
    exp_short = Counter(
        exp_c[i]
        for i, t in enumerate(exp)
        if len(t) < MIN_BLOCK and t.isalpha()
    )
    obs_short = Counter(
        obs_c_all[j]
        for j, o in enumerate(obs)
        if len(o) < MIN_BLOCK and o.isalpha()
    )
    missing_short = exp_short - obs_short
    excess_short = obs_short - exp_short
    replaced = [
        a
        for a in missing_short
        for b in excess_short
        if len(a) == len(b)
    ]
    if replaced:
        contradicted_chars += max(len(a) for a in replaced)
    # Observed-side superset (Blocker 3): name-shaped observed tokens the
    # recorded band cannot explain — appended names, merged second rows,
    # message/cc rows that merely MENTION the recorded patient.
    unexplained_names = sum(
        1
        for j, raw in enumerate(obs_raw)
        if not explained[j] and _name_shaped(raw, obs[j])
    )
    return BandMatch(
        matched_chars / total_chars,
        max(uncovered_runs, default=0),
        contradicted_chars,
        suspect_chars,
        unexplained_names,
        max_absent_alpha,
    )


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


def _band_ok(match: BandMatch) -> bool:
    """The pinned operating point (docs/validation/IDENTITY_ROC.md):
    coverage, uncovered-run, contradiction, suspect (letter-letter
    collision), unexplained-name and absent-name budgets must ALL hold."""
    return (
        match.coverage >= COVERAGE_THRESHOLD
        and match.max_uncovered_run <= UNCOVERED_RUN_CAP
        and match.contradicted_chars <= CONTRADICTED_CHARS_CAP
        and match.suspect_chars <= SUSPECT_CHARS_CAP
        and match.unexplained_name_tokens <= UNEXPLAINED_NAME_TOKENS_CAP
        and match.max_absent_alpha_token <= ABSENT_NAME_TOKEN_CAP
    )


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
        match = band_match(substituted, observed_text)
        return IdentityCheck(
            status="verified" if _band_ok(match) else "mismatch",
            mode="param",
            coverage=round(match.coverage, 4),
            expected=substituted,
            observed=observed_text,
            param=in_band[0],
        )

    if not hay:
        return IdentityCheck(status="unreadable", expected=expected)
    match = band_match(context_text, observed_text)
    return IdentityCheck(
        status="verified" if _band_ok(match) else "mismatch",
        coverage=round(match.coverage, 4),
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
