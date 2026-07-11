"""Compose the grounding rung with the identity gate and measure it.

This is the measurement instrument for ``docs/grounding_rung.md`` and the
OSS-model memo's headline experiment: adding an open GUI-grounding model as
the last rung of the resolution ladder should convert *false-aborts*
(safe-halts on a target that was actually present) into successes WITHOUT
letting a wrong target ever be clicked, because the deterministic identity
band gate still verifies the resolved point before any action.

    grounder PROPOSES a coordinate  ->  identity band DISPOSES (verify/halt)

The two halves are exercised with the REAL runtime code, not re-implemented:

- **Propose.** :func:`openadapt_flow.runtime.resolver.resolve` is driven with
  the template/OCR/geometry rungs forced to miss (the exact precondition
  under which the grounder rung fires today, i.e. a would-be safe-halt) and a
  grounder injected. The resolution comes back on the ``"grounder"`` rung.
- **Dispose.** :func:`openadapt_flow.runtime.identity.verify_target_identity`
  — the very function the replayer calls after resolution and before the
  click — judges the recorded target band against the live band that OCR
  would read at the proposed point.

The live band at the proposed point is supplied by the frozen adversary
corpora (``adversary_corpus*``): every pair is a ``(recorded, observed)``
band with a ground-truth label. ``same_entity`` pairs are noisy reads of the
TRUE row (a present target the deterministic ladder missed -> a recoverable
false-abort); ``different_entity`` pairs are a wrong entity sitting where the
target was (the data-drift danger class -> must never be clicked). So the
corpus lets us measure both quantities over thousands of adversarial cases
with the real gate in the loop and no browser/GPU required.

Run ``python -m openadapt_flow.validation.grounding_composition`` for the
report; ``--json PATH`` writes machine-readable results.
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openadapt_flow.ir import Anchor
from openadapt_flow.runtime.grounder import GrounderMatch
from openadapt_flow.runtime.identity import verify_target_identity
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.validation import (
    adversary_corpus,
    adversary_corpus_v2,
    adversary_corpus_v3,
)


# -- test doubles (also imported by tests/test_grounding_rung.py) ------------


class MissAllVision:
    """A vision namespace whose deterministic rungs all miss.

    Reproduces the ladder state that produces a safe-halt today: template
    (local + global), OCR, and geometry all fail, so resolution falls through
    to the grounder rung. ``find_text`` also backs the (unused here) landmark
    rung; returning None keeps geometry empty.
    """

    def find_template(self, *args, **kwargs):  # noqa: D102, ANN002, ANN003
        return None

    def find_text(self, *args, **kwargs):  # noqa: D102, ANN002, ANN003
        return None


class FaithfulMockGrounder:
    """A grounder stand-in for the composition measurement.

    A real GUI-Owl call needs a rendered screenshot of the specific row; the
    frozen corpora are band *text*, not pixels. This mock supplies the OTHER
    half of the contract the corpus can't: it always PROPOSES a candidate
    point (availability), modelling a confident grounder. Faithfulness to the
    adversarial setting comes from the corpus, not the mock: on a
    ``different_entity`` pair the observed band the identity gate reads at the
    proposed point IS the wrong entity's row — i.e. the mock is deliberately
    pointing at a plausible-but-wrong target, the worst case for safety. The
    identity gate must still reject it. On a ``same_entity`` pair the observed
    band is the true (noisy) row — the mock is pointing at the genuinely
    present target the deterministic ladder missed.

    This is the honest abstraction of the memo's "correct coords when present,
    plausible-but-wrong coords under drift": the grounder is trusted only to
    return *a* point; the band at that point decides safety.
    """

    def __init__(self, point: tuple[int, int] = (500, 300)) -> None:
        self.point = point
        self.calls: list[tuple[str, Optional[str]]] = []

    def locate(
        self, screen_png: bytes, intent: str, ocr_text: Optional[str] = None
    ) -> Optional[GrounderMatch]:
        self.calls.append((intent, ocr_text))
        x, y = self.point
        region = (max(0, x - 20), max(0, y - 10), 40, 20)
        return GrounderMatch(point=self.point, region=region, confidence=0.5)


def _blank_png(width: int = 1000, height: int = 600) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def _anchor_for(recorded_band: str, point: tuple[int, int]) -> Anchor:
    """An anchor whose only viable rung is the grounder.

    ``template=""`` (no crop passed) skips the two template rungs; a
    deliberately unfindable ``ocr_text`` (MissAllVision returns None anyway)
    and empty ``landmarks`` skip the OCR and geometry rungs; ``context_text``
    carries the recorded identity band the gate verifies against.
    """
    x, y = point
    return Anchor(
        template="templates/target.png",
        region=(x - 80, y - 32, 160, 64),
        click_point=(x, y),
        ocr_text="Open",
        context_text=recorded_band,
        landmarks=[],
    )


# -- corpus loading -----------------------------------------------------------


@dataclass(frozen=True)
class Case:
    recorded: str
    observed: str
    label: str
    category: str
    source: str


def load_all_cases() -> list[Case]:
    """The union of the frozen adversary corpora, tagged by source."""
    cases: list[Case] = []
    for p in adversary_corpus.generate_corpus():
        cases.append(Case(p.recorded, p.observed, p.label, p.category, "v1"))
    for p in adversary_corpus_v2.generate_corpus_v2():
        cases.append(Case(p.recorded, p.observed, p.label, p.category, "v2"))
    for p in adversary_corpus_v3.generate_corpus_v3():
        cases.append(Case(p.recorded, p.observed, p.label, p.category, "v3"))
    return cases


# -- the composition run ------------------------------------------------------


@dataclass
class CompositionResult:
    total_cases: int = 0
    grounder_fired: int = 0  # resolution came back on the grounder rung
    # same_entity (present target the deterministic ladder missed):
    present_total: int = 0
    present_recovered: int = 0  # verified -> false-abort converted to success
    present_still_halt: int = 0  # residual safe-halt ($-cost, not a safety hit)
    # different_entity (wrong entity at the target position):
    wrong_total: int = 0
    wrong_false_accepts: int = 0  # verified -> WRONG CLICK. MUST be 0.
    wrong_safe_halts: int = 0
    # v2 'indistinguishable' (ambiguous by construction; reported, not graded):
    ambiguous_total: int = 0
    ambiguous_verified: int = 0
    per_category: dict = field(default_factory=dict)

    @property
    def false_abort_reduction(self) -> float:
        if self.present_total == 0:
            return 0.0
        return self.present_recovered / self.present_total

    @property
    def false_accept_rate(self) -> float:
        if self.wrong_total == 0:
            return 0.0
        return self.wrong_false_accepts / self.wrong_total


def run_composition(
    cases: Optional[list[Case]] = None, grounder=None
) -> CompositionResult:
    """Drive propose+dispose over the corpora and tally the outcomes.

    Args:
        cases: Corpus cases (defaults to :func:`load_all_cases`).
        grounder: The grounder to inject (defaults to
            :class:`FaithfulMockGrounder`). Any object with a compatible
            ``locate`` works — pass a real :class:`GuiOwlGrounder` to measure
            a served model instead.

    Returns:
        A :class:`CompositionResult`.
    """
    cases = cases if cases is not None else load_all_cases()
    grounder = grounder if grounder is not None else FaithfulMockGrounder()
    point = getattr(grounder, "point", (500, 300))
    png = _blank_png()
    viewport = (1000, 600)
    res = CompositionResult(total_cases=len(cases))

    for case in cases:
        anchor = _anchor_for(case.recorded, point)
        # PROPOSE: real ladder, all deterministic rungs miss -> grounder rung.
        resolved = resolve(
            anchor,
            png,
            MissAllVision(),
            grounder,
            intent="click the target row's action button",
            template_png=None,
            viewport=viewport,
        )
        assert resolved is not None, "grounder rung must produce a resolution"
        resolution, _region = resolved
        assert resolution.rung == "grounder", resolution.rung
        res.grounder_fired += 1

        # DISPOSE: the exact identity check the replayer runs pre-click.
        check = verify_target_identity(case.recorded, case.observed)
        verified = check.status == "verified"

        cat = res.per_category.setdefault(
            f"{case.source}:{case.category}",
            {"label": case.label, "n": 0, "verified": 0},
        )
        cat["n"] += 1
        cat["verified"] += int(verified)

        if case.label == adversary_corpus.LABEL_SAME:
            res.present_total += 1
            if verified:
                res.present_recovered += 1
            else:
                res.present_still_halt += 1
        elif case.label == adversary_corpus.LABEL_DIFFERENT:
            res.wrong_total += 1
            if verified:
                res.wrong_false_accepts += 1
            else:
                res.wrong_safe_halts += 1
        else:  # 'indistinguishable' (v2)
            res.ambiguous_total += 1
            res.ambiguous_verified += int(verified)

    return res


# -- reporting ----------------------------------------------------------------


def format_report(res: CompositionResult, baseline: bool = True) -> str:
    lines: list[str] = []
    lines.append("Grounding rung x identity gate — composition measurement")
    lines.append("=" * 60)
    lines.append(f"corpus cases (v1+v2+v3):        {res.total_cases}")
    lines.append(f"grounder rung fired:            {res.grounder_fired}")
    lines.append("")
    if baseline:
        lines.append("BASELINE (NullGrounder): every ladder-exhausted case")
        lines.append("safe-halts. Present-target halts are false-aborts;")
        lines.append(f"  false-aborts (present targets): {res.present_total}")
        lines.append("  false-accepts:                  0 (nothing clicked)")
        lines.append("")
    lines.append("WITH GROUNDER (grounder proposes, identity disposes):")
    lines.append("  Availability (false-abort side)")
    lines.append(f"    present targets:              {res.present_total}")
    lines.append(
        f"    recovered (halt -> success):  {res.present_recovered}"
        f"  ({res.false_abort_reduction * 100:.1f}% of present)"
    )
    lines.append(
        f"    residual safe-halt ($ only):  {res.present_still_halt}"
    )
    lines.append("  Safety (false-accept side) — MUST stay 0")
    lines.append(f"    wrong-entity cases:           {res.wrong_total}")
    lines.append(
        f"    false-accepts (wrong click):  {res.wrong_false_accepts}"
        f"  ({res.false_accept_rate * 100:.3f}%)"
    )
    lines.append(f"    correctly safe-halted:        {res.wrong_safe_halts}")
    if res.ambiguous_total:
        lines.append("  Ambiguous (v2 'indistinguishable', not graded)")
        lines.append(
            f"    cases / verified:             {res.ambiguous_total}"
            f" / {res.ambiguous_verified}"
        )
    lines.append("")
    verdict = (
        "PASS — availability up, false-accept 0.000%"
        if res.wrong_false_accepts == 0 and res.present_recovered > 0
        else "FAIL — see false-accepts above"
    )
    lines.append(f"VERDICT: {verdict}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", type=Path, default=None, help="Write results JSON here."
    )
    args = parser.parse_args()
    res = run_composition()
    print(format_report(res))
    if args.json:
        payload = {
            "total_cases": res.total_cases,
            "grounder_fired": res.grounder_fired,
            "present_total": res.present_total,
            "present_recovered": res.present_recovered,
            "false_abort_reduction": res.false_abort_reduction,
            "wrong_total": res.wrong_total,
            "wrong_false_accepts": res.wrong_false_accepts,
            "false_accept_rate": res.false_accept_rate,
            "ambiguous_total": res.ambiguous_total,
            "ambiguous_verified": res.ambiguous_verified,
            "per_category": res.per_category,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
