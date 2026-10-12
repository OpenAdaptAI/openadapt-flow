"""Does the record check cover every value the item typed or selected?

Under "Only ask a person about problems", a save is allowed without a person
only when the deployment's system-of-record read-back will confirm the values
the item entered. This module answers that statically, from the workflow:

- every typing or selection step before a save must take its value from a
  declared input (``Step.param``). A literal typed from the recording, or a
  secret, is never covered;
- every such input must be bound in that save's effects, as a ``field_equals``
  value or a ``match`` / ``exact_new_set`` selector read from the system of
  record. An idempotency key alone doesn't cover an input;
- every executable actuation path of the save declares at least one effect,
  and no effect is an unbound placeholder or an on-screen read-back.

Program-graph workflows aren't covered in phase 1: their order of steps isn't
linear, so every save in them fails coverage and goes to a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow
    from openadapt_flow.runtime.effects.effect import Effect


@dataclass(frozen=True)
class SaveCoverage:
    """Coverage of one save. PHI-free: step ids and input names only."""

    step_id: str
    #: Closed reason codes; empty when the save is covered.
    problems: tuple[str, ...]
    #: Declared inputs entered before this save that its effects don't bind.
    uncovered_inputs: tuple[str, ...] = ()

    @property
    def covered(self) -> bool:
        return not self.problems


def _effect_read_params(effect: "Effect") -> set[str]:
    """Inputs an effect reads back from the system of record."""

    names: set[str] = set()
    for expr in effect.match.values():
        if expr.param is not None:
            names.add(expr.param)
    for selector in effect.new_records:
        for expr in selector.values():
            if expr.param is not None:
                names.add(expr.param)
    if effect.field is not None and effect.value is not None:
        if effect.value.param is not None:
            names.add(effect.value.param)
    return names


def _entry_steps(step: "Step") -> bool:
    from openadapt_flow.ir import ActionKind

    return step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION)


def save_coverage(
    workflow: "Workflow",
    save: "Step",
    *,
    is_save: Optional[Callable[["Step"], bool]] = None,
) -> SaveCoverage:
    """Return the coverage of one save step in a linear workflow.

    ``is_save`` decides which earlier steps are saves (the window of entries
    for this save starts after the previous save). It defaults to the run
    gate's fail-closed consequential-step classifier.
    """

    from openadapt_flow.policy import iter_effect_paths

    problems: list[str] = []
    if workflow.program is not None:
        return SaveCoverage(step_id=save.id, problems=("program_graph",))
    if is_save is None:
        from openadapt_flow.run_gate import is_consequential

        def is_save(candidate: "Step") -> bool:
            return is_consequential(candidate, workflow)

    try:
        index = next(i for i, s in enumerate(workflow.steps) if s.id == save.id)
    except StopIteration:
        return SaveCoverage(step_id=save.id, problems=("save_not_in_workflow",))

    paths = list(iter_effect_paths(save))
    if not paths or any(not effects for _path, effects in paths):
        problems.append("no_record_check")
    path_reads: list[set[str]] = []
    for _path, effects in paths:
        reads: set[str] = set()
        for effect in effects:
            if effect.needs_operator_confirmation:
                problems.append("placeholder_effect")
            if effect.readback is not None:
                problems.append("onscreen_readback")
            reads |= _effect_read_params(effect)
        path_reads.append(reads)

    entered: list[str] = []
    window_start = 0
    for earlier in range(index - 1, -1, -1):
        if is_save(workflow.steps[earlier]):
            window_start = earlier + 1
            break
    for step in [*workflow.steps[window_start:index], save]:
        if not _entry_steps(step):
            continue
        if step.secret:
            problems.append("secret_entry")
            continue
        if step.param is None:
            problems.append("recorded_value_entry")
            continue
        entered.append(step.param)

    uncovered = sorted(
        {name for name in entered for reads in path_reads if name not in reads}
    )
    if uncovered:
        problems.append("input_not_read_back")
    # Stable, de-duplicated reason codes.
    seen: list[str] = []
    for problem in problems:
        if problem not in seen:
            seen.append(problem)
    return SaveCoverage(
        step_id=save.id, problems=tuple(seen), uncovered_inputs=tuple(uncovered)
    )


def workflow_coverage(workflow: "Workflow") -> list[SaveCoverage]:
    """Coverage of every save in a workflow, in step order."""

    from openadapt_flow.run_gate import is_consequential

    if workflow.program is not None:
        return [SaveCoverage(step_id="<program>", problems=("program_graph",))]
    return [
        save_coverage(workflow, step)
        for step in workflow.steps
        if is_consequential(step, workflow)
    ]
