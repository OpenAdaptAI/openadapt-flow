"""Adversarial corpus v3 — identifier letter/digit collisions.

The 2026-07-10 SECOND review of PR #16 found a 5th wrong-patient P0
reopening (see ``tests/test_identity_out_of_corpus.py``,
``TestBlocker5*``): the round-3 suspect budget guarded NAME tokens only,
so an alphanumeric identifier (MRN, account number) differing only by a
letter/digit-confusable character (l/1, O/0, S/5, Z/2, B/8, g/9) between
two DIFFERENT patients canonicalized equal and silently verified — while
corpus v1's ``mrn_digit_swap`` class only ever swapped/changed DIGITS
(748291 vs 748292), which are never in one confusion class, so it could
not surface the letter/digit hole.

This module is a VERSIONED EXTENSION, same discipline as v2: corpora v1
and v2 are untouched (their generators, seeds and manifests stay frozen,
history intact); v3 has its own seed and its own SHA manifest
(``docs/validation/adversary_corpus_v3_manifest.json``), committed BEFORE
the identifier-suspect matcher change is evaluated on it (the corpus-v3
commit precedes the matcher-fix commit in git history).

Single class, ``different_entity``:

- ``id_letter_digit_collision`` — two entities identical in every token
  EXCEPT an alphanumeric identifier that differs by exactly one
  letter/digit-confusable position, generated systematically from the
  frozen letter/digit confusion pairs. A VERIFY here is a wrong-patient
  action: the identifier is the sole thing distinguishing the two rows,
  and it is exactly what MRN-based disambiguation relies on. ABORT is
  the only safe outcome (the true-row-OCR-noise reading is
  indistinguishable at band level, so it is charged to availability —
  see docs/LIMITS.md and the identifier-suspect rule in
  runtime.identity).

The corpus deliberately does NOT add a same-entity identifier-noise
class: under the chosen safety-first design ALL confusion-differing
recorded identifiers abort, so a same-entity label would be unwinnable
by construction (the availability cost is measured directly on v2's
``digit_confusion_true_row`` class and reported in IDENTITY_ROC.md).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from random import Random
from typing import Callable

from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    PROCEDURES,
    CorpusPair,
    _person,
    build_manifest,
)

FROZEN_SEED_V3 = 20260712

N_COLLISION = 300

# Letter/digit confusion pairs (the character classes that mix a letter
# and a digit — the exact hole). Each pair (letter, digit) can OCR either
# way; the generator uses both directions.
LETTER_DIGIT_PAIRS = (
    ("l", "1"),
    ("i", "1"),
    ("o", "0"),
    ("s", "5"),
    ("z", "2"),
    ("b", "8"),
    ("g", "9"),
)

DEPARTMENTS = [
    "Cardiology",
    "Neurology",
    "Billing",
    "Oncology",
    "Radiology",
    "Orthopedics",
    "Dermatology",
    "Pediatrics",
    "Nephrology",
    "Endocrinology",
]

ID_LABELS = ["MRN", "Acct", "ID", "Chart", "Ref"]

_ID_ALPHABET = "ACDEFHJKMNPRTUVWXY"  # unambiguous prefix letters
_ID_DIGITS = "0123456789"


def _make_identifier(rng: Random) -> str:
    """A realistic alphanumeric identifier: optional leading letter then
    5-6 digits (MRN/account shape)."""
    body = "".join(rng.choice(_ID_DIGITS) for _ in range(rng.randint(5, 6)))
    if rng.random() < 0.6:
        return rng.choice(_ID_ALPHABET) + body
    return body


def _confusable_variant(identifier: str, rng: Random) -> str | None:
    """Flip exactly one position of ``identifier`` across a letter/digit
    confusion pair, so the result canonicalizes equal but is raw-unequal
    (a plausible OCR misread of a DIFFERENT real identifier). None when
    no confusable position exists."""
    positions = list(range(len(identifier)))
    rng.shuffle(positions)
    for pos in positions:
        ch = identifier[pos].lower()
        for a, b in LETTER_DIGIT_PAIRS:
            if ch == a:
                repl = b
            elif ch == b:
                repl = a
            else:
                continue
            variant = identifier[:pos] + repl + identifier[pos + 1:]
            if variant != identifier:
                return variant
    return None


def _row_template(rng: Random, p: dict, label: str) -> str:
    """An EMR-like row with a ``{id}`` placeholder for the identifier.

    Rendered ONCE per pair and formatted with each of the two
    identifiers, so the two rows differ in the identifier token and
    NOTHING else — the identifier is the sole discriminator by
    construction (a different shape/dept/procedure on each side would be
    a second difference and defeat the class)."""
    shape = rng.randrange(3)
    if shape == 0:
        return f"{p['last']} {p['first']} {label} {{id}} {p['dept']}"
    if shape == 1:
        return f"{p['last']}, {p['first']} {p['dob']} {label} {{id}}"
    return f"{p['last']}, {p['first']} {label} {{id}} {p['proc']}"


def _gen_id_letter_digit_collision(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    p["dept"] = rng.choice(DEPARTMENTS)
    p["proc"] = rng.choice(PROCEDURES)
    label = rng.choice(ID_LABELS)
    while True:
        identifier = _make_identifier(rng)
        variant = _confusable_variant(identifier, rng)
        if variant is not None:
            break
    if rng.random() < 0.5:
        identifier, variant = variant, identifier
    template = _row_template(rng, p, label)
    return template.format(id=identifier), template.format(id=variant)


_V3_GENERATORS: list[
    tuple[str, str, int, Callable[[Random], tuple[str, str]]]
] = [
    (LABEL_DIFFERENT, "id_letter_digit_collision", N_COLLISION,
     _gen_id_letter_digit_collision),
]


def generate_corpus_v3(seed: int = FROZEN_SEED_V3) -> list[CorpusPair]:
    """Generate corpus v3, deterministically, from ``seed``."""
    rng = Random(seed)
    pairs: list[CorpusPair] = []
    for label, category, count, gen in _V3_GENERATORS:
        for _ in range(count):
            recorded, observed = gen(rng)
            pairs.append(CorpusPair(recorded, observed, label, category))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", type=Path, default=None)
    args = parser.parse_args()
    pairs = generate_corpus_v3()
    manifest = build_manifest(pairs, seed=FROZEN_SEED_V3)
    print(json.dumps(manifest, indent=2))
    if args.write_manifest:
        args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.write_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
