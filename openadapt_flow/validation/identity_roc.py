"""ROC evaluation of the identity band matcher on the frozen corpus.

Sweeps the matcher's decision parameters over the held-out adversarial
corpus (:mod:`openadapt_flow.validation.adversary_corpus` — FROZEN, seed
and hash manifest committed before any evaluation) and reports the
false-accept / false-abort trade-off:

- **false accept** — a ``different_entity`` pair VERIFIED. In an EMR
  context this is a wrong-patient write: catastrophic, weighted
  accordingly (see ``pick_operating_point``).
- **false abort** — a ``same_entity`` pair not verified. This costs one
  hybrid-fallback escalation (~$0.10) or a human retry: cheap.

Outputs (committed under docs/validation/): ``identity_roc.png`` (the
curves), ``IDENTITY_ROC.md`` (tables + the chosen operating point with
rationale), ``identity_roc.json`` (raw sweep numbers).

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
    CONTRADICTED_CHARS_CAP,
    CONTRADICTION_SIM,
    COVERAGE_THRESHOLD,
    MIN_BLOCK,
    UNCOVERED_RUN_CAP,
    BandMatch,
    band_match,
    longest_run,
    squash,
    tokenize,
)
from openadapt_flow.validation.adversary_corpus import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    CorpusPair,
    generate_corpus,
)

# -- sweep grids --------------------------------------------------------------

SIM_GRID = (0.55, 0.62, 0.70, 0.75)
COVERAGE_GRID = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
RUN_CAP_GRID = (2, 3, 4, 5, 6, 8)
CONTRA_CAP_GRID = (0, 2, 4, 10**9)  # 10**9 == contradiction rule disabled

# The production operating point (must mirror runtime.identity constants;
# pinned by tests/test_identity.py boundary tests).
OPERATING_POINT = {
    "contradiction_sim": CONTRADICTION_SIM,
    "coverage_threshold": COVERAGE_THRESHOLD,
    "uncovered_run_cap": UNCOVERED_RUN_CAP,
    "contradicted_chars_cap": CONTRADICTED_CHARS_CAP,
}


# -- frozen legacy matcher (pre-2026-07-10), for the before-curve -------------


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
    """The pre-2026-07-10 band matcher (contradiction always 0)."""
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


# -- evaluation ---------------------------------------------------------------


@dataclass(frozen=True)
class SweepPoint:
    matcher: str  # "current" | "legacy"
    contradiction_sim: float | None
    coverage_threshold: float
    uncovered_run_cap: int
    contradicted_chars_cap: int
    false_accept: float  # fraction of different_entity VERIFIED
    false_abort: float  # fraction of same_entity NOT verified

    def as_dict(self) -> dict:
        return {
            "matcher": self.matcher,
            "contradiction_sim": self.contradiction_sim,
            "coverage_threshold": self.coverage_threshold,
            "uncovered_run_cap": self.uncovered_run_cap,
            "contradicted_chars_cap": self.contradicted_chars_cap,
            "false_accept": self.false_accept,
            "false_abort": self.false_abort,
        }


def _decide(
    m: BandMatch, coverage: float, run_cap: int, contra_cap: int
) -> bool:
    return (
        m.coverage >= coverage
        and m.max_uncovered_run <= run_cap
        and m.contradicted_chars <= contra_cap
    )


def _rates(
    pairs: list[CorpusPair],
    stats: list[BandMatch],
    coverage: float,
    run_cap: int,
    contra_cap: int,
) -> tuple[float, float]:
    fa = fan = ab = abn = 0
    for pair, m in zip(pairs, stats):
        verified = _decide(m, coverage, run_cap, contra_cap)
        if pair.label == LABEL_DIFFERENT:
            fan += 1
            fa += verified
        else:
            abn += 1
            ab += not verified
    return fa / fan, ab / abn


def sweep(pairs: list[CorpusPair]) -> list[SweepPoint]:
    """Full decision-parameter sweep for both matchers."""
    points: list[SweepPoint] = []
    for sim in SIM_GRID:
        stats = [
            band_match(p.recorded, p.observed, contradiction_sim=sim)
            for p in pairs
        ]
        for coverage in COVERAGE_GRID:
            for run_cap in RUN_CAP_GRID:
                for contra_cap in CONTRA_CAP_GRID:
                    fa, ab = _rates(pairs, stats, coverage, run_cap, contra_cap)
                    points.append(
                        SweepPoint(
                            "current", sim, coverage, run_cap, contra_cap,
                            fa, ab,
                        )
                    )
    legacy_stats = [legacy_band_match(p.recorded, p.observed) for p in pairs]
    for coverage in COVERAGE_GRID:
        for run_cap in RUN_CAP_GRID:
            fa, ab = _rates(pairs, legacy_stats, coverage, run_cap, 10**9)
            points.append(
                SweepPoint("legacy", None, coverage, run_cap, 10**9, fa, ab)
            )
    return points


def per_category(
    pairs: list[CorpusPair], match_fn
) -> dict[str, dict[str, float]]:
    """Per-generator-category error rates at the production decision."""
    counts: dict[tuple[str, str], list[int]] = {}
    for p in pairs:
        m = match_fn(p.recorded, p.observed)
        verified = _decide(
            m,
            OPERATING_POINT["coverage_threshold"],
            OPERATING_POINT["uncovered_run_cap"],
            OPERATING_POINT["contradicted_chars_cap"],
        )
        wrong = verified if p.label == LABEL_DIFFERENT else not verified
        n, w = counts.setdefault((p.label, p.category), [0, 0])
        counts[(p.label, p.category)] = [n + 1, w + wrong]
    out: dict[str, dict[str, float]] = {LABEL_DIFFERENT: {}, LABEL_SAME: {}}
    for (label, category), (n, wrong) in sorted(counts.items()):
        out[label][category] = wrong / n
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


# -- outputs ------------------------------------------------------------------


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
        s=16, marker="o", color="#2c7fb8", alpha=0.45,
        label="new matcher (OCR-equivalence + contradiction budgets)",
    )
    front = pareto(current)
    ax.plot(
        [p.false_accept * 100 for p in front],
        [p.false_abort * 100 for p in front],
        color="#2c7fb8", linewidth=1.2, alpha=0.9, zorder=3,
    )
    op = next(
        (
            p
            for p in current
            if p.contradiction_sim == OPERATING_POINT["contradiction_sim"]
            and p.coverage_threshold == OPERATING_POINT["coverage_threshold"]
            and p.uncovered_run_cap == OPERATING_POINT["uncovered_run_cap"]
            and p.contradicted_chars_cap
            == OPERATING_POINT["contradicted_chars_cap"]
        ),
        None,
    )
    if op is not None:
        ax.scatter(
            [op.false_accept * 100], [op.false_abort * 100],
            s=180, marker="*", color="#1a9850", zorder=4,
            label=(
                "chosen operating point "
                f"(FA {op.false_accept:.2%}, FAbort {op.false_abort:.1%})"
            ),
        )
    ax.set_xlabel("false-accept rate, % (different entity VERIFIED — wrong-patient click)")
    ax.set_ylabel("false-abort rate, % (same entity refused — $0.10 fallback)")
    ax.set_title(
        "Identity band matcher on the frozen adversarial corpus "
        "(4360 pairs, seed 20260710)"
    )
    ax.set_xscale("symlog", linthresh=0.1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    plt.close(fig)


def _op_point(points: list[SweepPoint]) -> SweepPoint:
    return next(
        p
        for p in points
        if p.matcher == "current"
        and p.contradiction_sim == OPERATING_POINT["contradiction_sim"]
        and p.coverage_threshold == OPERATING_POINT["coverage_threshold"]
        and p.uncovered_run_cap == OPERATING_POINT["uncovered_run_cap"]
        and p.contradicted_chars_cap
        == OPERATING_POINT["contradicted_chars_cap"]
    )


def _find(points, matcher, sim, cov, cap, contra):
    return next(
        p
        for p in points
        if p.matcher == matcher
        and p.contradiction_sim == sim
        and p.coverage_threshold == cov
        and p.uncovered_run_cap == cap
        and p.contradicted_chars_cap == contra
    )


def render_markdown(
    points: list[SweepPoint],
    cat_current: dict,
    cat_legacy: dict,
    out_md: Path,
) -> None:
    """Render IDENTITY_ROC.md (tables + operating-point rationale) from
    the sweep data, so the committed doc regenerates from the numbers."""
    op = _op_point(points)
    sim = OPERATING_POINT["contradiction_sim"]
    legacy_prod = _find(points, "legacy", None, 0.80, 4, 10**9)
    no_contra = _find(points, "current", sim, 0.80, 4, 10**9)
    loose = _find(points, "current", sim, 0.70, 8, 0)
    loose_no_contra = _find(points, "current", sim, 0.70, 8, 10**9)

    lines = [
        "# Identity band matcher — held-out adversarial ROC",
        "",
        "Generated by `python -m openadapt_flow.validation.identity_roc` "
        "from the FROZEN corpus (4360 pairs, seed 20260710, hash manifest "
        "`adversary_corpus_manifest.json` committed before any evaluation "
        "or matcher change). Do not edit by hand.",
        "",
        "- **false accept** = a `different_entity` pair VERIFIED — a "
        "wrong-patient click, catastrophic in an EMR.",
        "- **false abort** = a `same_entity` pair refused — one hybrid "
        "fallback (~$0.10) or a human retry.",
        "",
        "![ROC](identity_roc.png)",
        "",
        "## Chosen operating point",
        "",
        f"`contradiction_sim={sim}`, "
        f"`coverage_threshold={OPERATING_POINT['coverage_threshold']}`, "
        f"`uncovered_run_cap={OPERATING_POINT['uncovered_run_cap']}`, "
        f"`contradicted_chars_cap="
        f"{OPERATING_POINT['contradicted_chars_cap']}` →",
        f"**false accept {op.false_accept:.3%}, false abort "
        f"{op.false_abort:.2%}** (legacy matcher at its production "
        f"thresholds: {legacy_prod.false_accept:.1%} / "
        f"{legacy_prod.false_abort:.1%}).",
        "",
        "**The weighting, out loud:** a false accept is a wrong-patient "
        "write on a real EMR — a clinical-safety event that downstream "
        "note verification does NOT catch (the note really is saved, in "
        "the wrong chart). A false abort costs one ~$0.10 hybrid-fallback "
        "escalation or a human retry. We price that asymmetry at four-plus "
        "orders of magnitude, so only zero-measured-false-accept points "
        "were considered at all, and among those we did **not** take the "
        "minimum-false-abort corner:",
        "",
        f"- Pareto-minimal on-corpus is `coverage 0.70 / run_cap 8 / "
        f"contra_cap 0` at FA {loose.false_accept:.2%} / FAbort "
        f"{loose.false_abort:.2%} — but its zero rests entirely on the "
        f"contradiction rule: disable contradiction there and FA is "
        f"**{loose_no_contra.false_accept:.1%}**.",
        f"- At the chosen `coverage 0.80 / run_cap 4` the coverage and "
        f"uncovered-run budgets independently catch most adversaries even "
        f"with contradiction disabled (FA {no_contra.false_accept:.1%} "
        f"vs {loose_no_contra.false_accept:.1%}) — defense in depth "
        f"against off-corpus siblings that evade the contradiction rule.",
        f"- The {op.false_abort - loose.false_abort:+.2%} extra false "
        "aborts this buys are concentrated in the `occlusion` category — "
        "bands whose leading/trailing tokens (usually the NAME) were not "
        "read at all. Verifying a row whose identity tokens are "
        "unreadable would be coverage-by-accident; refusing is the "
        "correct epistemic outcome, and it is the cheap direction.",
        f"- `contradiction_sim {sim}` (not 0.70/0.75): catches 3-char "
        "single-edit names (Ted/Tad, ratio 0.67) by near-miss in addition "
        "to the replacement rule — redundant on this corpus (identical "
        "rates at 0.75), kept for the same depth argument. "
        "`contradicted_chars_cap` must be 0: at cap 2 the Jr/Sr class "
        "re-enters (FA "
        f"{_find(points, 'current', sim, 0.8, 4, 2).false_accept:.1%}).",
        "",
        "## Error rates by generator category "
        "(at the production decision)",
        "",
        "| category | label | legacy matcher | new matcher |",
        "| --- | --- | --- | --- |",
    ]
    for label in (LABEL_DIFFERENT, LABEL_SAME):
        kind = (
            "false accept" if label == LABEL_DIFFERENT else "false abort"
        )
        for category in cat_current[label]:
            lines.append(
                f"| `{category}` | {kind} | "
                f"{cat_legacy[label][category]:.1%} | "
                f"{cat_current[label][category]:.1%} |"
            )
    lines += [
        "",
        "## Pareto frontier (new matcher)",
        "",
        "| contradiction_sim | coverage | run_cap | contra_cap | "
        "false accept | false abort |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in pareto([p for p in points if p.matcher == "current"]):
        contra = (
            "off" if p.contradicted_chars_cap >= 10**9
            else p.contradicted_chars_cap
        )
        lines.append(
            f"| {p.contradiction_sim} | {p.coverage_threshold} | "
            f"{p.uncovered_run_cap} | {contra} | "
            f"{p.false_accept:.3%} | {p.false_abort:.2%} |"
        )
    lines += [
        "",
        "Raw sweep data: `identity_roc.json`. The operating point is "
        "pinned by boundary tests in `tests/test_identity.py`; the four "
        "confirmed sibling probes (Phil/Philip both directions, "
        "John/Joan, Phil/Phillipa) are pinned as permanent mismatches "
        "there too.",
        "",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/validation"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    pairs = generate_corpus()
    points = sweep(pairs)
    front = pareto([p for p in points if p.matcher == "current"])
    cat_current = per_category(pairs, band_match)
    cat_legacy = per_category(pairs, legacy_band_match)
    render_markdown(
        points, cat_current, cat_legacy, args.out / "IDENTITY_ROC.md"
    )

    (args.out / "identity_roc.json").write_text(
        json.dumps(
            {
                "operating_point": OPERATING_POINT,
                "points": [p.as_dict() for p in points],
                "pareto_current": [p.as_dict() for p in front],
                "per_category_current": cat_current,
                "per_category_legacy_at_same_decision": cat_legacy,
            },
            indent=2,
        )
        + "\n"
    )
    render_chart(points, args.out / "identity_roc.png")
    print(f"wrote {args.out}/identity_roc.json and identity_roc.png")
    print("\nPareto frontier (current matcher):")
    for p in front:
        marker = (
            " <== OPERATING POINT"
            if (
                p.contradiction_sim == OPERATING_POINT["contradiction_sim"]
                and p.coverage_threshold
                == OPERATING_POINT["coverage_threshold"]
                and p.uncovered_run_cap == OPERATING_POINT["uncovered_run_cap"]
                and p.contradicted_chars_cap
                == OPERATING_POINT["contradicted_chars_cap"]
            )
            else ""
        )
        print(
            f"  sim={p.contradiction_sim} cov={p.coverage_threshold} "
            f"run_cap={p.uncovered_run_cap} "
            f"contra_cap={p.contradicted_chars_cap}: "
            f"FA={p.false_accept:.3%} FAbort={p.false_abort:.2%}{marker}"
        )


if __name__ == "__main__":
    main()
