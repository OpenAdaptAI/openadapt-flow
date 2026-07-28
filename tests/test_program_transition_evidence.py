import hashlib
from types import SimpleNamespace

from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
)
from openadapt_flow.ir import (
    ActionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
    predicate_contract_sha256,
)
from openadapt_flow.qualification import workflow_contract_sha256
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_replayer import FakeBackend, FakeVision, make_png


class _SettledVision(FakeVision):
    def wait_settled_result(self, backend, **kwargs):
        del kwargs
        return SimpleNamespace(png=backend.screenshot(), settled=True)


def _visual_branch_workflow() -> Workflow:
    return Workflow(
        name="retained-visual-branch",
        program=ProgramGraph(
            entry="pick",
            states={
                "pick": State(
                    id="pick",
                    kind=StateKind.BRANCH,
                    transitions=[
                        Transition(
                            guard=Predicate(
                                kind=PredicateKind.TEXT_PRESENT,
                                text="Use first path",
                            ),
                            target="first",
                        ),
                        Transition(target="second"),
                    ],
                ),
                "first": State(
                    id="first",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="first",
                        intent="first",
                        action=ActionKind.KEY,
                        key="F",
                    ),
                    transitions=[Transition(target="done")],
                ),
                "second": State(
                    id="second",
                    kind=StateKind.ACTION,
                    step=Step(
                        id="second",
                        intent="second",
                        action=ActionKind.KEY,
                        key="S",
                    ),
                    transitions=[Transition(target="done")],
                ),
                "done": State(
                    id="done",
                    kind=StateKind.TERMINAL,
                    outcome="success",
                ),
            },
        ),
    )


def _prefixed_visual_branch_workflow() -> Workflow:
    workflow = _visual_branch_workflow()
    assert workflow.program is not None
    workflow.program.entry = "start"
    workflow.program.states["start"] = State(
        id="start",
        kind=StateKind.ACTION,
        step=Step(id="start", intent="start", action=ActionKind.KEY, key="T"),
        transitions=[Transition(target="pick")],
    )
    return workflow


def _governed_report(workflow: Workflow, root):
    bundle_dir = root / "bundle"
    workflow.save(bundle_dir)
    workflow = Workflow.load(bundle_dir)
    assert workflow.manifest is not None
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
        admitted_policy_name="permissive",
        execution_profile="standard",
        minimum_effect_tier=int(VerificationTier.PERSISTED_STATE_REACQUISITION),
        required_identity_step_ids=(),
    )
    run_dir = root / "run"
    report = Replayer(
        FakeBackend(frame=make_png((1280, 720))),
        vision=_SettledVision(),
        governed_authorization=authorization,
        durable=True,
        require_settled=True,
        poll_interval_s=0.01,
    ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir)
    return workflow, run_dir, report


def test_runtime_retains_exact_ordered_visual_transition_evidence(tmp_path):
    workflow = _visual_branch_workflow()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    run_dir = tmp_path / "run"
    frame = make_png((1280, 720))

    report = Replayer(
        FakeBackend(frame=frame),
        vision=_SettledVision(),
        poll_interval_s=0.01,
    ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir)

    assert report.success is True
    assert report.visited_states == ["pick", "second", "done"]
    decision = report.program_transition_evidence[:2]
    assert [item.transition_index for item in decision] == [0, 1]
    assert [item.guard_verdict for item in decision] == [False, True]
    assert [item.selected for item in decision] == [False, True]
    assert {item.selected_target for item in decision} == {"second"}
    assert decision[0].guard_contract_sha256 == predicate_contract_sha256(
        workflow.program.states["pick"].transitions[0].guard
    )
    assert decision[1].guard_contract_sha256 == predicate_contract_sha256(None)
    assert decision[0].program_scope == decision[1].program_scope
    assert decision[0].program_scope[-1].graph_id == "__program__"

    frame_digest = hashlib.sha256(frame).hexdigest()
    assert decision[0].observed_frame_sha256 == frame_digest
    assert decision[0].observed_frame_inventory_ref == (
        f"private/program-transitions/{frame_digest}.png"
    )
    assert (run_dir / decision[0].observed_frame_inventory_ref).read_bytes() == frame
    assert decision[1].observed_frame_sha256 is None
    assert decision[1].observed_frame_inventory_ref is None

    restored = RunReport.model_validate_json(report.model_dump_json())
    assert restored.program_transition_evidence == report.program_transition_evidence


def test_visual_transition_requires_exact_retained_frame_bytes(tmp_path):
    workflow, run_dir, report = _governed_report(_visual_branch_workflow(), tmp_path)

    assert report.execution_outcome == ExecutionOutcome.VERIFIED.value
    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.VERIFIED
    )

    without_evidence = report.model_copy(update={"program_transition_evidence": []})
    assert (
        classify_execution_outcome(
            without_evidence,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )

    frame_ref = report.program_transition_evidence[0].observed_frame_inventory_ref
    assert frame_ref is not None
    frame_path = run_dir / frame_ref
    original = frame_path.read_bytes()
    frame_path.write_bytes(b"not the retained frame")
    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    frame_path.write_bytes(original)

    backing = frame_path.with_name("backing.png")
    backing.write_bytes(original)
    frame_path.unlink()
    frame_path.symlink_to(backing.name)
    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    frame_path.unlink()
    backing.unlink()
    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
    frame_path.write_bytes(original)

    altered = report.model_copy(deep=True)
    altered.program_transition_evidence[0] = altered.program_transition_evidence[
        0
    ].model_copy(update={"guard_contract_sha256": "0" * 64})
    assert (
        classify_execution_outcome(
            altered,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )

    changed_verdict = report.model_copy(deep=True)
    changed_verdict.program_transition_evidence[0] = (
        changed_verdict.program_transition_evidence[0].model_copy(
            update={"guard_verdict": True}
        )
    )
    changed_target = report.model_copy(deep=True)
    changed_target.program_transition_evidence[:2] = [
        item.model_copy(update={"selected_target": "first"})
        for item in changed_target.program_transition_evidence[:2]
    ]
    changed_order = report.model_copy(deep=True)
    changed_order.program_transition_evidence[:2] = list(
        reversed(changed_order.program_transition_evidence[:2])
    )
    for candidate in (changed_verdict, changed_target, changed_order):
        assert (
            classify_execution_outcome(
                candidate,
                workflow,
                ExecutionProfile.STANDARD,
                transition_evidence_root=run_dir,
            )
            is ExecutionOutcome.COMPLETED_UNVERIFIED
        )


def test_transition_evidence_rejects_deleted_prefix_and_linear_invention(tmp_path):
    workflow, run_dir, report = _governed_report(
        _prefixed_visual_branch_workflow(), tmp_path / "program"
    )
    deleted_prefix = report.model_copy(deep=True)
    deleted_prefix.program_transition_evidence = [
        item.model_copy(update={"decision_index": item.decision_index - 1})
        for item in deleted_prefix.program_transition_evidence
        if item.decision_index > 0
    ]
    assert (
        classify_execution_outcome(
            deleted_prefix,
            workflow,
            ExecutionProfile.STANDARD,
            transition_evidence_root=run_dir,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )

    linear = Workflow(
        name="linear-evidence-invention",
        steps=[Step(id="only", intent="only", action=ActionKind.KEY, key="O")],
    )
    linear_report = RunReport(
        workflow_name=linear.name,
        started_at="2026-07-28T00:00:00Z",
        success=True,
        execution_completed=True,
        governed_authorization_id="authorization-1",
        governed_runtime_inputs_digest="a" * 64,
        workflow_contract_sha256=workflow_contract_sha256(linear),
        results=[
            report.results[0].model_copy(
                update={"step_id": "only", "intent": "only", "program_scope": []}
            )
        ],
    )
    assert (
        classify_execution_outcome(
            linear_report,
            linear,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.VERIFIED
    )
    linear_report.program_transition_evidence = [report.program_transition_evidence[0]]
    assert (
        classify_execution_outcome(
            linear_report,
            linear,
            ExecutionProfile.STANDARD,
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )
