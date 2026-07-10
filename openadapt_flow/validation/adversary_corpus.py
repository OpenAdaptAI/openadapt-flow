"""Held-out adversarial corpus for the identity band matcher.

The recurring failure mode of the identity layer has been fixing against
exactly the adversaries that found the last bug (a fixed point, not a
false-negative rate). This module breaks that cycle: a deterministic,
seeded generator of ``(recorded_band, observed_band, label)`` pairs,
``label in {"same_entity", "different_entity"}``, built and FROZEN
**before** the 2026-07-10 matcher change was designed.

Frozen protocol
---------------

1. The generator, ``FROZEN_SEED``, and a hash manifest of the generated
   set (``docs/validation/adversary_corpus_manifest.json``) are committed
   BEFORE the matcher is evaluated or modified, so post-hoc tuning of the
   corpus toward a matcher is detectable in git history.
2. After the first evaluation the generator must not be modified to make
   results look better. Genuine generator bugs may be fixed, but must be
   disclosed explicitly, and the manifest regenerated in the same commit.
3. ``tests/test_adversary_corpus.py`` regenerates the corpus and fails if
   its hash or per-category counts drift from the committed manifest.

Corpus shape
------------

``different_entity`` (the false-accept side — a VERIFIED here is a
wrong-patient action):

- ``prefix_extension``     — Phil/Philip/Phillipa-class name pairs
- ``single_letter_edit``   — John/Joan-class sibling names
- ``transposition``        — adjacent-letter transposed names
- ``generational_suffix``  — Jr/Sr/II/III/IV on one side only
- ``same_surname_diff_first`` / ``same_first_diff_surname``
- ``shared_clinical_text`` — different person, identical long
  procedure/reason columns (name is a small fraction of the band)
- ``dob_off_by_one_field`` — same name, one DOB field differs
- ``mrn_digit_swap``       — same name+DOB, MRN digits swapped/changed
- ``adjacent_row_mixture`` — the wrong sibling row plus stray tokens
  bleeding from adjacent rows

``same_entity`` (the false-abort side — a MISMATCH here is a $-cost
fallback, not a safety event): OCR noise on the TRUE row —

- ``ocr_confusion``        — l/1/I, O/0, rn/m, cl/d, 5/s, 2/z class swaps
- ``token_split`` / ``token_join``
- ``dropped_short_tokens`` — short tokens (<= 3 chars) lost by OCR
- ``case_whitespace``      — case and whitespace jitter
- ``reordered_segments``   — the modal-band permutation class
- ``occlusion``            — dropped leading/trailing tokens
- ``spurious_tokens``      — 1-2 tokens bleeding in from adjacent rows
- ``compound_noise``       — several of the above at once

A deliberate exclusion, for the record: bare 2-character name prefixes
("Jo" vs "Joan") are not generated — the matcher's token floor makes
2-char tokens verbatim-only evidence and the class is irreducibly
ambiguous at band level; the >= 3-char prefix families cover the sibling
mechanism.

Different-entity name perturbations are guaranteed NOT to be plausible
OCR misreads of the original: the corpus carries its own frozen copy of
the standard OCR confusion classes (independent of whatever table the
matcher uses, so later matcher changes cannot silently re-shape the
corpus) and rejects perturbations that are confusion-equivalent to the
original — those would be mislabeled pairs, not adversaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Callable

FROZEN_SEED = 20260710

# Pairs per generator category (10 different + 9 same categories below):
# >= 2000 pairs per label, as required by the validation protocol.
N_DIFFERENT_PER_CATEGORY = 220  # 10 categories -> 2200 different_entity
N_SAME_PER_CATEGORY = 240  # 9 categories  -> 2160 same_entity

LABEL_SAME = "same_entity"
LABEL_DIFFERENT = "different_entity"


@dataclass(frozen=True)
class CorpusPair:
    """One adversarial probe: a recorded band vs an observed band."""

    recorded: str
    observed: str
    label: str  # LABEL_SAME | LABEL_DIFFERENT
    category: str


# -- frozen OCR-confusion model (corpus-local copy; see module docstring) ----

# Character classes a real OCR engine confuses. Used for two things:
# (a) generating same_entity OCR noise, (b) REJECTING different_entity
# name perturbations that would be confusion-equivalent to the original
# (mislabeled data). Deliberately independent of the matcher's table.
_CONFUSION_GROUPS = ("l1i|!", "o0", "s5", "z2", "b8", "g9")
_CONFUSION_MULTI = (("rn", "m"), ("cl", "d"), ("vv", "w"))
_CANON = {}
for _group in _CONFUSION_GROUPS:
    for _ch in _group:
        _CANON[_ch] = _group[0]

# Substitutions the same_entity noise generator draws from (raw-text
# forms, case preserved where OCR would see it).
_NOISE_SUBS = (
    ("l", "1"),
    ("1", "l"),
    ("i", "l"),
    ("I", "1"),
    ("I", "l"),
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
    ("rn", "m"),
    ("m", "rn"),
    ("cl", "d"),
    ("d", "cl"),
    ("w", "vv"),
)


def corpus_canonical(text: str) -> str:
    """Frozen confusion-canonical form (corpus-local; lowercased)."""
    t = text.lower()
    for a, b in _CONFUSION_MULTI:
        t = t.replace(a, b)
    return "".join(_CANON.get(ch, ch) for ch in t)


# -- fixture data -------------------------------------------------------------

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "Michael", "Linda", "David",
    "Barbara", "William", "Elizabeth", "Richard", "Susan", "Joseph",
    "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Christopher",
    "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret",
    "Mark", "Sandra", "Donald", "Ashley", "Steven", "Kimberly", "Paul",
    "Emily", "Andrew", "Donna", "Joshua", "Michelle", "Kenneth", "Carol",
    "Kevin", "Amanda", "Brian", "Dorothy", "George", "Melissa", "Timothy",
    "Deborah", "Ronald", "Stephanie", "Edward", "Rebecca", "Jason",
    "Sharon", "Jeffrey", "Laura", "Ryan", "Cynthia", "Jacob", "Kathleen",
    "Gary", "Amy", "Nicholas", "Angela", "Eric", "Shirley", "Jonathan",
    "Anna", "Stephen", "Brenda", "Larry", "Pamela", "Justin", "Emma",
    "Scott", "Nicole", "Brandon", "Helen", "Benjamin", "Samantha",
    "Samuel", "Katherine", "Gregory", "Christine", "Alexander", "Debra",
    "Patrick", "Rachel", "Frank", "Carolyn", "Raymond", "Janet", "Jack",
    "Catherine", "Dennis", "Maria", "Jerry", "Heather", "Tyler", "Diane",
    "Aaron", "Ruth", "Jose", "Julie", "Adam", "Olivia", "Nathan", "Joyce",
    "Henry", "Virginia", "Douglas", "Victoria", "Zachary", "Kelly",
    "Peter", "Lauren", "Kyle", "Christina", "Ethan", "Joan", "Walter",
    "Evelyn",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson",
    "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez",
    "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen",
    "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans",
    "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes",
    "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers",
    "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
    "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Belford",
]

# Prefix-extension families: every member >= 3 chars; each shorter member
# is a strict prefix of each longer member it is paired with.
PREFIX_FAMILIES = [
    ("Phil", "Philip", "Phillip", "Phillipa"),
    ("Sam", "Samuel", "Samantha"),
    ("Dan", "Daniel", "Daniela", "Danielle"),
    ("Ben", "Benjamin"),
    ("Alex", "Alexander", "Alexandra", "Alexis"),
    ("Chris", "Christopher", "Christina", "Christine", "Christian"),
    ("Rob", "Robert", "Roberta"),
    ("Will", "William"),
    ("Kat", "Katherine", "Kathleen"),
    ("Kate", "Katelyn"),
    ("Joan", "Joanna"),
    ("Joseph", "Josephine"),
    ("Ann", "Anna", "Anne", "Annette"),
    ("Tom", "Tommy"),
    ("Matt", "Matthew"),
    ("Pat", "Patrick", "Patricia"),
    ("Edwin", "Edwina"),
    ("Jen", "Jenna", "Jennifer"),
    ("Steph", "Stephanie", "Stephen"),
    ("Max", "Maxwell", "Maxine"),
    ("Gabriel", "Gabriella"),
    ("Jack", "Jackson", "Jacklyn"),
    ("John", "Johnny", "Johnathan"),
    ("Rose", "Rosemary", "Roseanne"),
    ("Carl", "Carla", "Carlos"),
    ("Mari", "Maria", "Marian", "Marianne"),
    ("Don", "Donna", "Donald"),
    ("Ray", "Raymond"),
    ("Vic", "Victor", "Victoria"),
    ("Fred", "Frederick", "Freda"),
    ("Nat", "Natalie", "Nathan", "Nathaniel"),
    ("Theo", "Theodore", "Theodora"),
    ("Gene", "Geneva", "Genevieve"),
    ("Lou", "Louis", "Louise", "Louisa"),
    ("Art", "Arthur"),
    ("Stan", "Stanley"),
    ("Marc", "Marcus", "Marcia"),
    ("Tim", "Timothy"),
    ("Andre", "Andrea", "Andres", "Andrew"),
    ("Rich", "Richard"),
    ("Deb", "Debra", "Deborah"),
    ("Herb", "Herbert"),
    ("Cass", "Cassandra", "Cassidy"),
    ("Fran", "Frances", "Francine", "Francisco"),
    ("Gus", "Gustavo"),
    ("Abe", "Abel"),
    ("Ron", "Ronald", "Ronnie"),
    ("Kim", "Kimberly"),
    ("Jess", "Jessica", "Jesse"),
    ("Mel", "Melissa", "Melinda", "Melvin"),
    ("Cal", "Calvin"),
    ("Sal", "Sally", "Salvador"),
]

# Curated single-letter-edit sibling pairs (edit is NOT an OCR confusion).
EDIT_SIBLING_PAIRS = [
    ("John", "Joan"),
    ("Joan", "Jean"),
    ("Jane", "June"),
    ("Jane", "Jade"),
    ("Mark", "Marc"),
    ("Mary", "Macy"),
    ("Mary", "Mara"),
    ("Karen", "Karin"),
    ("Jon", "Jan"),
    ("Terry", "Kerry"),
    ("Harry", "Larry"),
    ("Jerry", "Perry"),
    ("Rita", "Rina"),
    ("Rose", "Rosa"),
    ("Gary", "Cary"),
    ("Bill", "Will"),
    ("Ted", "Tad"),
    ("Susan", "Suzan"),
    ("Dana", "Dara"),
    ("Ellen", "Elden"),
]

GENERATIONAL_SUFFIXES = ["Jr", "Sr", "II", "III", "IV"]

PROCEDURES = [
    "Comprehensive metabolic panel with lipid screening",
    "Cardiology follow-up consultation and medication review",
    "MRI lumbar spine without contrast",
    "Annual wellness examination with immunization update",
    "Physical therapy evaluation for chronic knee pain",
    "Colonoscopy screening with biopsy if indicated",
    "Echocardiogram transthoracic complete with doppler",
    "Pulmonary function testing pre and post bronchodilator",
    "Diabetic retinopathy screening with dilated fundus exam",
    "Orthopedic intake for rotator cuff impingement",
    "Dermatology full body skin examination",
    "Behavioral health intake assessment and treatment plan",
    "CT abdomen and pelvis with oral contrast",
    "Sleep study polysomnography with CPAP titration",
    "Allergy panel testing environmental and food",
    "Gastroenterology consult for reflux management",
    "Renal function panel with electrolyte monitoring",
    "Prenatal visit with fetal heart tone assessment",
    "Post operative wound check and suture removal",
    "Vaccination influenza and pneumococcal administration",
    "Thyroid ultrasound with fine needle aspiration",
    "Stress test exercise tolerance with EKG monitoring",
    "Hemoglobin A1c and fasting glucose monitoring",
    "Bone density DEXA scan hip and spine",
    "Urology consult for recurrent kidney stones",
    "Neurology evaluation for chronic migraine management",
    "Mammogram bilateral screening digital tomosynthesis",
    "Hearing evaluation with audiometry and tympanometry",
    "Wound care debridement and dressing change",
    "Infusion therapy iron sucrose administration",
]

PRIORITIES = ["High", "Medium", "Low", "Urgent", "Routine", "STAT"]
SEXES = ["M", "F"]

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


# -- row/band construction ----------------------------------------------------


def _person(rng: Random) -> dict:
    year = rng.randint(1930, 2009)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return {
        "first": rng.choice(FIRST_NAMES),
        "last": rng.choice(LAST_NAMES),
        "dob": f"{year:04d}-{month:02d}-{day:02d}",
        "sex": rng.choice(SEXES),
        "mrn": rng.choice("ABCDEFGH")
        + "".join(str(rng.randint(0, 9)) for _ in range(6)),
        "phone": f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
    }


def _band(rng: Random, p: dict, template: int | None = None) -> str:
    """Render one EMR-like row band for a person. Deterministic per rng."""
    t = rng.randint(0, 4) if template is None else template
    if t == 0:
        return f"{p['last']}, {p['first']} {p['dob']} {p['sex']}"
    if t == 1:
        return (
            f"{p['last']}, {p['first']} {p['dob']} {p['sex']} MRN {p['mrn']}"
        )
    if t == 2:
        proc = p.get("procedure") or rng.choice(PROCEDURES)
        pri = p.get("priority") or rng.choice(PRIORITIES)
        return f"{p['first']} {p['last']} {proc} {pri}"
    if t == 3:
        proc = p.get("procedure") or rng.choice(PROCEDURES)
        return f"{p['last']}, {p['first']} {p['mrn']} {proc}"
    return f"{p['last']}, {p['first']} {p['dob']} {p['phone']}"


def _with_first(p: dict, first: str) -> dict:
    q = dict(p)
    q["first"] = first
    return q


# -- noise primitives (same_entity side, also mild noise on wrong rows) ------


def _apply_confusions(text: str, rng: Random, n: int) -> str:
    """Apply up to ``n`` OCR character confusions at random positions."""
    out = text
    for _ in range(n):
        options = [(src, dst) for src, dst in _NOISE_SUBS if src in out]
        if not options:
            break
        src, dst = rng.choice(options)
        # replace ONE occurrence, chosen at random
        starts = []
        start = out.find(src)
        while start != -1:
            starts.append(start)
            start = out.find(src, start + 1)
        pos = rng.choice(starts)
        out = out[:pos] + dst + out[pos + len(src):]
    return out


def _split_token(text: str, rng: Random, times: int = 1) -> str:
    tokens = text.split()
    for _ in range(times):
        idx = [i for i, t in enumerate(tokens) if len(t) >= 4]
        if not idx:
            break
        i = rng.choice(idx)
        tok = tokens[i]
        cut = rng.randint(2, len(tok) - 2)
        tokens[i: i + 1] = [tok[:cut], tok[cut:]]
    return " ".join(tokens)


def _join_tokens(text: str, rng: Random, times: int = 1) -> str:
    tokens = text.split()
    for _ in range(times):
        if len(tokens) < 2:
            break
        i = rng.randrange(len(tokens) - 1)
        tokens[i: i + 2] = [tokens[i] + tokens[i + 1]]
    return " ".join(tokens)


def _drop_short_tokens(text: str, rng: Random) -> str:
    tokens = text.split()
    short = [i for i, t in enumerate(tokens) if len(t) <= 3]
    if not short:
        return text
    n = min(len(short), rng.randint(1, 2))
    drop = set(rng.sample(short, n))
    kept = [t for i, t in enumerate(tokens) if i not in drop]
    return " ".join(kept) if kept else text


def _case_whitespace_jitter(text: str, rng: Random) -> str:
    chars = []
    for ch in text:
        if ch.isalpha() and rng.random() < 0.25:
            chars.append(ch.upper() if ch.islower() else ch.lower())
        else:
            chars.append(ch)
        if ch == " " and rng.random() < 0.3:
            chars.append(" ")
    return "".join(chars)


def _reorder_segments(text: str, rng: Random) -> str:
    tokens = text.split()
    if len(tokens) < 3:
        return text
    k = rng.randint(2, min(4, len(tokens)))
    cuts = sorted(rng.sample(range(1, len(tokens)), k - 1))
    segments = []
    prev = 0
    for c in [*cuts, len(tokens)]:
        segments.append(tokens[prev:c])
        prev = c
    order = list(range(len(segments)))
    while True:
        rng.shuffle(order)
        if order != sorted(order):
            break
    return " ".join(" ".join(segments[i]) for i in order)


def _occlude(text: str, rng: Random) -> str:
    tokens = text.split()
    n = rng.randint(1, 2) if len(tokens) > 3 else 1
    if rng.random() < 0.5:
        kept = tokens[n:]
    else:
        kept = tokens[:-n]
    return " ".join(kept) if kept else text


def _spurious(text: str, rng: Random) -> str:
    tokens = text.split()
    bleed_src = rng.choice(PROCEDURES).split()
    for _ in range(rng.randint(1, 2)):
        tok = rng.choice(bleed_src)
        tokens.insert(rng.randint(0, len(tokens)), tok)
    return " ".join(tokens)


def _maybe_mild_noise(text: str, rng: Random, p: float = 0.3) -> str:
    """Wrong-sibling rows are read by the same OCR: sometimes noisy too."""
    if rng.random() < p:
        return _apply_confusions(text, rng, rng.randint(1, 2))
    return text


# -- different_entity name perturbation guards -------------------------------


def _distinct_entities(a: str, b: str) -> bool:
    """True when two name strings are NOT confusion-equivalent (a real
    sibling pair, not a plausible OCR misread of the same name)."""
    return corpus_canonical(a) != corpus_canonical(b)


def _random_letter_edit(name: str, rng: Random) -> str | None:
    """One-letter substitution that is not an OCR confusion of the
    original; None when no valid edit exists."""
    if len(name) < 4:
        return None
    for _ in range(20):
        pos = rng.randrange(1, len(name))
        if not name[pos].isalpha():
            continue
        repl = rng.choice(_ALPHABET)
        if repl == name[pos].lower():
            continue
        variant = name[:pos] + repl + name[pos + 1:]
        if _distinct_entities(variant, name):
            return variant
    return None


def _transpose(name: str, rng: Random) -> str | None:
    """Adjacent transposition that is not confusion-equivalent."""
    if len(name) < 4:
        return None
    positions = list(range(len(name) - 1))
    rng.shuffle(positions)
    for pos in positions:
        a, b = name[pos], name[pos + 1]
        if a.lower() == b.lower():
            continue
        variant = name[:pos] + b + a + name[pos + 2:]
        if _distinct_entities(variant, name):
            return variant
    return None


# -- generators: different_entity ---------------------------------------------


def _gen_prefix_extension(rng: Random) -> tuple[str, str]:
    family = rng.choice(PREFIX_FAMILIES)
    a, b = rng.sample(list(family), 2)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    template = rng.randint(0, 4)
    recorded = _band(rng, _with_first(p, a), template)
    observed = _band(rng, _with_first(p, b), template)
    return recorded, _maybe_mild_noise(observed, rng)


def _gen_single_letter_edit(rng: Random) -> tuple[str, str]:
    if rng.random() < 0.5:
        a, b = rng.choice(EDIT_SIBLING_PAIRS)
        if rng.random() < 0.5:
            a, b = b, a
    else:
        a = rng.choice(FIRST_NAMES)
        b = _random_letter_edit(a, rng)
        while b is None:
            a = rng.choice(FIRST_NAMES)
            b = _random_letter_edit(a, rng)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    template = rng.randint(0, 4)
    recorded = _band(rng, _with_first(p, a), template)
    observed = _band(rng, _with_first(p, b), template)
    return recorded, _maybe_mild_noise(observed, rng)


def _gen_transposition(rng: Random) -> tuple[str, str]:
    a = rng.choice(FIRST_NAMES)
    b = _transpose(a, rng)
    while b is None:
        a = rng.choice(FIRST_NAMES)
        b = _transpose(a, rng)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    template = rng.randint(0, 4)
    recorded = _band(rng, _with_first(p, a), template)
    observed = _band(rng, _with_first(p, b), template)
    return recorded, _maybe_mild_noise(observed, rng)


def _gen_generational_suffix(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    suffix = rng.choice(GENERATIONAL_SUFFIXES)
    q = dict(p)
    q["first"] = f"{p['first']} {suffix}"
    template = rng.randint(0, 4)
    if rng.random() < 0.5:
        recorded, observed = _band(rng, p, template), _band(rng, q, template)
    else:
        recorded, observed = _band(rng, q, template), _band(rng, p, template)
    return recorded, _maybe_mild_noise(observed, rng)


def _gen_same_surname_diff_first(rng: Random) -> tuple[str, str]:
    a, b = rng.sample(FIRST_NAMES, 2)
    while not _distinct_entities(a, b):
        a, b = rng.sample(FIRST_NAMES, 2)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    template = rng.randint(0, 4)
    recorded = _band(rng, _with_first(p, a), template)
    observed = _band(rng, _with_first(p, b), template)
    return recorded, _maybe_mild_noise(observed, rng)


def _gen_same_first_diff_surname(rng: Random) -> tuple[str, str]:
    la, lb = rng.sample(LAST_NAMES, 2)
    while not _distinct_entities(la, lb):
        la, lb = rng.sample(LAST_NAMES, 2)
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    q = dict(p)
    q["last"] = lb
    p["last"] = la
    template = rng.randint(0, 4)
    return _band(rng, p, template), _maybe_mild_noise(
        _band(rng, q, template), rng
    )


def _gen_shared_clinical_text(rng: Random) -> tuple[str, str]:
    proc = rng.choice(PROCEDURES)
    pri = rng.choice(PRIORITIES)
    a, b = _person(rng), _person(rng)
    while a["first"] == b["first"] or not _distinct_entities(
        f"{a['first']} {a['last']}", f"{b['first']} {b['last']}"
    ):
        b = _person(rng)
    for p in (a, b):
        p["procedure"] = proc
        p["priority"] = pri
    template = rng.choice([2, 3])
    return _band(rng, a, template), _maybe_mild_noise(
        _band(rng, b, template), rng
    )


def _gen_dob_off_by_one_field(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    year, month, day = (int(x) for x in p["dob"].split("-"))
    field = rng.choice(["year", "month", "day"])
    if field == "year":
        year += rng.choice([-1, 1])
    elif field == "month":
        month = month + 1 if month < 12 else month - 1
    else:
        day = day + 1 if day < 28 else day - 1
    q = dict(p)
    q["dob"] = f"{year:04d}-{month:02d}-{day:02d}"
    template = rng.choice([0, 1, 4])
    return _band(rng, p, template), _maybe_mild_noise(
        _band(rng, q, template), rng
    )


def _gen_mrn_digit_swap(rng: Random) -> tuple[str, str]:
    p = _person(rng)
    digits = list(p["mrn"][1:])
    for _ in range(20):
        i = rng.randrange(len(digits) - 1)
        if digits[i] != digits[i + 1]:
            digits[i], digits[i + 1] = digits[i + 1], digits[i]
            break
    else:
        i = rng.randrange(len(digits))
        digits[i] = str((int(digits[i]) + rng.randint(1, 8)) % 10)
    q = dict(p)
    q["mrn"] = p["mrn"][0] + "".join(digits)
    if q["mrn"] == p["mrn"]:  # all-equal-digit MRN: force a digit change
        digits[0] = str((int(digits[0]) + 1) % 10)
        q["mrn"] = p["mrn"][0] + "".join(digits)
    template = rng.choice([1, 3])
    return _band(rng, p, template), _maybe_mild_noise(
        _band(rng, q, template), rng
    )


def _gen_adjacent_row_mixture(rng: Random) -> tuple[str, str]:
    """The wrong sibling row, with stray tokens bleeding from neighbors."""
    proc = rng.choice(PROCEDURES)
    pri = rng.choice(PRIORITIES)
    a, b = _person(rng), _person(rng)
    while a["first"] == b["first"]:
        b = _person(rng)
    for p in (a, b):
        p["procedure"] = proc
        p["priority"] = pri
    template = rng.choice([2, 3])
    recorded = _band(rng, a, template)
    observed = _spurious(_band(rng, b, template), rng)
    return recorded, _maybe_mild_noise(observed, rng)


# -- generators: same_entity --------------------------------------------------


def _true_row(rng: Random) -> str:
    p = _person(rng)
    p["procedure"] = rng.choice(PROCEDURES)
    p["priority"] = rng.choice(PRIORITIES)
    return _band(rng, p)


def _gen_ocr_confusion(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _apply_confusions(row, rng, rng.randint(1, 3))


def _gen_token_split(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _split_token(row, rng, rng.randint(1, 2))


def _gen_token_join(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _join_tokens(row, rng, rng.randint(1, 2))


def _gen_dropped_short_tokens(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _drop_short_tokens(row, rng)


def _gen_case_whitespace(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _case_whitespace_jitter(row, rng)


def _gen_reordered_segments(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _reorder_segments(row, rng)


def _gen_occlusion(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _occlude(row, rng)


def _gen_spurious_tokens(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    return row, _spurious(row, rng)


def _gen_compound_noise(rng: Random) -> tuple[str, str]:
    row = _true_row(rng)
    noisy = _apply_confusions(row, rng, rng.randint(1, 2))
    if rng.random() < 0.5:
        noisy = _split_token(noisy, rng)
    else:
        noisy = _join_tokens(noisy, rng)
    noisy = _case_whitespace_jitter(noisy, rng)
    if rng.random() < 0.3:
        noisy = _spurious(noisy, rng)
    return row, noisy


# -- corpus assembly ----------------------------------------------------------

_DIFFERENT_GENERATORS: list[tuple[str, Callable[[Random], tuple[str, str]]]] = [
    ("prefix_extension", _gen_prefix_extension),
    ("single_letter_edit", _gen_single_letter_edit),
    ("transposition", _gen_transposition),
    ("generational_suffix", _gen_generational_suffix),
    ("same_surname_diff_first", _gen_same_surname_diff_first),
    ("same_first_diff_surname", _gen_same_first_diff_surname),
    ("shared_clinical_text", _gen_shared_clinical_text),
    ("dob_off_by_one_field", _gen_dob_off_by_one_field),
    ("mrn_digit_swap", _gen_mrn_digit_swap),
    ("adjacent_row_mixture", _gen_adjacent_row_mixture),
]

_SAME_GENERATORS: list[tuple[str, Callable[[Random], tuple[str, str]]]] = [
    ("ocr_confusion", _gen_ocr_confusion),
    ("token_split", _gen_token_split),
    ("token_join", _gen_token_join),
    ("dropped_short_tokens", _gen_dropped_short_tokens),
    ("case_whitespace", _gen_case_whitespace),
    ("reordered_segments", _gen_reordered_segments),
    ("occlusion", _gen_occlusion),
    ("spurious_tokens", _gen_spurious_tokens),
    ("compound_noise", _gen_compound_noise),
]


def generate_corpus(seed: int = FROZEN_SEED) -> list[CorpusPair]:
    """Generate the full corpus, deterministically, from ``seed``.

    Category order and per-category counts are fixed; every random choice
    flows from one ``random.Random(seed)`` stream, so the output is
    byte-stable across runs and platforms.
    """
    rng = Random(seed)
    pairs: list[CorpusPair] = []
    for category, gen in _DIFFERENT_GENERATORS:
        for _ in range(N_DIFFERENT_PER_CATEGORY):
            recorded, observed = gen(rng)
            pairs.append(
                CorpusPair(recorded, observed, LABEL_DIFFERENT, category)
            )
    for category, gen in _SAME_GENERATORS:
        for _ in range(N_SAME_PER_CATEGORY):
            recorded, observed = gen(rng)
            pairs.append(CorpusPair(recorded, observed, LABEL_SAME, category))
    return pairs


def canonical_serialization(pairs: list[CorpusPair]) -> str:
    """Stable text form of the corpus, the input to the frozen hash."""
    return "\n".join(
        f"{p.label}\t{p.category}\t{p.recorded}\t{p.observed}" for p in pairs
    )


def corpus_sha256(pairs: list[CorpusPair]) -> str:
    return hashlib.sha256(
        canonical_serialization(pairs).encode("utf-8")
    ).hexdigest()


def build_manifest(pairs: list[CorpusPair], seed: int = FROZEN_SEED) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for p in pairs:
        counts.setdefault(p.label, {})
        counts[p.label][p.category] = counts[p.label].get(p.category, 0) + 1
    return {
        "seed": seed,
        "n_total": len(pairs),
        "sha256": corpus_sha256(pairs),
        "counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        type=Path,
        default=None,
        help="Write the frozen hash manifest JSON to this path.",
    )
    args = parser.parse_args()
    pairs = generate_corpus()
    manifest = build_manifest(pairs)
    print(json.dumps(manifest, indent=2))
    if args.write_manifest:
        args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.write_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
