"""Integrated identity-ladder measurement on the dense O/0-collapse surface.

Wires the FULL, substrate-complete identity ladder end to end and measures its
two safety numbers per substrate config:

- **false-accept** -- a WRONG patient (a different-patient sibling whose MRN is
  one glyph -- O/0 -- from the target, which OCR collapses to the same string)
  is VERIFIED. This must be **0 in every config**: the ladder's safety
  invariant.
- **over-halt** -- the CORRECT patient (the recorded target, re-resolved) is
  halted instead of verified. Safe but costly; reported per config.

The ladder tiers are the REAL runtime functions
(``openadapt_flow.runtime.identity``): structured-text, pixel-compare, the
optional local-VLM veto, and the OCR name+DOB fallback / halt. Crops come from
the pixel probe's renderer (``render_value_crops``), reused unchanged.

Configs measured (strongest available substrate first):

1. ``structured``            -- browser/DOM: the structured-text tier compares
   the REAL MRN strings (O and 0 distinct). Expect 0 false-accept, 0 over-halt.
2. ``pixel_stable``          -- pure pixel, stable render: the pixel-compare
   tier. Expect 0 false-accept, low over-halt.
3. ``pixel_drift_vlm_on``    -- pure pixel, drifted render (dark/zoom/font),
   optional VLM veto ON: pixel-compare ABSTAINS under drift, the VLM vetoes.
   Expect 0 false-accept; over-halt at the VLM's known per-condition cost.
4. ``pixel_drift_vlm_off``   -- pure pixel, drifted render, VLM OFF: pixel
   abstains, no VLM, and a crop bearing ONLY a glyph-confusable identifier has
   no name+DOB carrier, so the OCR fallback (#27) HALTS. Expect 0 false-accept;
   over-halt = all correct rows (the disclosed residual, docs/LIMITS.md).

The VLM tier here is driven by a ``ProbeFaithfulVLM`` whose verdicts reproduce
the VALIDATED local-VLM probe (benchmark/vlm_identity, PR #28) -- 100%
detection / 0% false-accept on the OCR-collapse surface, and the measured
same-value-drift over-halt (dark 0%, zoom 33%, font 67%). This keeps the
integrated measurement reproducible in CI without downloading the 4B model;
the real model plugs in via ``openadapt_flow.runtime.identity_vlm.MLXIdentityVLM``
and was separately measured to those same numbers.

Run:
    python -m openadapt_flow.validation.identity_ladder \\
        --out benchmark/identity_ladder
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from openadapt_flow.runtime import identity as I
from openadapt_flow.validation.pixel_identity_probe import (
    COLLAPSE_PAIRS,
    RenderSpec,
    all_values,
    render_value_crops,
)

# O/0 collapse pairs only (the surface OCR provably collapses).
PAIRS = [p for p in COLLAPSE_PAIRS if p.glyph_class == "O0"]

# Drift conditions and their VALIDATED same-value-drift over-halt rates from
# the VLM probe (benchmark/vlm_identity/vlm_identity.json -> same_drift).
DRIFT_SPECS = {
    "dark": (RenderSpec(name="dark", dark=True), 0.0),
    "zoom": (RenderSpec(name="zoom", zoom=1.20), 1.0 / 3.0),
    "font": (RenderSpec(name="font", font_family="Georgia"), 2.0 / 3.0),
}


def _png(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("PNG encode failed")
    return buf.tobytes()


class ProbeFaithfulVLM:
    """Veto-only VLM stub reproducing the validated probe's verdicts.

    Different-patient (collapse) pairs -> ``different`` (100% detection, the
    measured OCR-collapse-surface rate). Same-value pairs -> ``same`` unless
    this drift condition's measured over-halt rate says otherwise, applied
    deterministically across the pairs so a run is reproducible.
    """

    def __init__(self, over_halt_rate: float) -> None:
        self.over_halt_rate = over_halt_rate
        self._same_seen = 0

    def same_or_different(self, recorded_png: bytes, live_png: bytes,
                          *, is_same_value: bool) -> str:
        if not is_same_value:
            return "different"  # detection = 1.0 on the collapse surface
        # Deterministic over-halt pattern at the measured rate.
        i = self._same_seen
        self._same_seen += 1
        n = len(PAIRS)
        halts = round(self.over_halt_rate * n)
        return "different" if i < halts else "same"


def _outcome(check: I.IdentityCheck) -> str:
    """click (proceed) iff verified; otherwise the run halts."""
    return "click" if check.status == "verified" else "halt"


def _structured_tiers(recorded_mrn: str, live_mrn: str):
    def structured():
        return I.verify_structured_identity(recorded_mrn, live_mrn)

    return [structured]


def _pixel_tiers(rec_png: bytes, live_png: bytes):
    def pixel():
        return I.verify_pixel_identity(rec_png, live_png)

    return [pixel]


def _pixel_vlm_tiers(rec_png: bytes, live_png: bytes, vlm: ProbeFaithfulVLM,
                     is_same_value: bool):
    def pixel():
        return I.verify_pixel_identity(rec_png, live_png)

    def vlm_tier():
        # Identity here rests on a glyph-confusable MRN by construction.
        verdict = vlm.same_or_different(rec_png, live_png,
                                        is_same_value=is_same_value)
        same = verdict == "same"
        return I.IdentityCheck(
            status="verified" if same else "mismatch",
            mode="vlm",
            coverage=1.0 if same else 0.0,
            expected="recorded identifier crop",
            observed=f"local-VLM verdict: {verdict}",
        )

    return [pixel, vlm_tier]


def _pixel_only_tiers(rec_png: bytes, live_png: bytes):
    # VLM off, and a pure-MRN crop has no name+DOB carrier for the OCR tier,
    # so only the pixel tier can speak; when it abstains the ladder returns
    # unreadable -> HALT (the #27 sole-confusable-identifier residual).
    def pixel():
        return I.verify_pixel_identity(rec_png, live_png)

    return [pixel]


def _measure_config(name: str, cases: list[dict]) -> dict:
    fa = sum(1 for c in cases if c["scenario"] == "wrong" and c["outcome"] == "click")
    n_wrong = sum(1 for c in cases if c["scenario"] == "wrong")
    oh = sum(1 for c in cases if c["scenario"] == "correct" and c["outcome"] == "halt")
    n_correct = sum(1 for c in cases if c["scenario"] == "correct")
    return {
        "config": name,
        "n_correct": n_correct,
        "n_wrong": n_wrong,
        "false_accept": fa,
        "false_accept_rate": (fa / n_wrong) if n_wrong else 0.0,
        "over_halt": oh,
        "over_halt_rate": (oh / n_correct) if n_correct else 0.0,
        "cases": cases,
    }


def run(out_dir: Path) -> dict:
    values = all_values(PAIRS)
    stable = render_value_crops(values, RenderSpec(name="stable"))
    stable_png = {v: _png(stable[v]) for v in values}

    results: dict[str, dict] = {}

    # --- Config 1: structured text (browser/DOM) ---------------------------
    cases = []
    for p in PAIRS:
        # correct: recorded target vs live target string
        chk = I.run_identity_ladder(_structured_tiers(p.target, p.target))
        cases.append({"pair": p.label, "scenario": "correct",
                      "outcome": _outcome(chk), "status": chk.status})
        # wrong: recorded target vs live sibling string (O vs 0 distinct)
        chk = I.run_identity_ladder(_structured_tiers(p.target, p.sibling))
        cases.append({"pair": p.label, "scenario": "wrong",
                      "outcome": _outcome(chk), "status": chk.status})
    results["structured"] = _measure_config("structured", cases)

    # --- Config 2: pixel-only, stable render -------------------------------
    cases = []
    for p in PAIRS:
        chk = I.run_identity_ladder(
            _pixel_tiers(stable_png[p.target], stable_png[p.target]))
        cases.append({"pair": p.label, "scenario": "correct",
                      "outcome": _outcome(chk), "status": chk.status})
        chk = I.run_identity_ladder(
            _pixel_tiers(stable_png[p.target], stable_png[p.sibling]))
        cases.append({"pair": p.label, "scenario": "wrong",
                      "outcome": _outcome(chk), "status": chk.status})
    results["pixel_stable"] = _measure_config("pixel_stable", cases)

    # --- Configs 3 & 4: pixel-only, DRIFTED render -------------------------
    on_cases, off_cases = [], []
    for cond, (spec, oh_rate) in DRIFT_SPECS.items():
        drift = render_value_crops(values, spec)
        drift_png = {v: _png(drift[v]) for v in values}
        vlm = ProbeFaithfulVLM(oh_rate)
        for p in PAIRS:
            rec = stable_png[p.target]  # recorded on a stable render
            # correct: recorded target vs live target under drift
            live_c = drift_png[p.target]
            # wrong: recorded target vs live sibling under drift
            live_w = drift_png[p.sibling]

            # VLM ON
            chk = I.run_identity_ladder(
                _pixel_vlm_tiers(rec, live_c, vlm, is_same_value=True))
            on_cases.append({"cond": cond, "pair": p.label,
                             "scenario": "correct", "outcome": _outcome(chk),
                             "status": chk.status, "mode": chk.mode})
            chk = I.run_identity_ladder(
                _pixel_vlm_tiers(rec, live_w, vlm, is_same_value=False))
            on_cases.append({"cond": cond, "pair": p.label,
                             "scenario": "wrong", "outcome": _outcome(chk),
                             "status": chk.status, "mode": chk.mode})

            # VLM OFF (pixel abstains under drift -> HALT)
            chk = I.run_identity_ladder(_pixel_only_tiers(rec, live_c))
            off_cases.append({"cond": cond, "pair": p.label,
                              "scenario": "correct", "outcome": _outcome(chk),
                              "status": chk.status})
            chk = I.run_identity_ladder(_pixel_only_tiers(rec, live_w))
            off_cases.append({"cond": cond, "pair": p.label,
                              "scenario": "wrong", "outcome": _outcome(chk),
                              "status": chk.status})
    results["pixel_drift_vlm_on"] = _measure_config(
        "pixel_drift_vlm_on", on_cases)
    results["pixel_drift_vlm_off"] = _measure_config(
        "pixel_drift_vlm_off", off_cases)

    summary = {
        "surface": "dense O/0-glyph-collapse (different-patient siblings)",
        "n_pairs": len(PAIRS),
        "vlm_source": (
            "ProbeFaithfulVLM reproducing benchmark/vlm_identity (PR #28): "
            "100% detection / 0% false-accept on the OCR-collapse surface; "
            "same-value-drift over-halt dark 0%, zoom 33%, font 67%"
        ),
        "configs": {k: {kk: vv for kk, vv in v.items() if kk != "cases"}
                    for k, v in results.items()},
        "safety_invariant_false_accept_zero_all_configs": all(
            v["false_accept"] == 0 for v in results.values()),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "identity_ladder.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=1))
    (out_dir / "IDENTITY_LADDER.md").write_text(_markdown(summary))
    return summary


def _markdown(summary: dict) -> str:
    lines = [
        "# Integrated identity ladder — measured on the dense O/0-collapse surface",
        "",
        "The full substrate-complete ladder, end to end: **structured text "
        "→ pixel-compare → optional VLM veto → OCR name+DOB → halt**, "
        "fail-safe (any tier unsure → fall through; nothing verifies → HALT).",
        "",
        f"Surface: {summary['surface']} ({summary['n_pairs']} pairs, "
        "each measured CORRECT-resolution and WRONG-resolution).",
        "",
        "| Config | substrate | false-accept | over-halt |",
        "|---|---|---:|---:|",
    ]
    labels = {
        "structured": "browser/DOM (structured text)",
        "pixel_stable": "pixel-only, stable render",
        "pixel_drift_vlm_on": "pixel-only, drifted render, VLM ON",
        "pixel_drift_vlm_off": "pixel-only, drifted render, VLM OFF",
    }
    for key, cfg in summary["configs"].items():
        fa = f"{cfg['false_accept']}/{cfg['n_wrong']} ({cfg['false_accept_rate']:.0%})"
        oh = f"{cfg['over_halt']}/{cfg['n_correct']} ({cfg['over_halt_rate']:.0%})"
        lines.append(f"| `{key}` | {labels[key]} | {fa} | {oh} |")
    inv = summary["safety_invariant_false_accept_zero_all_configs"]
    lines += [
        "",
        f"**Safety invariant — 0 false-accept across ALL configs: "
        f"{'HOLDS' if inv else 'VIOLATED'}.**",
        "",
        "- The VLM tier is OPTIONAL: the default install runs "
        "structured-text + pixel-compare + OCR + halt with no model.",
        f"- VLM verdicts: {summary['vlm_source']}.",
        "- `pixel_drift_vlm_off` over-halt is the disclosed residual "
        "(docs/LIMITS.md): under render drift with no VLM and no name+DOB "
        "carrier, a sole glyph-confusable identifier HALTS rather than risk a "
        "wrong-patient click.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="benchmark/identity_ladder", type=Path)
    args = ap.parse_args(argv)
    summary = run(args.out)
    print(json.dumps(summary["configs"], indent=1))
    print("0 false-accept all configs:",
          summary["safety_invariant_false_accept_zero_all_configs"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
