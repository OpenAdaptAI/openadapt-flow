"""Program-level regression gate: reuse PR #70's per-step invariant, per skill.

PR #70 established the invariant a REPAIR must satisfy: it may change HOW a step
is performed (its locator / rung) but must NEVER silently weaken WHAT it means
(its identity band), how its effects are verified (effect coverage), or its risk
class. That gate (:class:`openadapt_flow.runtime.healing.RegressionGate`) rules
on a single heal patch. A learned program REVISION is the same risk at a larger
grain: a candidate :class:`~openadapt_flow.ir.ProgramGraph` may quietly drop an
armed step's identity band, a declared system-of-record effect, or downgrade an
irreversible step -- and still look like it "covers more traces".

So this module lifts the SAME gate from one patch to a whole program: for every
step id that survives from the active program into the candidate, it builds a
:class:`~openadapt_flow.runtime.healing.HealPatch` describing the step's
before/after anchor and runs the UNCHANGED
:meth:`RegressionGate.evaluate` (identity + effect + risk). A single surviving
step that regresses fails the whole gate -- the candidate is refused exactly as a
regressing heal is quarantined. New steps (no predecessor) carry nothing to
weaken and are not gated here; a removed step is reported (dropping an armed step
is a coverage decision the loop surfaces, not silently accepts).

Deterministic and ``$0``: the identity check reuses the same OCR band verifier
the pre-click gate uses; effects compare structurally. No model calls.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    Anchor,
    HealEvent,
    ProgramGraph,
    StateKind,
    Step,
)
from openadapt_flow.runtime.effects.effect import Effect
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    GateResult,
    RegressionGate,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch


def _action_steps(
    graph: ProgramGraph, subflows: Optional[dict[str, ProgramGraph]] = None
) -> dict[str, Step]:
    """Map step id -> Step across a program graph AND its subflows (a loop
    body's steps count too)."""
    graphs = [graph, *(subflows or {}).values()]
    steps: dict[str, Step] = {}
    for g in graphs:
        for state in g.states.values():
            if state.kind is StateKind.ACTION and state.step is not None:
                steps[state.step.id] = state.step
    return steps


def _effect_key(effect: Effect) -> str:
    """A canonical, order-independent key identifying an effect's CONTRACT.

    Two effects with the same key assert the same thing about the system of
    record; a candidate that no longer contains a baseline effect's key has
    dropped that coverage."""
    match = ",".join(f"{k}={v}" for k, v in sorted(effect.match.items()))
    return (
        f"{effect.kind.value}|match[{match}]|field={effect.field}"
        f"|value={effect.value}|count={effect.expected_count}"
    )


def _effect_baseline_and_now(
    old_step: Step, new_step: Step
) -> tuple[dict[str, bool], dict[str, "object"]]:
    """Build the (baseline, now) effect maps :meth:`RegressionGate.evaluate`
    consumes, so a dropped/altered confirmed effect trips effect-regression.

    Each of the OLD step's effects is treated as CONFIRMED in the baseline; the
    matching "now" re-check returns True iff the NEW step still declares an
    effect with the same contract key. Dropping or mutating an effect => the
    re-check fails => effect regression."""
    new_keys = {_effect_key(e) for e in new_step.effects}
    baseline: dict[str, bool] = {}
    now: dict[str, object] = {}
    for effect in old_step.effects:
        key = _effect_key(effect)
        baseline[key] = True
        now[key] = lambda k=key: k in new_keys
    return baseline, now


class StepGateVerdict(BaseModel):
    """The gate verdict for one surviving step."""

    step_id: str
    passed: bool
    result: GateResult


class ProgramGateReport(BaseModel):
    """Aggregate verdict of the program-level regression gate.

    ``passed`` iff every surviving step passed PR #70's per-step gate. ``removed``
    lists step ids present in the active program but absent from the candidate
    (surfaced for review -- dropping an armed step is a real change), of which
    ``armed_removed`` were identity-armed."""

    passed: bool
    per_step: list[StepGateVerdict] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    armed_removed: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        n = len(self.per_step)
        n_pass = sum(1 for v in self.per_step if v.passed)
        return (
            f"regression gate {n_pass}/{n} surviving steps pass; "
            f"{len(self.removed)} removed ({len(self.armed_removed)} armed); "
            f"passed={self.passed}"
        )


def _neutral_anchor() -> Anchor:
    """A minimal anchor for a step that declares none (keyboard/wait step): the
    gate's identity check treats it as unarmed, so it never blocks such a step."""
    return Anchor(template="", region=(0, 0, 0, 0), click_point=(0, 0))


def program_regression_gate(
    active: ProgramGraph,
    candidate: ProgramGraph,
    *,
    active_subflows: Optional[dict[str, ProgramGraph]] = None,
    candidate_subflows: Optional[dict[str, ProgramGraph]] = None,
    gate: Optional[RegressionGate] = None,
    band_verifier: BandVerifier = _default_band_verifier,
) -> ProgramGateReport:
    """Run PR #70's regression gate over every step surviving active->candidate.

    Returns a :class:`ProgramGateReport`; ``passed`` is False as soon as any
    surviving step would weaken its identity band, drop a confirmed effect, or
    downgrade its risk class -- the candidate is then refused by the loop exactly
    as a regressing heal patch is quarantined."""
    gate = gate or RegressionGate()
    active_steps = _action_steps(active, active_subflows)
    cand_steps = _action_steps(candidate, candidate_subflows)

    per_step: list[StepGateVerdict] = []
    failures: list[str] = []
    for step_id, old_step in active_steps.items():
        new_step = cand_steps.get(step_id)
        if new_step is None:
            continue  # removed -> handled below, not this per-step gate
        old_anchor = old_step.anchor or _neutral_anchor()
        new_anchor = new_step.anchor or _neutral_anchor()
        # Build the reviewable patch the gate rules on (same object the heal
        # path uses); rung label is cosmetic here (no live resolution happened).
        patch = HealPatch.from_event(
            HealEvent(
                step_id=step_id,
                rung_used="template",
                old_anchor=old_anchor,
                new_anchor=new_anchor,
            )
        )
        effect_baseline, effect_now = _effect_baseline_and_now(old_step, new_step)
        result = gate.evaluate(
            patch,
            old_anchor,
            new_anchor,
            old_step=old_step,
            new_step=new_step,
            band_verifier=band_verifier,
            effect_baseline=effect_baseline,
            effect_now=effect_now,  # type: ignore[arg-type]
        )
        per_step.append(
            StepGateVerdict(step_id=step_id, passed=result.passed, result=result)
        )
        if not result.passed:
            failures.append(f"step '{step_id}': " + "; ".join(result.failures))

    removed = [sid for sid in active_steps if sid not in cand_steps]
    armed_removed = [
        sid
        for sid in removed
        if (a := active_steps[sid].anchor) is not None
        and bool(a.context_text or a.structured_identity or a.identity_template)
    ]

    return ProgramGateReport(
        passed=not failures,
        per_step=per_step,
        removed=removed,
        armed_removed=armed_removed,
        failures=failures,
    )
