"""Out-of-corpus reviewer probes for the identity band matcher.

The 2026-07-10 review of the identity ROC (PR #16) verified thirteen
probes against the shipped matcher at the shipped operating point — all
thirteen VERIFIED, i.e. wrong-patient clicks, despite the corpus-v1
headline of 0.000% false accepts. The corpus excluded every one of these
classes BY CONSTRUCTION (its labeling rule treats confusion-equivalent
bands as same-entity; short tokens and observed-superset shapes were
never generated), so the headline zero was partially tautological.

This file commits the probes verbatim, all asserting MISMATCH, as the
acceptance criteria for the matcher redesign:

- BLOCKER 1 — canonicalization equates distinct names: two DIFFERENT
  patients whose names are OCR-confusion-equivalent (Neil/Nell via i/l,
  Clay/Day via cl/d, Marnie/Mamie via rn/m, Gail/Gall via i/l) verified
  as the same person. Param mode was NOT vulnerable (its raw
  ``longest_run`` check rejects Neil→Nell) — the stricter raw pattern
  already existed in the codebase.
- BLOCKER 2 — tokens shorter than MIN_BLOCK were invisible to the
  contradiction rules: a changed middle initial, a flipped SEX column,
  and changed 2-character names all verified.
- BLOCKER 3 — an observed-side superset always verified: context mode
  had no unexplained-observed-token budget (param mode HAS one), so
  appended tokens, a two-row OCR merge, and a wrong row that merely
  MENTIONS the recorded patient (cc: lines, message rows) verified.
- MAJOR 4 — a fully absent 4-char first name sits exactly at the
  uncovered-run cap: the band verified with the identity token never
  read at all (this exact shape was previously PINNED AS CORRECT in
  tests/test_identity.py::TestOperatingPoint — that pin is flipped in
  the same change that makes these pass).

FREEZE DISCIPLINE: this file is committed BEFORE the matcher redesign
and before corpus v2, so the acceptance criteria are on record first.
At the commit that introduces this file, the thirteen probe tests FAIL
(they reproduce the review); the safe-direction pins below PASS and
must keep passing.

SECOND REVIEW (2026-07-10, 5th wrong-patient reopening — TestBlocker5
below): the round-3 suspect budget guards NAME tokens only
(``_name_plausible`` is False for any token with a digit), so the rule
was OFF for MRNs/account numbers while the confusion canonicalization
(l/1, O/0, S/5, Z/2, B/8, g/9) still applied to them — an alphanumeric
identifier differing only by a letter/digit-confusable character
silently VERIFIED as same-entity, defeating MRN-based disambiguation of
same-name patients. These probes are committed FAILING (they reproduce
the review) before the identifier-suspect fix and corpus v3; the
identifier-noise "stays-verify" cases below are my chosen design's
documented true-row availability boundary.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.identity import verify_target_identity


def _status(recorded: str, observed: str) -> str:
    return verify_target_identity(recorded, observed).status


# -- BLOCKER 1: confusion-collided distinct names -----------------------------


class TestBlocker1ConfusionCollidedNames:
    """Two different patients whose names are confusion-equivalent must
    never verify: when the only evidence for the name token is
    OCR-confusion-equivalence, the honest outcome is an abort."""

    def test_probe_01_neil_vs_nell_i_l(self):
        assert _status(
            "Smith, Neil 1985-03-12 M MRN A482913",
            "Smith, Nell 1985-03-12 M MRN A482913",
        ) == "mismatch"

    def test_probe_02_clay_vs_day_cl_d(self):
        assert _status(
            "Clay, Susan 1962-07-04 F MRN B771204",
            "Day, Susan 1962-07-04 F MRN B771204",
        ) == "mismatch"

    def test_probe_03_marnie_vs_mamie_rn_m(self):
        assert _status(
            "Baker, Marnie 1990-11-02 F",
            "Baker, Mamie 1990-11-02 F",
        ) == "mismatch"

    def test_probe_04_gail_vs_gall_i_l_shared_clinical_text(self):
        assert _status(
            "Gail Turner Comprehensive metabolic panel with lipid"
            " screening High",
            "Gall Turner Comprehensive metabolic panel with lipid"
            " screening High",
        ) == "mismatch"


# -- BLOCKER 2: short tokens invisible to contradiction -----------------------


class TestBlocker2ShortTokenDiscriminators:
    """1-2 char tokens that are raw-unequal and not confusion-equivalent
    are affirmative contradiction, not ignorable residue."""

    def test_probe_05_middle_initial_changed(self):
        assert _status(
            "Smith, John J 1985-03-12 M",
            "Smith, John K 1985-03-12 M",
        ) == "mismatch"

    def test_probe_06_sex_column_flipped(self):
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Belford, Phil 1985-03-12 F",
        ) == "mismatch"

    def test_probe_07_two_char_name_al_vs_bo(self):
        assert _status(
            "Belford, Al 1985-03-12 M",
            "Belford, Bo 1985-03-12 M",
        ) == "mismatch"

    def test_probe_08_two_char_name_jo_vs_ed(self):
        assert _status(
            "Smith, Jo 1985-03-12 M",
            "Smith, Ed 1985-03-12 M",
        ) == "mismatch"


# -- BLOCKER 3: observed-side superset ----------------------------------------


class TestBlocker3ObservedSuperset:
    """A live band that fully contains the recorded band plus extra
    name-shaped tokens is not the recorded row: context mode needs an
    unexplained-observed-token budget (param mode already had one)."""

    def test_probe_09_appended_middle_name(self):
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Belford, Phil James 1985-03-12 M",
        ) == "mismatch"

    def test_probe_10_two_row_ocr_merge(self):
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Belford, Phil 1985-03-12 M Smith, Joan 1962-01-01 F",
        ) == "mismatch"

    def test_probe_11_wrong_row_mentioning_recorded_patient(self):
        # The realistic shape: a message/cc row about the recorded
        # patient — mentions the whole recorded band, is not the row.
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Dr. Smith, John re Belford, Phil 1985-03-12 M",
        ) == "mismatch"


# -- MAJOR 4: absent identity token at the run cap ----------------------------


class TestMajor4AbsentNameToken:
    """A band must not verify when a name-like alphabetic token was never
    read: absence of the identity token is worse than absence of
    trailing numerics."""

    def test_probe_12_absent_four_char_first_name(self):
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Belford, 1985-03-12 M",
        ) == "mismatch"

    def test_probe_13_synthetic_pure_alpha_absence_at_run_cap(self):
        # The exact shape previously pinned VERIFIED in
        # tests/test_identity.py::TestOperatingPoint::
        # test_pure_absence_boundary_at_run_cap — a fully absent 4-char
        # alphabetic token, nothing in its place.
        assert _status(
            "abcd efgh ijkl mnop qrst",
            "abcd efgh ijkl mnop",
        ) == "mismatch"


# -- safe direction: shapes that are correct today and must stay so ----------


class TestSafeDirectionPins:
    """Correct outcomes that the redesign must not regress."""

    def test_hyphenated_name_split_by_ocr_verifies(self):
        assert _status(
            "Smith-Jones, Carol 1985-03-12 F",
            "Smith- Jones, Carol 1985-03-12 F",
        ) == "verified"

    def test_bob_vs_robert_mismatches(self):
        assert _status(
            "Smith, Bob 1985-03-12 M",
            "Smith, Robert 1985-03-12 M",
        ) == "mismatch"

    def test_alison_vs_allison_contradiction_mismatches(self):
        assert _status(
            "Smith, Alison 1985-03-12 F",
            "Smith, Allison 1985-03-12 F",
        ) == "mismatch"

    def test_mrn_edit_mismatches(self):
        assert _status(
            "Belford, Phil 1985-03-12 M MRN A123456",
            "Belford, Phil 1985-03-12 M MRN A123465",
        ) == "mismatch"

    def test_dob_edit_mismatches(self):
        assert _status(
            "Belford, Phil 1985-03-12 M",
            "Belford, Phil 1985-03-13 M",
        ) == "mismatch"

    def test_digit_class_homoglyph_misread_verifies(self):
        # Digit/symbol-involving confusions cannot be a different NAME
        # (human names contain no digits): genuine OCR noise, verified.
        assert _status(
            "Belford, Phil 1985-03-12 M MRN A123456",
            "Be1ford, Phi1 1985-03-12 M MRN A123456",
        ) == "verified"

    def test_digit_class_jitter_on_non_name_tokens_verifies(self):
        assert _status(
            "Jane Sample Knee pain referral High",
            "Jane 5ample Knee pain referral High",
        ) == "verified"

    def test_param_mode_raw_run_still_rejects_neil_to_nell(self):
        # Param mode was NOT vulnerable to Blocker 1: the raw
        # longest_run check rejects Neil→Nell without canonicalization.
        check = verify_target_identity(
            "Open chart for Neil (active)",
            "Open chart for Nell (active)",
            params={"patient": "Neil"},
            param_examples={"patient": "Neil"},
        )
        assert check.status == "mismatch"
        assert check.mode == "param"


class TestBlocker5IdentifierLetterDigitCollision:
    """5th wrong-patient reopening (second review): an alphanumeric
    identifier differing only by a letter/digit-confusable character
    (l/1, O/0, S/5, Z/2, B/8, g/9) canonicalizes equal, so a DIFFERENT
    patient's MRN/account number verified as same-entity. The chosen
    fix (identifier-suspect: a confusion-only match on a RECORDED token
    that contains a digit is suspect -> abort) makes all four abort.

    Design note: the fix is scoped to tokens the RECORDING shows as
    identifiers (recorded token contains a digit). It is NOT a blanket
    "any digit in the observed token" rule — that would abort names
    OCR'd with a digit-class confusion ('Belford' -> 'Be1ford'), which
    must stay verified (TestSafeDirectionPins). There is no wrong-patient
    residual: unlike a corroboration-escape design, a confusion-differing
    identifier aborts even when name and DOB raw-match, so two same-name
    patients distinguished only by an OCR-confusable MRN char never
    verify."""

    def test_probe_14_mrn_l_vs_1(self):
        assert _status(
            "Belford Jane MRN l482913 Cardiology",
            "Belford Jane MRN 1482913 Cardiology",
        ) == "mismatch"

    def test_probe_15_mrn_O_vs_0(self):
        assert _status(
            "Chen Wei MRN O52133 Neurology",
            "Chen Wei MRN 052133 Neurology",
        ) == "mismatch"

    def test_probe_16_acct_S_vs_5(self):
        assert _status(
            "Ramirez Ana Acct S5821 Billing",
            "Ramirez Ana Acct 55821 Billing",
        ) == "mismatch"

    def test_probe_17_same_name_mrn_sole_discriminator(self):
        # The canonical clinical case: two same-name patients whose ONLY
        # difference is one OCR-confusable MRN char. Name raw-matches;
        # the MRN is the sole discriminator; it must abort regardless
        # (this is exactly the case a corroboration-escape design would
        # wrongly allow).
        assert _status(
            "Doe John MRN AO1234",
            "Doe John MRN A01234",
        ) == "mismatch"

    def test_probe_18_fires_in_param_mode(self):
        # MRN as a parameter: the param-mode raw longest_run tolerated a
        # single confusable char in a long identifier, then band_match
        # verified the substituted band. The identifier-suspect rule in
        # band_match closes it in param mode too.
        check = verify_target_identity(
            "Belford Jane MRN l482913 Cardiology",
            "Belford Jane MRN 1482913 Cardiology",
            params={"mrn": "l482913"},
            param_examples={"mrn": "l482913"},
        )
        assert check.status == "mismatch"


class TestBlocker5Controls:
    """The letter/digit boundary is precise: all-digit differences and
    identifier-side noise boundaries must behave as designed."""

    def test_all_digit_mrn_difference_still_mismatches(self):
        # Control: 748291 vs 748292 is NOT a confusion equivalence (2 and
        # 1 are not in one confusion class), so it mismatches via
        # coverage/contradiction, NOT the suspect rule.
        assert _status(
            "Doe John MRN 748291",
            "Doe John MRN 748292",
        ) == "mismatch"

    def test_raw_equal_identifier_still_verifies(self):
        # A raw-identical MRN with only name-side digit-class noise: the
        # identifier is not confusion-differing, so it verifies.
        assert _status(
            "Belford, Phil 1985-03-12 M MRN A123456",
            "Be1ford, Phi1 1985-03-12 M MRN A123456",
        ) == "verified"

    def test_true_row_identifier_noise_aborts_availability_cost(self):
        # DOCUMENTED AVAILABILITY COST of the chosen (safety-first)
        # design: when the TRUE row's own MRN is OCR-garbled by a
        # letter/digit-confusable char, we abort rather than gamble on
        # identity. Indistinguishable from a different-patient row at
        # band level; the halt is the cheap direction. Disclosed in
        # docs/LIMITS.md.
        assert _status(
            "Belford, Phil 1985-03-12 M MRN A01234",
            "Belford, Phil 1985-03-12 M MRN AO1234",
        ) == "mismatch"


class TestDisclosedResidualEdges:
    """Known residual behavior, disclosed in docs/LIMITS.md rather than
    fixed: pinned here so a silent change is visible."""

    def test_ann_marie_vs_annmarie_verifies_via_join_rule(self):
        # The token-join rule (OCR splits one token into two) cannot
        # distinguish 'Ann Marie' from 'Annmarie' — raw-equal after
        # concatenation. Two real patients with those names verify.
        assert _status(
            "Annmarie Cox 1985-03-12 F",
            "Ann marie Cox 1985-03-12 F",
        ) == "verified"
