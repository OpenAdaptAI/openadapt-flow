"""ROC evaluation of the identity band matcher on the frozen corpora.

Sweeps the matcher's decision parameters over the held-out adversarial
corpora — v1 (:mod:`~openadapt_flow.validation.adversary_corpus`, 4360
pairs, frozen 2026-07-10 before the first rebuild) plus v2
(:mod:`~openadapt_flow.validation.adversary_corpus_v2`, 2240 pairs,
frozen 2026-07-10 before the out-of-corpus redesign) — and reports the
false-accept / false-abort trade-off:

- **false accept** — a ``different_entity`` pair VERIFIED (wrong-patient
  write: catastrophic), or an ``indistinguishable`` pair VERIFIED (the
  band is textually identical to a real different-entity twin, so a
  verify is a false accept for the twin).
- **false abort** — a ``same_entity`` pair not verified: one
  hybrid-fallback escalation (~$0.10) or a human retry.
- **justified abort** — an ``indistinguishable`` pair not verified: the
  correct outcome for both readings of the band. Reported separately;
  never counted as a false abort.

SELECTION-BIAS DISCLOSURE, stated plainly: the operating point is chosen
on the SAME corpora that produce the headline numbers. The corpora were
frozen before the matcher changes they evaluate (detectable in git
history), which prevents tuning the corpus toward the matcher — it does
not prevent the operating point from being fit to these corpora. Every
zero below is "zero on corpus v1+v2 plus the out-of-corpus probe set",
not "zero in the world"; corpus v1's own zero was shown partially
tautological by the 2026-07-10 review, which is why v2 and the probe
set exist.

Outputs (committed under docs/validation/): ``identity_roc.png``,
``IDENTITY_ROC.md`` (tables + the chosen operating point with
rationale, occlusion recount, realistic-exposure analysis),
``identity_roc.json`` (raw sweep numbers).

The LEGACY matcher evaluated for the before-curve is a frozen verbatim
copy of the pre-2026-07-10 ``band_match`` (verbatim / 0.8-containment /
0.7-similarity tiers) — the implementation that verified Phil/Philip,
John/Joan and Jr/Sr sibling rows (the third wrong-patient reopening).

Run: ``python -m openadapt_flow.validation.identity_roc --out docs/validation``
"""

from __future__ import annotations

import argparse
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from openadapt_flow.runtime.identity import (
    ABSENT_NAME_TOKEN_CAP,
    CONTRADICTED_CHARS_CAP,
    CONTRADICTION_SIM,
    COVERAGE_THRESHOLD,
    MIN_BLOCK,
    SUSPECT_CHARS_CAP,
    UNCOVERED_RUN_CAP,
    UNEXPLAINED_NAME_TOKENS_CAP,
    BandMatch,
    band_match,
    longest_run,
    ocr_canonical,
    squash,
    tokenize,
)
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    CorpusPair,
    generate_corpus,
)
from openadapt_flow.validation.adversary_corpus_v2 import (
    LABEL_INDISTINGUISHABLE,
    generate_corpus_v2,
)
from openadapt_flow.validation.adversary_corpus_v3 import generate_corpus_v3

BIG = 10**9  # "budget disabled" sentinel in the sweep grids

# -- sweep grids ----------------------------------------------------------------

SIM_GRID = (0.62, 0.75)
COVERAGE_GRID = (0.70, 0.75, 0.80, 0.85, 0.90)
RUN_CAP_GRID = (2, 3, 4, 6, 8)
CONTRA_CAP_GRID = (0, 2, BIG)
SUSPECT_CAP_GRID = (0, BIG)
NAME_CAP_GRID = (0, 1, BIG)
ALPHA_CAP_GRID = (3, BIG)

# The production operating point (must mirror runtime.identity constants;
# pinned by tests/test_identity.py boundary tests).
OPERATING_POINT = {
    "contradiction_sim": CONTRADICTION_SIM,
    "coverage_threshold": COVERAGE_THRESHOLD,
    "uncovered_run_cap": UNCOVERED_RUN_CAP,
    "contradicted_chars_cap": CONTRADICTED_CHARS_CAP,
    "suspect_chars_cap": SUSPECT_CHARS_CAP,
    "unexplained_name_tokens_cap": UNEXPLAINED_NAME_TOKENS_CAP,
    "absent_name_token_cap": ABSENT_NAME_TOKEN_CAP,
}

# The decision the PRE-redesign matcher shipped with (new budgets off):
# used for the occlusion recount and before/after comparisons.
SHIPPED_CAPS = dict(
    coverage=COVERAGE_THRESHOLD,
    run_cap=UNCOVERED_RUN_CAP,
    contra_cap=CONTRADICTED_CHARS_CAP,
    suspect_cap=BIG,
    name_cap=BIG,
    alpha_cap=BIG,
)

PRODUCTION_CAPS = dict(
    coverage=OPERATING_POINT["coverage_threshold"],
    run_cap=OPERATING_POINT["uncovered_run_cap"],
    contra_cap=OPERATING_POINT["contradicted_chars_cap"],
    suspect_cap=OPERATING_POINT["suspect_chars_cap"],
    name_cap=OPERATING_POINT["unexplained_name_tokens_cap"],
    alpha_cap=OPERATING_POINT["absent_name_token_cap"],
)


# -- frozen legacy matcher (pre-2026-07-10), for the before-curve ---------------


def _legacy_token_matched(
    token: str, hay_squashed: str, hay_tokens: list[str]
) -> bool:
    """Verbatim / containment(0.8-run) / similarity(0.7) tiers, verbatim
    copy of the matcher that shipped with feat/fix-wrong-actions."""
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
            if ratio >= 0.7:
                return True
    return False


def legacy_band_match(expected_text: str, observed_text: str) -> BandMatch:
    """The pre-2026-07-10 band matcher (all new-budget fields 0)."""
    expected_tokens = tokenize(expected_text)
    if not expected_tokens:
        return BandMatch(0.0, 0, 0)
    hay_squashed = squash(observed_text)
    hay_tokens = tokenize(observed_text)
    matched_chars = 0
    total_chars = 0
    uncovered_runs: list[int] = []
    current_run = 0
    for token in expected_tokens:
        total_chars += len(token)
        if hay_squashed and _legacy_token_matched(
            token, hay_squashed, hay_tokens
        ):
            matched_chars += len(token)
            if current_run:
                uncovered_runs.append(current_run)
                current_run = 0
        else:
            current_run += len(token)
    if current_run:
        uncovered_runs.append(current_run)
    return BandMatch(
        matched_chars / total_chars, max(uncovered_runs, default=0), 0
    )


# -- evaluation -------------------------------------------------------------------


@dataclass(frozen=True)
class SweepPoint:
    matcher: str  # "current" | "legacy"
    contradiction_sim: float | None
    coverage_threshold: float
    uncovered_run_cap: int
    contradicted_chars_cap: int
    suspect_chars_cap: int
    unexplained_name_tokens_cap: int
    absent_name_token_cap: int
    false_accept: float  # fraction of different+indistinguishable VERIFIED
    false_abort: float  # fraction of same_entity NOT verified
    justified_abort: float  # fraction of indistinguishable NOT verified

    def as_dict(self) -> dict:
        return {
            "matcher": self.matcher,
            "contradiction_sim": self.contradiction_sim,
            "coverage_threshold": self.coverage_threshold,
            "uncovered_run_cap": self.uncovered_run_cap,
            "contradicted_chars_cap": self.contradicted_chars_cap,
            "suspect_chars_cap": self.suspect_chars_cap,
            "unexplained_name_tokens_cap": self.unexplained_name_tokens_cap,
            "absent_name_token_cap": self.absent_name_token_cap,
            "false_accept": self.false_accept,
            "false_abort": self.false_abort,
            "justified_abort": self.justified_abort,
        }


def _decide(
    m: BandMatch,
    *,
    coverage: float,
    run_cap: int,
    contra_cap: int,
    suspect_cap: int = BIG,
    name_cap: int = BIG,
    alpha_cap: int = BIG,
) -> bool:
    return (
        m.coverage >= coverage
        and m.max_uncovered_run <= run_cap
        and m.contradicted_chars <= contra_cap
        and m.suspect_chars <= suspect_cap
        and m.unexplained_name_tokens <= name_cap
        and m.max_absent_alpha_token <= alpha_cap
    )


def _rates(
    pairs: list[CorpusPair], stats: list[BandMatch], **caps
) -> tuple[float, float, float]:
    fa = fa_n = ab = ab_n = ja = ja_n = 0
    for pair, m in zip(pairs, stats):
        verified = _decide(m, **caps)
        if pair.label == LABEL_SAME:
            ab_n += 1
            ab += not verified
        else:
            fa_n += 1
            fa += verified
            if pair.label == LABEL_INDISTINGUISHABLE:
                ja_n += 1
                ja += not verified
    return (
        (fa / fa_n) if fa_n else 0.0,
        (ab / ab_n) if ab_n else 0.0,
        (ja / ja_n) if ja_n else 0.0,
    )


def sweep(pairs: list[CorpusPair]) -> list[SweepPoint]:
    """Full decision-parameter sweep for both matchers on v1+v2."""
    points: list[SweepPoint] = []
    for sim in SIM_GRID:
        stats = [
            band_match(p.recorded, p.observed, contradiction_sim=sim)
            for p in pairs
        ]
        for coverage in COVERAGE_GRID:
            for run_cap in RUN_CAP_GRID:
                for contra_cap in CONTRA_CAP_GRID:
                    for suspect_cap in SUSPECT_CAP_GRID:
                        for name_cap in NAME_CAP_GRID:
                            for alpha_cap in ALPHA_CAP_GRID:
                                fa, ab, ja = _rates(
                                    pairs,
                                    stats,
                                    coverage=coverage,
                                    run_cap=run_cap,
                                    contra_cap=contra_cap,
                                    suspect_cap=suspect_cap,
                                    name_cap=name_cap,
                                    alpha_cap=alpha_cap,
                                )
                                points.append(
                                    SweepPoint(
                                        "current", sim, coverage, run_cap,
                                        contra_cap, suspect_cap, name_cap,
                                        alpha_cap, fa, ab, ja,
                                    )
                                )
    legacy_stats = [legacy_band_match(p.recorded, p.observed) for p in pairs]
    for coverage in COVERAGE_GRID:
        for run_cap in RUN_CAP_GRID:
            fa, ab, ja = _rates(
                pairs, legacy_stats, coverage=coverage, run_cap=run_cap,
                contra_cap=BIG,
            )
            points.append(
                SweepPoint(
                    "legacy", None, coverage, run_cap, BIG, BIG, BIG, BIG,
                    fa, ab, ja,
                )
            )
    return points


def per_category(
    pairs: list[CorpusPair], match_fn, caps: dict
) -> dict[str, dict[str, float]]:
    """Per-generator-category error rates at a given decision."""
    counts: dict[tuple[str, str], list[int]] = {}
    for p in pairs:
        m = match_fn(p.recorded, p.observed)
        verified = _decide(m, **caps)
        wrong = (not verified) if p.label == LABEL_SAME else verified
        n, w = counts.setdefault((p.label, p.category), [0, 0])
        counts[(p.label, p.category)] = [n + 1, w + wrong]
    out: dict[str, dict[str, float]] = {}
    for (label, category), (n, wrong) in sorted(counts.items()):
        out.setdefault(label, {})[category] = wrong / n
    return out


def pareto(points: list[SweepPoint]) -> list[SweepPoint]:
    """Non-dominated frontier (lower is better on both axes)."""
    frontier = []
    for p in points:
        if not any(
            (q.false_accept <= p.false_accept
             and q.false_abort <= p.false_abort
             and (q.false_accept < p.false_accept
                  or q.false_abort < p.false_abort))
            for q in points
        ):
            frontier.append(p)
    return sorted(frontier, key=lambda p: (p.false_accept, p.false_abort))


# -- targeted analyses ---------------------------------------------------------


def occlusion_recount(v1_pairs: list[CorpusPair]) -> dict:
    """Recount of the occlusion false-aborts (2026-07-10 review): how
    many aborted bands still had BOTH name tokens readable?

    The earlier IDENTITY_ROC.md framed occlusion aborts as "bands whose
    identity tokens were not read at all — refusing is the correct
    epistemic outcome". The reviewer measured that roughly half of them
    still had both name tokens readable and aborted on trailing DOB/MRN
    loss instead. This recount reproduces that measurement: the v1
    occlusion generator drops leading OR trailing tokens of a band whose
    first two tokens are always the name, so name readability is
    checked by canonical token presence.
    """
    def names_readable(p: CorpusPair) -> bool:
        name_tokens = tokenize(" ".join(p.recorded.split()[:2]))
        obs_c = {ocr_canonical(t) for t in tokenize(p.observed)}
        return all(ocr_canonical(t) in obs_c for t in name_tokens)

    out = {}
    for tag, caps in (("shipped", SHIPPED_CAPS), ("production", PRODUCTION_CAPS)):
        aborts = names_ok = 0
        for p in v1_pairs:
            if p.label != LABEL_SAME or p.category != "occlusion":
                continue
            m = band_match(p.recorded, p.observed)
            if _decide(m, **caps):
                continue
            aborts += 1
            names_ok += names_readable(p)
        out[tag] = {"aborts": aborts, "aborts_with_both_names_readable": names_ok}
    return out


def realistic_exposure(
    v2_pairs: list[CorpusPair], v3_pairs: list[CorpusPair]
) -> dict:
    """Exposure analysis: what catches each collision class when the
    SUSPECT rule is disabled, isolating what the suspect rule alone
    defends. Name-collision classes (v2) and the identifier letter/digit
    collision class (v3, the 5th reopening) are measured together — the
    identifier row is exactly the 'name is the only discriminative token'
    shape one level deeper: an MRN whose confusion twin is a different
    real patient, with everything else raw-equal, is caught ONLY by the
    suspect rule (now extended to identifiers)."""
    no_suspect = dict(PRODUCTION_CAPS)
    no_suspect["suspect_cap"] = BIG
    out = {}
    sources = {
        "confusion_collision_name_only": v2_pairs,
        "confusion_collision_ids_differ": v2_pairs,
        "confusion_collision_ids_same": v2_pairs,
        "id_letter_digit_collision": v3_pairs,
    }
    for category, src in sources.items():
        subset = [p for p in src if p.category == category]
        n = len(subset)
        fa_full = sum(
            _decide(band_match(p.recorded, p.observed), **PRODUCTION_CAPS)
            for p in subset
        )
        fa_wo_suspect = sum(
            _decide(band_match(p.recorded, p.observed), **no_suspect)
            for p in subset
        )
        out[category] = {
            "n": n,
            "false_accepts_at_production": fa_full,
            "false_accepts_without_suspect_rule": fa_wo_suspect,
        }
    return out


# -- outputs --------------------------------------------------------------------


def render_chart(points: list[SweepPoint], out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    current = [p for p in points if p.matcher == "current"]
    legacy = [p for p in points if p.matcher == "legacy"]
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.scatter(
        [p.false_accept * 100 for p in legacy],
        [p.false_abort * 100 for p in legacy],
        s=28, marker="x", color="#c0392b", alpha=0.8,
        label="legacy matcher (containment + 0.7-similarity tiers)",
    )
    ax.scatter(
        [p.false_accept * 100 for p in current],
        [p.false_abort * 100 for p in current],
        s=16, marker="o", color="#2c7fb8", alpha=0.35,
        label="redesigned matcher (name+identifier suspect, 2026-07-10)",
    )
    front = pareto(current)
    ax.plot(
        [p.false_accept * 100 for p in front],
        [p.false_abort * 100 for p in front],
        color="#2c7fb8", linewidth=1.2, alpha=0.9, zorder=3,
    )
    op = _op_point(points)
    ax.scatter(
        [op.false_accept * 100], [op.false_abort * 100],
        s=180, marker="*", color="#1a9850", zorder=4,
        label=(
            "chosen operating point "
            f"(FA {op.false_accept:.2%}, FAbort {op.false_abort:.1%})"
        ),
    )
    ax.set_xlabel(
        "false-accept rate, % (different/indistinguishable VERIFIED — "
        "wrong-patient click)"
    )
    ax.set_ylabel("false-abort rate, % (same entity refused — $0.10 fallback)")
    ax.set_title(
        "Identity band matcher on frozen corpora v1+v2+v3 "
        "(6900 pairs, seeds 20260710/11/12)"
    )
    ax.set_xscale("symlog", linthresh=0.1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    plt.close(fig)


def _matches_op(p: SweepPoint) -> bool:
    return (
        p.matcher == "current"
        and p.contradiction_sim == OPERATING_POINT["contradiction_sim"]
        and p.coverage_threshold == OPERATING_POINT["coverage_threshold"]
        and p.uncovered_run_cap == OPERATING_POINT["uncovered_run_cap"]
        and p.contradicted_chars_cap
        == OPERATING_POINT["contradicted_chars_cap"]
        and p.suspect_chars_cap == OPERATING_POINT["suspect_chars_cap"]
        and p.unexplained_name_tokens_cap
        == OPERATING_POINT["unexplained_name_tokens_cap"]
        and p.absent_name_token_cap
        == OPERATING_POINT["absent_name_token_cap"]
    )


def _op_point(points: list[SweepPoint]) -> SweepPoint:
    return next(p for p in points if _matches_op(p))


def _find_current(points, **kw):
    for p in points:
        if p.matcher != "current":
            continue
        if all(getattr(p, k) == v for k, v in kw.items()):
            return p
    raise KeyError(kw)


def _cap_str(v: int) -> str:
    return "off" if v >= BIG else str(v)


def _corner_paragraph(points: list[SweepPoint]) -> str:
    """Why the minimum-false-abort zero-FA Pareto corner was rejected."""
    zero_fa = [
        p for p in points
        if p.matcher == "current" and p.false_accept == 0.0
        and not _matches_op(p)
    ]
    if not zero_fa:
        return (
            "No other zero-false-accept point exists on the sweep grid: "
            "the operating point is the unique zero-FA corner."
        )
    corner = min(zero_fa, key=lambda p: p.false_abort)
    op = _op_point(points)
    return (
        f"**Why not the cheaper zero-FA corner** (`coverage "
        f"{corner.coverage_threshold} / run_cap "
        f"{corner.uncovered_run_cap} / absent-name cap "
        f"{_cap_str(corner.absent_name_token_cap)}`, FAbort "
        f"{corner.false_abort:.2%} vs {op.false_abort:.2%}): that "
        "corner disables the absent-name budget and relies on the "
        f"coverage threshold ({corner.coverage_threshold}) sitting just "
        "above the Major-4 probe's coverage (0.826 for 'Belford, Phil' "
        "-> 'Belford,'). The protection is an artifact of band length: "
        "the same absent 4-char name inside a longer band "
        "('Montgomery-Winchester, Phil 1985-03-12 M MRN A482913' loses "
        "'Phil' at coverage 0.915) clears the threshold and verifies "
        "with the identity token never read. The absent-name cap "
        "refuses structurally, independent of band length — that "
        "independence is what the extra "
        f"{op.false_abort - corner.false_abort:+.2%} false aborts buy."
    )


def render_markdown(
    points: list[SweepPoint],
    *,
    corpus_rates: dict,
    cat_tables: dict,
    occlusion: dict,
    exposure: dict,
    out_md: Path,
) -> None:
    """Render IDENTITY_ROC.md from the sweep data (do not edit by hand)."""
    op = _op_point(points)
    sim = OPERATING_POINT["contradiction_sim"]
    legacy_prod = next(
        p for p in points
        if p.matcher == "legacy" and p.coverage_threshold == 0.80
        and p.uncovered_run_cap == 4
    )
    shipped = _find_current(
        points, contradiction_sim=sim, coverage_threshold=0.80,
        uncovered_run_cap=4, contradicted_chars_cap=0,
        suspect_chars_cap=BIG, unexplained_name_tokens_cap=BIG,
        absent_name_token_cap=BIG,
    )
    occ_ship = occlusion["shipped"]
    occ_prod = occlusion["production"]
    exp_no = exposure["confusion_collision_name_only"]
    exp_diff = exposure["confusion_collision_ids_differ"]
    exp_same = exposure["confusion_collision_ids_same"]
    exp_id = exposure["id_letter_digit_collision"]

    lines = [
        "# Identity band matcher — held-out adversarial ROC "
        "(corpora v1+v2+v3)",
        "",
        "Generated by `python -m openadapt_flow.validation.identity_roc` "
        "from the FROZEN corpora: v1 (4360 pairs, seed 20260710), v2 "
        "(2240 pairs, seed 20260711, the classes v1 excluded by "
        "construction) and v3 (300 pairs, seed 20260712, identifier "
        "letter/digit collisions — the 5th-reopening class), hash "
        "manifests committed before the matcher changes they evaluate. "
        "Do not edit by hand.",
        "",
        "**Scope of every number below, stated plainly:** measured on "
        "corpus v1+v2+v3 plus the 18 out-of-corpus reviewer probes "
        "(`tests/test_identity_out_of_corpus.py`) — not 'in the world'. "
        "The operating point is FIT TO THESE CORPORA: freezing the "
        "corpora before the matcher change prevents tuning the corpus "
        "toward the matcher, but nothing prevents the operating point "
        "from being tuned toward the corpora — v1's own 0.000% headline "
        "was shown partially tautological by the first 2026-07-10 review "
        "(its labeling rule excluded confusion-collided names, short-"
        "token discriminators, observed supersets and absent-name "
        "shapes by construction), and v2's zero was in turn falsified by "
        "the SECOND review (its `mrn_digit_swap` class only ever swapped "
        "DIGITS, so it never surfaced the identifier letter/digit "
        "collision the 5th reopening exploited). v3 exists because of "
        "that second review; the same criticism could apply again.",
        "",
        "- **false accept** = a `different_entity` OR `indistinguishable` "
        "pair VERIFIED — a wrong-patient click, catastrophic in an EMR.",
        "- **false abort** = a `same_entity` pair refused — one hybrid "
        "fallback (~$0.10) or a human retry.",
        "- **justified abort** = an `indistinguishable` pair refused — "
        "the true row misread by a letter-letter confusion is textually "
        "identical to a real sibling (Neil misread as Nell vs an actual "
        "patient Nell), so ABORT is correct for BOTH readings and is "
        "never counted as a false abort.",
        "",
        "![ROC](identity_roc.png)",
        "",
        "## Chosen operating point",
        "",
        f"`contradiction_sim={sim}`, "
        f"`coverage_threshold={OPERATING_POINT['coverage_threshold']}`, "
        f"`uncovered_run_cap={OPERATING_POINT['uncovered_run_cap']}`, "
        f"`contradicted_chars_cap="
        f"{OPERATING_POINT['contradicted_chars_cap']}`, "
        f"`suspect_chars_cap={OPERATING_POINT['suspect_chars_cap']}`, "
        f"`unexplained_name_tokens_cap="
        f"{OPERATING_POINT['unexplained_name_tokens_cap']}`, "
        f"`absent_name_token_cap="
        f"{OPERATING_POINT['absent_name_token_cap']}` →",
        f"**false accept {op.false_accept:.3%}, false abort "
        f"{op.false_abort:.2%}, indistinguishable-class abort "
        f"{op.justified_abort:.1%}** across v1+v2+v3.",
        "",
        "Reference points at the same coverage/run/contradiction caps:",
        "",
        f"- legacy matcher (pre-rebuild tiers): FA "
        f"{legacy_prod.false_accept:.1%} / FAbort "
        f"{legacy_prod.false_abort:.1%};",
        f"- the SHIPPED pre-review decision (new budgets off): FA "
        f"{shipped.false_accept:.2%} / FAbort {shipped.false_abort:.2%} "
        "— every one of those false accepts is an out-of-corpus-review "
        "class (collision/short-token/superset/absent-name) that v1 "
        "could not see;",
        f"- per corpus at the production point: v1 FA "
        f"{corpus_rates['v1']['fa']:.3%} / FAbort "
        f"{corpus_rates['v1']['fabort']:.2%}; v2 FA "
        f"{corpus_rates['v2']['fa']:.3%} / FAbort "
        f"{corpus_rates['v2']['fabort']:.2%}, indistinguishable abort "
        f"{corpus_rates['v2']['justified']:.1%}; v3 (identifier "
        f"collisions) FA {corpus_rates['v3']['fa']:.3%}.",
        "",
        "**The weighting, out loud:** a false accept is a wrong-patient "
        "write on a real EMR — a clinical-safety event that downstream "
        "note verification does NOT catch (the note really is saved, in "
        "the wrong chart). A false abort costs one ~$0.10 hybrid-fallback "
        "escalation or a human retry. We price that asymmetry at four-plus "
        "orders of magnitude, so only zero-measured-false-accept points "
        "were considered, and the six budgets are kept independently "
        "strict (defense in depth) rather than taking the minimum-false-"
        "abort zero-FA corner. The availability price is real and stated "
        "in the tables below: the v1 false-abort rate rose from 10.7% "
        "(pre-review matcher) through 21.2% (first redesign) to "
        f"{corpus_rates['v1']['fabort']:.1%} after the identifier-suspect "
        "fix — concentrated in occlusion, digit-class OCR noise that "
        "lands on an identifier token (DOB/MRN/phone) and now aborts "
        "(the true-row identifier-noise cost, see below), the "
        "indistinguishable letter-letter mechanism, and capitalized "
        "adjacent-row bleed — because each review showed the cheaper "
        "operating point was buying availability with silent "
        "wrong-patient classes.",
        "",
        _corner_paragraph(points),
        "",
        "## The indistinguishable trade-off",
        "",
        "The suspect rule cannot verify a letter-letter-collided name "
        "and cannot distinguish a misread from a sibling — nobody can, "
        "at band level: the bands are textually identical. The price of "
        "refusing the Neil/Nell sibling (Blocker 1) is refusing the "
        "true row whenever OCR letter-letter-garbles a name token:",
        "",
        f"- v2 `confusion_misread_true_row` (all 200 labeled "
        "indistinguishable): "
        f"{corpus_rates['v2']['justified']:.1%} abort — correct for "
        "both readings, counted as justified;",
        f"- v1 `ocr_confusion` / `compound_noise` false aborts "
        f"({cat_tables['v1_current'][LABEL_SAME]['ocr_confusion']:.1%} / "
        f"{cat_tables['v1_current'][LABEL_SAME]['compound_noise']:.1%}) "
        "are dominated by the same letter-letter shapes (v1 labels them "
        "same_entity because its generator KNOWS it applied noise; the "
        "matcher cannot know that, and treating them as verifiable is "
        "exactly the Blocker-1 hole).",
        "",
        "## The identifier letter/digit collision (5th reopening)",
        "",
        "The SECOND review found the suspect budget guarded NAME tokens "
        "only (`_name_plausible` is False for any token with a digit), so "
        "it was OFF for MRNs/account numbers while the confusion "
        "canonicalization (l/1, O/0, S/5, Z/2, B/8, g/9) still applied to "
        "them — a DIFFERENT patient's identifier one confusable char "
        "apart ('A01234' vs 'AO1234') silently VERIFIED, defeating "
        "MRN-based disambiguation of same-name patients. The suspect rule "
        "now also fires on a confusion-only match where the RECORDED "
        "token contains a digit (an identifier):",
        "",
        f"- v3 `id_letter_digit_collision` (300 pairs, DIFFERENT patients "
        f"one confusable identifier char apart): FA "
        f"{cat_tables['v3_current'][LABEL_DIFFERENT]['id_letter_digit_collision']:.1%} "
        f"(legacy "
        f"{cat_tables['v3_legacy'][LABEL_DIFFERENT]['id_letter_digit_collision']:.1%}).",
        "- **Chosen design: option A, no corroboration escape.** A "
        "confusion-differing identifier aborts even when name and DOB "
        "raw-match, so two same-name patients distinguished ONLY by an "
        "OCR-confusable identifier char never verify — the case a "
        "'corroborate with name+DOB' design (option B) would wrongly "
        "ALLOW (two real patients can share a name and DOB; the MRN is "
        "the sole unique key). Option B's corroboration escape is itself "
        "a wrong-patient verify, so it was rejected for the clinical "
        "release.",
        "- **Scoping matters:** the rule keys on the RECORDED token "
        "carrying a digit, so a NAME OCR'd with a digit-class confusion "
        "('Belford' -> 'Be1ford') stays a clean match (the recorded "
        "'Belford' is all-alpha — proof it is a name), while an "
        "identifier aborts. All-digit differences (748291 vs 748292) are "
        "not confusion-equivalent and mismatch via coverage/contradiction "
        "as before.",
        f"- **The availability cost, honestly:** true-row OCR noise ON an "
        "identifier now aborts too — indistinguishable at band level from "
        "a different patient. v2 `digit_confusion_true_row` rose from "
        f"0.0% to "
        f"{cat_tables['v2_current'][LABEL_SAME]['digit_confusion_true_row']:.1%} "
        "false aborts, and v1's digit-noise classes rose correspondingly. "
        "That is the cheap direction (a ~$0.10 halt vs a wrong-patient "
        "write) and is disclosed in docs/LIMITS.md.",
        "",
        "## Occlusion recount (correcting the earlier framing)",
        "",
        "The earlier IDENTITY_ROC.md claimed occlusion false-aborts were "
        "'bands whose identity tokens were not read at all — refusing is "
        "the correct epistemic outcome'. The reviewer measured otherwise "
        "and this recount confirms it:",
        "",
        f"- shipped decision: {occ_ship['aborts']}/240 occlusion aborts, "
        f"of which **{occ_ship['aborts_with_both_names_readable']}** "
        "still had BOTH name tokens readable (the abort was trailing "
        "DOB/MRN loss, not unreadable identity);",
        f"- production decision: {occ_prod['aborts']}/240 aborts, "
        f"**{occ_prod['aborts_with_both_names_readable']}** with both "
        "name tokens readable.",
        "",
        "So roughly half of the occlusion aborts are a plain "
        "availability cost on rows whose name WAS readable — kept "
        "because a band that lost its trailing discriminators (DOB/MRN) "
        "retains only the name, and the name alone is exactly the "
        "surface the collision classes attack. That is a priced "
        "trade-off, not an epistemic virtue.",
        "",
        "## Realistic-exposure analysis (collision shapes)",
        "",
        "What catches the wrong row if the suspect rule is disabled — "
        "i.e. what does the suspect rule alone defend? (Correcting the "
        "first review's write-up: its 'ids differ -> 180/180 caught "
        "without the suspect rule' claim was true only because those "
        "pairs differ in the NAME by a letter-letter confusion AND carry "
        "distinct DOB/MRN. It did NOT cover the letter/DIGIT identifier "
        "collision, which the second review then exploited — see the v3 "
        "row.)",
        "",
        "| collision class | n | FA at production | FA without the "
        "suspect rule |",
        "| --- | --- | --- | --- |",
        f"| name collision, distinct DOB/MRN present | {exp_diff['n']} | "
        f"{exp_diff['false_accepts_at_production']} | "
        f"{exp_diff['false_accepts_without_suspect_rule']} |",
        f"| name collision, identical DOB/MRN (probe shape) | "
        f"{exp_same['n']} | "
        f"{exp_same['false_accepts_at_production']} | "
        f"{exp_same['false_accepts_without_suspect_rule']} |",
        f"| name collision, name is the ONLY discriminator | {exp_no['n']} "
        f"| {exp_no['false_accepts_at_production']} | "
        f"{exp_no['false_accepts_without_suspect_rule']} |",
        f"| identifier letter/DIGIT collision (v3) | {exp_id['n']} | "
        f"{exp_id['false_accepts_at_production']} | "
        f"{exp_id['false_accepts_without_suspect_rule']} |",
        "",
        "Reading: a name collision with distinct DOB/MRN is caught by the "
        "absence/contradiction budgets alone "
        f"({exp_diff['n'] - exp_diff['false_accepts_without_suspect_rule']}"
        f"/{exp_diff['n']} without the suspect rule). But when the "
        "collision is in the SOLE discriminator — a name with no other "
        "distinguishing token, or an IDENTIFIER (the v3 row: "
        f"{exp_id['false_accepts_without_suspect_rule']}/{exp_id['n']} "
        "verify without the suspect rule, 0 with it) — the suspect rule "
        "is the only defense. It defends only against collisions INSIDE "
        "the frozen confusion table: an exotic misread outside the table, "
        "a collision by case/whitespace only, the 'Ann Marie'/'Annmarie' "
        "token-join equivalence, and short (1-2 char) ALL-ALPHA codes "
        "confused with a digit (the recorded token carries no digit, so "
        "the identifier rule does not see it, and the name rule needs "
        ">= 3 chars) remain verifiable — disclosed in docs/LIMITS.md.",
        "",
        "## Error rates by generator category (at the production decision)",
        "",
        "### Corpus v1",
        "",
        "| category | label | legacy matcher | redesigned matcher |",
        "| --- | --- | --- | --- |",
    ]
    for label in (LABEL_DIFFERENT, LABEL_SAME):
        kind = "false accept" if label == LABEL_DIFFERENT else "false abort"
        for category in cat_tables["v1_current"][label]:
            lines.append(
                f"| `{category}` | {kind} | "
                f"{cat_tables['v1_legacy'][label][category]:.1%} | "
                f"{cat_tables['v1_current'][label][category]:.1%} |"
            )
    lines += [
        "",
        "### Corpus v2",
        "",
        "| category | label | legacy matcher | redesigned matcher |",
        "| --- | --- | --- | --- |",
    ]
    for label in (LABEL_DIFFERENT, LABEL_INDISTINGUISHABLE, LABEL_SAME):
        if label == LABEL_SAME:
            kind = "false abort"
        elif label == LABEL_INDISTINGUISHABLE:
            kind = "false accept (verify on indistinguishable)"
        else:
            kind = "false accept"
        for category in cat_tables["v2_current"].get(label, {}):
            lines.append(
                f"| `{category}` | {kind} | "
                f"{cat_tables['v2_legacy'][label][category]:.1%} | "
                f"{cat_tables['v2_current'][label][category]:.1%} |"
            )
    lines += [
        "",
        "### Corpus v3 (identifier letter/digit collisions)",
        "",
        "| category | label | legacy matcher | redesigned matcher |",
        "| --- | --- | --- | --- |",
    ]
    for category in cat_tables["v3_current"].get(LABEL_DIFFERENT, {}):
        lines.append(
            f"| `{category}` | false accept | "
            f"{cat_tables['v3_legacy'][LABEL_DIFFERENT][category]:.1%} | "
            f"{cat_tables['v3_current'][LABEL_DIFFERENT][category]:.1%} |"
        )
    lines += [
        "",
        "## Pareto frontier (redesigned matcher, v1+v2+v3)",
        "",
        "| sim | coverage | run_cap | contra | suspect | name | "
        "absent-alpha | false accept | false abort |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in pareto([p for p in points if p.matcher == "current"]):
        lines.append(
            f"| {p.contradiction_sim} | {p.coverage_threshold} | "
            f"{p.uncovered_run_cap} | {_cap_str(p.contradicted_chars_cap)} | "
            f"{_cap_str(p.suspect_chars_cap)} | "
            f"{_cap_str(p.unexplained_name_tokens_cap)} | "
            f"{_cap_str(p.absent_name_token_cap)} | "
            f"{p.false_accept:.3%} | {p.false_abort:.2%} |"
        )
    lines += [
        "",
        "Raw sweep data: `identity_roc.json`. The operating point is "
        "pinned by boundary tests in `tests/test_identity.py`; the "
        "sibling probes and the 18 out-of-corpus reviewer probes (13 "
        "first review + 5 identifier collision) are pinned as permanent "
        "mismatches in `tests/test_identity.py` and "
        "`tests/test_identity_out_of_corpus.py`.",
        "",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/validation"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    v1 = generate_corpus()
    v2 = generate_corpus_v2()
    v3 = generate_corpus_v3()
    pairs = v1 + v2 + v3
    points = sweep(pairs)
    front = pareto([p for p in points if p.matcher == "current"])

    def corpus_rate(subset):
        stats = [band_match(p.recorded, p.observed) for p in subset]
        fa, ab, ja = _rates(subset, stats, **PRODUCTION_CAPS)
        return {"fa": fa, "fabort": ab, "justified": ja}

    corpus_rates = {
        "v1": corpus_rate(v1),
        "v2": corpus_rate(v2),
        "v3": corpus_rate(v3),
    }
    cat_tables = {
        "v1_current": per_category(v1, band_match, PRODUCTION_CAPS),
        "v1_legacy": per_category(
            v1, legacy_band_match,
            dict(coverage=0.8, run_cap=4, contra_cap=BIG),
        ),
        "v2_current": per_category(v2, band_match, PRODUCTION_CAPS),
        "v2_legacy": per_category(
            v2, legacy_band_match,
            dict(coverage=0.8, run_cap=4, contra_cap=BIG),
        ),
        "v3_current": per_category(v3, band_match, PRODUCTION_CAPS),
        "v3_legacy": per_category(
            v3, legacy_band_match,
            dict(coverage=0.8, run_cap=4, contra_cap=BIG),
        ),
    }
    occlusion = occlusion_recount(v1)
    exposure = realistic_exposure(v2, v3)

    render_markdown(
        points,
        corpus_rates=corpus_rates,
        cat_tables=cat_tables,
        occlusion=occlusion,
        exposure=exposure,
        out_md=args.out / "IDENTITY_ROC.md",
    )
    (args.out / "identity_roc.json").write_text(
        json.dumps(
            {
                "operating_point": OPERATING_POINT,
                "corpus_rates_at_operating_point": corpus_rates,
                "points": [p.as_dict() for p in points],
                "pareto_current": [p.as_dict() for p in front],
                "per_category": cat_tables,
                "occlusion_recount": occlusion,
                "realistic_exposure": exposure,
            },
            indent=2,
        )
        + "\n"
    )
    render_chart(points, args.out / "identity_roc.png")
    print(f"wrote {args.out}/identity_roc.json and identity_roc.png")
    op = _op_point(points)
    print(
        f"operating point: FA={op.false_accept:.3%} "
        f"FAbort={op.false_abort:.2%} justified={op.justified_abort:.1%}"
    )
    print("\nPareto frontier (redesigned matcher):")
    for p in front:
        marker = " <== OPERATING POINT" if _matches_op(p) else ""
        print(
            f"  sim={p.contradiction_sim} cov={p.coverage_threshold} "
            f"run={p.uncovered_run_cap} contra={_cap_str(p.contradicted_chars_cap)} "
            f"suspect={_cap_str(p.suspect_chars_cap)} "
            f"name={_cap_str(p.unexplained_name_tokens_cap)} "
            f"alpha={_cap_str(p.absent_name_token_cap)}: "
            f"FA={p.false_accept:.3%} FAbort={p.false_abort:.2%}{marker}"
        )


if __name__ == "__main__":
    main()
