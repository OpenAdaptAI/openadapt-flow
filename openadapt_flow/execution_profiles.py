"""Named execution profiles over OpenAdapt's existing governed runtime.

Profiles do not implement a second policy or replay path.  They select which
requirements the existing run gate must enforce, whether the shared replayer
must be durable, and how the resulting report may be described.

The low-level controls remain available for embedding and backwards
compatibility.  Production callers can choose one reviewed profile instead of
assembling a potentially contradictory collection of permissive flags.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Mapping

from openadapt_flow.decision_delivery import DecisionDeliveryTier
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Workflow


class ExecutionProfile(str, Enum):
    """The supported runtime postures."""

    DEMO = "demo"
    STANDARD = "standard"
    REGULATED = "regulated"


class ExecutionOutcome(str, Enum):
    """Precise result of applying a profile's evidence contract."""

    VERIFIED = "VERIFIED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    HALTED = "HALTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


AUTOMATED_GUI_ACTUATIONS = frozenset(
    {"uia", "dom", "guarded_coordinate", "guarded_keyboard"}
)
HUMAN_ATTENDED_ACTUATIONS = frozenset({"human_attended", "human_attended_skip"})


@dataclass(frozen=True)
class ExecutionProfileContract:
    """Requirements a named profile applies to the existing runtime."""

    profile: ExecutionProfile
    production: bool
    require_certification: bool
    require_identity_coverage: bool
    require_effect_contracts: bool
    minimum_effect_tier: VerificationTier | None
    require_approval_for_unverified_effects: bool
    allow_unverified_write_approval: bool
    require_encryption: bool
    strict_templates: bool
    require_durable: bool
    require_settled: bool
    default_policy: str | None
    #: The most context this profile permits a REMOTE attended-decision surface
    #: to carry. Local delivery is unaffected: the loopback console and the
    #: runner-local portal always serve
    #: :attr:`~openadapt_flow.decision_delivery.DecisionDeliveryTier.LOCAL_FULL`.
    #: Every profile currently permits ``REMOTE_CLOSED_CONTEXT`` because that
    #: tier adds only closed enums, bounded integers, and booleans — it widens
    #: what a remote operator KNOWS without widening what the envelope can
    #: REPRESENT. A future scrubbed tier would not have that property, and this
    #: field is where ``regulated`` refuses it.
    max_remote_decision_tier: DecisionDeliveryTier


_CONTRACTS = {
    ExecutionProfile.DEMO: ExecutionProfileContract(
        profile=ExecutionProfile.DEMO,
        production=False,
        require_certification=False,
        require_identity_coverage=False,
        require_effect_contracts=False,
        minimum_effect_tier=None,
        require_approval_for_unverified_effects=True,
        allow_unverified_write_approval=True,
        require_encryption=False,
        strict_templates=False,
        require_durable=False,
        require_settled=False,
        default_policy=None,
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
    ExecutionProfile.STANDARD: ExecutionProfileContract(
        profile=ExecutionProfile.STANDARD,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=False,
        strict_templates=False,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
    ExecutionProfile.REGULATED: ExecutionProfileContract(
        profile=ExecutionProfile.REGULATED,
        production=True,
        require_certification=True,
        require_identity_coverage=True,
        require_effect_contracts=True,
        minimum_effect_tier=VerificationTier.PERSISTED_STATE_REACQUISITION,
        require_approval_for_unverified_effects=False,
        allow_unverified_write_approval=False,
        require_encryption=True,
        strict_templates=True,
        require_durable=True,
        require_settled=True,
        default_policy="clinical-write",
        max_remote_decision_tier=DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT,
    ),
}


def resolve_execution_profile(
    value: ExecutionProfile | str | None,
    *,
    default: ExecutionProfile = ExecutionProfile.REGULATED,
) -> ExecutionProfile:
    """Resolve a profile name or fail loudly on an unknown value."""

    if value is None:
        return default
    if isinstance(value, ExecutionProfile):
        return value
    try:
        return ExecutionProfile(str(value).strip().lower())
    except ValueError as exc:
        choices = ", ".join(profile.value for profile in ExecutionProfile)
        raise ValueError(
            f"unknown execution profile {value!r}; expected one of: {choices}"
        ) from exc


def execution_profile_contract(
    value: ExecutionProfile | str,
) -> ExecutionProfileContract:
    """Return the immutable contract for ``value``."""

    return _CONTRACTS[resolve_execution_profile(value)]


def required_effect_tier(
    workflow: Workflow,
    profile: ExecutionProfile | str,
) -> VerificationTier | None:
    """Return the strongest profile/project minimum for this workflow."""

    contract = execution_profile_contract(profile)
    required = contract.minimum_effect_tier
    if not contract.production:
        return required
    project_minimum = getattr(workflow.qualification, "minimum_effect_tier", None)
    if project_minimum is not None:
        candidate = VerificationTier(project_minimum)
        if required is None or int(candidate) < int(required):
            required = candidate
    return required


@dataclass(frozen=True)
class _ProgramActionOccurrence:
    """One structurally proven action occurrence in a program trace."""

    state_id: str
    step: Any
    program_scope: tuple[Any, ...]
    exception_edge: bool = False


def _program_action_trace(
    workflow: Workflow,
    visited_states: list[str],
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
) -> list[_ProgramActionOccurrence] | None:
    """Validate one complete graph walk and return its action occurrences.

    This proves entry, declared transitions, nested subflow returns, exact loop
    iteration counts, and a successful top-level terminal or declared fall-off.
    State and step identifiers must be globally unique so no weaker action
    contract can replace another one during report classification.
    """

    if not visited_states or workflow.program is None:
        return None
    from openadapt_flow.ir import ProgramExecutionScopeFrame, StateKind

    graphs = {"__program__": workflow.program, **workflow.subflows}
    state_owner: dict[str, str] = {}
    action_step_ids: set[str] = set()
    for graph_id, graph in graphs.items():
        for key, state in graph.states.items():
            if key != state.id or state.id in state_owner:
                return None
            state_owner[state.id] = graph_id
            if state.kind is StateKind.ACTION:
                if state.step is None or state.step.id in action_step_ids:
                    return None
                action_step_ids.add(state.step.id)

    cursor = 0
    actions: list[_ProgramActionOccurrence] = []

    def _rows(relation: str) -> list[dict[str, str]] | None:
        if runtime_worklists is not None and relation in runtime_worklists:
            return list(runtime_worklists[relation])
        declared = workflow.data_sources.get(relation)
        return None if declared is None else list(declared.rows)

    def _next_state(state: Any, graph: Any, occurrence_index: int | None) -> str | None:
        nonlocal cursor
        if not state.transitions and state.on_exception is None:
            return None
        if cursor >= len(visited_states):
            raise ValueError("program trace ended before a declared successor")
        candidate = visited_states[cursor]
        normal_targets = {transition.target for transition in state.transitions}
        if candidate in normal_targets:
            return candidate
        if state.on_exception is not None and candidate == state.on_exception:
            if occurrence_index is not None:
                previous = actions[occurrence_index]
                actions[occurrence_index] = _ProgramActionOccurrence(
                    state_id=previous.state_id,
                    step=previous.step,
                    program_scope=previous.program_scope,
                    exception_edge=True,
                )
            return candidate
        raise ValueError("program trace crossed an undeclared transition")

    def _consume_graph(
        graph_id: str,
        scope: tuple[Any, ...],
        *,
        depth: int,
    ) -> None:
        nonlocal cursor
        if depth > 64:
            raise ValueError("program trace nesting is not bounded")
        graph = graphs.get(graph_id)
        if graph is None:
            raise ValueError("program trace references an unknown graph")
        state_id: str | None = graph.entry
        while state_id is not None:
            if cursor >= len(visited_states) or visited_states[cursor] != state_id:
                raise ValueError("program trace does not follow the graph entry")
            state = graph.states.get(state_id)
            if state is None or state_owner.get(state_id) != graph_id:
                raise ValueError("program trace references an unknown state")
            cursor += 1
            occurrence_index: int | None = None
            if state.kind is StateKind.ACTION:
                if state.step is None:
                    raise ValueError("action state has no step")
                occurrence_index = len(actions)
                actions.append(
                    _ProgramActionOccurrence(
                        state_id=state.id,
                        step=state.step,
                        program_scope=scope,
                    )
                )
            if state.kind is StateKind.TERMINAL:
                if (state.outcome or "success") != "success":
                    raise ValueError("successful trace reached a non-success terminal")
                return
            if state.kind is StateKind.SUBFLOW_CALL:
                if state.subflow is None or state.subflow not in graphs:
                    raise ValueError("subflow call target is missing")
                _consume_graph(
                    state.subflow,
                    (*scope, ProgramExecutionScopeFrame(graph_id=state.subflow)),
                    depth=depth + 1,
                )
            elif state.kind is StateKind.LOOP:
                if state.loop is None or state.loop.body not in graphs:
                    raise ValueError("loop body is missing")
                rows = _rows(state.loop.relation)
                if rows is None or len(rows) > state.loop.max_iterations:
                    raise ValueError(
                        "loop worklist is unavailable or outside its bound"
                    )
                for row_index in range(len(rows)):
                    _consume_graph(
                        state.loop.body,
                        (
                            *scope,
                            ProgramExecutionScopeFrame(
                                graph_id=state.loop.body,
                                loop_state_id=state.id,
                                relation=state.loop.relation,
                                row_index=row_index,
                            ),
                        ),
                        depth=depth + 1,
                    )
            state_id = _next_state(state, graph, occurrence_index)

    try:
        _consume_graph(
            "__program__",
            (ProgramExecutionScopeFrame(graph_id="__program__"),),
            depth=0,
        )
    except ValueError:
        return None
    if cursor != len(visited_states):
        return None
    return actions


def classify_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
) -> ExecutionOutcome:
    """Classify a completed report without changing legacy ``success``.

    Demo success is always visibly non-production.  Standard and Regulated
    success becomes ``VERIFIED`` only when every executed consequential action
    has a confirmed effect at or above the workflow's required evidence tier.
    Therefore an approved-unverified or immediate-screen-only result can never
    be reported as ``VERIFIED`` under either production profile.
    """

    resolved = resolve_execution_profile(profile)
    execution_completed = (
        report.execution_completed
        if report.execution_completed is not None
        else report.success
    )
    if _completed_compensation_actions(report) > 0:
        return ExecutionOutcome.ROLLED_BACK
    if execution_completed and (
        report.canceled
        or report.halt is not None
        or report.terminal_outcome
        in {
            "halt",
            "escalate",
            "cancelled",
            "canceled",
        }
    ):
        return ExecutionOutcome.HALTED
    if execution_completed and report.terminal_outcome in {"failed", "failure"}:
        return ExecutionOutcome.FAILED
    if any(
        result.failure_category == "runtime_failure"
        and (result.ok or not result.exception_handled)
        for result in report.results
    ):
        return ExecutionOutcome.FAILED
    if any(result.ok and result.error is not None for result in report.results):
        return ExecutionOutcome.FAILED
    if not execution_completed:
        refusal_step_ids = {"<authorization>", "<params>", "<profile>"}
        governed_halt = (
            report.terminal_outcome in {"halt", "escalate"}
            or any(result.safety_halt for result in report.results)
            or any(
                result.safety_refusal_evidence is not None for result in report.results
            )
            or any(
                result.failure_category in {"governed_refusal", "safety_halt"}
                for result in report.results
            )
            or any(result.step_id in refusal_step_ids for result in report.results)
        )
        return ExecutionOutcome.HALTED if governed_halt else ExecutionOutcome.FAILED

    if resolved is ExecutionProfile.DEMO:
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    if not (report.governed_authorization_id and report.governed_runtime_inputs_digest):
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    unhandled_results = [
        result
        for result in report.results
        if not result.ok and not result.skipped and not result.exception_handled
    ]
    if any(
        result.safety_halt
        or result.safety_refusal_evidence is not None
        or result.failure_category in {"governed_refusal", "safety_halt"}
        for result in report.results
    ):
        return ExecutionOutcome.HALTED
    if unhandled_results:
        return ExecutionOutcome.FAILED

    # Import lazily: run_gate imports this module for the profile contract.
    from openadapt_flow.qualification import workflow_contract_sha256
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    if report.workflow_contract_sha256 != workflow_contract_sha256(workflow):
        return ExecutionOutcome.COMPLETED_UNVERIFIED

    qualification_review_context = bool(
        report.qualification_evidence_only
        and workflow.qualification is not None
        and report.governed_qualification_project_id
        == workflow.qualification.project_id
        and report.governed_qualification_project_revision
        == workflow.qualification.revision
        and report.governed_qualification_project_contract_sha256
        == workflow.qualification.contract_sha256()
    )
    all_steps = list(iter_workflow_steps(workflow))
    if len({step.id for step in all_steps}) != len(all_steps):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    consequential = {
        step.id
        for step in all_steps
        if is_consequential(
            step,
            workflow,
            require_current_risk_certification=not qualification_review_context,
            certifying_policy_sha256=report.governed_policy_contract_sha256,
        )
    }
    paired_results: list[tuple[Any, Any]] = []
    if workflow.program is None:
        if [result.step_id for result in report.results] != [
            step.id for step in workflow.steps
        ]:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        paired_results = list(zip(report.results, workflow.steps))
    else:
        if report.terminal_outcome != "success":
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        expected_action_trace = _program_action_trace(
            workflow,
            report.visited_states,
            runtime_worklists=runtime_worklists,
        )
        if expected_action_trace is None:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if len(report.results) != len(expected_action_trace):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        for result, occurrence in zip(report.results, expected_action_trace):
            if (
                result.step_id != occurrence.step.id
                or tuple(result.program_scope) != occurrence.program_scope
                or occurrence.exception_edge
                != bool(result.exception_handled and not result.ok)
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            paired_results.append((result, occurrence.step))
    required_identity_ids = set(report.required_identity_step_ids)
    if any(
        result.identity is None or result.identity.status != "verified"
        for result in report.results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ):
        return ExecutionOutcome.COMPLETED_UNVERIFIED
    minimum = required_effect_tier(workflow, resolved)
    assert minimum is not None
    from openadapt_flow.ir import ActionKind

    def _scoped_params(result: Any) -> dict[str, str] | None:
        scoped = dict(report.params)
        for frame in result.program_scope:
            if frame.relation is None:
                continue
            if runtime_worklists is not None and frame.relation in runtime_worklists:
                rows = runtime_worklists[frame.relation]
            else:
                relation = workflow.data_sources.get(frame.relation)
                if relation is None:
                    return None
                rows = relation.rows
            if frame.row_index is None or frame.row_index >= len(rows):
                return None
            scoped.update(rows[frame.row_index])
        return scoped

    for result, step in paired_results:
        if result.skipped or result.exception_handled:
            if (
                result.delivery_attempted is not False
                or result.delivery_receipt is not None
                or result.delivery_uncertainty is not None
                or result.effect_verified is not None
                or result.effect_contract_hashes
                or result.effect_evidence
            ):
                return ExecutionOutcome.COMPLETED_UNVERIFIED
            continue
        if not result.ok:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        is_consequential_result = result.step_id in consequential
        if step.action is ActionKind.WAIT:
            if result.actuation is not None:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        elif result.actuation is None:
            # Older local, non-consequential keyboard/mouse delivery did not
            # label its actuation path. It still carries settle and delivery
            # proof. A consequential result must name a closed path unless a
            # typed uncertain-delivery record proves that the GUI dispatch was
            # attempted. That record still needs the complete postcondition and
            # effect contract below before it can verify.
            if is_consequential_result and result.delivery_uncertainty is None:
                return ExecutionOutcome.COMPLETED_UNVERIFIED
        elif result.actuation not in {
            "api",
            *AUTOMATED_GUI_ACTUATIONS,
            *HUMAN_ATTENDED_ACTUATIONS,
        }:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        automated_gui = result.actuation in AUTOMATED_GUI_ACTUATIONS or (
            result.actuation is None and step.action is not ActionKind.WAIT
        )
        if automated_gui and result.starting_state_settled is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if (
            automated_gui
            and step.action in {ActionKind.TYPE, ActionKind.SELECT_OPTION}
            and result.input_verified is not True
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if (
            automated_gui
            and step.action is not ActionKind.WAIT
            and result.delivery_attempted is not True
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if result.identity is not None and result.identity.status != "verified":
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(
            not interstitial.ok
            or not interstitial.delivered
            or interstitial.clearance_ok is not True
            for interstitial in result.interstitial_actions
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        uncertainty = result.delivery_uncertainty
        if uncertainty is not None and not (
            uncertainty.verification_attempted
            and uncertainty.effects_confirmed is True
            and uncertainty.resolved_by_contract
            and (not step.expect or uncertainty.postconditions_confirmed is True)
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if step.expect and result.postconditions_ok is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if not is_consequential_result:
            continue
        if result.effect_approved_unverified or result.effect_verified is not True:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        from openadapt_flow.policy import effects_for_actuation

        effects = effects_for_actuation(step, result.actuation)
        scoped_params = _scoped_params(result)
        if scoped_params is None:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        opaque = (
            {"__run_id__": report.run_id_sha256}
            if report.run_id_sha256 is not None
            else {}
        )
        try:
            expected_hashes = Counter(
                effect.resolved_contract_hash(
                    scoped_params,
                    opaque_param_sha256=opaque,
                )
                for effect in effects
            )
        except ValueError:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if Counter(result.effect_contract_hashes) != expected_hashes:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        if any(
            item.initial_verdict != "confirmed"
            or item.final_verdict != "confirmed"
            or item.observed_effect != "present"
            or item.reconciliation_completed
            or item.reconciliation_actions
            or item.verification_tier is None
            or not VerificationTier(item.verification_tier).satisfies(minimum)
            for item in result.effect_evidence
        ):
            return ExecutionOutcome.COMPLETED_UNVERIFIED
        evidence_hashes = Counter(
            item.effect_contract_hash for item in result.effect_evidence
        )
        if not expected_hashes or evidence_hashes != expected_hashes:
            return ExecutionOutcome.COMPLETED_UNVERIFIED
    return ExecutionOutcome.VERIFIED


def stamp_execution_outcome(
    report: RunReport,
    workflow: Workflow,
    profile: ExecutionProfile | str,
    *,
    runtime_worklists: Mapping[str, list[dict[str, str]]] | None = None,
) -> ExecutionOutcome:
    """Write the profile and precise outcome into ``report``."""

    resolved = resolve_execution_profile(profile)
    report.external_network_calls = _external_network_call_state(report)
    if report.execution_completed is None:
        report.execution_completed = report.success
    report.qualification_evidence_only = bool(
        report.governed_qualification_case_id_sha256
    )
    outcome = classify_execution_outcome(
        report,
        workflow,
        resolved,
        runtime_worklists=runtime_worklists,
    )
    report.execution_profile = resolved.value
    report.execution_outcome = outcome.value
    report.production_eligible = bool(
        execution_profile_contract(resolved).production
        and outcome is ExecutionOutcome.VERIFIED
        and not report.qualification_evidence_only
    )
    if execution_profile_contract(resolved).production:
        report.success = outcome is ExecutionOutcome.VERIFIED
    elif outcome is ExecutionOutcome.ROLLED_BACK:
        report.success = False
    report.outcome_envelope = build_outcome_envelope(report, workflow)
    # Section 3: refine the coarse outcome into a first-class terminal
    # transaction outcome + effect journal. Additive -- reads the fields set
    # above and never mutates them (leaf import; see openadapt_flow.transaction).
    from openadapt_flow.transaction import stamp_transaction_outcome

    stamp_transaction_outcome(report, workflow)
    return outcome


def _completed_compensation_actions(report: RunReport) -> int:
    """Count only compensations that completed and were re-verified."""

    return sum(
        evidence.reconciliation_actions
        for result in report.results
        for evidence in result.effect_evidence
        if (
            evidence.reconciliation_completed
            and evidence.reconciliation_actions > 0
            and evidence.final_verdict == "confirmed"
        )
    )


def build_outcome_envelope(report: RunReport, workflow: Workflow):
    """Build the versioned PHI-free evidence summary for ``report``.

    Counts are derived from typed workflow/report fields only.  Free-text
    intents, parameters, identifiers, effect hashes, and observed values never
    enter the envelope.
    """

    from openadapt_flow.ir import (
        ExecutionOutcomeEnvelope,
        OutcomeContractCounts,
        OutcomeEvidenceClass,
    )
    from openadapt_flow.traversal import iter_workflow_steps

    if report.execution_outcome is None:
        raise ValueError("execution outcome must be classified before enveloping")

    steps_by_id = {step.id: step for step in iter_workflow_steps(workflow)}
    production = report.execution_profile in {"standard", "regulated"}

    required_authorization = 1 if production else 0
    passed_authorization = int(
        bool(
            required_authorization
            and report.governed_authorization_id
            and report.governed_runtime_inputs_digest
        )
    )

    required_identity_ids = set(report.required_identity_step_ids)
    identity_results = [
        result
        for result in report.results
        if (
            not result.skipped
            and not result.exception_handled
            and result.step_id in required_identity_ids
        )
    ]
    required_identity = len(identity_results)
    passed_identity = sum(
        result.identity is not None and result.identity.status == "verified"
        for result in identity_results
    )

    required_postconditions = 0
    passed_postconditions = 0
    required_effects = 0
    passed_effects = 0
    evidence_classes: set[OutcomeEvidenceClass] = set()
    effect_class_by_tier: dict[int, OutcomeEvidenceClass] = {
        1: "effect_tier_1",
        2: "effect_tier_2",
        3: "effect_tier_3",
        4: "effect_tier_4",
    }
    minimum_effect_tier = (
        required_effect_tier(workflow, report.execution_profile)
        if report.execution_profile is not None
        else None
    )
    compensation_actions = _completed_compensation_actions(report)
    for result in report.results:
        if result.skipped or result.exception_handled:
            continue
        step = steps_by_id.get(result.step_id)
        if step is not None:
            postcondition_count = len(step.expect)
            required_postconditions += postcondition_count
            if result.postconditions_ok is True:
                passed_postconditions += postcondition_count
            from openadapt_flow.policy import effects_for_actuation

            effects = effects_for_actuation(step, result.actuation)
            required_effects += len(effects)
        if result.effect_verified is True:
            sufficient_evidence = Counter(
                evidence.effect_contract_hash
                for evidence in result.effect_evidence
                if evidence.final_verdict == "confirmed"
                and (
                    minimum_effect_tier is None
                    or (
                        evidence.verification_tier is not None
                        and VerificationTier(evidence.verification_tier).satisfies(
                            minimum_effect_tier
                        )
                    )
                )
            )
            passed_effects += sum(
                (sufficient_evidence & Counter(result.effect_contract_hashes)).values()
            )
        if result.identity is not None and result.identity.status == "verified":
            evidence_classes.add("identity")
        if result.postconditions_ok is True and step is not None and step.expect:
            evidence_classes.add("postcondition")
        for evidence in result.effect_evidence:
            if (
                evidence.final_verdict == "confirmed"
                and evidence.verification_tier is not None
            ):
                evidence_class = effect_class_by_tier.get(evidence.verification_tier)
                if evidence_class is not None:
                    evidence_classes.add(evidence_class)
            if (
                evidence.reconciliation_completed
                and evidence.reconciliation_actions > 0
                and evidence.final_verdict == "confirmed"
            ):
                evidence_classes.add("compensation")

    required = OutcomeContractCounts(
        authorization=required_authorization,
        identity=required_identity,
        postcondition=required_postconditions,
        effect=required_effects,
    )
    passed = OutcomeContractCounts(
        authorization=passed_authorization,
        identity=min(passed_identity, required_identity),
        postcondition=min(passed_postconditions, required_postconditions),
        effect=min(passed_effects, required_effects),
    )
    if passed.authorization:
        evidence_classes.add("authorization")
    if report.model_calls:
        evidence_classes.add("model")

    return ExecutionOutcomeEnvelope(
        outcome=report.execution_outcome,
        profile=report.execution_profile,
        production_eligible=report.production_eligible,
        qualification_evidence_only=report.qualification_evidence_only,
        execution_completed=bool(report.execution_completed),
        required_contracts=required,
        passed_contracts=passed,
        evidence_classes=sorted(evidence_classes),
        model_calls=report.model_calls,
        external_network_calls=report.external_network_calls,
        compensation_actions=compensation_actions,
    )


def _external_network_call_state(
    report: RunReport,
) -> Literal["none", "observed", "unknown"]:
    """Report observed egress without turning absence of instrumentation into 0."""

    if report.external_network_calls == "observed" or report.model_calls > 0:
        return "observed"
    if report.execution_origin or report.execution_entry_url:
        return "observed"
    if report.execution_target_kind in {"web", "rdp", "citrix"}:
        return "observed"

    local_substrates = {
        "onscreen",
        "file",
        "document_hash",
        "snapshot",
        "test",
        "fake",
    }
    for result in report.results:
        if result.actuation == "api":
            return "observed"
        for evidence in result.effect_evidence:
            substrate = evidence.substrate.strip().lower()
            if substrate in {"rest", "fhir", "sftp", "http", "https"}:
                return "observed"
            if substrate not in local_substrates:
                return "unknown"
    # A native target says where input was delivered, not whether this process
    # or one of its integrations opened a socket. Until an explicit network
    # observer proves the negative, absence of an observed call remains unknown.
    return report.external_network_calls
