"""Adversarial corpus v2 — the classes corpus v1 excluded by construction.

The 2026-07-10 review of PR #16 showed that corpus v1's 0.000%
false-accept headline was partially tautological: v1's labeling rule
treats confusion-equivalent bands as the same entity (it *rejects*
different-entity perturbations that are confusion-equivalent to the
original, calling them "mislabeled"), and it never generates short-token
discriminators, observed-side supersets, or absent-name-token pairs. The
thirteen out-of-corpus reviewer probes (all silent verifies at the
shipped operating point) each belong to one of those excluded classes —
pinned verbatim in ``tests/test_identity_out_of_corpus.py``.

This module is a VERSIONED EXTENSION: corpus v1
(:mod:`openadapt_flow.validation.adversary_corpus`) is untouched — its
generator, seed and manifest stay frozen exactly as committed, history
intact. v2 has its own seed and its own SHA manifest
(``docs/validation/adversary_corpus_v2_manifest.json``), committed
BEFORE the redesigned matcher is evaluated on it (same freeze
discipline: the corpus-v2 commit precedes the matcher-fix commit, so
post-hoc tuning of the corpus toward the matcher is detectable in git
history).

Labels — v2 introduces a THIRD label:

- ``different_entity`` — a VERIFY here is a wrong-patient action
  (false accept). ABORT is correct.
- ``same_entity`` — a VERIFY here is correct; an abort is a false
  abort (~$0.10 hybrid fallback / human retry).
- ``indistinguishable`` — the TRUE row misread by a letter-letter OCR
  confusion (e.g. true row 'Neil' read as 'Nell'). The band is
  *textually identical* to its different-entity twin (a real patient
  named Nell), so no band-level matcher can separate them: **ABORT is
  the correct outcome for both readings.** Scoring: abort = correct
  (a justified abort, NOT a false abort); verify = a false accept for
  the different-entity twin.

``different_entity`` categories (all excluded from v1 by construction):

- ``confusion_collision_name_only``  — Neil/Nell-class collided names
  where the name is the ONLY discriminative token in the band (shared
  clinical text, no DOB/MRN) — the true residual-exposure shape.
- ``confusion_collision_ids_differ`` — collided names on realistic
  distinct patients: DOB and MRN present and DIFFERENT.
- ``confusion_collision_ids_same``   — the reviewer-probe shape:
  collided names with IDENTICAL DOB/MRN (unrealistic — MRNs are
  unique — but pinned by the probes and kept measurable).
- ``middle_initial``                 — 1-char middle initial changed.
- ``sex_column``                     — M/F flipped, all else equal.
- ``two_char_name``                  — 2-char first names changed.
- ``superset_appended_name``         — observed appends a middle name.
- ``superset_merged_row``            — observed merges a second row.
- ``superset_title_mention``         — a different row (Dr./message)
  that MENTIONS the recorded patient's full band.
- ``absent_name_token``              — the 4-char first name absent
  outright, nothing in its place (the run-cap boundary shape).

``indistinguishable`` category:

- ``confusion_misread_true_row``     — the true row with ONE
  letter-letter confusion (i/l, rn/m, cl/d, w/vv) applied inside a
  NAME token.

``same_entity`` categories (the availability side of the new budgets):

- ``digit_confusion_true_row``       — digit/symbol-class OCR noise
  only (l/1, O/0, 5/s, 2/z, 8/B, 9/g): a human name contains no
  digits, so these CANNOT be a different name — must verify.
- ``adjacent_bleed_lowercase``       — 1-2 lowercase mid-procedure
  tokens bleeding in from adjacent rows — must verify.
- ``hyphenated_split``               — hyphenated surname split at the
  hyphen by OCR segmentation — must verify.

Confusion-collided variants are generated SYSTEMATICALLY from the
letter-letter members of the frozen confusion table applied over the v1
name lists, plus the curated realistic pairs from the review.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from random import Random
from typing import Callable

from openadapt_flow.validation.adversary_corpus import (
    FIRST_NAMES,
    LABEL_DIFFERENT,
    LABEL_SAME,
    LAST_NAMES,
    PRIORITIES,
    PROCEDURES,
    CorpusPair,
    _band,
    _person,
    _with_first,
    build_manifest,
    corpus_canonical,
)

FROZEN_SEED_V2 = 20260711

LABEL_INDISTINGUISHABLE = "indistinguishable"

# Per-category pair counts (fixed; the manifest pins them).
N_COLLISION = 180  # x3 collision categories
N_DIFFERENT = 150  # x7 remaining different_entity categories
N_INDISTINGUISHABLE = 200
N_SAME = 150  # x3 same_entity categories

# Letter-letter members of the frozen OCR confusion table: substitutions
# whose BOTH sides are plausible name characters (no digits, no symbols).
# These — and only these — can turn one real name into another real name
# (Neil->Nell, Marnie->Mamie, Clay->Day); the digit/symbol members
# (l/1, O/0, 5/s, ...) cannot, because human names contain no digits.
LETTER_CONFUSION_SUBS = (
    ("i", "l"),
    ("l", "i"),
    ("rn", "m"),
    ("m", "rn"),
    ("cl", "d"),
    ("d", "cl"),
    ("w", "vv"),
    ("vv", "w"),
)

# Curated realistic collided pairs (reviewer probes + same mechanism).
CURATED_COLLIDED_FIRST = [
    ("Neil", "Nell"),
    ("Gail", "Gall"),
    ("Marnie", "Mamie"),
    ("Arnie", "Amie"),
    ("Nella", "Neila"),
]
CURATED_COLLIDED_LAST = [
    ("Clay", "Day"),
    ("Clark", "Dark"),
]

TWO_CHAR_NAMES = ["Al", "Bo", "Cy", "Ed", "Jo", "Lu", "Mo", "Ty", "Vi"]

# Digit/symbol-class OCR substitutions (raw-text forms) — the noise a
# matcher must keep tolerating, because no name collision is possible.
DIGIT_NOISE_SUBS = (
    ("l", "1"),
    ("1", "l"),
    ("I", "1"),
    ("O", "0"),
    ("0", "O"),
    ("o", "0"),
    ("S", "5"),
    ("s", "5"),
    ("5", "s"),
    ("Z", "2"),
    ("z", "2"),
    ("B", "8"),
    ("g", "9"),
)

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


# -- collided-name machinery ---------------------------------------------------


def letter_confusion_variants(name: str) -> list[str]:
    """All single-substitution letter-letter collided variants of a name.

    Systematic application of :data:`LETTER_CONFUSION_SUBS` at every
    interior occurrence (the leading capital is left alone — OCR case
    behavior there differs and the reviewer probes are interior). Every
    variant is confusion-equivalent to the original by construction,
    raw-distinct, and purely alphabetic.
    """
    variants: list[str] = []
    body = name[1:]
    for src, dst in LETTER_CONFUSION_SUBS:
        start = 0
        while True:
            pos = body.find(src, start)
            if pos == -1:
                break
            variant = name[0] + body[:pos] + dst + body[pos + len(src) :]
            start = pos + 1
            if variant.lower() == name.lower():
                continue
            if not variant.isalpha():
                continue
            # Sanity: must remain confusion-equivalent (mislabeling guard
            # in the opposite direction from v1 — these pairs MUST be
            # canonical-equal, that is the class).
            if corpus_canonical(variant) != corpus_canonical(name):
                continue
            variants.append(variant)
    return variants


def _collided_first(rng: Random) -> tuple[str, str]:
    """A (name, collided_name) first-name pair, curated or systematic."""
    if rng.random() < 0.4:
        a, b = rng.choice(CURATED_COLLIDED_FIRST)
        if rng.random() < 0.5:
            a, b = b, a
        return a, b
    while True:
        name = rng.choice(FIRST_NAMES)
        variants = letter_confusion_variants(name)
        if variants:
            return name, rng.choice(variants)


def _collided_last(rng: Random) -> tuple[str, str]:
    if rng.random() < 0.4:
        a, b = rng.choice(CURATED_COLLIDED_LAST)
        if rng.random() < 0.5:
            a, b = b, a
        return a, b
    while True:
        name = rng.choice(LAST_NAMES)
        variants = letter_confusion_variants(name)
        if variants:
            return name, rng.choice(variants)


# -- generators: different_entity ----------------------------------------------


def _gen_collision_name_only(rng: Random) -> tuple[str, str]:
    """Collided name is the ONLY discriminative token: shared clinical
    text, no DOB, no MRN — the true residual-exposure shape."""
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    if rng.random() < 0.5:
        a, b = _collided_first(rng)
        recorded = _band(rng, _with_first(p, a), 2)
        observed = _band(rng, _with_first(p, b), 2)
    else:
        a, b = _collided_last(rng)
        q = dict(p)
        p["last"], q["last"] = a, b
        recorded = _band(rng, p, 2)
        observed = _band(rng, q, 2)
    return recorded, observed


def _gen_collision_ids_differ(rng: Random) -> tuple[str, str]:
    """Collided names on realistic distinct patients: DOB and MRN both
    present and DIFFERENT (MRNs are unique in a real EMR)."""
    a, b = _collided_first(rng)
    p = _person(rng)
    q = _person(rng)  # fresh DOB/MRN/phone
    q["last"] = p["last"]
    while q["dob"] == p["dob"] or q["mrn"] == p["mrn"]:
        q = _person(rng)
        q["last"] = p["last"]
    template = rng.choice([1, 4])
    return (
        _band(rng, _with_first(p, a), template),
        _band(rng, _with_first(q, b), template),
    )


def _gen_collision_ids_same(rng: Random) -> tuple[str, str]:
    """The reviewer-probe shape: identical DOB/MRN, collided name."""
    a, b = _collided_first(rng)
    p = _person(rng)
    template = rng.choice([0, 1])
    return (
        _band(rng, _with_first(p, a), template),
        _band(rng, _with_first(p, b), template),
    )


def _distinct_letters(rng: Random) -> tuple[str, str]:
    while True:
        a, b = rng.sample(_ALPHABET, 2)
        if corpus_canonical(a) != corpus_canonical(b):
            return a.upper(), b.upper()


def _gen_middle_initial(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    a, b = _distinct_letters(rng)
    base = f"{p['last']}, {p['first']} {{}} {p['dob']} {p['sex']}"
    return base.format(a), base.format(b)


def _gen_sex_column(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    q = dict(p)
    q["sex"] = "F" if p["sex"] == "M" else "M"
    template = rng.choice([0, 1])
    return _band(rng, p, template), _band(rng, q, template)


def _gen_two_char_name(rng: Random) -> tuple[str, str]:
    while True:
        a, b = rng.sample(TWO_CHAR_NAMES, 2)
        if corpus_canonical(a) != corpus_canonical(b):
            break
    p = _person(rng)
    template = rng.choice([0, 1])
    return (
        _band(rng, _with_first(p, a), template),
        _band(rng, _with_first(p, b), template),
    )


def _gen_superset_appended_name(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    middle = rng.choice(FIRST_NAMES)
    while middle == p["first"]:
        middle = rng.choice(FIRST_NAMES)
    q = dict(p)
    q["first"] = f"{p['first']} {middle}"
    template = rng.choice([0, 1])
    return _band(rng, p, template), _band(rng, q, template)


def _gen_superset_merged_row(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    other = _person(rng)
    while other["first"] == p["first"] and other["last"] == p["last"]:
        other = _person(rng)
    template = rng.choice([0, 1])
    recorded = _band(rng, p, template)
    return recorded, f"{recorded} {_band(rng, other, 0)}"


def _gen_superset_title_mention(rng: Random) -> tuple[str, str]:
    """A different row (a provider line, a message row) that MENTIONS
    the recorded patient's full band — cc: lines, message rows."""
    p = _person(rng)
    other = _person(rng)
    recorded = _band(rng, p, 0)
    shape = rng.randrange(3)
    if shape == 0:
        observed = f"Dr. {other['last']}, {other['first']} re {recorded}"
    elif shape == 1:
        observed = f"Message from {other['first']} {other['last']} re {recorded}"
    else:
        observed = f"cc: {other['last']}, {other['first']} {recorded}"
    return recorded, observed


def _gen_absent_name_token(rng: Random) -> tuple[str, str]:
    """The 4-char first name absent outright — sits exactly at the old
    uncovered-run cap with the identity token never read."""
    four_char = [n for n in FIRST_NAMES if len(n) == 4]
    p = _person(rng)
    p["first"] = rng.choice(four_char)
    recorded = f"{p['last']}, {p['first']} {p['dob']} {p['sex']}"
    observed = f"{p['last']}, {p['dob']} {p['sex']}"
    return recorded, observed


# -- generators: indistinguishable ----------------------------------------------


def _gen_confusion_misread_true_row(rng: Random) -> tuple[str, str]:
    """The TRUE row, with one letter-letter confusion inside a name
    token: textually identical to a collided different-entity pair, so
    ABORT is the correct outcome for both readings."""
    if rng.random() < 0.5:
        a, b = _collided_first(rng)
        p = _person(rng)
        p["procedure"] = rng.choice(PROCEDURES)
        p["priority"] = rng.choice(PRIORITIES)
        template = rng.randint(0, 4)
        return (
            _band(rng, _with_first(p, a), template),
            _band(rng, _with_first(p, b), template),
        )
    a, b = _collided_last(rng)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    q = dict(p)
    p["last"], q["last"] = a, b
    template = rng.randint(0, 4)
    return _band(rng, p, template), _band(rng, q, template)


# -- generators: same_entity -----------------------------------------------------


def _apply_digit_noise(text: str, rng: Random, n: int) -> str:
    out = text
    applied = 0
    for _ in range(n * 4):
        if applied >= n:
            break
        options = [(s, d) for s, d in DIGIT_NOISE_SUBS if s in out]
        if not options:
            break
        src, dst = rng.choice(options)
        starts = []
        start = out.find(src)
        while start != -1:
            starts.append(start)
            start = out.find(src, start + 1)
        pos = rng.choice(starts)
        out = out[:pos] + dst + out[pos + len(src) :]
        applied += 1
    return out


def _gen_digit_confusion_true_row(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    row = _band(rng, p)
    noisy = _apply_digit_noise(row, rng, rng.randint(1, 2))
    return row, noisy


def _gen_adjacent_bleed_lowercase(rng: Random) -> tuple[str, str]:
    """1-2 lowercase mid-procedure tokens bleeding in from an adjacent
    row: the legitimate spurious-token class the unexplained-observed
    budget must not kill."""
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    row = _band(rng, p)
    tokens = row.split()
    bleed_pool = [
        w for w in rng.choice(PROCEDURES).split() if w.islower() and len(w) >= 3
    ]
    for _ in range(rng.randint(1, 2)):
        tokens.insert(rng.randint(0, len(tokens)), rng.choice(bleed_pool))
    return row, " ".join(tokens)


def _gen_hyphenated_split(rng: Random) -> tuple[str, str]:
    l1, l2 = rng.sample(LAST_NAMES, 2)
    p = _person(rng)
    p["last"] = f"{l1}-{l2}"
    template = rng.choice([0, 1])
    recorded = _band(rng, p, template)
    observed = recorded.replace(f"{l1}-{l2}", f"{l1}- {l2}", 1)
    return recorded, observed


# -- corpus assembly -------------------------------------------------------------

_V2_GENERATORS: list[tuple[str, str, int, Callable[[Random], tuple[str, str]]]] = [
    (
        LABEL_DIFFERENT,
        "confusion_collision_name_only",
        N_COLLISION,
        _gen_collision_name_only,
    ),
    (
        LABEL_DIFFERENT,
        "confusion_collision_ids_differ",
        N_COLLISION,
        _gen_collision_ids_differ,
    ),
    (
        LABEL_DIFFERENT,
        "confusion_collision_ids_same",
        N_COLLISION,
        _gen_collision_ids_same,
    ),
    (LABEL_DIFFERENT, "middle_initial", N_DIFFERENT, _gen_middle_initial),
    (LABEL_DIFFERENT, "sex_column", N_DIFFERENT, _gen_sex_column),
    (LABEL_DIFFERENT, "two_char_name", N_DIFFERENT, _gen_two_char_name),
    (
        LABEL_DIFFERENT,
        "superset_appended_name",
        N_DIFFERENT,
        _gen_superset_appended_name,
    ),
    (LABEL_DIFFERENT, "superset_merged_row", N_DIFFERENT, _gen_superset_merged_row),
    (
        LABEL_DIFFERENT,
        "superset_title_mention",
        N_DIFFERENT,
        _gen_superset_title_mention,
    ),
    (LABEL_DIFFERENT, "absent_name_token", N_DIFFERENT, _gen_absent_name_token),
    (
        LABEL_INDISTINGUISHABLE,
        "confusion_misread_true_row",
        N_INDISTINGUISHABLE,
        _gen_confusion_misread_true_row,
    ),
    (LABEL_SAME, "digit_confusion_true_row", N_SAME, _gen_digit_confusion_true_row),
    (LABEL_SAME, "adjacent_bleed_lowercase", N_SAME, _gen_adjacent_bleed_lowercase),
    (LABEL_SAME, "hyphenated_split", N_SAME, _gen_hyphenated_split),
]


def generate_corpus_v2(seed: int = FROZEN_SEED_V2) -> list[CorpusPair]:
    """Generate corpus v2, deterministically, from ``seed``.

    Category order and per-category counts are fixed; every random
    choice flows from one ``random.Random(seed)`` stream, so the output
    is byte-stable across runs and platforms.
    """
    rng = Random(seed)
    pairs: list[CorpusPair] = []
    for label, category, count, gen in _V2_GENERATORS:
        for _ in range(count):
            recorded, observed = gen(rng)
            pairs.append(CorpusPair(recorded, observed, label, category))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        type=Path,
        default=None,
        help="Write the frozen v2 hash manifest JSON to this path.",
    )
    args = parser.parse_args()
    pairs = generate_corpus_v2()
    manifest = build_manifest(pairs, seed=FROZEN_SEED_V2)
    print(json.dumps(manifest, indent=2))
    if args.write_manifest:
        args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.write_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
